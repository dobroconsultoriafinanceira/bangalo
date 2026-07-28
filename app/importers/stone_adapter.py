# -*- coding: utf-8 -*-
"""Adapter da API de Conciliação Stone.

A Conciliação Stone é baseada em ARQUIVO: autentica-se com credenciais
atreladas ao(s) Stone Code(s) da loja e baixa-se o arquivo de conciliação
de cada dia (Layouts 2.2/2.4), com bruto, líquido, bandeira, taxa (MDR),
status e datas de captura/liquidação.

Este módulo isola a integração em três camadas:

  1. `StoneClient`      — baixa o arquivo bruto de um dia (HTTP). Precisa das
                          credenciais reais; sem elas, levanta erro claro.
  2. `parse_conciliacao`— converte o arquivo bruto em `TransacaoStone`
                          normalizadas. Como o layout exato depende da
                          versão/credenciais, começa com um parser CSV por
                          nome de coluna, ajustável quando houver um arquivo
                          de exemplo real.
  3. `to_lancamentos`   — transform PURO e testável: mapeia transações para
                          lançamentos de ENTRADA por bandeira. É onde mora a
                          regra de negócio (independe de rede/DB).

Decisão de valor (padrão, configurável): registramos o valor **bruto** da
venda como entrada (espelha as linhas por bandeira da planilha). A taxa
(MDR) pode, opcionalmente, virar uma saída em "Despesas Bancárias"
(STONE_LANCAR_TAXA=true).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.importers.comum import para_data, para_decimal

# ---- Mapa bandeira × produto -> categoria do plano de contas (nomes do seed) ----
# produto: "credito" | "debito" | "pix" | "voucher"
MAPA_CATEGORIA: dict[tuple[str, str], str] = {
    ("visa", "credito"): "Visa Crédito",
    ("visa", "debito"): "Visa Electron Débito",
    ("mastercard", "credito"): "Master Card Crédito",
    ("mastercard", "debito"): "Maestro Débito",
    ("master", "credito"): "Master Card Crédito",
    ("master", "debito"): "Maestro Débito",
    ("elo", "credito"): "ELO Crédito",
    ("elo", "debito"): "ELO Débito",
    ("amex", "credito"): "Amex Crédito",
    ("american express", "credito"): "Amex Crédito",
}
CATEGORIA_PIX = "Pagamento em PIX"
CATEGORIA_FALLBACK = "Outros/Acerto"
CATEGORIA_TAXA = "Despesas Bancárias"  # grupo Outros/Financeiro (seed)

# status que contam como venda efetivada
STATUS_VALIDOS = {"approved", "aprovada", "aprovado", "captured", "capturada", "paid", "pago", "liquidada"}


def categoria_para(bandeira: str, produto: str) -> str:
    b = (bandeira or "").strip().lower()
    p = (produto or "").strip().lower()
    if p == "pix" or b == "pix":
        return CATEGORIA_PIX
    return MAPA_CATEGORIA.get((b, p), CATEGORIA_FALLBACK)


@dataclass
class TransacaoStone:
    """Transação normalizada (desacoplada do layout do arquivo)."""

    id: str                       # id único da transação na Stone (dedupe)
    data_venda: date
    bandeira: str                 # visa, mastercard, elo, amex, pix...
    produto: str                  # credito, debito, pix
    valor_bruto: Decimal
    valor_liquido: Decimal | None = None
    taxa: Decimal | None = None
    status: str = "approved"
    data_liquidacao: date | None = None
    stone_code: str | None = None

    @property
    def efetivada(self) -> bool:
        return (self.status or "").strip().lower() in STATUS_VALIDOS


@dataclass
class LancamentoEntrada:
    """Resultado do transform (o serviço grava isto como Lancamento)."""

    data: date
    categoria_nome: str
    forma_pagamento: str
    valor: Decimal
    origem_id: str
    descricao: str
    tipo: str = "entrada"  # "entrada" (venda) ou "saida" (taxa MDR)


def to_lancamentos(
    transacoes: list[TransacaoStone],
    lancar_taxa: bool = False,
) -> list[LancamentoEntrada]:
    """Transform PURO: transações Stone -> lançamentos por bandeira.

    - Só considera transações efetivadas.
    - Uma entrada (bruto) por transação, na categoria da bandeira.
    - Se `lancar_taxa`, cria também uma saída da taxa (MDR) em Despesas
      Bancárias, com origem_id sufixado (`:taxa`) para não colidir.
    """
    saida: list[LancamentoEntrada] = []
    for t in transacoes:
        if not t.efetivada or not t.valor_bruto:
            continue
        cat = categoria_para(t.bandeira, t.produto)
        saida.append(LancamentoEntrada(
            data=t.data_venda,
            categoria_nome=cat,
            forma_pagamento=cat,
            valor=Decimal(t.valor_bruto).quantize(Decimal("0.01")),
            origem_id=t.id,
            descricao=f"Stone · {t.bandeira}/{t.produto}"
                      + (f" · code {t.stone_code}" if t.stone_code else ""),
        ))
        if lancar_taxa and t.taxa and t.taxa > 0:
            saida.append(LancamentoEntrada(
                data=t.data_venda,
                categoria_nome=CATEGORIA_TAXA,
                forma_pagamento="",
                valor=Decimal(t.taxa).quantize(Decimal("0.01")),
                origem_id=f"{t.id}:taxa",
                descricao=f"Taxa Stone (MDR) · {t.bandeira}/{t.produto}",
                tipo="saida",
            ))
    return saida


# --------- Parser do arquivo de conciliação (camada ajustável) ---------

# Nomes de coluna esperados. Confirmar/ajustar contra um arquivo real da
# conciliação (Layout 2.2/2.4). Cada chave aceita uma lista de sinônimos.
COLUNAS = {
    "id": ["id", "transaction_id", "id_transacao", "stone_id", "nsu", "codigo_venda"],
    "data_venda": ["data_venda", "sale_date", "data_captura", "capture_date", "data"],
    "data_liquidacao": ["data_liquidacao", "payment_date", "data_pagamento"],
    "bandeira": ["bandeira", "brand", "card_brand"],
    "produto": ["produto", "product", "tipo", "modalidade"],
    "valor_bruto": ["valor_bruto", "gross_amount", "valor", "amount"],
    "valor_liquido": ["valor_liquido", "net_amount"],
    "taxa": ["taxa", "mdr", "fee", "custo"],
    "status": ["status", "situacao"],
    "stone_code": ["stone_code", "stonecode", "affiliation_code"],
}


def _pega(row: dict, chave: str):
    for nome in COLUNAS[chave]:
        for col, val in row.items():
            if col and col.strip().lower() == nome:
                return val
    return None


def parse_conciliacao_csv(conteudo: str | bytes, delimitador: str = ";") -> list[TransacaoStone]:
    """Parser genérico por nome de coluna (ajustar ao layout real da Stone)."""
    if isinstance(conteudo, bytes):
        conteudo = conteudo.decode("utf-8-sig", errors="replace")
    leitor = csv.DictReader(io.StringIO(conteudo), delimiter=delimitador)
    transacoes: list[TransacaoStone] = []
    for row in leitor:
        rid = _pega(row, "id")
        dv = para_data(_pega(row, "data_venda"))
        bruto = para_decimal(_pega(row, "valor_bruto"))
        if not rid or not dv or bruto is None:
            continue
        transacoes.append(TransacaoStone(
            id=str(rid).strip(),
            data_venda=dv,
            bandeira=str(_pega(row, "bandeira") or "").strip(),
            produto=str(_pega(row, "produto") or "").strip(),
            valor_bruto=bruto,
            valor_liquido=para_decimal(_pega(row, "valor_liquido")),
            taxa=para_decimal(_pega(row, "taxa")),
            status=str(_pega(row, "status") or "approved").strip(),
            data_liquidacao=para_data(_pega(row, "data_liquidacao")),
            stone_code=(str(_pega(row, "stone_code")).strip() if _pega(row, "stone_code") else None),
        ))
    return transacoes


# --------------------- Cliente HTTP (camada de rede) ---------------------

@dataclass
class StoneConfig:
    base_url: str = ""
    client_application_key: str = ""
    secret_key: str = ""
    stone_codes: list[str] = field(default_factory=list)

    @classmethod
    def from_app(cls, app) -> "StoneConfig":
        codes = (app.config.get("STONE_CODES") or "").replace(";", ",")
        return cls(
            base_url=app.config.get("STONE_BASE_URL", ""),
            client_application_key=app.config.get("STONE_CLIENT_APPLICATION_KEY", ""),
            secret_key=app.config.get("STONE_SECRET_KEY", ""),
            stone_codes=[c.strip() for c in codes.split(",") if c.strip()],
        )

    @property
    def configurada(self) -> bool:
        return bool(self.base_url and self.client_application_key and self.secret_key)


class StoneClient:
    """Baixa o arquivo de conciliação de um dia.

    IMPORTANTE: o endpoint/formato exatos e o handshake de autenticação são
    finalizados quando as credenciais reais da conta da cliente estiverem
    disponíveis (ver README › Integração Stone). Até lá, `baixar_dia`
    levanta erro explicativo em vez de inventar uma chamada.
    """

    def __init__(self, config: StoneConfig):
        self.config = config

    def baixar_dia(self, dia: date) -> str:
        if not self.config.configurada:
            raise RuntimeError(
                "Credenciais Stone ausentes. Configure STONE_BASE_URL, "
                "STONE_CLIENT_APPLICATION_KEY e STONE_SECRET_KEY no .env "
                "(ver README › Integração Stone)."
            )
        # Estrutura da chamada real (a confirmar com a doc/credenciais):
        #   import requests
        #   url = f"{self.config.base_url}/conciliation/{dia:%Y-%m-%d}"
        #   headers = {
        #       "ClientApplicationKey": self.config.client_application_key,
        #       "SecretKey": self.config.secret_key,
        #   }
        #   resp = requests.get(url, headers=headers, timeout=60)
        #   resp.raise_for_status()
        #   return resp.text
        raise NotImplementedError(
            "Download do arquivo Stone ainda não habilitado: falta confirmar o "
            "endpoint e o layout com um arquivo real da conta da cliente."
        )

    def transacoes_do_dia(self, dia: date) -> list[TransacaoStone]:
        return parse_conciliacao_csv(self.baixar_dia(dia))
