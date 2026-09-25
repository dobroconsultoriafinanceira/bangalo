# -*- coding: utf-8 -*-
"""Importa a aba '2026' da planilha 'FLUXO DE CAIXA BANGALO'.

A aba é uma matriz conta(linha) × dia(coluna). Regras confirmadas:

  - Cada mês = colunas de DIA + 1 coluna de SUBTOTAL mensal. A coluna de dia
    tem "Saldo Inicial" (linha 2) preenchido; a de subtotal tem vazio. Só os
    dias são importados (senão cada mês contaria em dobro).
  - O ano dos cabeçalhos está errado (2023/2024/2026 misturados): usamos
    mês/dia e forçamos 2026. 29/02 não existe em 2026 e é pulado.
  - Células de TEXTO que parecem número (ex.: "284.87") CONTAM (são lançamentos
    reais que o Excel deixa de somar) e são listadas no relatório.

IMPORTANTE: as linhas da aba mudam de posição quando o cliente insere/remove
contas (é uma planilha viva). Por isso o layout é detectado pelos RÓTULOS das
seções (col A/B), não por números de linha fixos.

Idempotente: apaga lançamentos e aplicações do ano-alvo antes de reimportar.
Emite conferência contra as linhas "Total" diárias da própria planilha.
"""
import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
from sqlalchemy import extract, func

from app.extensions import db
from app.importers.comum import para_data, para_decimal
from app.importers.stone_adapter import CATEGORIAS_STONE
from app.models.fluxo import (
    AplicacaoFinanceira,
    Categoria,
    ConfigSistema,
    Fornecedor,
    Lancamento,
)
from app.services.fluxo_caixa import CHAVE_DATA_ABERTURA, CHAVE_SALDO_ABERTURA

ANO_ALVO = 2026
ABA = "2026"
CHAVE_APLICACAO_ABERTURA = "saldo_aplicacao_abertura"

SKIP_LABELS = {"subtotal", "total", "receita líquida", "saldo"}



# O cliente redigita rótulos ("MUSICOS", "Cartão de Crédito 15 Itau"): a linha é
# casada com a categoria/fornecedor existente ignorando acento, caixa e
# pontuação. Estes aliases cobrem grafias que nem assim batem.
_TEXTO_NUMERO = re.compile(r"-?(\d{1,3}(\.\d{3})+|\d+)(,\d{1,2})?|-?\d+\.\d{1,2}")

ALIASES_CATEGORIA = {
    "visaeletrondebito": "visaelectrondebito",
    "manutencaoobraseequipam": "manutencaoobrasequip",
    "falaeexperienciab25": "falaeexperienciab2s",
}


def _pular(label: str) -> bool:
    low = label.strip().lower()
    return not low or any(k in low for k in SKIP_LABELS)


def _norm(v) -> str:
    s = str(v or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


# Desde 23/09/2026 as entradas de cartão vêm da API da Stone, não da planilha.
# A linha continua existindo no arquivo do cliente; o importador apenas a ignora.
LINHAS_DA_STONE = {re.sub(r"[^a-z0-9]", "", _norm(n)) for n in CATEGORIAS_STONE}


def _chave(v) -> str:
    """Rótulo sem acento, caixa, espaços e pontuação (para casar grafias)."""
    return re.sub(r"[^a-z0-9]", "", _norm(v))


def _mapear_layout(cel, nlinhas: int) -> tuple[dict, list[str]]:
    """Localiza as seções pelos rótulos. Retorna (mapa, erros)."""

    def achar(coluna0: int, alvo: str, inicio: int = 1):
        alvo = _norm(alvo)
        for r in range(inicio, nlinhas + 1):
            if _norm(cel(r, coluna0)) == alvo:
                return r
        return None

    A, B = 0, 1
    m = {}
    m["saldo_inicial"] = achar(B, "saldo inicial") or 2
    m["total_entradas"] = achar(A, "total entradas")
    m["saidas"] = achar(A, "saidas")                      # cabeçalho da seção (linha do DAS)
    m["total_impostos"] = achar(A, "total impostos")
    m["cv1"] = achar(A, "custo variavel 1")               # 1ª linha de Folha/Salários
    m["sub_salarios"] = achar(A, "subtotal salarios")
    m["sub_desp_gerais"] = achar(A, "subtotal despesas gerais")
    m["sub_compras"] = achar(A, "subtotal compras")
    m["cv2"] = achar(A, "custo variavel 2")               # 1ª linha de Despesas Fixas
    m["outros"] = achar(B, "emprestimos heitor")          # 1ª linha de Outros/Financeiro
    m["total_despesas"] = achar(A, "total despesas")
    m["total_saidas"] = achar(A, "total saidas")
    m["saldo_aplicacao"] = achar(B, "saldo aplicacao")
    # bloco de aplicação vem SEMPRE depois do "SALDO APLICAÇÃO"
    ap0 = (m["saldo_aplicacao"] or 0) + 1
    m["ap_entrada"] = achar(B, "entrada", inicio=ap0)
    m["ap_rendimento"] = achar(B, "rendimento", inicio=ap0)
    m["ap_resgate"] = achar(B, "resgate", inicio=ap0)

    faltando = [k for k, v in m.items() if v is None]
    return m, faltando


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    if ABA not in wb.sheetnames:
        wb.close()
        return [f"Aba '{ABA}' não encontrada."]
    ws = wb[ABA]
    linhas = list(ws.iter_rows(values_only=True))
    wb.close()

    def cel(row_idx1: int, col_idx0: int):
        r = row_idx1 - 1
        if r < 0 or r >= len(linhas) or col_idx0 >= len(linhas[r]):
            return None
        return linhas[r][col_idx0]

    textos_numericos: list[str] = []

    def valor_num(row_idx1: int, col_idx0: int) -> Decimal | None:
        """Valor numérico da célula. Texto com cara de número (ex.: "284.87")
        também conta — é lançamento real que o Excel deixa de somar (decisão da
        cliente em 15/09/2026) — e vai para o relatório para corrigir a planilha."""
        v = cel(row_idx1, col_idx0)
        if isinstance(v, (int, float)):
            return para_decimal(v)
        if isinstance(v, str) and _TEXTO_NUMERO.fullmatch(v.strip()):
            valor = para_decimal(v)
            if valor:
                textos_numericos.append(
                    f'{str(cel(row_idx1, 1) or "").strip()} em {col_data.get(col_idx0)}: "{v.strip()}"'
                )
            return valor
        return None

    # --- descobre o layout pelos rótulos ---
    m, faltando = _mapear_layout(cel, len(linhas))
    if faltando:
        return [
            "Não foi possível mapear a estrutura da aba 2026 — rótulos ausentes: "
            + ", ".join(faltando)
            + ". A planilha pode ter mudado; ajuste os rótulos-âncora do importador."
        ]

    L_SALDO_INI = m["saldo_inicial"]
    L_TOT_ENT = m["total_entradas"]
    L_TOT_IMP = m["total_impostos"]
    L_TOT_SAI = m["total_saidas"]
    L_SALDO_APLIC = m["saldo_aplicacao"]

    # (ini, fim_inclusive, tipo, grupo|None p/ derivar do rótulo, eh_fornecedor)
    faixas = [
        (L_SALDO_INI + 1, L_TOT_ENT - 1, "entrada", None, False),
        (m["saidas"], L_TOT_IMP - 1, "saida", "Impostos", False),
        (m["cv1"], m["sub_salarios"] - 1, "saida", "Folha/Salários", False),
        (m["sub_salarios"] + 1, m["sub_desp_gerais"] - 1, "saida", "Demais Salários", False),
        (m["sub_desp_gerais"] + 1, m["sub_compras"] - 1, "saida", "Compras", True),
        (m["cv2"], m["outros"] - 1, "saida", "Despesas Fixas", False),
        (m["outros"], m["total_despesas"] - 1, "saida", "Outras despesas", False),
    ]
    aplicacao_rows = {
        m["ap_entrada"]: "entrada",
        m["ap_rendimento"]: "rendimento",
        m["ap_resgate"]: "resgate",
    }

    header = linhas[0]

    # --- classifica colunas: DIA (Saldo Inicial preenchido) x SUBTOTAL (vazio) ---
    col_data: dict[int, object] = {}
    cols_subtotal: list[int] = []
    feb29 = 0
    for col in range(2, len(header)):
        v1 = header[col]
        saldo = cel(L_SALDO_INI, col)
        if not isinstance(v1, datetime):
            if saldo is None and (cel(L_TOT_ENT, col) is not None or cel(L_SALDO_INI + 1, col) is not None):
                cols_subtotal.append(col)
            continue
        if isinstance(saldo, (int, float)):
            try:
                col_data[col] = v1.replace(year=ANO_ALVO).date()
            except ValueError:
                feb29 += 1
        else:
            cols_subtotal.append(col)

    primeira_col = min(col_data) if col_data else 2
    saldo_abertura = para_decimal(cel(L_SALDO_INI, primeira_col))
    data_abertura = col_data.get(primeira_col)
    aplic_abertura = para_decimal(cel(L_SALDO_APLIC, primeira_col))

    # idempotência — a conciliação bancária aponta para lançamentos que serão
    # recriados: solta os vínculos antes (SQLite não aplica ON DELETE em cascata)
    from app.models.banco import ConciliacaoItem, MovimentoBancario

    ids_do_ano = db.select(Lancamento.id).filter(extract("year", Lancamento.data) == ANO_ALVO)
    movimentos_afetados = db.select(ConciliacaoItem.movimento_id).filter(
        ConciliacaoItem.lancamento_id.in_(ids_do_ano))
    db.session.query(MovimentoBancario).filter(MovimentoBancario.id.in_(movimentos_afetados)).update(
        {"conciliado_em": None, "diferenca_conciliacao": 0}, synchronize_session=False
    )
    db.session.query(ConciliacaoItem).filter(
        ConciliacaoItem.lancamento_id.in_(ids_do_ano)
    ).delete(synchronize_session=False)
    db.session.query(Lancamento).filter(
        extract("year", Lancamento.data) == ANO_ALVO,
        Lancamento.origem == "manual",   # o que veio da Stone/Itaú fica
    ).delete(synchronize_session=False)
    db.session.query(AplicacaoFinanceira).filter(
        extract("year", AplicacaoFinanceira.data) == ANO_ALVO
    ).delete(synchronize_session=False)

    ultima_cat = db.session.query(func.max(Categoria.id)).scalar() or 0
    ultimo_forn = db.session.query(func.max(Fornecedor.id)).scalar() or 0
    cache_cat: dict[tuple[str, str], Categoria] = {}
    cache_forn: dict[str, Fornecedor] = {}
    total_entradas = Decimal("0")
    total_impostos = Decimal("0")
    total_saidas_oper = Decimal("0")
    n_lanc = 0

    def grupo_entrada(label_norm: str) -> str:
        """Cada entrada não-cartão virou categoria própria (sem subcategoria)."""
        proprias = {"patrocinio": "Patrocínio", "emprestimos": "Empréstimo",
                    "emprestimo": "Empréstimo", "resgate": "Resgate",
                    "outros/acerto": "Outros/Acertos", "outros/acertos": "Outros/Acertos",
                    "dinheiro": "Dinheiro", "ifood": "IFOOD", "99 food": "99 FOOD"}
        return proprias.get(label_norm, "Vendas - Repasse Stone")

    # --- lançamentos de fluxo ---
    pulados_stone: set[str] = set()
    for ini, fim, tipo, grupo, eh_forn in faixas:
        for r in range(ini, fim + 1):
            label = str(cel(r, 1) or "").strip()  # col B
            if _pular(label):
                continue
            if ALIASES_CATEGORIA.get(_chave(label), _chave(label)) in LINHAS_DA_STONE:
                pulados_stone.add(label)   # fonte agora é a API da Stone
                continue
            g = grupo if grupo is not None else grupo_entrada(_norm(label))
            if eh_forn:
                fornecedor = _get_fornecedor(cache_forn, label)
                categoria = _get_categoria(cache_cat, "Compras", tipo, g)
                fornecedor_id = fornecedor.id
            else:
                categoria = _get_categoria(cache_cat, label, tipo, g)
                fornecedor_id = None
            for col, data in col_data.items():
                valor = valor_num(r, col)
                if valor is None or valor == 0:
                    continue
                db.session.add(Lancamento(
                    data=data, categoria_id=categoria.id,
                    fornecedor_id=fornecedor_id, valor=valor,
                ))
                n_lanc += 1
                if tipo == "entrada":
                    total_entradas += valor
                elif g == "Impostos":
                    total_impostos += valor
                else:
                    total_saidas_oper += valor

    # --- bloco de aplicação financeira ---
    n_aplic = 0
    for row_idx, tipo_aplic in aplicacao_rows.items():
        for col, data in col_data.items():
            valor = valor_num(row_idx, col)
            if valor is None or valor == 0:
                continue
            db.session.add(AplicacaoFinanceira(data=data, tipo=tipo_aplic, valor=valor))
            n_aplic += 1

    # --- configs de abertura ---
    if saldo_abertura is not None:
        ConfigSistema.definir(CHAVE_SALDO_ABERTURA, str(saldo_abertura))
    if data_abertura is not None:
        ConfigSistema.definir(CHAVE_DATA_ABERTURA, data_abertura.isoformat())
    if aplic_abertura is not None:
        ConfigSistema.definir(CHAVE_APLICACAO_ABERTURA, str(aplic_abertura))

    db.session.flush()

    # --- refaz a conciliação bancária contra os lançamentos recriados ---
    from datetime import date

    from app.services import itau_sync

    conciliacao = itau_sync.conciliar_periodo(date(ANO_ALVO, 1, 1), date(ANO_ALVO, 12, 31))

    # --- conferência contra as linhas "Total" DIÁRIAS da planilha ---
    def soma_dia(linha):
        return sum((para_decimal(cel(linha, c)) or Decimal("0") for c in col_data), Decimal("0"))

    plan_entradas = soma_dia(L_TOT_ENT)
    plan_impostos = soma_dia(L_TOT_IMP)
    plan_saidas = soma_dia(L_TOT_SAI)

    from collections import defaultdict

    diario_ent: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for c, d in col_data.items():
        diario_ent[d.month] += para_decimal(cel(L_TOT_ENT, c)) or Decimal("0")
    resumo_ent: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for i, c in enumerate(sorted(cols_subtotal), start=1):
        resumo_ent[i] += para_decimal(cel(L_TOT_ENT, c)) or Decimal("0")
    meses_inconsistentes = sorted(
        mes for mes in range(1, 13)
        if (diario_ent[mes] - resumo_ent.get(mes, Decimal("0"))).copy_abs() > Decimal("0.05")
    )

    tol = Decimal("0.05")

    def linha_conf(rotulo, importado, planilha):
        dif = importado - planilha
        if dif.copy_abs() <= tol:
            status = "OK"
        elif dif > 0:
            status = f"import +R$ {dif} (a planilha não totaliza algumas linhas; import mais completo)"
        else:
            status = f"conferir (faltam R$ {dif.copy_abs()})"
        return f"{rotulo}: importado R$ {importado} | planilha (dias) R$ {planilha} -> {status}."

    relatorio = [
        f"Fluxo {ANO_ALVO}: {n_lanc} lançamentos em {len(col_data)} dias; "
        f"{n_aplic} movimentos de aplicação.",
        linha_conf("Entradas", total_entradas, plan_entradas),
        linha_conf("Impostos", total_impostos, plan_impostos),
        linha_conf("Saídas operacionais (sem impostos)", total_saidas_oper, plan_saidas),
        f"Saldo de abertura: R$ {saldo_abertura or 0} em {data_abertura or '—'} "
        f"| aplicação inicial: R$ {aplic_abertura or 0}.",
    ]
    novas_cats = db.session.execute(
        db.select(Categoria.grupo, Categoria.nome).filter(Categoria.id > ultima_cat)
    ).all()
    novos_forns = db.session.execute(
        db.select(Fornecedor.nome).filter(Fornecedor.id > ultimo_forn)
    ).scalars().all()
    if textos_numericos:
        relatorio.append(
            "Valores digitados como TEXTO na planilha (o Excel não soma; o sistema conta — "
            "corrija a célula na planilha): " + "; ".join(textos_numericos) + "."
        )
    if novas_cats:
        relatorio.append("Categorias novas criadas: " + "; ".join(f"{g} › {n}" for g, n in novas_cats) + ".")
    if novos_forns:
        relatorio.append("Fornecedores novos criados: " + "; ".join(novos_forns) + ".")
    if conciliacao["conciliados"]:
        relatorio.append(f"Conciliação bancária refeita: {conciliacao['conciliados']} movimentos conciliados.")
    if feb29:
        relatorio.append("29/02 ignorado (inexistente em 2026, sem movimento real na planilha).")
    if meses_inconsistentes:
        nomes = ", ".join(str(mes).zfill(2) for mes in meses_inconsistentes)
        relatorio.append(
            "Aviso: na planilha, a coluna de resumo mensal diverge da soma dos dias "
            f"nos meses {nomes}. Importamos a soma diária (movimentos reais)."
        )
    return relatorio


def _get_categoria(cache, nome, tipo, grupo) -> Categoria:
    chave = ALIASES_CATEGORIA.get(_chave(nome), _chave(nome))
    if (chave, grupo) in cache:
        return cache[(chave, grupo)]
    cat = next(
        (c for c in db.session.execute(db.select(Categoria).filter_by(grupo=grupo)).scalars()
         if _chave(c.nome) == chave),
        None,
    )
    if not cat:
        cat = Categoria(nome=nome, tipo=tipo, grupo=grupo, ordem=0, ativo=True)
        db.session.add(cat)
        db.session.flush()
    cache[(chave, grupo)] = cat
    return cat


def _get_fornecedor(cache, nome) -> Fornecedor:
    chave = _chave(nome)
    if chave in cache:
        return cache[chave]
    forn = next(
        (f for f in db.session.execute(db.select(Fornecedor)).scalars() if _chave(f.nome) == chave),
        None,
    )
    if not forn:
        forn = Fornecedor(nome=nome, ativo=True)
        db.session.add(forn)
        db.session.flush()
    cache[chave] = forn
    return forn
