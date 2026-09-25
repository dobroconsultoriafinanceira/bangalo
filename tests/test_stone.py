# -*- coding: utf-8 -*-
"""Integração Stone: transform puro, parser CSV/XML, cliente da API e importação."""
from datetime import date
from decimal import Decimal

import pytest

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
    assert stone.categoria_para("pix", "pix") == "Pix Stone"
    assert stone.categoria_para("desconhecida", "x") == "Outros/Acertos"


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


# ---------------- arquivo XML da API de Conciliação (layout 2.2) ----------------

XML = """<?xml version="1.0" encoding="utf-8"?>
<Conciliation>
  <Header>
    <GenerationDateTime>20260923134248</GenerationDateTime>
    <StoneCode>181345124</StoneCode>
    <LayoutVersion>2.2</LayoutVersion>
    <FileId>0</FileId>
    <ReferenceDate>20260922</ReferenceDate>
  </Header>
  <FinancialTransactions>
    <Transaction>
      <Events><CancellationCharges>0</CancellationCharges><Cancellations>0</Cancellations>
        <Captures>1</Captures><ChargebackRefunds>0</ChargebackRefunds><Chargebacks>0</Chargebacks>
        <Payments>0</Payments></Events>
      <AcquirerTransactionKey>36262779846039</AcquirerTransactionKey>
      <CaptureLocalDateTime>20260922123755</CaptureLocalDateTime>
      <AccountType>1</AccountType>
      <BrandId>1</BrandId>
      <Poi><PoiType>1</PoiType></Poi>
      <Installments><Installment>
        <InstallmentNumber>1</InstallmentNumber>
        <GrossAmount>100.000000</GrossAmount>
        <NetAmount>97.510000</NetAmount>
        <PrevisionPaymentDate>20261022</PrevisionPaymentDate>
      </Installment></Installments>
    </Transaction>
    <Transaction>
      <Events><CancellationCharges>0</CancellationCharges><Cancellations>1</Cancellations>
        <Captures>1</Captures><ChargebackRefunds>0</ChargebackRefunds><Chargebacks>0</Chargebacks>
        <Payments>0</Payments></Events>
      <AcquirerTransactionKey>99999999999999</AcquirerTransactionKey>
      <CaptureLocalDateTime>20260922130000</CaptureLocalDateTime>
      <AccountType>2</AccountType>
      <BrandId>2</BrandId>
      <Installments><Installment><GrossAmount>50.000000</GrossAmount>
        <NetAmount>49.000000</NetAmount></Installment></Installments>
    </Transaction>
  </FinancialTransactions>
  <FinancialEvents />
  <FinancialTransactionsAccounts />
  <FinancialEventAccounts />
  <Payments>
    <Payment><Id>6184553574</Id><WalletTypeId>3</WalletTypeId><TotalAmount>5388.08</TotalAmount></Payment>
    <Payment><Id>6184553643</Id><WalletTypeId>6</WalletTypeId><TotalAmount>4651.27</TotalAmount></Payment>
    <Payment><Id>6184553830</Id><WalletTypeId>15</WalletTypeId><TotalAmount>810.91</TotalAmount></Payment>
    <Payment><Id>6184553867</Id><WalletTypeId>12</WalletTypeId><TotalAmount>152.34</TotalAmount></Payment>
  </Payments>
  <Trailer><CapturedTransactionsQuantity>2</CapturedTransactionsQuantity></Trailer>
</Conciliation>
"""


def test_le_o_arquivo_do_dia():
    arq = stone.parse_conciliacao_xml(XML)
    assert arq.stone_code == "181345124" and arq.referencia == date(2026, 9, 22)
    # a venda cancelada não entra
    assert len(arq.transacoes) == 1
    venda = arq.transacoes[0]
    assert (venda.bandeira, venda.produto) == ("visa", "debito")   # AccountType 1 = débito
    assert venda.valor_bruto == Decimal("100.00") and venda.valor_liquido == Decimal("97.51")
    assert venda.taxa == Decimal("2.49")            # MDR = bruto - líquido
    assert venda.data_liquidacao == date(2026, 10, 22)


def test_repasses_viram_as_linhas_de_cartao_da_planilha():
    arq = stone.parse_conciliacao_xml(XML)
    por_categoria = {p.categoria_nome: p.valor for p in arq.pagamentos}
    assert por_categoria == {
        "Visa Crédito": Decimal("5388.08"),
        "Master Card Crédito": Decimal("4651.27"),
        "Amex Crédito": Decimal("810.91"),
        "ELO Crédito": Decimal("152.34"),
    }
    assert arq.total_repassado == Decimal("11002.60")


def test_wallet_desconhecida_cai_em_outros():
    p = stone.PagamentoStone(id="1", data=date(2026, 9, 22), wallet_type_id=17, valor=Decimal("10.00"))
    assert p.categoria_nome == stone.CATEGORIA_FALLBACK


def test_url_e_autenticacao_seguem_o_contrato_da_stone(monkeypatch):
    """Basic auth com a chave como usuário e senha vazia + x-user-type: client."""
    cfg = stone.StoneConfig(base_url="https://conciliation.stone.com.br", secret_key="sk_teste",
                            stone_codes=["181345124"])
    cli = stone.StoneClient(cfg)
    assert cli._url("181345124", date(2026, 9, 22)).endswith(
        "/v2/merchant/181345124/conciliation-file/20260922")

    chamadas = {}

    class RespostaFalsa:
        status_code = 200
        text = XML

        def raise_for_status(self):
            return None

    def get_falso(url, **kwargs):
        chamadas.update(url=url, **kwargs)
        return RespostaFalsa()

    import requests

    monkeypatch.setattr(requests, "get", get_falso)
    arq = cli.arquivo_do_dia(date(2026, 9, 22))
    assert arq.referencia == date(2026, 9, 22)
    assert chamadas["auth"] == ("sk_teste", "")
    assert chamadas["headers"]["x-user-type"] == "client"
    assert chamadas["params"]["layout"] == "XML2_2"


@pytest.mark.parametrize("codigo,trecho", [(401, "recusou a chave"), (403, "não tem acesso"), (503, "manutenção")])
def test_erros_da_stone_viram_mensagem_clara(monkeypatch, codigo, trecho):
    cfg = stone.StoneConfig(base_url="https://conciliation.stone.com.br", secret_key="sk_teste",
                            stone_codes=["181345124"])

    class RespostaFalsa:
        status_code = codigo
        text = ""

        def raise_for_status(self):
            raise AssertionError("não deveria chegar aqui")

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: RespostaFalsa())
    with pytest.raises(RuntimeError, match=trecho):
        stone.StoneClient(cfg).baixar_dia(date(2026, 9, 22))


# ---------------- Stone como fonte das entradas de cartão ----------------

def test_repasses_viram_lancamentos_e_substituem_a_planilha(app, monkeypatch):
    """A Stone passa a ser a fonte: cria os lançamentos e tira os da planilha."""
    from app.models.fluxo import Categoria

    seed_plano_contas()
    db.session.commit()

    visa = db.session.execute(
        db.select(Categoria).filter_by(nome="Visa Crédito", tipo="entrada")
    ).scalar_one()
    # linha antiga, digitada na planilha, no mesmo dia
    db.session.add(Lancamento(data=date(2026, 9, 22), categoria_id=visa.id,
                              valor=Decimal("9999.00"), origem="manual"))
    db.session.commit()

    class ClienteFalso:
        def __init__(self, *a, **k):
            pass

        def arquivo_do_dia(self, dia, stone_code=None):
            return stone.parse_conciliacao_xml(XML)

    monkeypatch.setattr(stone, "StoneClient", ClienteFalso)
    monkeypatch.setattr(stone.StoneConfig, "from_app", classmethod(
        lambda cls, app: cls(base_url="x", secret_key="y", stone_codes=["181345124"])))

    rel = stone_import.importar_repasses(date(2026, 9, 22), date(2026, 9, 22))
    db.session.commit()

    assert rel["inseridos"] == 4 and rel["removidos"] == 1
    assert rel["total"] == Decimal("11002.60")
    da_stone = db.session.execute(
        db.select(Lancamento).filter_by(origem="stone")
    ).scalars().all()
    assert {l.valor for l in da_stone} == {Decimal("5388.08"), Decimal("4651.27"),
                                          Decimal("810.91"), Decimal("152.34")}
    assert db.session.execute(
        db.select(Lancamento).filter_by(origem="manual")
    ).scalars().all() == []

    # reimportar não duplica
    rel2 = stone_import.importar_repasses(date(2026, 9, 22), date(2026, 9, 22))
    db.session.commit()
    assert (rel2["inseridos"], rel2["atualizados"]) == (0, 4)
    assert db.session.query(Lancamento).count() == 4


def test_tela_importa_e_confere(app, client, monkeypatch):
    """Botões da tela: importar repasses grava; 'só conferir' não grava nada."""
    from app.models.usuario import Usuario

    seed_plano_contas()
    u = Usuario(nome="Igor", email="i@x.com", role="consultoria")
    u.definir_senha("Cafe-com-leite-42")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "i@x.com", "senha": "Cafe-com-leite-42"})

    class ClienteFalso:
        def __init__(self, *a, **k):
            pass

        def arquivo_do_dia(self, dia, stone_code=None):
            return stone.parse_conciliacao_xml(XML)

    monkeypatch.setattr(stone, "StoneClient", ClienteFalso)
    monkeypatch.setattr(stone.StoneConfig, "from_app", classmethod(
        lambda cls, app: cls(base_url="x", secret_key="y", stone_codes=["181345124"])))

    conf = client.post("/admin/stone/conferir", data={"inicio": "2026-09-22", "fim": "2026-09-22"})
    assert conf.status_code == 200 and "Conferência Stone" in conf.get_data(as_text=True)
    assert db.session.query(Lancamento).count() == 0   # conferir não grava

    imp = client.post("/admin/stone/importar", data={"inicio": "2026-09-22", "fim": "2026-09-22"})
    assert imp.status_code in (302, 303)
    assert db.session.query(Lancamento).filter_by(origem="stone").count() == 4


# ---------------- agenda de recebíveis (previsão no caixa) ----------------

XML_AGENDA = XML.replace("<PrevisionPaymentDate>20261022</PrevisionPaymentDate>",
                         "<PrevisionPaymentDate>20260925</PrevisionPaymentDate>")


def _stone_falso(monkeypatch, xml):
    class ClienteFalso:
        def __init__(self, *a, **k):
            pass

        def arquivo_do_dia(self, dia, stone_code=None):
            return stone.parse_conciliacao_xml(xml)

    monkeypatch.setattr(stone, "StoneClient", ClienteFalso)
    monkeypatch.setattr(stone.StoneConfig, "from_app", classmethod(
        lambda cls, app: cls(base_url="x", secret_key="y", stone_codes=["181345124"])))


def test_agenda_traz_o_que_ainda_vai_cair(app, monkeypatch):
    _stone_falso(monkeypatch, XML_AGENDA)
    agenda = stone_import.agenda_de_recebiveis(date(2026, 9, 22), dias_atras=1)
    # a venda do arquivo paga em 25/09; lida 2 vezes (dias 21 e 22 do intervalo)
    assert list(agenda["por_data"]) == [date(2026, 9, 25)]
    assert agenda["por_data"][date(2026, 9, 25)] == Decimal("195.02")


def test_agenda_vira_previsao_e_substitui_a_da_planilha(app, monkeypatch):
    from app.models.fluxo import Categoria

    seed_plano_contas()
    visa = db.session.execute(
        db.select(Categoria).filter_by(nome="Visa Crédito", tipo="entrada")
    ).scalar_one()
    # previsão antiga, digitada na planilha, para um dia futuro
    db.session.add(Lancamento(data=date(2026, 9, 25), categoria_id=visa.id,
                              valor=Decimal("4000.00"), origem="manual"))
    db.session.commit()

    _stone_falso(monkeypatch, XML_AGENDA)
    rel = stone_import.importar_agenda(date(2026, 9, 22), dias_atras=1)
    db.session.commit()

    assert rel["dias"] == 1 and rel["removidos_da_planilha"] == 1
    previsto = db.session.execute(
        db.select(Lancamento).filter_by(origem="stone_agenda")
    ).scalar_one()
    assert previsto.data == date(2026, 9, 25) and previsto.valor == Decimal("195.02")
    assert previsto.categoria.nome == "Recebíveis de cartão (previsto)"
    assert db.session.execute(db.select(Lancamento).filter_by(origem="manual")).scalars().all() == []

    # rodar de novo reescreve a agenda inteira, sem duplicar
    rel2 = stone_import.importar_agenda(date(2026, 9, 22), dias_atras=1)
    db.session.commit()
    assert rel2["substituidos"] == 1
    assert db.session.query(Lancamento).filter_by(origem="stone_agenda").count() == 1


def test_arquivo_que_chega_depois_refaz_o_pix_do_dia(app):
    """O caso de 23/09: o crédito entrou como PIX inteiro e o arquivo veio só no dia seguinte."""
    from datetime import date as _date
    from decimal import Decimal as _D

    from app.models.banco import MovimentoBancario
    from app.models.fluxo import Categoria, Lancamento
    from app.services import itau_fluxo

    for nome in ("Pix Stone", "Maestro Débito"):
        db.session.add(Categoria(nome=nome, tipo="entrada", grupo="Vendas - Repasse Stone"))
    db.session.add(MovimentoBancario(
        banco="itau", conta="1", id_externo="c1", data=_date(2026, 9, 23), tipo="credito",
        valor=_D("3557.49"), descricao="PIX TRANSF BANGALO23/09",
        contraparte_instituicao="STONE IP S.A.", categoria_gerencial="rec_stone",
        bloco="operacional"))
    db.session.commit()

    # sem o arquivo do dia, o crédito inteiro vira PIX
    itau_fluxo.importar_periodo(_date(2026, 9, 23), _date(2026, 9, 23), substituir_planilha=False)
    db.session.commit()
    pix = lambda: db.session.execute(
        db.select(Lancamento).join(Categoria, Categoria.id == Lancamento.categoria_id)
        .filter(Categoria.nome == "Pix Stone")).scalars().all()
    assert [l.valor for l in pix()] == [_D("3557.49")]

    # o arquivo chega no dia seguinte e traz a venda no débito
    cat = db.session.execute(
        db.select(Categoria).filter_by(nome="Maestro Débito")).scalar_one()
    db.session.add(Lancamento(data=_date(2026, 9, 23), categoria_id=cat.id,
                              valor=_D("1181.57"), origem="stone", origem_id="pg:99"))
    db.session.commit()

    # refazer o período abate a bandeira e o PIX fica só com a diferença
    itau_fluxo.importar_periodo(_date(2026, 9, 23), _date(2026, 9, 23), substituir_planilha=False)
    db.session.commit()
    assert [l.valor for l in pix()] == [_D("2375.92")]
