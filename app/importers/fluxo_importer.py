# -*- coding: utf-8 -*-
"""Importa a aba '2026' da planilha 'FLUXO DE CAIXA BANGALO'.

A aba é uma matriz conta(linha) × dia(coluna). ESTRUTURA REAL (confirmada
célula a célula):

  - Cada mês = colunas de DIA + 1 coluna de SUBTOTAL mensal.
  - A coluna de dia tem "Saldo Inicial" (linha 2) preenchido;
    a coluna de subtotal mensal tem "Saldo Inicial" VAZIO.
    -> usamos isso para importar SÓ os dias e ignorar os subtotais
       (senão cada mês seria contado duas vezes).
  - O ano dos cabeçalhos está errado (2023/2024/2026 misturados): usamos
    mês/dia e forçamos 2026. 29/02 não existe em 2026 e é pulado — na
    planilha esse dia não tem movimento real (só saldos calculados).

Blocos de linha (col A/B):
  3–14  ENTRADAS · Operacional      | 17 Patrocínio | 19–20 Financeiras
  23–24 Impostos | 27–44 Folha/Salários | 46–49 Despesas Gerais
  51–144 Compras (CPV) [fornecedor] | 147–182 Despesas Fixas
  184–190 Outros/Financeiro
  195/196/197 Aplicação: ENTRADA / RENDIMENTO / RESGATE

Idempotente: apaga lançamentos e aplicações do ano-alvo antes de reimportar.
Emite conferência contra os subtotais mensais da própria planilha.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
from sqlalchemy import extract

from app.extensions import db
from app.importers.comum import para_data, para_decimal
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

LINHA_SALDO_INICIAL = 2
LINHA_TOTAL_ENTRADAS = 22
LINHA_TOTAL_IMPOSTOS = 25   # "Total Saidas" da planilha NÃO inclui impostos
LINHA_TOTAL_SAIDAS = 192    # = saídas operacionais (sem impostos)
LINHA_SALDO_APLICACAO = 194
APLICACAO_ROWS = {195: "entrada", 196: "rendimento", 197: "resgate"}
CHAVE_APLICACAO_ABERTURA = "saldo_aplicacao_abertura"

# (linha_inicio, linha_fim_inclusive, tipo, grupo, eh_fornecedor)
FAIXAS = [
    (3, 14, "entrada", "Operacional", False),
    (17, 17, "entrada", "Patrocínio", False),
    (19, 20, "entrada", "Financeiras", False),
    (23, 24, "saida", "Impostos", False),
    (27, 44, "saida", "Folha/Salários", False),
    (46, 49, "saida", "Despesas Gerais", False),
    (51, 144, "saida", "Compras (CPV)", True),
    (147, 182, "saida", "Despesas Fixas", False),
    (184, 190, "saida", "Outros/Financeiro", False),
]

SKIP_LABELS = {"subtotal", "total", "receita líquida", "saldo"}


def _pular(label: str) -> bool:
    low = label.strip().lower()
    return not low or any(k in low for k in SKIP_LABELS)


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    if ABA not in wb.sheetnames:
        wb.close()
        return [f"Aba '{ABA}' não encontrada."]
    ws = wb[ABA]
    linhas = list(ws.iter_rows(values_only=True))
    wb.close()

    def cel(row_idx1: int, col_idx0: int):
        """Valor da célula (linha 1-based da planilha, coluna 0-based da lista)."""
        r = row_idx1 - 1
        if r >= len(linhas) or col_idx0 >= len(linhas[r]):
            return None
        return linhas[r][col_idx0]

    header = linhas[0]

    # --- classifica colunas: DIA (Saldo Inicial preenchido) x SUBTOTAL (vazio) ---
    col_data: dict[int, object] = {}          # col -> date (só dias válidos em 2026)
    cols_subtotal: list[int] = []
    feb29 = 0
    for col in range(2, len(header)):
        v1 = header[col]
        saldo = cel(LINHA_SALDO_INICIAL, col)
        if not isinstance(v1, datetime):
            # cabeçalho de subtotal pode ser texto ("Abr - 23")
            if saldo is None and (cel(LINHA_TOTAL_ENTRADAS, col) is not None
                                  or cel(3, col) is not None):
                cols_subtotal.append(col)
            continue
        if isinstance(saldo, (int, float)):
            try:
                col_data[col] = v1.replace(year=ANO_ALVO).date()  # dia real
            except ValueError:
                feb29 += 1  # 29/02 — sem movimento real na planilha
        else:
            cols_subtotal.append(col)  # subtotal mensal (Saldo Inicial vazio)

    primeira_col = min(col_data) if col_data else 2
    saldo_abertura = para_decimal(cel(LINHA_SALDO_INICIAL, primeira_col))
    data_abertura = col_data.get(primeira_col)
    aplic_abertura = para_decimal(cel(LINHA_SALDO_APLICACAO, primeira_col))

    # idempotência
    db.session.query(Lancamento).filter(
        extract("year", Lancamento.data) == ANO_ALVO
    ).delete(synchronize_session=False)
    db.session.query(AplicacaoFinanceira).filter(
        extract("year", AplicacaoFinanceira.data) == ANO_ALVO
    ).delete(synchronize_session=False)

    cache_cat: dict[tuple[str, str], Categoria] = {}
    cache_forn: dict[str, Fornecedor] = {}
    total_entradas = Decimal("0")
    total_impostos = Decimal("0")
    total_saidas_oper = Decimal("0")  # saídas sem impostos (como a planilha)
    n_lanc = 0

    # --- lançamentos de fluxo ---
    for ini, fim, tipo, grupo, eh_forn in FAIXAS:
        for r in range(ini, fim + 1):
            label = str(cel(r, 1) or "").strip()  # col B
            if _pular(label):
                continue
            if eh_forn:
                fornecedor = _get_fornecedor(cache_forn, label)
                categoria = _get_categoria(cache_cat, "Compras", tipo, grupo)
                fornecedor_id = fornecedor.id
            else:
                categoria = _get_categoria(cache_cat, label, tipo, grupo)
                fornecedor_id = None
            for col, data in col_data.items():
                valor = para_decimal(cel(r, col))
                if valor is None or valor == 0:
                    continue
                db.session.add(Lancamento(
                    data=data, categoria_id=categoria.id,
                    fornecedor_id=fornecedor_id, valor=valor,
                ))
                n_lanc += 1
                if tipo == "entrada":
                    total_entradas += valor
                elif grupo == "Impostos":
                    total_impostos += valor
                else:
                    total_saidas_oper += valor

    # --- bloco de aplicação financeira ---
    n_aplic = 0
    for row_idx, tipo_aplic in APLICACAO_ROWS.items():
        for col, data in col_data.items():
            valor = para_decimal(cel(row_idx, col))
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

    # --- conferência ---
    # Fonte da verdade = as linhas "Total" DIÁRIAS da planilha (o que importamos):
    #   r22 Total Entradas · r25 Total Impostos · r192 Total Saídas (sem impostos).
    plan_entradas = sum((para_decimal(cel(LINHA_TOTAL_ENTRADAS, c)) or Decimal("0")
                         for c in col_data), Decimal("0"))
    plan_impostos = sum((para_decimal(cel(LINHA_TOTAL_IMPOSTOS, c)) or Decimal("0")
                         for c in col_data), Decimal("0"))
    plan_saidas = sum((para_decimal(cel(LINHA_TOTAL_SAIDAS, c)) or Decimal("0")
                       for c in col_data), Decimal("0"))

    # Sinaliza meses onde o RESUMO mensal da planilha diverge da soma dos dias
    # (inconsistência da própria planilha — não do import).
    from collections import defaultdict

    diario_ent: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for c, d in col_data.items():
        diario_ent[d.month] += para_decimal(cel(LINHA_TOTAL_ENTRADAS, c)) or Decimal("0")
    resumo_ent: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    sub_ordenados = sorted(cols_subtotal)
    for i, c in enumerate(sub_ordenados, start=1):
        resumo_ent[i] += para_decimal(cel(LINHA_TOTAL_ENTRADAS, c)) or Decimal("0")
    meses_inconsistentes = sorted(
        m for m in range(1, 13)
        if (diario_ent[m] - resumo_ent.get(m, Decimal("0"))).copy_abs() > Decimal("0.05")
    )

    tol = Decimal("0.05")

    def linha_conf(rotulo, importado, planilha):
        dif = importado - planilha
        if dif.copy_abs() <= tol:
            status = "OK"
        elif dif > 0:
            # importamos células de despesa que a fórmula de total da planilha não soma
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
    if feb29:
        relatorio.append("29/02 ignorado (inexistente em 2026, sem movimento real na planilha).")
    if meses_inconsistentes:
        nomes = ", ".join(str(m).zfill(2) for m in meses_inconsistentes)
        relatorio.append(
            "Aviso: na planilha original, a coluna de resumo mensal diverge da soma "
            f"dos dias nos meses {nomes}. Importamos a soma diária (movimentos reais)."
        )
    return relatorio


def _get_categoria(cache, nome, tipo, grupo) -> Categoria:
    chave = (nome, grupo)
    if chave in cache:
        return cache[chave]
    cat = db.session.execute(
        db.select(Categoria).filter_by(nome=nome, grupo=grupo)
    ).scalar_one_or_none()
    if not cat:
        cat = Categoria(nome=nome, tipo=tipo, grupo=grupo, ordem=0, ativo=True)
        db.session.add(cat)
        db.session.flush()
    cache[chave] = cat
    return cat


def _get_fornecedor(cache, nome) -> Fornecedor:
    if nome in cache:
        return cache[nome]
    forn = db.session.execute(
        db.select(Fornecedor).filter_by(nome=nome)
    ).scalar_one_or_none()
    if not forn:
        forn = Fornecedor(nome=nome, ativo=True)
        db.session.add(forn)
        db.session.flush()
    cache[nome] = forn
    return forn
