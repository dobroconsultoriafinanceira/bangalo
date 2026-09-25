# -*- coding: utf-8 -*-
"""Engine de metas validada contra a planilha 'Metas de Faturamento 2026'."""
from datetime import date
from decimal import Decimal

from app.services import metas as m

# Faturamento histórico real (2022–2025) por mês
HIST = {
    2022: {1: Decimal("274823.93"), 2: Decimal("286511.51"), 3: Decimal("278627.22")},
    2023: {1: Decimal("358792.97"), 2: Decimal("362339.98"), 3: Decimal("383174.91")},
    2024: {1: Decimal("344790.53"), 2: Decimal("330089.42"), 3: Decimal("313526.63")},
    2025: {1: Decimal("465296.65"), 2: Decimal("377000.09"), 3: Decimal("441317.92")},
}


def test_meta_janeiro_2026_bate_planilha():
    p = m.Premissas()  # 60/40, crescimento 8%
    media_jan = m.media_historica_mes(1, HIST, [2022, 2023, 2024, 2025])
    assert media_jan.quantize(Decimal("0.01")) == Decimal("360926.02")

    base = m.base_ponderada(HIST[2025][1], media_jan, p)
    assert base.quantize(Decimal("0.01")) == Decimal("423548.40")

    meta = m.meta_do_mes(HIST[2025][1], media_jan, p)
    assert meta.quantize(Decimal("0.01")) == Decimal("457432.27")


def test_tabela_anual_acumulados():
    p = m.Premissas()
    realizados = {1: Decimal("448827.97")}
    tabela = m.tabela_anual(2026, HIST, realizados, p, anos_historicos=[2022, 2023, 2024, 2025])
    jan = tabela[0]
    assert jan.meta == Decimal("457432.27")
    assert jan.realizado == Decimal("448827.97")
    assert jan.pct_meta == Decimal("0.9812")  # 448827.97 / 457432.27
    assert jan.meta_acumulada == Decimal("457432.27")


def test_metas_diarias_julho_qua():
    """JUL/2026: soma de pesos 35 → Qua (peso 1) = 400471.21 × 1/35 = 11442.03."""
    p = m.Premissas()
    metas = m.metas_diarias(2026, 7, Decimal("400471.21"), p)
    # 1º de julho de 2026 é quarta-feira
    assert date(2026, 7, 1).weekday() == 2
    assert metas[date(2026, 7, 1)] == Decimal("11442.03")
    # segunda-feira = fechado (peso 0)
    assert metas[date(2026, 7, 6)] == Decimal("0.00")


def test_meta_quinzena_metade():
    assert m.meta_quinzena(Decimal("400471.21")) == Decimal("200235.61")


def test_pesos_do_dia():
    p = m.Premissas()
    assert p.peso_do_dia(date(2026, 7, 6)) == Decimal("0")      # segunda
    assert p.peso_do_dia(date(2026, 7, 7)) == Decimal("1.0")    # terça
    assert p.peso_do_dia(date(2026, 7, 4)) == Decimal("1.6")    # sábado
    assert p.peso_do_dia(date(2026, 7, 5)) == Decimal("1.4")    # domingo


def test_feriado_na_segunda_aberto_entra_no_rateio():
    """07/09/2026 (feriado numa segunda) aberto, com a folga trocada para 09/09."""
    from datetime import date

    p = m.Premissas()
    meta = Decimal("336279.77")
    metas = m.metas_diarias(2026, 9, meta, p,
                            dias_fechados_extra={date(2026, 9, 9)},
                            dias_abertos_extra={date(2026, 9, 7)})
    assert metas[date(2026, 9, 7)] == metas[date(2026, 9, 1)]   # peso de terça/quarta
    assert metas[date(2026, 9, 9)] == Decimal("0")              # folga trocada
    assert metas[date(2026, 9, 14)] == Decimal("0")             # segunda comum segue fechada
    assert abs(sum(metas.values()) - meta) <= Decimal("0.20")   # rateio não perde a meta do mês



def test_soma_das_metas_diarias_fecha_com_a_meta_do_mes():
    """O rateio exato tem dízima: a sobra de centavos volta para os dias."""
    p = m.Premissas()
    casos = [(2026, 9, Decimal("336279.77")), (2026, 2, Decimal("100000.00")),
             (2026, 7, Decimal("287654.33")), (2026, 1, Decimal("0.07"))]
    for ano, mes, meta in casos:
        dias = m.metas_diarias(ano, mes, meta, p)
        assert sum(dias.values(), Decimal("0")) == meta, (ano, mes)
        # dia fechado (segunda, sem exceção) não recebe sobra
        assert all(dias[d] == Decimal("0") for d in dias if d.weekday() == 0)



def test_sobra_do_rateio_fica_em_um_centavo_por_dia():
    """A sobra é espalhada: dias de mesmo peso não podem divergir mais de 1 centavo."""
    p = m.Premissas()
    dias = m.metas_diarias(2026, 9, Decimal("336279.77"), p)
    por_peso: dict = {}
    for d, valor in dias.items():
        if valor:
            por_peso.setdefault(p.peso_do_dia(d), []).append(valor)
    for peso, valores in por_peso.items():
        assert max(valores) - min(valores) <= Decimal("0.01"), peso
