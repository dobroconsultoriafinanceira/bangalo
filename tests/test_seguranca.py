# -*- coding: utf-8 -*-
"""Papéis, permissões, sessão e proteções de autenticação."""
import re
import time

import pytest

from app import create_app
from app.extensions import db
from app.models.usuario import TentativaLogin, Usuario
from app.utils import permissoes as perm
from app.utils.seguranca import destino_seguro, problema_na_senha

SENHA = "Cafe-com-leite-42"

LIVRES_DE_PERMISSAO = {
    "auth.login", "auth.logout", "auth.esqueci_senha", "auth.redefinir_senha", "auth.trocar_senha",
    "dashboard.trocar_periodo",
}


@pytest.fixture(autouse=True)
def _usuario_por_requisicao(app):
    """O fixture `app` mantém um app context aberto e o `g` dele seria compartilhado
    entre requisições; aqui cada requisição carrega o usuário da própria sessão
    (como em produção), o que permite testar dois aparelhos ao mesmo tempo."""
    from flask import g

    def limpar():
        g.pop("_login_user", None)

    app.before_request_funcs.setdefault(None, []).insert(0, limpar)


def _usuario(email="u@x.com", role="gerencia", permissoes=None, nome="Pessoa Teste"):
    u = Usuario(nome=nome, email=email, role=role, permissoes=permissoes)
    u.definir_senha(SENHA)
    db.session.add(u)
    db.session.commit()
    return u


def _entrar(client, email="u@x.com", senha=SENHA, **kw):
    return client.post("/login", data={"email": email, "senha": senha}, **kw)


# ---------------------------- cobertura das rotas ----------------------------

def test_toda_rota_que_altera_dados_exige_permissao(app):
    sem_permissao = []
    for regra in app.url_map.iter_rules():
        if regra.endpoint in LIVRES_DE_PERMISSAO or regra.endpoint == "static":
            continue
        view = app.view_functions[regra.endpoint]
        if regra.methods & {"POST", "PUT", "PATCH", "DELETE"} and not getattr(view, "permissao", None):
            sem_permissao.append(regra.endpoint)
        if regra.endpoint.startswith("admin.") and getattr(view, "permissao", None) != "admin.acessar":
            sem_permissao.append(regra.endpoint)
        if regra.endpoint.startswith("relatorios.") and getattr(view, "permissao", None) != "relatorios.gerar":
            sem_permissao.append(regra.endpoint)
    assert sem_permissao == []


def test_sem_login_nada_abre(app, client):
    publicas = {"auth.login", "auth.esqueci_senha", "auth.redefinir_senha", "static", "healthz"}
    for regra in app.url_map.iter_rules():
        if regra.endpoint in publicas:
            continue
        url = re.sub(r"<(?:int:)?[^>]+>", "1", regra.rule)
        metodo = "get" if "GET" in regra.methods else "post"
        resp = getattr(client, metodo)(url)
        assert resp.status_code in (302, 401), (regra.rule, resp.status_code)
        if resp.status_code == 302:
            assert "/login" in resp.headers["Location"], regra.rule


# ---------------------------- papéis ----------------------------

def test_gerencia_ve_operacao_mas_nao_relatorios_admin_nem_edita_metas(app, client):
    _usuario()
    _entrar(client)
    for url in ("/", "/conciliacao/itau", "/fluxo/", "/fluxo/lancamentos", "/dre/gerencial", "/dre/contabil",
                "/gorjetas/", "/metas/", "/metas/registro-diario", "/metas/historico", "/cadastros/equipe"):
        assert client.get(url).status_code == 200, url

    home = client.get("/").get_data(as_text=True)
    assert "Relatórios" not in home and "Administração" not in home

    for metodo, url in (("get", "/relatorios/"), ("get", "/relatorios/exportacoes"), ("post", "/relatorios/gerar"),
                        ("get", "/admin/usuarios"), ("get", "/admin/integracoes"), ("post", "/admin/usuarios/salvar"),
                        ("post", "/metas/premissas/2026"), ("post", "/metas/registro-diario/salvar"),
                        ("post", "/metas/historico"), ("get", "/gorjetas/1/relatorio/completo.pdf")):
        assert getattr(client, metodo)(url).status_code == 403, url

    registro = client.get("/metas/registro-diario").get_data(as_text=True)
    assert "data-somente-leitura" in registro and "Salvar registro" not in registro
    assert "Editar histórico" not in client.get("/metas/historico").get_data(as_text=True)

    from app.models.auditoria import LogAuditoria

    negados = db.session.query(LogAuditoria).filter_by(acao="acesso_negado").count()
    assert negados >= 10  # cada tentativa bloqueada fica na auditoria


def test_gerencia_opera_o_dia_a_dia(app, client):
    from app.models.fluxo import Categoria

    cat = Categoria(nome="Dinheiro", tipo="entrada", grupo="Vendas - Repasse Stone")
    db.session.add(cat)
    db.session.commit()
    _usuario()
    _entrar(client)
    resp = client.post("/fluxo/lancamentos/novo", data={"data": "2026-09-01", "categoria_id": cat.id, "valor": "10,00"})
    assert resp.status_code in (302, 303)


def test_somente_leitura_nao_altera_nada(app, client):
    _usuario(role="leitura")
    _entrar(client)
    assert client.get("/fluxo/lancamentos").status_code == 200
    for url in ("/fluxo/lancamentos/novo", "/cadastros/fornecedores/salvar", "/gorjetas/novo",
                "/conciliacao/itau/revisar-lote", "/dre/contabil/faturamento"):
        assert client.post(url).status_code == 403, url
    assert "Novo lançamento" not in client.get("/fluxo/lancamentos").get_data(as_text=True)


def test_admin_total_tem_tudo_e_nao_pode_ser_reduzido():
    assert perm.efetivas("consultoria", []) == frozenset(perm.PERMISSOES)
    # administração nunca vai para outro papel, mesmo marcada
    assert "admin.acessar" not in perm.efetivas("gerencia", ["admin.acessar", "relatorios.gerar"])
    assert perm.efetivas("gerencia", None) == perm.PAPEIS["gerencia"]["padrao"]


def test_permissao_ajustada_por_usuario(app, client):
    _usuario(permissoes=sorted(perm.PAPEIS["gerencia"]["padrao"] | {"relatorios.gerar"}))
    _entrar(client)
    assert client.get("/relatorios/").status_code == 200
    assert client.get("/admin/usuarios").status_code == 403


# ---------------------------- gestão de usuários ----------------------------

def test_admin_cria_usuario_com_senha_forte_e_permissoes(app, client):
    _usuario(email="adm@x.com", role="consultoria")
    _entrar(client, "adm@x.com")
    fraca = client.post("/admin/usuarios/salvar", data={"nome": "Manu", "email": "manu@x.com", "role": "gerencia",
                                                        "senha": "12345678", "ativo": "on"}, follow_redirects=True)
    assert "pelo menos 10" in fraca.get_data(as_text=True)
    assert not db.session.execute(db.select(Usuario).filter_by(email="manu@x.com")).scalar_one_or_none()

    client.post("/admin/usuarios/salvar", data={
        "nome": "Manu", "email": "Manu@X.com", "role": "gerencia", "senha": "Pao-de-queijo-77", "ativo": "on",
        "permissoes": ["caixa.editar", "admin.acessar"]})
    manu = db.session.execute(db.select(Usuario).filter_by(email="manu@x.com")).scalar_one()
    assert manu.permissoes == ["caixa.editar"]  # administração descartada
    assert manu.pode("caixa.editar") and not manu.pode("gorjetas.editar")

    dup = client.post("/admin/usuarios/salvar", data={"nome": "Outra", "email": "manu@x.com", "role": "leitura",
                                                      "senha": "Pao-de-queijo-77"}, follow_redirects=True)
    assert "Já existe" in dup.get_data(as_text=True)


def test_admin_nao_se_tranca_para_fora(app, client):
    adm = _usuario(email="adm@x.com", role="consultoria", nome="Igor")
    _entrar(client, "adm@x.com")
    resp = client.post("/admin/usuarios/salvar", data={"id": adm.id, "nome": "Igor", "email": "adm@x.com",
                                                       "role": "gerencia", "ativo": "on"}, follow_redirects=True)
    assert "próprio acesso" in resp.get_data(as_text=True)
    assert db.session.get(Usuario, adm.id).role == "consultoria"


def test_ultimo_admin_nao_pode_ser_removido(app):
    from app.blueprints import admin as admin_bp
    a = _usuario(email="a@x.com", role="consultoria")
    assert admin_bp._admins_ativos(exceto_id=a.id) == 0


def test_mudar_papel_derruba_a_sessao_da_pessoa(app):
    _usuario(email="adm@x.com", role="consultoria")
    manu = _usuario(email="manu@x.com")
    admin, gerente = app.test_client(), app.test_client()
    _entrar(admin, "adm@x.com")
    _entrar(gerente, "manu@x.com")
    assert gerente.get("/").status_code == 200

    admin.post("/admin/usuarios/salvar", data={"id": manu.id, "nome": manu.nome, "email": "manu@x.com",
                                               "role": "leitura", "ativo": "on"})
    resp = gerente.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert admin.get("/").status_code == 200


def test_encerrar_sessoes(app):
    _usuario(email="adm@x.com", role="consultoria")
    manu = _usuario(email="manu@x.com")
    admin, gerente = app.test_client(), app.test_client()
    _entrar(admin, "adm@x.com")
    _entrar(gerente, "manu@x.com")
    admin.post(f"/admin/usuarios/{manu.id}/encerrar-sessoes")
    assert gerente.get("/").status_code == 302


# ---------------------------- login e senha ----------------------------

def test_login_bloqueia_apos_tentativas_mesmo_com_senha_certa(app, client):
    _usuario()
    for _ in range(5):
        assert _entrar(client, senha="errada-errada").status_code == 401
    assert _entrar(client).status_code == 429
    assert db.session.query(TentativaLogin).count() == 5


def test_login_nao_diferencia_email_inexistente(app, client):
    _usuario()
    a = _entrar(client, "naoexiste@x.com").get_data(as_text=True)
    b = _entrar(client, senha="errada-errada").get_data(as_text=True)
    assert "E-mail ou senha inválidos." in a and "E-mail ou senha inválidos." in b


def test_usuario_inativo_nao_entra(app, client):
    u = _usuario()
    u.ativo = False
    db.session.commit()
    assert _entrar(client).status_code == 401


def test_next_nao_redireciona_para_fora(app, client):
    _usuario()
    for ruim in ("//evil.com", "/\\evil.com", "https://evil.com", "/\tevil"):
        resp = _entrar(client, query_string={"next": ruim})
        assert resp.headers["Location"] in ("/", "http://localhost/"), ruim
        client.post("/logout")
    assert destino_seguro("/fluxo/?mes=3", "/") == "/fluxo/?mes=3"


def test_trocar_senha_encerra_outros_aparelhos(app):
    _usuario()
    celular, computador = app.test_client(), app.test_client()
    _entrar(celular)
    _entrar(computador)
    resp = computador.post("/conta/senha", data={"atual": SENHA, "senha": "Nova-senha-segura-9",
                                                 "confirmar": "Nova-senha-segura-9"})
    assert resp.status_code == 302
    assert computador.get("/").status_code == 200  # quem trocou continua
    assert celular.get("/").status_code == 302     # o outro aparelho sai
    assert computador.post("/conta/senha", data={"atual": "errada", "senha": "x", "confirmar": "x"}).status_code == 400


def test_link_de_recuperacao_vale_uma_vez(app, client):
    from app.blueprints.auth import _impressao_da_senha, _serializer

    u = _usuario()
    token = _serializer().dumps({"id": u.id, "h": _impressao_da_senha(u)})
    assert client.get(f"/redefinir-senha/{token}").status_code == 200
    client.post(f"/redefinir-senha/{token}", data={"senha": "Outra-senha-forte-1", "confirmar": "Outra-senha-forte-1"})
    assert client.get(f"/redefinir-senha/{token}").status_code == 302  # já usado
    # token antigo (só e-mail, formato anterior) não vale
    assert client.get(f"/redefinir-senha/{_serializer().dumps(u.email)}").status_code == 302


def test_politica_de_senha():
    assert problema_na_senha("curta1")
    assert problema_na_senha("1234567890")
    assert problema_na_senha("somenteletrasaqui")
    assert problema_na_senha("manu-2026-casa", nome="Manu", email="manu@x.com")  # contém o e-mail
    assert problema_na_senha("Cafe-com-leite-42") is None


def test_sessao_expira_por_inatividade(app, client):
    _usuario()
    _entrar(client)
    with client.session_transaction() as s:
        s["visto_em"] = int(time.time()) - app.config["SESSAO_INATIVIDADE_MIN"] * 60 - 5
    resp = client.get("/fluxo/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_sessao_antiga_sem_versao_nao_vale(app, client):
    u = _usuario()
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id)  # formato antigo
        s["visto_em"] = int(time.time())
    assert client.get("/").status_code == 302


# ---------------------------- cabeçalhos, CSRF, produção ----------------------------

def test_cabecalhos_de_seguranca(app, client):
    _usuario()
    _entrar(client)
    resp = client.get("/")
    assert resp.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_post_sem_token_csrf_e_recusado(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/login", data={"email": "a@x.com", "senha": "x"}).status_code == 400


def test_producao_recusa_secret_key_fraca(monkeypatch):
    from app import config as cfg

    monkeypatch.setattr(cfg.ProductionConfig, "SECRET_KEY", "dev-inseguro-trocar")
    monkeypatch.setattr(cfg.ProductionConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app("production")


def test_documento_aprovado_so_reabre_quem_aprova(app):
    from app.models.documentos import DocumentoContabil
    from app.services import documentos_contabeis as dc

    doc = DocumentoContabil(ano=2026, mes=8, tipo="dre_pdf", nome_arquivo="dre.pdf", caminho="x", tamanho=1,
                            situacao="aprovado")
    with pytest.raises(PermissionError):
        dc.mudar_situacao(doc, "recebido", pode_aprovar=False)
    with pytest.raises(ValueError):
        dc.validar_conteudo("dre.pdf", b"<html><script>alert(1)</script>")
