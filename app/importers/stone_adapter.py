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

ZERO_DEC = Decimal("0.00")
CENTAVOS_DEC = Decimal("0.01")

# ---- Mapa bandeira × produto -> categoria do plano de contas (nomes do seed) ----
# produto: "credito" | "debito" | "pix" | "voucher"
MAPA_CATEGORIA: dict[tuple[str, str], str] = {
    ("visa", "credito"): "Visa Crédito",
    ("visa", "debito"): "Visa Eletron Débito",
    ("mastercard", "credito"): "Master Card Crédito",
    ("mastercard", "debito"): "Maestro Débito",
    ("master", "credito"): "Master Card Crédito",
    ("master", "debito"): "Maestro Débito",
    ("elo", "credito"): "ELO Crédito",
    ("elo", "debito"): "ELO Débito",
    ("amex", "credito"): "Amex Crédito",
    ("american express", "credito"): "Amex Crédito",
}
# WalletTypeId (tabela de domínio da Stone) -> categoria do nosso plano de contas.
# É por aqui que entra o REPASSE: cada pagamento do dia vem por arranjo de pagamento,
# exatamente como a planilha lança as linhas de cartão.
WALLET_TYPE = {
    2: "Visa Eletron Débito", 26: "Visa Eletron Débito",
    3: "Visa Crédito", 4: "Visa Crédito", 35: "Visa Crédito", 36: "Visa Crédito",
    5: "Maestro Débito", 27: "Maestro Débito",
    6: "Master Card Crédito", 7: "Master Card Crédito", 33: "Master Card Crédito", 34: "Master Card Crédito",
    11: "ELO Débito", 28: "ELO Débito",
    12: "ELO Crédito", 13: "ELO Crédito", 31: "ELO Crédito", 32: "ELO Crédito",
    14: "Amex Crédito", 15: "Amex Crédito", 16: "Amex Crédito",
}
# BrandId + AccountType do container de transações (vendas capturadas)
BRAND_ID = {1: "visa", 2: "mastercard", 3: "elo", 4: "amex", 171: "outras"}
# conferido contra a tela "Vendas" do app da Stone em 23/09/2026 (semana 17–23/09:
# crédito 246 vendas/R$ 58.541,78 e débito 57/R$ 11.847,91): 2 = crédito, 1 = débito.
ACCOUNT_TYPE = {1: "debito", 2: "credito", 3: "voucher", 4: "voucher"}

# Entradas cuja FONTE passou a ser a Stone (decisão da consultoria em 23/09/2026):
# a planilha não lança mais essas linhas — elas nascem do repasse da Stone.
CATEGORIAS_STONE = (
    "Visa Crédito", "Master Card Crédito", "ELO Crédito", "Amex Crédito",
    "ELO Débito", "Visa Eletron Débito", "Maestro Débito",
)

CATEGORIA_PIX = "Pix Stone"
CATEGORIA_FALLBACK = "Outros/Acertos"
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


# --------- Parser do arquivo XML (layout 2.2 / 2.4 da API de Conciliação) ---------


@dataclass
class PagamentoStone:
    """Repasse do dia: o que a Stone deposita na conta, por arranjo de pagamento."""

    id: str
    data: date
    wallet_type_id: int
    valor: Decimal

    @property
    def categoria_nome(self) -> str:
        return WALLET_TYPE.get(self.wallet_type_id, CATEGORIA_FALLBACK)


@dataclass
class ArquivoStone:
    """Um dia de conciliação: vendas capturadas + repasses liquidados."""

    stone_code: str
    referencia: date | None
    transacoes: list[TransacaoStone] = field(default_factory=list)
    pagamentos: list[PagamentoStone] = field(default_factory=list)

    @property
    def total_vendas(self) -> Decimal:
        return sum((t.valor_bruto for t in self.transacoes), Decimal("0.00"))

    @property
    def total_repassado(self) -> Decimal:
        return sum((p.valor for p in self.pagamentos), Decimal("0.00"))


def _data_stone(texto: str | None) -> date | None:
    """AAAAMMDD (ou AAAAMMDDHHMMSS) -> date."""
    texto = (texto or "").strip()
    if len(texto) < 8 or not texto[:8].isdigit():
        return None
    try:
        return date(int(texto[:4]), int(texto[4:6]), int(texto[6:8]))
    except ValueError:
        return None


def parse_conciliacao_xml(conteudo: str | bytes) -> ArquivoStone:
    """Lê o arquivo XML da API (validado contra arquivos reais do Bangalô).

    - `FinancialTransactions/Transaction`: vendas capturadas no dia (bruto e
      líquido por parcela, bandeira em BrandId e produto em AccountType).
    - `Payments/Payment`: os repasses liquidados no dia, por WalletTypeId —
      é o que a planilha registra nas linhas de cartão.
    """
    import xml.etree.ElementTree as ET

    if isinstance(conteudo, bytes):
        conteudo = conteudo.decode("utf-8", errors="replace")
    raiz = ET.fromstring(conteudo)
    arquivo = ArquivoStone(
        stone_code=(raiz.findtext("Header/StoneCode") or "").strip(),
        referencia=_data_stone(raiz.findtext("Header/ReferenceDate")),
    )

    for t in raiz.findall("FinancialTransactions/Transaction"):
        capturas = int((t.findtext("Events/Captures") or "0").strip() or 0)
        cancelamentos = int((t.findtext("Events/Cancellations") or "0").strip() or 0)
        if capturas <= 0 or cancelamentos > 0:
            continue  # só venda efetivada e não cancelada
        bruto = sum((para_decimal(i.findtext("GrossAmount")) or ZERO_DEC
                     for i in t.findall("Installments/Installment")), ZERO_DEC)
        liquido = sum((para_decimal(i.findtext("NetAmount")) or ZERO_DEC
                       for i in t.findall("Installments/Installment")), ZERO_DEC)
        venda = _data_stone(t.findtext("CaptureLocalDateTime")) or arquivo.referencia
        arquivo.transacoes.append(TransacaoStone(
            id=(t.findtext("AcquirerTransactionKey") or "").strip(),
            data_venda=venda,
            bandeira=BRAND_ID.get(int((t.findtext("BrandId") or "0").strip() or 0), ""),
            produto=ACCOUNT_TYPE.get(int((t.findtext("AccountType") or "0").strip() or 0), ""),
            valor_bruto=bruto.quantize(CENTAVOS_DEC),
            valor_liquido=liquido.quantize(CENTAVOS_DEC),
            taxa=(bruto - liquido).quantize(CENTAVOS_DEC),
            status="approved",
            data_liquidacao=_data_stone(t.findtext("Installments/Installment/PrevisionPaymentDate")),
            stone_code=arquivo.stone_code,
        ))

    for p in raiz.findall("Payments/Payment"):
        valor = para_decimal(p.findtext("TotalAmount"))
        if valor is None:
            continue
        arquivo.pagamentos.append(PagamentoStone(
            id=(p.findtext("Id") or "").strip(),
            data=arquivo.referencia,
            wallet_type_id=int((p.findtext("WalletTypeId") or "0").strip() or 0),
            valor=valor.quantize(CENTAVOS_DEC),
        ))
    return arquivo


# --------------------- Cliente HTTP (camada de rede) ---------------------

@dataclass
class StoneConfig:
    base_url: str = ""
    client_application_key: str = ""
    secret_key: str = ""
    stone_codes: list[str] = field(default_factory=list)
    layout: str = "XML2_2"

    @classmethod
    def from_app(cls, app) -> "StoneConfig":
        codes = (app.config.get("STONE_CODES") or "").replace(";", ",")
        return cls(
            base_url=app.config.get("STONE_BASE_URL", ""),
            client_application_key=app.config.get("STONE_CLIENT_APPLICATION_KEY", ""),
            secret_key=app.config.get("STONE_SECRET_KEY", ""),
            stone_codes=[c.strip() for c in codes.split(",") if c.strip()],
            layout=app.config.get("STONE_LAYOUT", "XML2_2"),
        )

    @property
    def configurada(self) -> bool:
        # a API de Conciliação do LOJISTA usa só a chave (Basic auth) + Stone Code;
        # ClientApplicationKey é do fluxo de conciliadora parceira.
        return bool(self.base_url and self.secret_key and self.stone_codes)

    @property
    def faltando(self) -> list[str]:
        """Credenciais que ainda não chegaram — a tela mostra o que pedir à Stone."""
        pendentes = []
        if not self.base_url:
            pendentes.append("STONE_BASE_URL (endereço da API de Conciliação)")
        if not self.secret_key:
            pendentes.append("STONE_SECRET_KEY (chave gerada no portal Stone)")
        if not self.stone_codes:
            pendentes.append("STONE_CODES (Stone Code da loja)")
        return pendentes


class StoneClient:
    """Baixa o arquivo de conciliação de um dia na API da Stone.

    Contrato confirmado na documentação oficial e contra a conta real do
    Bangalô (2026-09-23):

        GET https://conciliation.stone.com.br/v2/merchant/{stoneCode}/conciliation-file/{AAAAMMDD}?layout=XML2_2
        Authorization: Basic <chave>:   (chave como usuário, senha vazia)
        x-user-type: client

    A Stone só disponibiliza o arquivo de um dia depois das 5h do dia seguinte.
    """

    TIMEOUT = 120

    def __init__(self, config: StoneConfig):
        self.config = config

    def _url(self, stone_code: str, dia: date) -> str:
        return f"{self.config.base_url.rstrip('/')}/v2/merchant/{stone_code}/conciliation-file/{dia:%Y%m%d}"

    def baixar_dia(self, dia: date, stone_code: str | None = None) -> str:
        import requests

        if not self.config.configurada:
            raise RuntimeError(
                "Credenciais Stone incompletas. Preencha STONE_BASE_URL, STONE_SECRET_KEY e "
                "STONE_CODES no .env (ver README › Integração Stone)."
            )
        code = stone_code or self.config.stone_codes[0]
        resp = requests.get(
            self._url(code, dia),
            params={"layout": self.config.layout},
            auth=(self.config.secret_key, ""),
            headers={"x-user-type": "client", "Accept-Encoding": "gzip"},
            timeout=self.TIMEOUT,
        )
        if resp.status_code == 401:
            raise RuntimeError("Stone recusou a chave (401). Gere uma nova chave da API de Conciliação no portal.")
        if resp.status_code == 403:
            raise RuntimeError(f"A chave não tem acesso ao Stone Code {code} (403).")
        if resp.status_code == 503:
            raise RuntimeError("Stone em manutenção (503). O arquivo do dia sai depois das 5h do dia seguinte.")
        resp.raise_for_status()
        return resp.text

    def arquivo_do_dia(self, dia: date, stone_code: str | None = None) -> ArquivoStone:
        return parse_conciliacao_xml(self.baixar_dia(dia, stone_code))

    def transacoes_do_dia(self, dia: date, stone_code: str | None = None) -> list[TransacaoStone]:
        return self.arquivo_do_dia(dia, stone_code).transacoes
