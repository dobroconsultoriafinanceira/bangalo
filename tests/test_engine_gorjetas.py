# -*- coding: utf-8 -*-
"""Engine de gorjetas validada contra a aba '1ª Q Maio-26' (planilha real)."""
from datetime import date
from decimal import Decimal

from app.services import gorjetas as g

PERCENTUAIS = {"Cozinha": Decimal("0.25"), "Salão": Decimal("0.73"), "Caixa": Decimal("0.02")}
ENCARGOS = Decimal("0.20")

# Comissão por dia (dias 1..15 de Maio/26) — valores reais da planilha
COMISSAO_DIA = {
    date(2026, 5, 1): Decimal("1831.92"), date(2026, 5, 2): Decimal("2864.76"),
    date(2026, 5, 3): Decimal("1265.40"), date(2026, 5, 4): Decimal("0"),
    date(2026, 5, 5): Decimal("499.68"), date(2026, 5, 6): Decimal("614.04"),
    date(2026, 5, 7): Decimal("1122.12"), date(2026, 5, 8): Decimal("1173.72"),
    date(2026, 5, 9): Decimal("2395.56"), date(2026, 5, 10): Decimal("1458.36"),
    date(2026, 5, 11): Decimal("0"), date(2026, 5, 12): Decimal("1056.48"),
    date(2026, 5, 13): Decimal("917.04"), date(2026, 5, 14): Decimal("1740.36"),
    date(2026, 5, 15): Decimal("1477.08"),
}

# (nome, setor, funcao, pontos, dias_presentes[1..15], liquido esperado)
# Grade extraída célula a célula da aba real (fonte da verdade).
COLABS = [
    ("Daniel", "Cozinha", "Cozinheiro", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1011.60"),
    ("Valderi", "Cozinha", "Cozinheiro", 2, [1, 2, 3, 5, 6, 7, 8, 10, 12, 13, 14, 15], "851.90"),
    ("Tonhão", "Cozinha", "Cozinheiro", 2, [15], "53.71"),
    ("Micael", "Cozinha", "Auxiliar de Cozinha", 1, [2, 3, 5, 6, 7, 8, 9], "268.34"),
    ("Rogen", "Cozinha", "Auxiliar de Cozinha", 1, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "505.80"),
    ("Erivan", "Cozinha", "Cozinheiro", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12], "785.08"),
    ("Bruno", "Cozinha", "Cozinheiro", 2, [14, 15], "153.16"),
    ("Wagner", "Cozinha", "Cozinheiro", 2, [15], "53.71"),
    ("Roberto", "Salão", "Gerente", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Antonio Pereira", "Salão", "Barman", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Soraya", "Salão", "Barman", 2, [1, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1086.58"),
    ("Eliude Leonardo", "Salão", "Garçom", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Francisco Edno", "Salão", "Garçom", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Francisco Carlos", "Salão", "Garçom", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Mateus Gomes", "Salão", "Garçom", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Janilson", "Salão", "Garçom", 2, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "1325.58"),
    ("Jaciara de Morais", "Salão", "Garçom", 2, [], "0.00"),
    ("Elton Jhon (China)", "Salão", "Garçom em experiência", 1, [7, 8, 9, 10, 12, 13, 14, 15], "389.59"),
    ("Elizangela", "Caixa", "Caixa", 1, [1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15], "294.66"),
]


def _montar():
    colaboradores = []
    for i, (nome, setor, funcao, pontos, dias, _esp) in enumerate(COLABS):
        presencas = {date(2026, 5, d) for d in dias}
        colaboradores.append(g.ColaboradorRateio(
            id=i + 1, nome=nome, setor=setor, funcao=funcao, registro="CLT",
            pontos=Decimal(pontos), presencas=presencas,
        ))
    return colaboradores


def test_rateio_diario_bate_planilha_maio():
    colaboradores = _montar()
    resultado = g.ratear_diario(COMISSAO_DIA, ENCARGOS, PERCENTUAIS, colaboradores)
    por_nome = {r.nome: r.liquido for r in resultado.colaboradores}
    for nome, _s, _f, _p, _d, esperado in COLABS:
        assert por_nome[nome] == Decimal(esperado), f"{nome}: {por_nome[nome]} != {esperado}"


def test_total_liquido_a_ratear():
    total = g.total_liquido_a_ratear(Decimal("18416.52"), ENCARGOS)
    assert total.quantize(Decimal("0.01")) == Decimal("14733.22")


def test_pools_por_setor_conferem():
    colaboradores = _montar()
    resultado = g.ratear_diario(COMISSAO_DIA, ENCARGOS, PERCENTUAIS, colaboradores)
    # pools batem com os do resumo da planilha
    assert resultado.pools["Cozinha"] == Decimal("3683.30")
    assert resultado.pools["Salão"] == Decimal("10755.25")
    assert resultado.pools["Caixa"] == Decimal("294.66")
    for setor in PERCENTUAIS:
        assert resultado.status_setor(setor) == "OK"


def test_exemplo_simplificado_prompt():
    """Ex. do prompt: José Valderi 2 pts × 15 dias no pool Cozinha 3181.75 → 669.84."""
    colaboradores = [
        g.ColaboradorRateio(1, "Jose Valderi", "Cozinha", "Cozinheiro", "CLT",
                            Decimal("2"), dias_trabalhados_manual=15),
        # peso total cozinha = 142,5 → resto do setor com 112,5 pts-dia
        g.ColaboradorRateio(2, "Resto Cozinha", "Cozinha", "Cozinheiro", "CLT",
                            Decimal("112.5"), dias_trabalhados_manual=1),
        # Salão e Caixa preenchidos p/ NÃO redistribuir o pool a Cozinha
        g.ColaboradorRateio(3, "Garcom", "Salão", "Garçom", "CLT",
                            Decimal("2"), dias_trabalhados_manual=1),
        g.ColaboradorRateio(4, "Caixa", "Caixa", "Caixa", "CLT",
                            Decimal("1"), dias_trabalhados_manual=1),
    ]
    # comissão bruta tal que total líquido × 0.25 = 3181,752 (pool Cozinha)
    comissao_bruta = Decimal("12727.008") / (Decimal("1") - ENCARGOS)
    resultado = g.ratear_simplificado(comissao_bruta, ENCARGOS, PERCENTUAIS, colaboradores)
    valderi = next(r for r in resultado.colaboradores if r.id == 1)
    # 3181,752 × 30 / 142,5 = 669,84
    assert valderi.bruto_rateado == Decimal("669.84")


def test_setor_vazio_redistribui():
    """Caixa sem ninguém: o pool de Caixa é redistribuído a Cozinha e Salão."""
    colaboradores = [
        g.ColaboradorRateio(1, "A", "Cozinha", "Cozinheiro", "CLT", Decimal("2"),
                            presencas={date(2026, 5, 1)}),
        g.ColaboradorRateio(2, "B", "Salão", "Garçom", "CLT", Decimal("2"),
                            presencas={date(2026, 5, 1)}),
    ]
    comissao = {date(2026, 5, 1): Decimal("1000")}
    resultado = g.ratear_diario(comissao, ENCARGOS, PERCENTUAIS, colaboradores)
    # nada retido: soma dos líquidos == total líquido
    assert resultado.total_a_pagar == resultado.total_liquido
    assert resultado.redistribuido > 0


def test_divisor_do_extra_conta_quem_esta_de_ferias():
    """1ª Q Agosto/26: 8 presentes (16 pts) + Antonio de férias (2) + extra (2) = 20."""
    from datetime import date as _d

    dias = [_d(2026, 8, d) for d in range(1, 16)]
    equipe = [
        g.ColaboradorRateio(id=i, nome=f"G{i}", setor="Salão", funcao="Garçom", registro="",
                            pontos=Decimal("2"), presencas=set(dias))
        for i in range(1, 9)
    ]
    ferias = g.ColaboradorRateio(id=9, nome="Antonio", setor="Salão", funcao="Garçom", registro="",
                                 pontos=Decimal("2"), presencas=set(), em_ferias=True)
    extra = g.ExtraRateio(data=_d(2026, 8, 6), setor="Salão", turno="Noite", pontos=Decimal("2"),
                          comissao_turno=Decimal("1736.16"), pago_pelo_restaurante=Decimal("150"))
    resultados, descontos = g._calcular_extras(
        [extra], Decimal("0.8"), {"Salão": Decimal("0.73")}, equipe + [ferias])

    # 1736,16 × 0,8 × 0,73 × 2 / 20 = 101,39 (com divisor 18 daria 112,66)
    assert resultados[0].devido_pelo_rateio == Decimal("101.39")
    assert resultados[0].parte_restaurante == Decimal("48.61")
    # o reposto é dividido só entre os presentes (16 pontos), não entre 18
    assert descontos[1] == (Decimal("101.39") / Decimal("16")).quantize(Decimal("0.01")) * Decimal("2")
    assert 9 not in descontos      # quem está de férias não paga o extra


def test_extra_sem_ninguem_de_ferias_mantem_o_divisor_antigo():
    from datetime import date as _d

    equipe = [
        g.ColaboradorRateio(id=i, nome=f"G{i}", setor="Salão", funcao="Garçom", registro="",
                            pontos=Decimal("2"), presencas={_d(2026, 8, 6)})
        for i in range(1, 9)
    ]
    extra = g.ExtraRateio(data=_d(2026, 8, 6), setor="Salão", turno="Noite", pontos=Decimal("2"),
                          comissao_turno=Decimal("1736.16"), pago_pelo_restaurante=Decimal("150"))
    resultados, _ = g._calcular_extras([extra], Decimal("0.8"), {"Salão": Decimal("0.73")}, equipe)
    assert resultados[0].devido_pelo_rateio == Decimal("112.66")
