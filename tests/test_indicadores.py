# -*- coding: utf-8 -*-
"""Indicadores gerenciais: realizado × previsto, CMV, Prime Cost e telas."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.fluxo import Categoria, Lancamento
from app.models.usuario import Usuario
from app.services import fluxo_caixa as fluxo_srv
from app.services import indicadores as ind

HOJE = date(2026, 9, 15)


def _seed():
    cats = {nome: Categoria(nome=nome, tipo=tipo, grupo=grupo) for nome, tipo, grupo in [
        ("Visa", "entrada", "Vendas - Repasse Stone"),
        ("Compras", "saida", "Compras"),
        ("Salários", "saida", "Folha/Salários"),
        ("Músicos", "saida", "Demais Salários"),
        ("DAS", "saida", "Impostos"),
        ("Aluguel", "saida", "Despesas Fixas"),
    ]}
    db.session.add_all(cats.values())
    db.session.flush()

    def lanc(dia, cat, valor, mes=9):
        return Lancamento(data=date(2026, mes, dia), categoria_id=cats[cat].id, valor=Decimal(valor))

    db.session.add_all([
        lanc(10, "Visa", "1000"), lanc(10, "Compras", "300"), lanc(12, "Salários", "200"),
        lanc(14, "Músicos", "50"), lanc(14, "DAS", "60"),
        lanc(20, "Visa", "500"), lanc(25, "Aluguel", "400"),          # depois de hoje = previsto
        lanc(10, "Visa", "800", mes=8), lanc(20, "Visa", "999", mes=8),
    ])
    db.session.commit()


def _login(client):
    u = Usuario(nome="U", email="u@x.com", role="consultoria")
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "u@x.com", "senha": "senha1234"})


def test_realizado_previsto_cmv_e_prime_cost(app):
    _seed()
    periodo = ind.mes_realizado_previsto(2026, 9, HOJE)
    real, previsto = periodo["realizado"], periodo["previsto"]
    assert periodo["corte"] == HOJE
    assert (real.entradas, real.saidas, real.impostos) == (Decimal("1000.00"), Decimal("610.00"), Decimal("60.00"))
    assert (real.cmv, real.pessoal, real.prime_cost) == (Decimal("300.00"), Decimal("250.00"), Decimal("550.00"))
    assert (real.pct_cmv, real.pct_pessoal, real.pct_prime_cost) == (0.3, 0.25, 0.55)
    assert (real.resultado, real.margem_liquida, real.margem_bruta) == (Decimal("390.00"), 0.39, 0.7)
    assert (previsto.entradas, previsto.saidas) == (Decimal("500.00"), Decimal("400.00"))


def test_mes_passado_e_mes_futuro(app):
    _seed()
    agosto = ind.mes_realizado_previsto(2026, 8, HOJE)
    assert agosto["previsto"].vazio
    assert agosto["realizado"].entradas == Decimal("1799.00")
    outubro = ind.mes_realizado_previsto(2026, 10, HOJE)
    assert outubro["realizado"].vazio and outubro["realizado"].pct_cmv is None
    assert outubro["previsto"].inicio == date(2026, 10, 1)


def test_mesmo_periodo_do_mes_anterior(app):
    _seed()
    anterior = ind.mesmo_periodo_mes_anterior(HOJE)
    assert (anterior.inicio, anterior.fim, anterior.entradas) == (date(2026, 8, 1), date(2026, 8, 15), Decimal("800.00"))
    assert ind.mesmo_periodo_mes_anterior(date(2026, 3, 31)).fim == date(2026, 2, 28)
    assert ind.mesmo_periodo_mes_anterior(date(2026, 1, 10)).inicio == date(2025, 12, 1)


def test_dre_realizado_ate_uma_data_e_prime_cost(app):
    _seed()
    parcial = fluxo_srv.dre_mensal(2026, 9, ate=HOJE)
    completo = fluxo_srv.dre_mensal(2026, 9)
    assert (parcial["receita_bruta"], completo["receita_bruta"]) == (Decimal("1000.00"), Decimal("1500.00"))
    assert (parcial["cpv"], parcial["total_pessoal"], parcial["prime_cost"]) == (
        Decimal("300.00"), Decimal("250.00"), Decimal("550.00"))
    assert completo["despesas_fixas"] == Decimal("400.00") and parcial["despesas_fixas"] == Decimal("0.00")
    assert fluxo_srv.dre_mensal(2026, 9, ate=date(2026, 8, 31))["receita_bruta"] == Decimal("0.00")


def test_telas_dashboard_e_dre_renderizam(app, client):
    _seed()
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    assert "Prime Cost" in home.get_data(as_text=True)
    for url in ("/dre/?ano=2026&mes=9", "/dre/?ano=2026&mes=9&visao=completo", "/dre/?ano=2026&mes=8",
                "/dre/?ano=2026&mes=12"):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert "Prime Cost" in resp.get_data(as_text=True)
