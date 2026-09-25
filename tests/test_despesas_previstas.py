# -*- coding: utf-8 -*-
"""Despesas previstas: projeção de saídas e baixa quando o pagamento cai."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.banco import MovimentoBancario
from app.models.fluxo import Categoria, Fornecedor, Lancamento
from app.models.previsao import DespesaPrevista
from app.services import despesas_previstas as dp


def _base(app):
    cat = Categoria(nome="Compras", tipo="saida", grupo="Compras")
    forn = Fornecedor(nome="Get Distribuidora")
    db.session.add_all([cat, forn])
    db.session.commit()
    return cat, forn


def _pagamento(dia, valor, contraparte="GET DISTRIBUIDORA LTDA", id_externo="p1"):
    mov = MovimentoBancario(banco="itau", conta="1234500123456", id_externo=id_externo, data=dia,
                            tipo="debito", valor=Decimal(valor), descricao="PIX ENVIADO GET DIST",
                            contraparte=contraparte, categoria_gerencial="pag_fornecedores",
                            bloco="operacional")
    db.session.add(mov)
    db.session.commit()
    return mov


def test_despesa_prevista_entra_na_projecao_do_caixa(app):
    """O caso da Get Distribuidora: R$ 801,79 combinado para 25/09."""
    cat, forn = _base(app)
    despesa = dp.salvar({"data_prevista": date(2026, 9, 25), "categoria_id": cat.id,
                         "fornecedor_id": forn.id, "descricao": "Pedido semanal",
                         "valor": "801.79"})
    db.session.commit()

    lanc = db.session.execute(db.select(Lancamento)).scalar_one()
    assert lanc.origem == "previsto" and lanc.data == date(2026, 9, 25)
    assert lanc.valor == Decimal("801.79") and lanc.fornecedor_id == forn.id
    assert despesa.em_aberto


def test_pagamento_no_extrato_baixa_a_previsao_sozinho(app):
    cat, forn = _base(app)
    dp.salvar({"data_prevista": date(2026, 9, 25), "categoria_id": cat.id,
               "fornecedor_id": forn.id, "descricao": "Pedido semanal", "valor": "801.79"})
    db.session.commit()

    _pagamento(date(2026, 9, 25), "801.79")
    rel = dp.baixar_automatico(date(2026, 9, 1), date(2026, 9, 30))
    db.session.commit()

    assert rel["baixadas"] == 1 and rel["total"] == Decimal("801.79")
    despesa = db.session.execute(db.select(DespesaPrevista)).scalar_one()
    assert despesa.situacao == "baixada" and despesa.baixa_automatica
    assert despesa.aviso_pendente is True          # a tela avisa que foi sozinho
    # a previsão sai do caixa: quem conta agora é o lançamento do extrato
    assert db.session.execute(db.select(Lancamento).filter_by(origem="previsto")).scalars().all() == []


def test_nao_baixa_pagamento_de_outro_fornecedor_com_mesmo_valor(app):
    cat, forn = _base(app)
    dp.salvar({"data_prevista": date(2026, 9, 25), "categoria_id": cat.id,
               "fornecedor_id": forn.id, "valor": "801.79"})
    db.session.commit()

    _pagamento(date(2026, 9, 25), "801.79", contraparte="OUTRA EMPRESA LTDA", id_externo="x9")
    rel = dp.baixar_automatico(date(2026, 9, 1), date(2026, 9, 30))
    db.session.commit()
    assert rel["baixadas"] == 0
    assert db.session.execute(db.select(DespesaPrevista)).scalar_one().em_aberto


def test_nao_baixa_fora_da_janela_de_datas(app):
    cat, forn = _base(app)
    dp.salvar({"data_prevista": date(2026, 9, 25), "categoria_id": cat.id,
               "fornecedor_id": forn.id, "valor": "801.79"})
    db.session.commit()
    _pagamento(date(2026, 9, 25) + timedelta(days=30), "801.79")
    assert dp.baixar_automatico(date(2026, 9, 1), date(2026, 11, 30))["baixadas"] == 0


def test_cancelar_tira_da_projecao(app):
    cat, _ = _base(app)
    despesa = dp.salvar({"data_prevista": date(2026, 10, 5), "categoria_id": cat.id,
                         "descricao": "Serviço extra", "valor": "300.00"})
    db.session.commit()
    assert db.session.query(Lancamento).count() == 1

    dp.cancelar(despesa)
    db.session.commit()
    assert db.session.query(Lancamento).count() == 0
    with pytest.raises(ValueError):
        dp.cancelar(despesa)


def test_tela_lista_e_salva(app, client):
    from app.models.usuario import Usuario

    cat, forn = _base(app)
    u = Usuario(nome="Manu", email="m@x.com", role="gerencia")
    u.definir_senha("Cafe-com-leite-42")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "m@x.com", "senha": "Cafe-com-leite-42"})

    resp = client.post("/fluxo/previstas/salvar", data={
        "data_prevista": "2026-09-25", "categoria_id": cat.id, "fornecedor_id": forn.id,
        "valor": "801,79", "descricao": "Pedido semanal"})
    assert resp.status_code in (302, 303)

    html = client.get("/fluxo/previstas").get_data(as_text=True)
    assert "Get Distribuidora" in html and "R$ 801,79" in html
    assert db.session.execute(db.select(Lancamento).filter_by(origem="previsto")).scalar_one()
