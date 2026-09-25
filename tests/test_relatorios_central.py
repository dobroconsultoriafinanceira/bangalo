# -*- coding: utf-8 -*-
"""Central de Relatórios: prévia, geração guardada no histórico e download."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.documentos import ExportacaoRelatorio
from app.models.fluxo import Categoria, Lancamento
from app.models.usuario import Usuario


def _entrar(client, role="consultoria"):
    u = Usuario(nome="Bárbara", email="b@x.com", role=role)
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "b@x.com", "senha": "senha1234"})


def _fluxo():
    ent = Categoria(nome="Dinheiro", tipo="entrada", grupo="Vendas - Repasse Stone")
    sai = Categoria(nome="Aluguel", tipo="saida", grupo="Despesas Fixas")
    db.session.add_all([ent, sai])
    db.session.flush()
    db.session.add_all([
        Lancamento(data=date(2026, 7, 3), categoria_id=ent.id, valor=Decimal("1000.00")),
        Lancamento(data=date(2026, 7, 10), categoria_id=sai.id, valor=Decimal("400.00")),
    ])
    db.session.commit()


def test_previa_e_geracao_do_caixa(app, client, tmp_path):
    app.instance_path = str(tmp_path / "instance")
    _fluxo()
    _entrar(client)
    html = client.get("/relatorios/?tipo=caixa&ano=2026&mes=7").get_data(as_text=True)
    assert "Resumo semanal" in html and "Gerar PDF" in html

    resp = client.post("/relatorios/gerar", data={"tipo": "caixa", "ano": 2026, "mes": 7, "composicao": "1"})
    assert resp.status_code in (302, 303)
    exp = db.session.execute(db.select(ExportacaoRelatorio)).scalar_one()
    assert exp.status == "gerado" and exp.titulo.startswith("Fluxo de caixa")
    arquivo = client.get(f"/relatorios/exportacoes/{exp.id}/arquivo")
    assert arquivo.status_code == 200 and arquivo.data.startswith(b"%PDF")
    assert "Fluxo de caixa" in client.get("/relatorios/exportacoes").get_data(as_text=True)


def test_falha_fica_registrada_e_pode_tentar_de_novo(app, client, tmp_path):
    app.instance_path = str(tmp_path / "instance")
    _entrar(client)
    client.post("/relatorios/gerar", data={"tipo": "gorjetas", "periodo_id": 999, "variante": "completo"})
    exp = db.session.execute(db.select(ExportacaoRelatorio)).scalar_one()
    assert exp.status == "falhou" and "Quinzena" in exp.erro
    assert client.get(f"/relatorios/exportacoes/{exp.id}/arquivo").status_code == 404
    client.post(f"/relatorios/exportacoes/{exp.id}/refazer")
    assert db.session.query(ExportacaoRelatorio).count() == 2


def test_todos_os_tipos_montam_documento(app, tmp_path):
    from app.services import relatorios_central as rc

    app.instance_path = str(tmp_path / "instance")
    _fluxo()
    for tipo, p in (("dre_gerencial", {"ano": 2026, "mes": 7}), ("caixa", {"ano": 2026, "mes": 7}),
                    ("metas", {"ano": 2026}), ("contabil", {"ano": 2026, "mes": 7})):
        doc = rc.documento(tipo, p)
        assert doc.tabelas and rc._pdf(doc).startswith(b"%PDF")


def test_voltar_da_geracao_com_o_pdf_destacado(app, client, tmp_path):
    """Depois de gerar, a tela volta com ?destaque=<id> — e tem de abrir."""
    app.instance_path = str(tmp_path / "instance")
    _fluxo()
    _entrar(client)

    resp = client.post("/relatorios/gerar", data={"tipo": "caixa", "ano": 2026, "mes": 7})
    destino = resp.headers["Location"]
    assert "destaque=" in destino

    volta = client.get(destino)
    assert volta.status_code == 200
    exp = db.session.execute(db.select(ExportacaoRelatorio)).scalar_one()
    assert exp.titulo in volta.get_data(as_text=True)


def test_previa_acompanha_as_opcoes(app, client, tmp_path):
    """"Atualizar prévia" é um GET com as opções: a página tem de mudar."""
    app.instance_path = str(tmp_path / "instance")
    _fluxo()
    _entrar(client)

    com = client.get("/relatorios/?tipo=caixa&ano=2026&mes=7&composicao=1").get_data(as_text=True)
    sem = client.get("/relatorios/?tipo=caixa&ano=2026&mes=7&composicao=0").get_data(as_text=True)
    julho = client.get("/relatorios/?tipo=caixa&ano=2026&mes=7").get_data(as_text=True)
    agosto = client.get("/relatorios/?tipo=caixa&ano=2026&mes=8").get_data(as_text=True)
    assert com != sem and julho != agosto
