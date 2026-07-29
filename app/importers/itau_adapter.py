# -*- coding: utf-8 -*-
"""Adapter (scaffold) da API direta do Itaú PJ.

Alvo: consumir dados da conta PJ da cliente — saldo, extrato (entradas/
saídas), cartões de crédito e investimentos — para conciliar com o fluxo.

Acesso à API direta do Itaú (definido com a cliente):
  - Autenticação OAuth2 **client_credentials** + **mTLS** (certificado da
    aplicação). client_id/secret e certificados são gerados no portal
    (developer.itau.com.br) com apoio do gerente. Ver README › Integração Itaú.

Este módulo isola tudo em camadas testáveis:
  1. `ItauConfig`      — credenciais/certificados via env.
  2. `ItauClient`      — token + chamadas HTTP (mTLS). Sem credenciais, erra
                          de forma explícita (não inventa chamada).
  3. `normalizar_*`    — funções PURAS que convertem o JSON do Itaú nas
                          estruturas normalizadas abaixo. É o ponto que se
                          confirma contra um payload real; o resto do sistema
                          (conciliação) depende só das estruturas normalizadas.

Enquanto a API real não está confirmada, os normalizadores aceitam um dict
tolerante a sinônimos de campo (mesma estratégia do adapter Stone), para já
poderem ser testados e ligados ao motor de conciliação.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.importers.comum import para_data, para_decimal

# tipos de movimento normalizados
CREDITO = "credito"  # entrada na conta
DEBITO = "debito"    # saída da conta


def _pega(d: dict, *nomes, default=None):
    """Primeiro valor encontrado entre `nomes` (case-insensitive, aninhado)."""
    baixo = {str(k).lower(): v for k, v in d.items()}
    for n in nomes:
        if n.lower() in baixo and baixo[n.lower()] not in (None, ""):
            return baixo[n.lower()]
    return default


# --------------------------- Estruturas normalizadas ---------------------------

@dataclass
class SaldoConta:
    conta: str
    data: date
    saldo: Decimal
    saldo_bloqueado: Decimal | None = None


@dataclass
class TransacaoBancaria:
    """Uma linha do extrato — base da conciliação."""

    id: str                    # id único no Itaú (dedupe)
    conta: str
    data: date
    valor: Decimal             # sempre positivo
    tipo: str                  # credito | debito
    descricao: str = ""
    documento: str | None = None
    saldo_apos: Decimal | None = None

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
# PUROS e testáveis. Confirmar o mapeamento de campos contra um payload real
# do Itaú (os nomes abaixo cobrem os mais comuns / Open Finance).

def normalizar_transacao(item: dict, conta: str = "") -> TransacaoBancaria | None:
    rid = _pega(item, "transactionId", "id", "idTransacao", "nsu")
    data = para_data(_pega(item, "transactionDate", "bookingDate", "data", "dataLancamento"))
    valor = para_decimal(_pega(item, "amount", "valor", "value"))
    if rid is None or data is None or valor is None:
        return None
    tipo_raw = str(_pega(item, "type", "tipo", "creditDebitType", "indicador", default="")).lower()
    tipo = CREDITO if tipo_raw in ("credito", "credit", "c", "credito_conta", "entrada") else (
        DEBITO if tipo_raw in ("debito", "debit", "d", "saida") else
        (CREDITO if valor >= 0 else DEBITO)
    )
    return TransacaoBancaria(
        id=str(rid),
        conta=str(_pega(item, "account", "conta", default=conta) or conta),
        data=data,
        valor=abs(valor),
        tipo=tipo,
        descricao=str(_pega(item, "description", "descricao", "historico", default="")),
        documento=(str(_pega(item, "document", "documento")) if _pega(item, "document", "documento") else None),
        saldo_apos=para_decimal(_pega(item, "balanceAfter", "saldoApos", "saldo")),
    )


def normalizar_extrato(payload: dict | list, conta: str = "") -> list[TransacaoBancaria]:
    itens = payload.get("data", payload.get("transactions", [])) if isinstance(payload, dict) else payload
    out = []
    for it in itens or []:
        t = normalizar_transacao(it, conta)
        if t:
            out.append(t)
    return out


def normalizar_saldo(payload: dict) -> SaldoConta:
    dados = payload.get("data", payload) if isinstance(payload, dict) else {}
    return SaldoConta(
        conta=str(_pega(dados, "account", "conta", default="")),
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
    base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    cert_path: str = ""          # certificado da aplicação (mTLS) .pem/.crt
    key_path: str = ""           # chave privada do certificado
    scopes: str = ""
    contas: list[str] = field(default_factory=list)

    @classmethod
    def from_app(cls, app) -> "ItauConfig":
        contas = (app.config.get("ITAU_CONTAS") or "").replace(";", ",")
        return cls(
            base_url=app.config.get("ITAU_BASE_URL", ""),
            client_id=app.config.get("ITAU_CLIENT_ID", ""),
            client_secret=app.config.get("ITAU_CLIENT_SECRET", ""),
            cert_path=app.config.get("ITAU_CERT_PATH", ""),
            key_path=app.config.get("ITAU_KEY_PATH", ""),
            scopes=app.config.get("ITAU_SCOPES", ""),
            contas=[c.strip() for c in contas.split(",") if c.strip()],
        )

    @property
    def configurada(self) -> bool:
        return bool(self.base_url and self.client_id and self.client_secret
                    and self.cert_path and self.key_path)


class ItauClient:
    """Cliente da API direta do Itaú (OAuth2 client_credentials + mTLS).

    A camada de rede fica pronta na estrutura, mas só é habilitada com as
    credenciais/certificados reais (ver README › Integração Itaú). Sem eles,
    cada método levanta erro explicativo em vez de simular uma chamada.
    """

    def __init__(self, config: ItauConfig):
        self.config = config
        self._token: str | None = None

    def _exigir_credenciais(self):
        if not self.config.configurada:
            raise RuntimeError(
                "Credenciais Itaú ausentes. Configure ITAU_BASE_URL, "
                "ITAU_CLIENT_ID, ITAU_CLIENT_SECRET, ITAU_CERT_PATH e "
                "ITAU_KEY_PATH no .env (ver README › Integração Itaú)."
            )

    def obter_token(self) -> str:
        self._exigir_credenciais()
        # Estrutura real (a confirmar com a doc do portal Itaú):
        #   import requests
        #   resp = requests.post(
        #       f"{self.config.base_url}/oauth/token",
        #       data={"grant_type": "client_credentials",
        #             "client_id": self.config.client_id,
        #             "client_secret": self.config.client_secret,
        #             "scope": self.config.scopes},
        #       cert=(self.config.cert_path, self.config.key_path),  # mTLS
        #       timeout=30)
        #   resp.raise_for_status()
        #   return resp.json()["access_token"]
        raise NotImplementedError(
            "Autenticação Itaú ainda não habilitada: falta confirmar o endpoint "
            "de token e instalar os certificados mTLS da conta da cliente."
        )

    def _get(self, caminho: str) -> dict:
        token = self.obter_token()  # levanta erro claro até haver credenciais
        raise NotImplementedError(f"GET {caminho} pendente de credenciais/certificado.")

    # --- endpoints (retornam estruturas normalizadas quando habilitados) ---

    def saldo(self, conta: str) -> SaldoConta:
        return normalizar_saldo(self._get(f"/accounts/{conta}/balances"))

    def extrato(self, conta: str, inicio: date, fim: date) -> list[TransacaoBancaria]:
        return normalizar_extrato(
            self._get(f"/accounts/{conta}/transactions?from={inicio}&to={fim}"), conta
        )

    def cartao(self, inicio: date, fim: date) -> list[LancamentoCartao]:
        payload = self._get(f"/credit-cards/transactions?from={inicio}&to={fim}")
        itens = payload.get("data", []) if isinstance(payload, dict) else payload
        return [c for c in (normalizar_cartao(i) for i in itens) if c]

    def investimentos(self) -> list[PosicaoInvestimento]:
        raise NotImplementedError("Endpoint de investimentos pendente de credenciais.")
