# -*- coding: utf-8 -*-
"""Integração Itaú (scaffold): normalizadores puros e motor de conciliação."""
from datetime import date
from decimal import Decimal

from app.importers import itau_adapter as itau
from app.services import conciliacao_bancaria as conc


def test_normalizar_transacao_credito_debito():
    t = itau.normalizar_transacao({
        "transactionId": "x1", "transactionDate": "2026-07-01",
        "amount": "150.00", "type": "CREDITO", "description": "Venda cartão",
    }, conta="123")
    assert t.id == "x1"
    assert t.conta == "123"
    assert t.tipo == itau.CREDITO
    assert t.valor == Decimal("150.00")
    assert t.valor_com_sinal == Decimal("150.00")

    d = itau.normalizar_transacao({
        "id": "x2", "data": "2026-07-02", "valor": "-80,00",  # negativo -> débito
    })
    assert d.tipo == itau.DEBITO
    assert d.valor == Decimal("80.00")
    assert d.valor_com_sinal == Decimal("-80.00")


def test_normalizar_extrato_lista_e_envelope():
    payload = {"data": [
        {"transactionId": "a", "transactionDate": "2026-07-01", "amount": "10", "type": "credito"},
        {"transactionId": "b", "transactionDate": "2026-07-01", "amount": "5", "type": "debito"},
        {"foo": "bar"},  # inválida -> ignorada
    ]}
    txns = itau.normalizar_extrato(payload, conta="1")
    assert len(txns) == 2


def test_normalizar_saldo():
    s = itau.normalizar_saldo({"data": {"account": "9", "date": "2026-07-10", "availableAmount": "1234.56"}})
    assert s.conta == "9"
    assert s.saldo == Decimal("1234.56")


def test_config_nao_configurada_erra_claro(app):
    cfg = itau.ItauConfig()
    assert not cfg.configurada
    client = itau.ItauClient(cfg)
    for chamada in (lambda: client.obter_token(), lambda: client.saldo("1")):
        try:
            chamada()
            assert False, "deveria ter erro"
        except RuntimeError as e:
            assert "credenciais" in str(e).lower()


# ---------------- Motor de conciliação ----------------

def _tx(id_, dia, valor, tipo):
    return itau.TransacaoBancaria(id=id_, conta="1", data=date(2026, 7, dia),
                                  valor=Decimal(valor), tipo=tipo)


def _lc(id_, dia, valor, tipo):
    return conc.LancamentoRef(id=id_, data=date(2026, 7, dia), valor=Decimal(valor), tipo=tipo)


def test_conciliacao_casa_por_valor_e_data():
    transacoes = [_tx("t1", 1, "100.00", itau.CREDITO), _tx("t2", 2, "50.00", itau.DEBITO)]
    lancamentos = [_lc(10, 1, "100.00", "entrada"), _lc(11, 3, "50.00", "saida")]
    r = conc.conciliar(transacoes, lancamentos, tolerancia_dias=3)
    assert len(r.conciliados) == 2
    assert not r.so_no_banco and not r.so_no_fluxo
    assert r.total_conciliado == Decimal("150.00")


def test_conciliacao_fora_da_janela_nao_casa():
    transacoes = [_tx("t1", 1, "100.00", itau.CREDITO)]
    lancamentos = [_lc(10, 20, "100.00", "entrada")]  # 19 dias de diferença
    r = conc.conciliar(transacoes, lancamentos, tolerancia_dias=3)
    assert len(r.so_no_banco) == 1
    assert len(r.so_no_fluxo) == 1


def test_conciliacao_separa_faltantes():
    transacoes = [_tx("t1", 1, "100.00", itau.CREDITO), _tx("t2", 1, "999.00", itau.DEBITO)]
    lancamentos = [_lc(10, 1, "100.00", "entrada"), _lc(11, 1, "77.00", "saida")]
    r = conc.conciliar(transacoes, lancamentos)
    assert len(r.conciliados) == 1
    assert [t.id for t in r.so_no_banco] == ["t2"]      # débito 999 não está no fluxo
    assert [l.id for l in r.so_no_fluxo] == [11]        # saída 77 não veio no extrato
