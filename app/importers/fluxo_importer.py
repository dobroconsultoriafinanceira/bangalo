# -*- coding: utf-8 -*-
"""Importa a aba '2026' da planilha 'FLUXO DE CAIXA BANGALO'.

A planilha é uma matriz conta(linha) × dia(coluna). Cada célula preenchida
vira um `lancamento`. Linhas de subtotal (calculadas) são puladas; os
cabeçalhos de data (linha 1) trazem o ano visualmente errado — usamos
mês/dia e forçamos o ano-alvo (2026). Blocos:

  linhas   3–14  ENTRADAS  · Operacional
  linha    17    ENTRADAS  · Patrocínio
  linhas   19–20 ENTRADAS  · Financeiras
  linhas   23–24 SAÍDAS    · Impostos
  linhas   27–44 SAÍDAS    · Folha/Salários
  linhas   46–49 SAÍDAS    · Despesas Gerais
  linhas   51–144 SAÍDAS   · Compras (CPV) [cada linha = fornecedor]
  linhas  147–182 SAÍDAS   · Despesas Fixas
  linhas  184–190 SAÍDAS   · Outros/Financeiro

Idempotente: apaga os lançamentos do ano-alvo antes de reimportar.
Emite relatório de conferência (total importado vs total esperado).
"""
from decimal import Decimal
from pathlib import Path

import openpyxl
from sqlalchemy import extract

from app.extensions import db
from app.importers.comum import para_data, para_decimal
from app.models.fluxo import Categoria, ConfigSistema, Fornecedor, Lancamento
from app.services.fluxo_caixa import CHAVE_DATA_ABERTURA, CHAVE_SALDO_ABERTURA

ANO_ALVO = 2026
ABA = "2026"

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
    return not low or any(low.startswith(k) or k in low for k in SKIP_LABELS)


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    if ABA not in wb.sheetnames:
        wb.close()
        return [f"Aba '{ABA}' não encontrada."]
    ws = wb[ABA]
    linhas = list(ws.iter_rows(values_only=True))
    wb.close()

    # mapa coluna -> data (linha 1, a partir da col C / índice 2)
    # O cabeçalho traz o ano visualmente errado (ex.: 2024): usamos mês/dia e
    # forçamos o ano-alvo. Datas inexistentes em 2026 (29/02) são puladas.
    header = linhas[0]
    col_data: dict[int, object] = {}
    colunas_puladas = 0
    for col in range(2, len(header)):
        d = para_data(header[col])
        if not d:
            continue
        try:
            col_data[col] = d.replace(year=ANO_ALVO)
        except ValueError:
            colunas_puladas += 1

    # saldo de abertura: linha 2 (Saldo Inicial), primeira coluna de data
    primeira_col = min(col_data) if col_data else 2
    saldo_abertura = para_decimal(linhas[1][primeira_col]) if len(linhas) > 1 else None
    data_abertura = col_data.get(primeira_col)

    # idempotência: remove lançamentos do ano-alvo antes de recriar
    db.session.query(Lancamento).filter(extract("year", Lancamento.data) == ANO_ALVO).delete(
        synchronize_session=False
    )

    cache_cat: dict[tuple[str, str], Categoria] = {}
    cache_forn: dict[str, Fornecedor] = {}
    total_entradas = Decimal("0")
    total_saidas = Decimal("0")
    n_lanc = 0

    for ini, fim, tipo, grupo, eh_forn in FAIXAS:
        for r in range(ini, fim + 1):
            if r - 1 >= len(linhas):
                continue
            row = linhas[r - 1]
            label = str(row[1] or "").strip()  # col B
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
                if col >= len(row):
                    continue
                valor = para_decimal(row[col])
                if valor is None or valor == 0:
                    continue
                db.session.add(Lancamento(
                    data=data, categoria_id=categoria.id, fornecedor_id=fornecedor_id,
                    valor=valor, descricao=None,
                ))
                n_lanc += 1
                if tipo == "entrada":
                    total_entradas += valor
                else:
                    total_saidas += valor

    if saldo_abertura is not None:
        ConfigSistema.definir(CHAVE_SALDO_ABERTURA, str(saldo_abertura))
    if data_abertura is not None:
        ConfigSistema.definir(CHAVE_DATA_ABERTURA, data_abertura.isoformat())

    db.session.flush()
    relatorio = [
        f"Fluxo {ANO_ALVO}: {n_lanc} lançamentos importados em {len(col_data)} dias.",
        f"Total de entradas: R$ {total_entradas} · total de saídas: R$ {total_saidas}.",
        f"Saldo de abertura: R$ {saldo_abertura or 0} em {data_abertura or '—'}.",
        "Confira estes totais contra os subtotais da planilha (tolerância de centavos).",
    ]
    if colunas_puladas:
        relatorio.append(
            f"Atenção: {colunas_puladas} coluna(s) de data inexistente(s) em {ANO_ALVO} "
            "(ex.: 29/02) foram puladas."
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
