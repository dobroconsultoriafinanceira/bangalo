# -*- coding: utf-8 -*-
"""Consolidação de fluxo, saldos derivados, auth e permissões por role."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.fluxo import Categoria, ConfigSistema, Lancamento
from app.models.usuario import Usuario
from app.services import fluxo_caixa as srv


def _seed_fluxo():
    entrada = Categoria(nome="Dinheiro", tipo="entrada", grupo="Operacional")
    saida = Categoria(nome="Aluguel", tipo="saida", grupo="Despesas Fixas")
    db.session.add_all([entrada, saida])
    db.session.flush()
    ConfigSistema.definir("saldo_inicial_abertura", "1000.00")
    ConfigSistema.definir("data_abertura", "2026-01-01")
    db.session.add_all([
        Lancamento(data=date(2026, 1, 1), categoria_id=entrada.id, valor=Decimal("500.00")),
        Lancamento(data=date(2026, 1, 1), categoria_id=saida.id, valor=Decimal("200.00")),
        Lancamento(data=date(2026, 1, 2), categoria_id=entrada.id, valor=Decimal("300.00")),
    ])
    db.session.commit()


def test_saldo_derivado(app):
    _seed_fluxo()
    # dia 1: 1000 + 500 - 200 = 1300
    assert srv.saldo_ate(date(2026, 1, 1)) == Decimal("1300.00")
    # dia 2: 1300 + 300 = 1600
    assert srv.saldo_ate(date(2026, 1, 2)) == Decimal("1600.00")


def test_totais_por_tipo(app):
    _seed_fluxo()
    totais = srv.totais_por_tipo(date(2026, 1, 1), date(2026, 1, 31))
    assert totais["entrada"] == Decimal("800.00")
    assert totais["saida"] == Decimal("200.00")


def test_visao_mensal_saldos(app):
    _seed_fluxo()
    visao = srv.visao_mensal(2026, 1)
    assert visao["saldo_inicial"] == Decimal("1000.00")
    assert visao["saldos_por_dia"][1] == Decimal("1300.00")
    assert visao["saldos_por_dia"][2] == Decimal("1600.00")


def _criar_usuarios():
    consultor = Usuario(nome="C", email="c@x.com", role="consultoria")
    consultor.definir_senha("senha1234")
    gerente = Usuario(nome="G", email="g@x.com", role="gerencia")
    gerente.definir_senha("senha1234")
    db.session.add_all([consultor, gerente])
    db.session.commit()


def test_login_exige_credenciais(app, client):
    _criar_usuarios()
    resp = client.post("/login", data={"email": "c@x.com", "senha": "errada"})
    assert b"inv" in resp.data.lower() or resp.status_code == 200
    resp2 = client.post("/login", data={"email": "c@x.com", "senha": "senha1234"},
                        follow_redirects=False)
    assert resp2.status_code in (302, 303)


def test_gerencia_nao_acessa_admin(app, client):
    _criar_usuarios()
    client.post("/login", data={"email": "g@x.com", "senha": "senha1234"})
    resp = client.get("/admin/usuarios")
    assert resp.status_code == 403


def test_consultoria_acessa_admin(app, client):
    _criar_usuarios()
    client.post("/login", data={"email": "c@x.com", "senha": "senha1234"})
    resp = client.get("/admin/usuarios")
    assert resp.status_code == 200


def test_healthz_sem_auth(client):
    assert client.get("/healthz").status_code == 200
