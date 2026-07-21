# -*- coding: utf-8 -*-
"""Regressão do importer de fluxo com planilha sintética (sem dados reais).

Cobre as regras críticas descobertas na planilha real:
  - coluna de DIA = "Saldo Inicial" preenchido; coluna de SUBTOTAL = vazio
    (o subtotal NÃO pode ser importado, senão conta o mês duas vezes);
  - ano do cabeçalho é ignorado e forçado para 2026;
  - 29/02 (inexistente em 2026) é pulado.
"""
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest

from app.extensions import db
from app.importers import fluxo_importer
from app.models.fluxo import Lancamento
from app.seeds import seed_plano_contas, seed_setores_funcoes


@pytest.fixture()
def planilha_sintetica(tmp_path):
    """Mini aba '2026': 2 dias de janeiro + 1 coluna de subtotal do mês."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026"

    # Cabeçalho (linha 1): ano ERRADO de propósito (2023/2024)
    ws.cell(row=1, column=3, value=datetime(2023, 1, 1))   # dia (col C)
    ws.cell(row=1, column=4, value=datetime(2024, 1, 2))   # dia (col D)
    ws.cell(row=1, column=5, value=datetime(2024, 1, 23))  # SUBTOTAL do mês (col E)

    # Linha 2 = Saldo Inicial: preenchido nos dias, VAZIO no subtotal
    ws.cell(row=2, column=2, value="Saldo Inicial")
    ws.cell(row=2, column=3, value=1000)
    ws.cell(row=2, column=4, value=1200)
    # col 5 (subtotal) fica vazio -> marca de coluna de subtotal

    # Linha 4 = "Visa Crédito" (entrada / Operacional). Faixa entrada = 3..14
    ws.cell(row=4, column=2, value="Visa Crédito")
    ws.cell(row=4, column=3, value=100)     # dia 1
    ws.cell(row=4, column=4, value=250)     # dia 2
    ws.cell(row=4, column=5, value=350)     # SUBTOTAL (100+250) — NÃO deve entrar

    wb.save(tmp_path / "fluxo.xlsx")
    return tmp_path / "fluxo.xlsx"


def test_importer_ignora_subtotal_e_forca_ano(app, planilha_sintetica):
    seed_setores_funcoes()
    seed_plano_contas()
    db.session.commit()

    fluxo_importer.importar(planilha_sintetica)
    db.session.commit()

    lancs = db.session.query(Lancamento).all()
    # só os 2 dias entram; a coluna de subtotal é ignorada (senão seriam 3)
    assert len(lancs) == 2
    assert sum((l.valor for l in lancs), Decimal("0")) == Decimal("350.00")
    # ano forçado para 2026, mês/dia preservados
    assert {l.data.isoformat() for l in lancs} == {"2026-01-01", "2026-01-02"}


def test_importer_ignora_celula_texto(app, tmp_path):
    """Célula digitada como TEXTO ("284.87") não deve ser importada.

    A planilha (como o Excel) ignora texto nas somas; importá-lo divergiria
    o saldo. Caso real: Umehara/24-06 na planilha do Bangalô.
    """
    seed_setores_funcoes()
    seed_plano_contas()
    db.session.commit()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026"
    ws.cell(row=1, column=3, value=datetime(2024, 6, 1))
    ws.cell(row=1, column=4, value=datetime(2024, 6, 2))
    ws.cell(row=2, column=2, value="Saldo Inicial")
    ws.cell(row=2, column=3, value=1000)
    ws.cell(row=2, column=4, value=1000)
    ws.cell(row=4, column=2, value="Visa Crédito")
    ws.cell(row=4, column=3, value=100)      # número -> entra
    ws.cell(row=4, column=4, value="284.87")  # TEXTO -> deve ser ignorado
    caminho = tmp_path / "texto.xlsx"
    wb.save(caminho)

    fluxo_importer.importar(caminho)
    db.session.commit()

    lancs = db.session.query(Lancamento).all()
    assert len(lancs) == 1
    assert lancs[0].valor == Decimal("100.00")


def test_importer_pula_29_02(app, tmp_path):
    seed_setores_funcoes()
    seed_plano_contas()
    db.session.commit()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026"
    ws.cell(row=1, column=3, value=datetime(2024, 2, 29))  # 29/02 (fonte bissexta)
    ws.cell(row=2, column=2, value="Saldo Inicial")
    ws.cell(row=2, column=3, value=500)
    ws.cell(row=4, column=2, value="Visa Crédito")
    ws.cell(row=4, column=3, value=99)
    caminho = tmp_path / "f2.xlsx"
    wb.save(caminho)

    relatorio = fluxo_importer.importar(caminho)
    db.session.commit()

    # 29/02 não existe em 2026 -> nenhum lançamento, e o aviso aparece
    assert db.session.query(Lancamento).count() == 0
    assert any("29/02" in linha for linha in relatorio)
