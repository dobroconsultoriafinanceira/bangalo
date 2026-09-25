# -*- coding: utf-8 -*-
"""Regressão do importer de fluxo com planilha sintética (sem dados reais).

Cobre as regras críticas:
  - layout detectado por RÓTULOS (imune a inserção de linhas);
  - coluna de dia (Saldo Inicial preenchido) x subtotal mensal (vazio);
  - ano do cabeçalho ignorado e forçado para 2026;
  - 29/02 (inexistente em 2026) é pulado;
  - célula de TEXTO que parece número é ignorada (como no Excel).
"""
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from app.extensions import db
from app.importers import fluxo_importer
from app.models.fluxo import Lancamento
from app.seeds import seed_plano_contas, seed_setores_funcoes

# Esqueleto com todas as âncoras que o importador procura (col A / col B).
# (linha, colA, colB) — a linha 3 (Dinheiro) recebe os valores dos testes.
# Cartão não entra mais pela planilha (fonte é a API da Stone): ver o teste no fim.
ESQUELETO = [
    (2, "", "Saldo Inicial"),
    (3, "Entradas", "Dinheiro"),
    (4, "Total Entradas", ""),
    (5, "Saídas", "DAS"),
    (6, "Total Impostos", ""),
    (7, "Custo Variável 1", "Salários"),
    (8, "Subtotal Salários", ""),
    (9, "", "Pro Labore/Lucro"),
    (10, "Subtotal Despesas Gerais", ""),
    (11, "", "Ambev/CRBS"),
    (12, "SubTotal Compras", ""),
    (13, "Custo Variavel 2", "Aluguel"),
    (14, "", "Emprestimos Heitor"),
    (15, "Total Despesas", ""),
    (16, "Total Saidas", ""),
    (17, "", "Saldo Final"),
    (18, "", "SALDO APLICAÇÃO"),
    (19, "", "ENTRADA"),
    (20, "", "RENDIMENTO"),
    (21, "", "RESGATE"),
]
LINHA_VISA = 3


def _montar(tmp_path, colunas):
    """colunas: lista de (header_datetime, saldo_inicial|None, visa_valor|None)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026"
    for linha, a, b in ESQUELETO:
        if a:
            ws.cell(row=linha, column=1, value=a)
        if b:
            ws.cell(row=linha, column=2, value=b)
    for i, (hdr, saldo, visa) in enumerate(colunas):
        c = 3 + i  # col C em diante
        ws.cell(row=1, column=c, value=hdr)
        if saldo is not None:
            ws.cell(row=2, column=c, value=saldo)
        if visa is not None:
            ws.cell(row=LINHA_VISA, column=c, value=visa)
    caminho = tmp_path / "fluxo.xlsx"
    wb.save(caminho)
    return caminho


@pytest.fixture()
def _seed(app):
    seed_setores_funcoes()
    seed_plano_contas()
    db.session.commit()


def test_importer_ignora_subtotal_e_forca_ano(_seed, tmp_path):
    caminho = _montar(tmp_path, [
        (datetime(2023, 1, 1), 1000, 100),   # dia
        (datetime(2024, 1, 2), 1200, 250),   # dia
        (datetime(2024, 1, 23), None, 350),  # SUBTOTAL mensal (saldo vazio) -> ignora
    ])
    fluxo_importer.importar(caminho)
    db.session.commit()

    lancs = db.session.query(Lancamento).all()
    assert len(lancs) == 2  # a coluna de subtotal não entra
    assert sum((l.valor for l in lancs), Decimal("0")) == Decimal("350.00")
    assert {l.data.isoformat() for l in lancs} == {"2026-01-01", "2026-01-02"}


def test_importer_conta_celula_texto_numerica_e_avisa(_seed, tmp_path):
    caminho = _montar(tmp_path, [
        (datetime(2024, 6, 1), 1000, 100),          # número -> entra
        (datetime(2024, 6, 2), 1000, "284.87"),     # TEXTO numérico -> entra e é listado
        (datetime(2024, 6, 3), 1000, "1.234,56"),   # formato brasileiro
        (datetime(2024, 6, 4), 1000, "ver nota"),   # texto de verdade -> ignorado
    ])
    relatorio = fluxo_importer.importar(caminho)
    db.session.commit()

    lancs = db.session.query(Lancamento).order_by(Lancamento.data).all()
    assert [l.valor for l in lancs] == [Decimal("100.00"), Decimal("284.87"), Decimal("1234.56")]
    aviso = next(linha for linha in relatorio if "TEXTO" in linha)
    assert '"284.87"' in aviso and '"1.234,56"' in aviso


def test_importer_pula_29_02(_seed, tmp_path):
    caminho = _montar(tmp_path, [
        (datetime(2024, 2, 29), 500, 99),  # 29/02 não existe em 2026
    ])
    relatorio = fluxo_importer.importar(caminho)
    db.session.commit()

    assert db.session.query(Lancamento).count() == 0
    assert any("29/02" in linha for linha in relatorio)


def test_importer_layout_resiliente_a_insercao(_seed, tmp_path):
    """Inserir uma linha nova no meio não deve quebrar o mapeamento."""
    caminho = _montar(tmp_path, [(datetime(2024, 1, 1), 1000, 100)])
    # insere uma conta nova de despesa fixa depois de 'Aluguel' (linha 13)
    wb = openpyxl.load_workbook(caminho)
    ws = wb["2026"]
    ws.insert_rows(14)
    ws.cell(row=14, column=2, value="Nova Conta Fixa")
    wb.save(caminho)

    fluxo_importer.importar(caminho)
    db.session.commit()
    # a linha de entrada continua sendo importada mesmo com a inserção
    lancs = db.session.query(Lancamento).all()
    assert len(lancs) == 1
    assert lancs[0].valor == Decimal("100.00")


def test_planilha_nao_lanca_mais_cartao(tmp_path, _seed):
    """Desde 23/09/2026 as linhas de cartão vêm da API da Stone.

    A planilha continua trazendo a linha (o cliente ainda digita), mas o
    importador a ignora — senão o mesmo dinheiro entraria duas vezes.
    """
    import openpyxl

    from app.models.fluxo import Categoria

    caminho = _montar(tmp_path, [(datetime(2026, 3, 2), 1000.0, 50.0)])
    wb = openpyxl.load_workbook(caminho)
    ws = wb["2026"]
    ws.cell(row=LINHA_VISA, column=2, value="Visa Crédito")   # a linha 3 vira cartão
    ws.cell(row=LINHA_VISA, column=3, value=4007.08)
    wb.save(caminho)

    fluxo_importer.importar(caminho)
    db.session.commit()

    visa = db.session.execute(
        db.select(Categoria).filter_by(nome="Visa Crédito", tipo="entrada")
    ).scalar_one_or_none()
    lancados = db.session.execute(
        db.select(Lancamento).filter(Lancamento.categoria_id == (visa.id if visa else 0))
    ).scalars().all()
    assert lancados == []
