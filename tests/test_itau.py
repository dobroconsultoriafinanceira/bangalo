# -*- coding: utf-8 -*-
"""Integração Itaú: funções puras, cliente HTTP (sessão fake) e conciliação."""
from datetime import date
from decimal import Decimal

import pytest

from app.importers import itau_adapter as itau
from app.services import conciliacao_bancaria as conc


# ---------------- Conta / CSR / certificado ----------------

def test_normalizar_conta_formatos():
    assert itau.normalizar_conta("1234-12345-6") == "123400123456"
    assert itau.normalizar_conta("1234 12345 6") == "123400123456"
    assert itau.normalizar_conta("123400123456") == "123400123456"
    for invalida in ("", "123-45", "123412345678"):
        with pytest.raises(ValueError):
            itau.normalizar_conta(invalida)


def test_gerar_chave_e_csr():
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    chave_pem, csr_pem = itau.gerar_chave_e_csr("abc-123", "bangalô app", "São Paulo", "sp")
    assert b"BEGIN PRIVATE KEY" in chave_pem
    assert len(csr_pem.decode().strip().splitlines()) > 15  # exigência do Itaú

    csr = x509.load_pem_x509_csr(csr_pem)
    assert csr.is_signature_valid
    assert csr.signature_hash_algorithm.name == "sha512"
    assert csr.public_key().key_size == 2048
    nome = lambda oid: csr.subject.get_attributes_for_oid(oid)[0].value  # noqa: E731
    assert nome(NameOID.COMMON_NAME) == "abc-123"
    assert nome(NameOID.ORGANIZATIONAL_UNIT_NAME) == "bangalo app"
    assert nome(NameOID.LOCALITY_NAME) == "Sao Paulo"
    assert nome(NameOID.STATE_OR_PROVINCE_NAME) == "SP"
    assert nome(NameOID.COUNTRY_NAME) == "BR"

    with pytest.raises(ValueError):
        itau.gerar_chave_e_csr("abc", "app", "Cidade", "Sao Paulo")


def test_separar_resposta_certificado():
    texto = "segredo-xyz\n-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    secret, cert = itau.separar_resposta_certificado(texto)
    assert secret == "segredo-xyz"
    assert cert.startswith("-----BEGIN CERTIFICATE-----")
    assert cert.strip().endswith("-----END CERTIFICATE-----")
    # formato real da produção: prefixo "Secret: "
    secret, _ = itau.separar_resposta_certificado(
        "Secret: 1111-2222\n-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----")
    assert secret == "1111-2222"
    with pytest.raises(ValueError):
        itau.separar_resposta_certificado("sem certificado")


# ---------------- Normalizadores: formato real do Itaú ----------------
# dados sintéticos no mesmo formato da API (nunca commitar extrato real)

def _evento(id_, dia, valor, operacao, descricao="PIX TRANSF TESTE", reversal=False):
    return {
        "id": id_, "type": "lancamento", "operation": operacao, "reversal": reversal,
        "date": {"event": f"2026-07-{dia:02d}T13:01:38.007Z", "accounting": f"2026-07-{dia:02d}"},
        "literal": {"code": "", "shortened": descricao[:24], "complete": descricao},
        "amount": {"value": valor, "currency": "BRL"},
        "counterpart": {"type": "CONTA_CORRENTE", "name": "FULANO LTDA", "document": "00000000000100"},
        "origin": {"identifier": "", "type": "PIX", "operation": "PIX_RECEPCAO", "channel": ""},
    }


def _pagina(eventos, page=1, total_pages=1):
    return {
        "data": [{"events": eventos, "balances": [
            {"type": "saldo_disponivel", "date": {"event": "2026-07-15T10:01:38.067-03:00"},
             "literal": {"shortened": "SALDO EM CONTA", "complete": "SALDO EM CONTA"},
             "amount": {"value": 1500.25, "currency": "BRL"}},
            {"type": "saldo_bloqueado", "date": {"event": "2026-07-15T10:01:38.067-03:00"},
             "literal": {}, "amount": {"value": 0, "currency": "BRL"}},
            {"type": "saldo_aplic_aut", "date": {"event": "2026-07-15T10:01:38.067-03:00"},
             "literal": {}, "amount": {"value": 300.5, "currency": "BRL"}},
        ]}],
        "pagination": {"links": {}, "page": page, "total_pages": total_pages,
                       "total_elements": len(eventos), "page_size": 100},
        "filters": None,
    }


def test_normalizar_evento_real():
    extrato = itau.normalizar_extrato(_pagina([
        _evento("uuid-1", 3, -5000.0, "D", "PIX ENVIADO FULANO"),
        _evento("uuid-2", 3, 8736.32, "C", reversal=True),
    ]), conta="123400123456")
    d, c = extrato
    assert (d.id, d.data, d.tipo, d.valor) == ("uuid-1", date(2026, 7, 3), itau.DEBITO, Decimal("5000.00"))
    assert d.descricao == "PIX ENVIADO FULANO"
    assert d.conta == "123400123456"
    assert (c.tipo, c.valor, c.estorno) == (itau.CREDITO, Decimal("8736.32"), True)
    assert c.origem == "PIX_RECEPCAO" and c.contraparte == "FULANO LTDA"
    assert c.contraparte_documento == "00000000000100"


def test_normalizar_saldo_real():
    s = itau.normalizar_saldo(_pagina([]), conta="123400123456")
    assert s.data == date(2026, 7, 15)
    assert s.saldo == Decimal("1500.25")
    assert s.saldo_bloqueado == Decimal("0.00")
    assert s.saldo_aplicacao_automatica == Decimal("300.50")


def test_agrupamento_sispag_usa_code_como_id():
    agr = {"type": "agrupamento", "operation": "D", "code": "2026-07-12SALARIOS",
           "date": {"event": "2026-07-14T23:59:59.999999999-03:00", "accounting": "2026-07-14"},
           "literal": {"shortened": "SISPAG SALARIOS"},
           "amount": {"value": -350.0, "currency": "BRL"}, "number_events": 1}
    outro = dict(agr, code="2026-07-08SALARIOS", amount={"value": -1000.0, "currency": "BRL"})
    txns = itau.normalizar_extrato(_pagina([agr, outro, dict(agr)]), conta="1")
    assert [t.id for t in txns] == ["agr:2026-07-12SALARIOS", "agr:2026-07-08SALARIOS",
                                    "agr:2026-07-12SALARIOS#2"]
    t = txns[0]
    assert (t.data, t.tipo, t.valor, t.descricao) == (
        date(2026, 7, 14), itau.DEBITO, Decimal("350.00"), "SISPAG SALARIOS")
    assert t.estorno is False


# ---------------- Normalizadores: sinônimos planos ----------------

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


def test_extrato_sem_id_gera_hash_estavel_e_desambigua_iguais():
    item = {"dataLancamento": "2026-07-03", "valorLancamento": "50,00",
            "tipoOperacao": "C", "descricaoLancamento": "PIX RECEBIDO"}
    payload = {"data": {"lancamentos": [item, dict(item)]}}
    a = itau.normalizar_extrato(payload, conta="123400123456")
    b = itau.normalizar_extrato(payload, conta="123400123456")
    assert [t.id for t in a] == [t.id for t in b]          # estável entre chamadas
    assert a[0].id.startswith("h:") and a[1].id == a[0].id + "#2"
    assert a[0].tipo == itau.CREDITO


def test_normalizar_saldo_plano():
    s = itau.normalizar_saldo({"data": {"account": "9", "date": "2026-07-10", "availableAmount": "1234.56"}})
    assert s.conta == "9"
    assert s.saldo == Decimal("1234.56")


# ---------------- Cliente (sessão HTTP fake) ----------------

class _Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class _Sessao:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def post(self, url, **kw):
        self.chamadas.append(("POST", url, kw))
        return self.respostas.pop(0)

    def get(self, url, **kw):
        self.chamadas.append(("GET", url, kw))
        return self.respostas.pop(0)


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "itau.crt").write_text("crt")
    (tmp_path / "itau.key").write_text("key")
    return itau.ItauConfig(client_id="cid", client_secret="sec",
                           cert_path=str(tmp_path / "itau.crt"),
                           key_path=str(tmp_path / "itau.key"))


def test_config_nao_configurada_erra_claro():
    cfg = itau.ItauConfig()
    assert not cfg.configurada
    with pytest.raises(RuntimeError, match="(?i)credenciais"):
        itau.ItauClient(cfg, session=_Sessao([])).obter_token()


def test_solicitar_certificado(cfg):
    sessao = _Sessao([_Resp(text="Secret: novo-secret\n-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----")])
    secret, cert = itau.ItauClient(cfg, session=sessao).solicitar_certificado("tmp", b"CSR")
    metodo, url, kw = sessao.chamadas[0]
    assert url == "https://sts.itau.com.br/seguranca/v1/certificado/solicitacao"
    assert kw["headers"] == {"Content-Type": "text/plain", "Authorization": "Bearer tmp"}
    assert kw["data"] == b"CSR"
    assert "cert" not in kw
    assert secret == "novo-secret" and "BEGIN CERTIFICATE" in cert


def test_token_mtls_e_cache(cfg):
    sessao = _Sessao([_Resp(json_data={"access_token": "tok", "expires_in": 300})])
    client = itau.ItauClient(cfg, session=sessao)
    assert client.obter_token() == "tok"
    assert client.obter_token() == "tok"  # reaproveita sem nova chamada
    assert len(sessao.chamadas) == 1
    _, url, kw = sessao.chamadas[0]
    assert url == "https://sts.itau.com.br/api/oauth/token"
    assert kw["data"] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "sec"}
    assert kw["cert"] == (cfg.cert_path, cfg.key_path)


def test_extrato_pagina_deduplica_e_filtra(cfg):
    cfg.ambiente = "homologacao"
    pag1 = _pagina([_evento("e3", 20, -7.0, "D"), _evento("e2", 10, 10.0, "C")], page=1, total_pages=2)
    # lançamento novo durante a paginação empurra "e2" para a página 2
    pag2 = _pagina([_evento("e2", 10, 10.0, "C"), _evento("e1", 1, 5.0, "C")], page=2, total_pages=2)
    sessao = _Sessao([_Resp(json_data={"access_token": "tok"}),
                      _Resp(json_data=pag1), _Resp(json_data=pag2),
                      _Resp(status=422, text="")])  # página além do fim
    txns = itau.ItauClient(cfg, session=sessao).extrato(
        "1234-12345-6", date(2026, 7, 1), date(2026, 7, 15))
    assert [t.id for t in txns] == ["e2", "e1"]  # e3 fora do período; e2 sem duplicar
    assert txns[0].conta == "123400123456"

    assert sessao.chamadas[0][1] == "https://sts.rdhi.com.br/api/oauth/token"
    gets = [c for c in sessao.chamadas if c[0] == "GET"]
    assert len(gets) == 3  # ignora total_pages: segue até 422/página vazia
    assert gets[2][2]["params"]["page"] == 3
    _, url, kw = gets[0]
    assert url == ("https://account-statement.api.hom.itau.com"
                   "/account-statement/v1/statements/123400123456")
    assert kw["params"] == {"type": "current_account", "start_date": "2026-07-01",
                            "page": 1, "page_size": 1000}
    assert gets[1][2]["params"]["page"] == 2
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["headers"]["x-itau-apikey"] == "cid"
    assert kw["cert"] == (cfg.cert_path, cfg.key_path)


def test_extrato_422_na_primeira_pagina_e_erro(cfg):
    sessao = _Sessao([_Resp(json_data={"access_token": "tok"}), _Resp(status=422, text="")])
    with pytest.raises(itau.ItauErroAPI):
        itau.ItauClient(cfg, session=sessao).extrato("1234-12345-6", date(2026, 7, 1))


def test_erro_http_vira_itau_erro_api(cfg):
    sessao = _Sessao([_Resp(json_data={"access_token": "tok"}),
                      _Resp(status=400, text='{"message": "Account Type x nonexistent"}')])
    with pytest.raises(itau.ItauErroAPI) as exc:
        itau.ItauClient(cfg, session=sessao).extrato_bruto("123400123456", date(2026, 7, 1))
    assert exc.value.status == 400
    assert "nonexistent" in str(exc.value)


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
