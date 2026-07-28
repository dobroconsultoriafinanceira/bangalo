# -*- coding: utf-8 -*-
"""Integração Stone: transform puro, parser CSV e importação idempotente."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.importers import stone_adapter as stone
from app.models.fluxo import Lancamento
from app.seeds import seed_plano_contas
from app.services import stone_import


def _txn(id_, bandeira, produto, bruto, **kw):
    return stone.TransacaoStone(
        id=id_, data_venda=date(2026, 7, 1), bandeira=bandeira, produto=produto,
        valor_bruto=Decimal(bruto), **kw
    )


def test_mapa_bandeira_categoria():
    assert stone.categoria_para("Visa", "credito") == "Visa Crédito"
    assert stone.categoria_para("Mastercard", "debito") == "Maestro Débito"
    assert stone.categoria_para("Elo", "credito") == "ELO Crédito"
    assert stone.categoria_para("Amex", "credito") == "Amex Crédito"
    assert stone.categoria_para("pix", "pix") == "Pagamento em PIX"
    assert stone.categoria_para("desconhecida", "x") == "Outros/Acerto"


def test_transform_gera_entrada_bruta():
    txns = [_txn("t1", "visa", "credito", "100.00"),
            _txn("t2", "elo", "debito", "50.00", status="approved")]
    lancs = stone.to_lancamentos(txns)
    assert len(lancs) == 2
    assert lancs[0].categoria_nome == "Visa Crédito"
    assert lancs[0].valor == Decimal("100.00")
    assert all(l.tipo == "entrada" for l in lancs)


def test_transform_ignora_nao_efetivada():
    txns = [_txn("t1", "visa", "credito", "100.00", status="cancelada")]
    assert stone.to_lancamentos(txns) == []


def test_transform_taxa_opcional():
    txns = [_txn("t1", "visa", "credito", "100.00", taxa=Decimal("2.50"))]
    com = stone.to_lancamentos(txns, lancar_taxa=True)
    assert len(com) == 2
    taxa = [l for l in com if l.tipo == "saida"][0]
    assert taxa.categoria_nome == "Despesas Bancárias"
    assert taxa.valor == Decimal("2.50")
    assert taxa.origem_id == "t1:taxa"
    # sem a flag, só a entrada
    assert len(stone.to_lancamentos(txns, lancar_taxa=False)) == 1


def test_parser_csv_por_nome_de_coluna():
    csv_txt = (
        "id;data_venda;bandeira;produto;valor_bruto;taxa;status\n"
        "abc1;2026-07-01;Visa;credito;100,00;2,50;approved\n"
        "abc2;2026-07-02;Elo;debito;30,00;0,90;aprovada\n"
    )
    txns = stone.parse_conciliacao_csv(csv_txt)
    assert len(txns) == 2
    assert txns[0].id == "abc1"
    assert txns[0].valor_bruto == Decimal("100.00")
    assert txns[1].bandeira == "Elo"


def test_import_idempotente(app):
    seed_plano_contas()
    db.session.commit()
    txns = [_txn("t1", "visa", "credito", "100.00"),
            _txn("t2", "mastercard", "credito", "200.00")]

    rel1 = stone_import.importar_transacoes(txns)
    db.session.commit()
    assert rel1["inseridos"] == 2
    assert db.session.query(Lancamento).count() == 2

    # reimportar o mesmo período: atualiza, não duplica
    rel2 = stone_import.importar_transacoes(txns)
    db.session.commit()
    assert rel2["inseridos"] == 0
    assert rel2["atualizados"] == 2
    assert db.session.query(Lancamento).count() == 2

    # valor corrigido na origem -> atualiza o lançamento existente
    txns[0].valor_bruto = Decimal("150.00")
    stone_import.importar_transacoes(txns)
    db.session.commit()
    l = db.session.execute(
        db.select(Lancamento).filter_by(origem="stone", origem_id="t1")
    ).scalar_one()
    assert l.valor == Decimal("150.00")
    assert db.session.query(Lancamento).count() == 2


def test_client_sem_credenciais_erro_claro(app):
    cfg = stone.StoneConfig()
    assert not cfg.configurada
    client = stone.StoneClient(cfg)
    try:
        client.baixar_dia(date(2026, 7, 1))
        assert False, "deveria ter levantado erro"
    except RuntimeError as e:
        assert "credenciais" in str(e).lower() or "STONE_" in str(e)
