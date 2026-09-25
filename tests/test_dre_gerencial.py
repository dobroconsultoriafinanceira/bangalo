# -*- coding: utf-8 -*-
"""DRE Gerencial apurada com os dados do sistema."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.fluxo import Categoria, Lancamento
from app.models.gorjetas import PeriodoGorjeta
from app.models.metas import FaturamentoDiario
from app.models.usuario import Usuario
from app.services import dre_gerencial as dg
from app.services import dre_previsao as dp

CATEGORIAS = [
    ("Visa Crédito", "entrada", "Vendas - Repasse Stone"),
    ("Outros/Acertos", "entrada", "Outros/Acertos"),
    ("DAS", "saida", "Impostos"),
    ("Compras", "saida", "Compras"),
    ("Salários", "saida", "Folha/Salários"),
    ("Pro Labore/Lucro", "saida", "Demais Salários"),
    ("Músicos", "saida", "Demais Salários"),
    ("Aluguel", "saida", "Despesas Fixas"),
    ("IPTU", "saida", "Despesas Fixas"),
    ("Light", "saida", "Despesas Fixas"),
    ("Cartão de Crédito 15 Itaú", "saida", "Despesas Fixas"),
    ("Aplicação", "saida", "Outras despesas"),
]


@pytest.fixture()
def fluxo(app):
    cats = {nome: Categoria(nome=nome, tipo=tipo, grupo=grupo) for nome, tipo, grupo in CATEGORIAS}
    db.session.add_all(cats.values())
    db.session.flush()

    def lanc(mes, dia, nome, valor):
        db.session.add(Lancamento(data=date(2026, mes, dia), categoria_id=cats[nome].id, valor=Decimal(valor)))

    # julho
    lanc(7, 5, "Visa Crédito", "300000")     # recebimento (não vira receita: a receita vem do PDV)
    lanc(7, 10, "Compras", "90000")
    lanc(7, 6, "Salários", "50000")
    lanc(7, 1, "Pro Labore/Lucro", "25000")  # 8.475,55 é pró-labore; o resto é lucro
    lanc(7, 5, "Músicos", "20000")
    lanc(7, 10, "Aluguel", "7000")
    lanc(7, 20, "IPTU", "1000")
    lanc(7, 15, "Light", "3000")
    lanc(7, 15, "Cartão de Crédito 15 Itaú", "9000")
    lanc(7, 25, "Aplicação", "40000")        # tesouraria: fora da DRE
    lanc(7, 12, "Outros/Acertos", "2000")
    # agosto: só o imposto, que é a competência de julho
    lanc(8, 20, "DAS", "34000")

    for dia in range(1, 32):
        db.session.add(FaturamentoDiario(data=date(2026, 7, dia), aberto=True, faturamento=Decimal("13000")))
    db.session.add_all([
        PeriodoGorjeta(referencia="1ª Julho", ordem_quinzena=1, mes=7, ano=2026,
                       data_inicio=date(2026, 7, 1), data_fim=date(2026, 7, 15),
                       comissao_bruta=Decimal("20000"), status="fechado"),
        PeriodoGorjeta(referencia="2ª Julho", ordem_quinzena=2, mes=7, ano=2026,
                       data_inicio=date(2026, 7, 16), data_fim=date(2026, 7, 31),
                       comissao_bruta=Decimal("19000"), status="fechado"),
    ])
    db.session.commit()
    return cats


def test_apuracao_em_caixa_reclassifica_as_linhas(app, fluxo):
    d = dg.apurar(2026, 7, regime="caixa")
    linhas = d["linhas"]

    assert d["receita_bruta"] == Decimal("403000.00")           # 31 dias × 13.000 (PDV)
    assert "registro diário (PDV)" in linhas["receita_bruta"].origem
    assert linhas["devolucoes"].valor == Decimal("39000.00")    # quinzenas fechadas
    assert linhas["impostos"].valor == Decimal("0.00")          # nada pago em julho
    assert linhas["cmv"].valor == Decimal("90000.00")
    assert linhas["pessoal"].valor == Decimal("50000.00")
    assert linhas["prolabore"].valor == dp.PRO_LABORE_MENSAL    # fixo; o resto sai da DRE
    assert linhas["ocupacao"].valor == Decimal("8000.00")       # aluguel + IPTU
    assert linhas["utilidades"].valor == Decimal("3000.00")
    assert linhas["entretenimento"].valor == Decimal("20000.00")
    assert linhas["cartao_credito"].valor == Decimal("9000.00")
    assert linhas["outras_receitas"].valor == Decimal("2000.00")
    # tesouraria e lucro dos sócios ficam fora
    fora = dict(d["fora_da_dre"])
    assert fora["Outras despesas › Aplicação"] == Decimal("40000.00")
    assert fora["Adiantamento de lucros (sócios)"] == Decimal("25000.00") - dp.PRO_LABORE_MENSAL
    # taxa de cartão é estimada (vem descontada no repasse)
    assert linhas["taxas_cartao"].valor > Decimal("0")
    assert "estimada" in linhas["taxas_cartao"].origem


def test_ajustes_de_competencia(app, fluxo):
    caixa = dg.apurar(2026, 7, regime="caixa")
    comp = dg.apurar(2026, 7, regime="competencia")

    # imposto do faturamento de julho é o pago em agosto
    assert caixa["linhas"]["impostos"].valor == Decimal("0.00")
    assert comp["linhas"]["impostos"].valor == Decimal("34000.00")
    assert "pago em 08/2026" in comp["linhas"]["impostos"].origem

    # CMV vira consumo estimado (% do faturamento) e a folha ganha provisões
    pct_cmv = Decimal(dp.parametros()["pct_cmv"])
    assert comp["linhas"]["cmv"].valor == (comp["receita_bruta"] * pct_cmv).quantize(Decimal("0.01"))
    fator = Decimal(dp.parametros()["fator_pessoal_operacional"])
    assert comp["linhas"]["pessoal"].valor == (Decimal("50000") * fator).quantize(Decimal("0.01"))
    assert ("(pago a fornecedores no mês)", Decimal("90000.00")) in comp["linhas"]["cmv"].itens

    assert comp["receita_liquida"] == comp["receita_bruta"] - Decimal("39000.00") - Decimal("34000.00")
    assert comp["ebit"] == comp["ebitda"] - comp["linhas"]["depreciacao"].valor


def test_analise_sinaliza_desvio_relevante(app, fluxo):
    analise = dg.analise(2026, 7, regime="caixa", janela=3)
    assert analise["atual"]["mes"] == 7
    assert len(analise["meses"]) == 7
    entretenimento = next(l for l in analise["linhas"] if l["chave"] == "entretenimento")
    # meses anteriores sem música: média 0 -> sem sinalização (evita falso positivo)
    assert entretenimento["valor"] == Decimal("20000.00")
    rotulos = [t["rotulo"] for t in analise["totais"]]
    assert "EBITDA" in rotulos and "LUCRO LÍQUIDO GERENCIAL" in rotulos


def test_tela_dre_gerencial(app, client, fluxo):
    u = Usuario(nome="U", email="u@x.com", role="gerencia")
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "u@x.com", "senha": "senha1234"})

    resp = client.get("/dre/gerencial?ano=2026&mes=7")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    for trecho in ("DRE gerencial", "EBITDA", "Prime Cost", "Entretenimento (Música ao Vivo)",
                   "Pró-labore", "Movimentos do mês que ficam fora da DRE"):
        assert trecho in html
    assert client.get("/dre/gerencial?ano=2026&mes=7&regime=caixa").status_code == 200


def test_faturamento_do_mes_avisa_quando_faltam_dias(app):
    """Mês pela metade: o valor é o que já foi lançado, e a origem diz o que falta."""
    from datetime import date

    from app.models.metas import FaturamentoDiario
    from app.services import faturamento as fonte

    for dia in range(1, 32):  # agosto/2026: segundas fechadas, resto aberto
        d = date(2026, 8, dia)
        aberto = d.weekday() != 0
        valor = Decimal("1000.00") if (aberto and dia <= 10) else None
        db.session.add(FaturamentoDiario(data=d, aberto=aberto, faturamento=valor))
    db.session.commit()

    parcial = fonte.do_mes(2026, 8)
    assert parcial.fonte == "diario" and not parcial.completo
    assert parcial.valor == Decimal("8000.00")       # 10 dias menos as segundas 03 e 10/08
    assert parcial.dias_abertos == 26 and parcial.dias_lancados == 8
    assert "parcial" in parcial.origem and "18 dias sem lançar" in parcial.origem

    for reg in db.session.execute(db.select(FaturamentoDiario)).scalars():
        if reg.aberto and reg.faturamento is None:
            reg.faturamento = Decimal("1000.00")
    db.session.commit()

    completo = fonte.do_mes(2026, 8)
    assert completo.completo and completo.valor == Decimal("26000.00")
    assert "mês completo" in completo.origem and completo.dias_faltando == 0
