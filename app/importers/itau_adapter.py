# -*- coding: utf-8 -*-
"""Adapter da API de Extrato Conta Corrente do Itaú PJ (account-statement v1).

Fluxo oficial (material "Fluxo Primeira Chamada Varejo" da Implantação Cash):

  1. **Credenciais** — o administrador do Devportal gera client_id + client
     secret e um *token temporário* em
     https://devportal.itau.com.br/baas/#/credentials.
  2. **Certificado dinâmico** — gera-se chave RSA 2048 + CSR (CN = client_id,
     SHA-512) e envia-se o CSR em texto para
     `POST {sts}/seguranca/v1/certificado/solicitacao` com o token temporário.
     A resposta traz "Secret: <client secret>" (1ª linha) e o certificado
     assinado (.crt, validade de 1 ano). -> `gerar_chave_e_csr` +
     `solicitar_certificado`
  3. **Access token** — `POST {sts}/api/oauth/token` (client_credentials) com
     o certificado + chave como *client certificate* (mTLS). Vale 5 min.
  4. **Extrato** — `GET {extrato}/account-statement/v1/statements/{conta}`
     com Bearer + mTLS, onde conta = agência(4) + "00" + conta(5) + DAC(1).

Contrato do extrato confirmado com a conta real (2026-09-15):
  - query obrigatória: `type=current_account` e `start_date=AAAA-MM-DD`;
    opcionais `page` e `page_size` (1000 aceito). `end_date` não é estrito —
    o período é filtrado localmente pela data contábil.
  - paginação NÃO confiável: `total_pages`/`total_elements` mudam entre
    chamadas iguais e páginas de 100 se sobrepõem e perdem itens. Usa-se
    page_size=1000 (uma página estável) e, se precisar de mais, pagina-se até
    página vazia/HTTP 422, deduplicando pelo id.
  - resposta: `data[].events[]` (mais recentes primeiro) e `data[].balances[]`.
    Lotes SISPAG vêm como `type=agrupamento`, sem `id`, com `code` estável.
  - evento: `id` (UUID), `operation` C/D, `reversal`, `date.accounting`,
    `amount.value` (com sinal), `literal.complete`, `origin`, `counterpart`.

Camadas:
  - `ItauConfig`   — credenciais/certificados via env.
  - `ItauClient`   — rede (requests). Sessão HTTP injetável para testes.
  - funções PURAS  — conta, CSR, resposta do certificado e `normalizar_*`.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import requests

from app.importers.comum import para_data, para_decimal

# tipos de movimento normalizados
CREDITO = "credito"  # entrada na conta
DEBITO = "debito"    # saída da conta

URLS = {
    "producao": {
        "sts": "https://sts.itau.com.br",
        "extrato": "https://account-statement.api.itau.com",
    },
    "homologacao": {
        "sts": "https://sts.rdhi.com.br",
        "extrato": "https://account-statement.api.hom.itau.com",
    },
}

TIPO_CONTA = "current_account"  # valor aceito no query param `type`
# páginas de 100 se sobrepõem/perdem lançamentos entre chamadas; com 1000 o
# período vem numa página só e estável (561 eventos em ~6 semanas na conta real)
PAGE_SIZE = 1000
MAX_PAGINAS = 500
TIMEOUT = 60


def _pega(d: dict, *nomes, default=None):
    """Primeiro valor encontrado entre `nomes` (case-insensitive)."""
    baixo = {str(k).lower(): v for k, v in d.items()}
    for n in nomes:
        if n.lower() in baixo and baixo[n.lower()] not in (None, ""):
            return baixo[n.lower()]
    return default


# ------------------------------- Conta / CSR -------------------------------

def normalizar_conta(texto: str) -> str:
    """Monta o identificador da conta: agência(4) + 00 + conta(5) + DAC(1).

    Aceita "1234-12345-6", "1234 12345 6", "1234123456" ou já no formato
    final "123400123456".
    """
    digitos = re.sub(r"\D", "", str(texto or ""))
    if len(digitos) == 10:
        return f"{digitos[:4]}00{digitos[4:]}"
    if len(digitos) == 12 and digitos[4:6] == "00":
        return digitos
    raise ValueError(
        f"Conta Itaú inválida: {texto!r}. Use agência(4) + conta(5) + DAC(1), "
        "ex.: 1234-12345-6."
    )


def _sem_especiais(texto: str) -> str:
    """Remove acentos e caracteres especiais (exigência do subject do CSR)."""
    ascii_ = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 .\-]", "", ascii_).strip()


def gerar_chave_e_csr(client_id: str, ou: str, cidade: str, uf: str,
                      pais: str = "BR") -> tuple[bytes, bytes]:
    """Gera chave privada RSA 2048 e CSR SHA-512 (equivale ao comando openssl
    do material do Itaú). Retorna (chave_pem, csr_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    client_id = (client_id or "").strip()
    uf = (uf or "").strip().upper()
    pais = (pais or "").strip().upper()
    if not client_id:
        raise ValueError("client_id é obrigatório (vai no CN do certificado).")
    if not re.fullmatch(r"[A-Z]{2}", uf):
        raise ValueError("UF deve ser a sigla com 2 letras, ex.: SP.")
    if not re.fullmatch(r"[A-Z]{2}", pais):
        raise ValueError("País deve ter 2 letras, ex.: BR.")
    ou, cidade = _sem_especiais(ou), _sem_especiais(cidade)
    if not ou or not cidade:
        raise ValueError("Informe o site/app (OU) e a cidade.")

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, client_id),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou),
            x509.NameAttribute(NameOID.LOCALITY_NAME, cidade),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, uf),
            x509.NameAttribute(NameOID.COUNTRY_NAME, pais),
        ]))
        .sign(chave, hashes.SHA512())
    )
    chave_pem = chave.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return chave_pem, csr.public_bytes(serialization.Encoding.PEM)


def separar_resposta_certificado(texto: str) -> tuple[str, str]:
    """Resposta da solicitação: 1ª linha = client secret; depois o .crt.

    Retorna (client_secret, certificado_pem).
    """
    inicio_tag, fim_tag = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    i = texto.find(inicio_tag)
    j = texto.find(fim_tag, i)
    if i < 0 or j < 0:
        raise ValueError("Resposta do Itaú não contém um certificado (BEGIN/END CERTIFICATE).")
    linhas_antes = [l.strip() for l in texto[:i].splitlines() if l.strip()]
    # a produção devolve "Secret: <uuid>" na 1ª linha
    secret = re.sub(r"^secret\s*:\s*", "", linhas_antes[0], flags=re.I) if linhas_antes else ""
    return secret, texto[i:j + len(fim_tag)] + "\n"


# --------------------------- Estruturas normalizadas ---------------------------

@dataclass
class SaldoConta:
    conta: str
    data: date
    saldo: Decimal
    saldo_bloqueado: Decimal | None = None
    saldo_aplicacao_automatica: Decimal | None = None


@dataclass
class TransacaoBancaria:
    """Uma linha do extrato — base da conciliação."""

    id: str                    # id do Itaú (UUID) ou hash estável (dedupe)
    conta: str
    data: date                 # data contábil
    valor: Decimal             # sempre positivo
    tipo: str                  # credito | debito
    descricao: str = ""
    documento: str | None = None
    saldo_apos: Decimal | None = None
    estorno: bool = False
    origem: str | None = None       # ex.: PIX_RECEPCAO, PIX_EMISSAO, DEBITO
    contraparte: str | None = None  # nome de quem pagou/recebeu
    contraparte_documento: str | None = None    # CPF/CNPJ, só dígitos
    contraparte_instituicao: str | None = None  # banco/instituição da contraparte

    @property
    def valor_com_sinal(self) -> Decimal:
        return self.valor if self.tipo == CREDITO else -self.valor


@dataclass
class LancamentoCartao:
    id: str
    cartao_final: str          # 4 últimos dígitos
    data: date
    valor: Decimal
    estabelecimento: str = ""
    parcela: str | None = None  # ex.: "2/6"


@dataclass
class PosicaoInvestimento:
    id: str
    produto: str               # CDB, Fundo, Tesouro...
    data: date
    valor_aplicado: Decimal
    saldo_bruto: Decimal
    rendimento: Decimal | None = None


# ------------------------------- Normalizadores -------------------------------
# PUROS e testáveis. Formato principal = evento real do Itaú (ver docstring do
# módulo); sinônimos planos continuam aceitos para arquivos/outras origens.

_TIPOS_CREDITO = {"credito", "crédito", "credit", "c", "cr", "credito_conta", "entrada"}
_TIPOS_DEBITO = {"debito", "débito", "debit", "d", "db", "saida", "saída"}


def _verdadeiro(valor) -> bool:
    return valor is True or str(valor).strip().lower() in ("true", "1", "sim", "s")


def normalizar_transacao(item: dict, conta: str = "") -> TransacaoBancaria | None:
    data_raw = _pega(item, "date")
    if isinstance(data_raw, dict):
        data = para_data(data_raw.get("accounting") or str(data_raw.get("event") or "")[:10])
    else:
        data = para_data(_pega(item, "transactionDate", "bookingDate", "dataLancamento",
                               "dataMovimento", "data", "date"))
    amount = _pega(item, "amount")
    valor = para_decimal(amount.get("value") if isinstance(amount, dict)
                         else _pega(item, "amount", "valorLancamento", "valor", "value"))
    if data is None or valor is None:
        return None

    tipo_raw = str(_pega(item, "operation", "type", "tipo", "tipoOperacao", "creditDebitType",
                         "creditDebitIndicator", "natureza", "indicador", default="")).lower()
    tipo = CREDITO if tipo_raw in _TIPOS_CREDITO else (
        DEBITO if tipo_raw in _TIPOS_DEBITO else (CREDITO if valor >= 0 else DEBITO)
    )

    literal = _pega(item, "literal")
    if isinstance(literal, dict):
        descricao = str(literal.get("complete") or literal.get("shortened") or "")
    else:
        descricao = str(_pega(item, "description", "descricaoLancamento", "descricao",
                              "historico", default=""))
    origem_raw = _pega(item, "origin")
    origem = (origem_raw.get("operation") or origem_raw.get("type") or None) if isinstance(origem_raw, dict) else None
    contra_raw = _pega(item, "counterpart")
    contra = contra_raw if isinstance(contra_raw, dict) else {}
    contraparte = contra.get("name") or None
    contraparte_documento = re.sub(r"\D", "", str(contra.get("document") or "")) or None
    contraparte_instituicao = contra.get("institution") or None

    conta_final = str(_pega(item, "account", "conta", default=conta) or conta)
    documento = _pega(item, "document", "numeroDocumento", "documento")
    rid = _pega(item, "transactionId", "id", "idTransacao", "idLancamento", "nsu")
    if rid is None and str(_pega(item, "type", default="")).lower() == "agrupamento" and _pega(item, "code"):
        # lote SISPAG (ex.: salários) vem sem id; o `code` é estável mesmo se o lote crescer
        rid = f"agr:{_pega(item, 'code')}"
    if rid is None:
        # sem id no payload: hash estável dos campos (dedupe na reimportação)
        base = f"{conta_final}|{data}|{abs(valor)}|{tipo}|{descricao}|{documento or ''}"
        rid = "h:" + hashlib.sha1(base.encode()).hexdigest()[:16]
    return TransacaoBancaria(
        id=str(rid),
        conta=conta_final,
        data=data,
        valor=abs(valor),
        tipo=tipo,
        descricao=descricao,
        documento=str(documento) if documento else None,
        saldo_apos=para_decimal(_pega(item, "balanceAfter", "saldoApos", "saldo")),
        estorno=_verdadeiro(_pega(item, "reversal", default=False)),
        origem=origem,
        contraparte=contraparte,
        contraparte_documento=contraparte_documento,
        contraparte_instituicao=contraparte_instituicao,
    )


_ENVELOPES = ("data", "transactions", "lancamentos", "statements", "extrato", "items")


def _itens_do_payload(payload) -> list:
    if isinstance(payload, list):
        itens = payload
    elif isinstance(payload, dict):
        itens = []
        for chave in _ENVELOPES:
            valor = _pega(payload, chave)
            if isinstance(valor, list):
                itens = valor
                break
            if isinstance(valor, dict):
                return _itens_do_payload(valor)
    else:
        return []
    # formato Itaú: data = [{"events": [...], "balances": [...]}]
    if itens and all(isinstance(b, dict) and "events" in b for b in itens):
        return [e for b in itens for e in (b.get("events") or [])]
    return itens


def normalizar_extrato(payload: dict | list, conta: str = "") -> list[TransacaoBancaria]:
    out: list[TransacaoBancaria] = []
    vistos: dict[str, int] = {}
    for it in _itens_do_payload(payload):
        t = normalizar_transacao(it, conta) if isinstance(it, dict) else None
        if not t:
            continue
        n = vistos.get(t.id, 0) + 1
        vistos[t.id] = n
        if n > 1:
            if not t.id.startswith(("h:", "agr:")):
                continue  # id real repetido = mesmo lançamento
            t.id = f"{t.id}#{n}"  # lançamentos idênticos sem id (ex.: dois PIX iguais)
        out.append(t)
    return out


def normalizar_saldo(payload: dict, conta: str = "") -> SaldoConta:
    dados = payload.get("data", payload) if isinstance(payload, dict) else {}
    if isinstance(dados, list):  # formato Itaú: data[].balances[]
        saldos = {b.get("type"): b for blk in dados if isinstance(blk, dict)
                  for b in (blk.get("balances") or [])}

        def valor(tipo):
            return para_decimal(((saldos.get(tipo) or {}).get("amount") or {}).get("value"))

        disp = saldos.get("saldo_disponivel") or {}
        return SaldoConta(
            conta=conta,
            data=para_data(str((disp.get("date") or {}).get("event") or "")[:10]) or date.today(),
            saldo=valor("saldo_disponivel") or Decimal("0"),
            saldo_bloqueado=valor("saldo_bloqueado"),
            saldo_aplicacao_automatica=valor("saldo_aplic_aut"),
        )
    return SaldoConta(
        conta=str(_pega(dados, "account", "conta", default=conta)),
        data=para_data(_pega(dados, "date", "data")) or date.today(),
        saldo=para_decimal(_pega(dados, "availableAmount", "saldo", "balance")) or Decimal("0"),
        saldo_bloqueado=para_decimal(_pega(dados, "blockedAmount", "saldoBloqueado")),
    )


def normalizar_cartao(item: dict) -> LancamentoCartao | None:
    rid = _pega(item, "transactionId", "id")
    data = para_data(_pega(item, "transactionDate", "data"))
    valor = para_decimal(_pega(item, "amount", "valor"))
    if rid is None or data is None or valor is None:
        return None
    return LancamentoCartao(
        id=str(rid),
        cartao_final=str(_pega(item, "cardLast4", "finalCartao", default="")),
        data=data,
        valor=abs(valor),
        estabelecimento=str(_pega(item, "merchantName", "estabelecimento", default="")),
        parcela=(str(_pega(item, "installment", "parcela")) if _pega(item, "installment", "parcela") else None),
    )


# --------------------------------- Config/Cliente ---------------------------------

@dataclass
class ItauConfig:
    ambiente: str = "producao"   # producao | homologacao
    client_id: str = ""
    client_secret: str = ""
    cert_path: str = ""          # certificado assinado pelo Itaú (.crt)
    key_path: str = ""           # chave privada gerada junto com o CSR
    contas: list[str] = field(default_factory=list)

    @classmethod
    def from_app(cls, app) -> "ItauConfig":
        contas = (app.config.get("ITAU_CONTAS") or "").replace(";", ",")
        pasta = Path(app.config.get("ITAU_CERT_DIR") or "instance/itau")
        return cls(
            ambiente=(app.config.get("ITAU_AMBIENTE") or "producao").strip().lower(),
            client_id=app.config.get("ITAU_CLIENT_ID", ""),
            client_secret=app.config.get("ITAU_CLIENT_SECRET", ""),
            cert_path=app.config.get("ITAU_CERT_PATH") or str(pasta / "itau.crt"),
            key_path=app.config.get("ITAU_KEY_PATH") or str(pasta / "itau.key"),
            contas=[c.strip() for c in contas.split(",") if c.strip()],
        )

    @property
    def urls(self) -> dict:
        if self.ambiente not in URLS:
            raise ValueError(f"ITAU_AMBIENTE inválido: {self.ambiente!r} (producao|homologacao).")
        return URLS[self.ambiente]

    def faltando(self) -> list[str]:
        itens = []
        if not self.client_id:
            itens.append("ITAU_CLIENT_ID")
        if not self.client_secret:
            itens.append("ITAU_CLIENT_SECRET")
        if not (self.cert_path and Path(self.cert_path).is_file()):
            itens.append(f"certificado ({self.cert_path or 'ITAU_CERT_PATH'})")
        if not (self.key_path and Path(self.key_path).is_file()):
            itens.append(f"chave privada ({self.key_path or 'ITAU_KEY_PATH'})")
        return itens

    @property
    def configurada(self) -> bool:
        return not self.faltando()


class ItauErroAPI(RuntimeError):
    def __init__(self, mensagem: str, status: int, corpo: str = ""):
        super().__init__(mensagem)
        self.status = status
        self.corpo = corpo


class ItauClient:
    """Cliente da API de Extrato (OAuth2 client_credentials + mTLS)."""

    def __init__(self, config: ItauConfig, session=None):
        self.config = config
        self.http = session or requests.Session()
        self._token: str | None = None
        self._token_expira = 0.0

    def _exigir_credenciais(self):
        faltando = self.config.faltando()
        if faltando:
            raise RuntimeError(
                "Credenciais Itaú ausentes: " + ", ".join(faltando)
                + " (ver README › Integração Itaú)."
            )

    @staticmethod
    def _verificar(resp, contexto: str):
        if resp.status_code >= 400:
            corpo = (resp.text or "")[:800]
            raise ItauErroAPI(f"{contexto}: HTTP {resp.status_code} — {corpo}",
                              resp.status_code, corpo)

    @property
    def _cert(self) -> tuple[str, str]:
        return (self.config.cert_path, self.config.key_path)

    # --- etapa 2: certificado dinâmico (não usa mTLS; usa o token temporário) ---

    def solicitar_certificado(self, token_temporario: str, csr_pem: bytes | str) -> tuple[str, str]:
        """Envia o CSR e retorna (client_secret, certificado_pem)."""
        resp = self.http.post(
            f"{self.config.urls['sts']}/seguranca/v1/certificado/solicitacao",
            data=csr_pem,
            headers={"Content-Type": "text/plain",
                     "Authorization": f"Bearer {token_temporario.strip()}"},
            timeout=TIMEOUT,
        )
        self._verificar(resp, "Solicitação de certificado")
        return separar_resposta_certificado(resp.text)

    # --- etapa 3: access token (mTLS) ---

    def obter_token(self) -> str:
        self._exigir_credenciais()
        if self._token and time.monotonic() < self._token_expira:
            return self._token
        resp = self.http.post(
            f"{self.config.urls['sts']}/api/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": self.config.client_id,
                  "client_secret": self.config.client_secret},
            cert=self._cert,
            timeout=TIMEOUT,
        )
        self._verificar(resp, "Access token")
        dados = resp.json()
        self._token = dados["access_token"]
        validade = int(dados.get("expires_in") or 300)  # doc: 5 minutos
        self._token_expira = time.monotonic() + max(validade - 30, 30)
        return self._token

    # --- etapa 4: extrato ---

    def extrato_bruto(self, conta: str, inicio: date, fim: date | None = None,
                      page: int = 1, page_size: int = PAGE_SIZE):
        """Uma página do extrato como veio do Itaú."""
        token = self.obter_token()
        params = {"type": TIPO_CONTA, "start_date": inicio.isoformat(),
                  "page": page, "page_size": page_size}
        if fim:
            params["end_date"] = fim.isoformat()
        resp = self.http.get(
            f"{self.config.urls['extrato']}/account-statement/v1/statements/{normalizar_conta(conta)}",
            params=params,
            headers={"Authorization": f"Bearer {token}",
                     "x-itau-apikey": self.config.client_id,
                     "x-itau-correlationID": str(uuid.uuid4()),
                     "Accept": "application/json"},
            cert=self._cert,
            timeout=TIMEOUT,
        )
        self._verificar(resp, "Extrato")
        return resp.json()

    def extrato(self, conta: str, inicio: date, fim: date | None = None) -> list[TransacaoBancaria]:
        """Todos os lançamentos desde `inicio` (todas as páginas), filtrados
        localmente pela data contábil até `fim`. `end_date` não é enviado
        porque a API não o aplica de forma estrita."""
        conta_id = normalizar_conta(conta)
        vistos: set[str] = set()
        out: list[TransacaoBancaria] = []
        page = 1
        while True:
            try:
                bruto = self.extrato_bruto(conta_id, inicio, page=page)
            except ItauErroAPI as exc:
                # página além do fim responde 422 (sem corpo)
                if exc.status == 422 and page > 1:
                    break
                raise
            # total_pages/total_elements oscilam entre chamadas e as páginas
            # podem se sobrepor: pagina até esvaziar e deduplica pelo id
            eventos = _itens_do_payload(bruto)
            for t in normalizar_extrato(bruto, conta=conta_id):
                if t.id not in vistos:
                    vistos.add(t.id)
                    out.append(t)
            if not eventos or page >= MAX_PAGINAS:
                break
            page += 1
        return [t for t in out if t.data >= inicio and (fim is None or t.data <= fim)]

    def saldo(self, conta: str) -> SaldoConta:
        conta_id = normalizar_conta(conta)
        bruto = self.extrato_bruto(conta_id, date.today(), page_size=1)
        return normalizar_saldo(bruto, conta=conta_id)
