# -*- coding: utf-8 -*-
"""Seletor de período do topo: vira o padrão das telas por mês, sem mandar na URL explícita."""
from app.extensions import db
from app.models.usuario import Usuario
from app.utils.datas import hoje_sp


def _entrar(client):
    u = Usuario(nome="Manu", email="m@x.com", role="gerencia")
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "m@x.com", "senha": "senha1234"})


def test_trocar_periodo_vale_como_padrao(app, client):
    _entrar(client)
    resp = client.post("/periodo", data={"ano": 2026, "mes": 3, "proximo": "/relatorios/?tipo=caixa"})
    assert resp.status_code in (302, 303) and resp.headers["Location"].endswith("/relatorios/?tipo=caixa")
    with client.session_transaction() as s:
        assert s["periodo_global"] == [2026, 3]

    html = client.get("/relatorios/?tipo=caixa").get_data(as_text=True)
    assert "Março 2026" in html  # botão do topo
    # parâmetro explícito continua mandando
    html = client.get("/relatorios/?tipo=caixa&ano=2026&mes=7").get_data(as_text=True)
    assert 'value="7"' in html

    client.post("/periodo", data={"acao": "atual", "proximo": "/"})
    with client.session_transaction() as s:
        assert "periodo_global" not in s


def test_proximo_externo_volta_para_inicio(app, client):
    _entrar(client)
    hoje = hoje_sp()
    resp = client.post("/periodo", data={"ano": hoje.year, "mes": hoje.month, "proximo": "//malicioso.com"})
    assert resp.headers["Location"].endswith("/")
    assert "malicioso" not in resp.headers["Location"]
    with client.session_transaction() as s:
        assert "periodo_global" not in s  # mês corrente acompanha o calendário
