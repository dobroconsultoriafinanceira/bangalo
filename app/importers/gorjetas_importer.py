# -*- coding: utf-8 -*-
"""Importa a planilha 'Calculadora de Gorjetas'.

Foco: a aba 'Histórico' vira snapshots imutáveis (`fechamento_gorjeta`),
uma quinzena fechada por referência. Setores/funções/pontos vêm do seed;
colaboradores ausentes são criados sob demanda.

Idempotente: uma referência de quinzena já fechada não é reimportada.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl

from app.extensions import db
from app.importers.comum import para_decimal
from app.models.gorjetas import (
    Colaborador,
    FechamentoGorjeta,
    Funcao,
    PeriodoGorjeta,
    Setor,
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _parse_referencia(texto: str) -> tuple[int, int, int] | None:
    """'1ª Quinzena Maio/26' -> (ordem=1, mes=5, ano=2026)."""
    m = re.search(r"([12])[ªa]?\s*Quinzena\s+([A-Za-zçãÇÃ]+)\s*/?\s*(\d{2,4})", texto, re.I)
    if not m:
        return None
    ordem = int(m.group(1))
    mes = MESES.get(m.group(2).strip().lower())
    if not mes:
        return None
    ano = int(m.group(3))
    if ano < 100:
        ano += 2000
    return ordem, mes, ano


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    if "Histórico" not in wb.sheetnames:
        wb.close()
        return ["Aba 'Histórico' não encontrada."]
    ws = wb["Histórico"]
    linhas = list(ws.iter_rows(values_only=True))
    wb.close()

    setores = {s.nome: s for s in db.session.execute(db.select(Setor)).scalars()}
    quinzenas_criadas = 0
    fechamentos = 0
    ignoradas = set()
    criadas_nesta_execucao: set[tuple[int, int, int]] = set()

    for row in linhas[3:]:  # dados a partir da linha 4
        if not row or not row[0] or not row[1]:
            continue
        ref = str(row[0]).strip()
        parsed = _parse_referencia(ref)
        if not parsed:
            continue
        ordem, mes, ano = parsed
        chave = (ano, mes, ordem)

        periodo = db.session.execute(
            db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes, ordem_quinzena=ordem)
        ).scalar_one_or_none()
        # pula só quinzenas que JÁ estavam fechadas antes desta execução
        # (as que criamos agora recebem várias linhas de colaborador)
        if periodo and periodo.fechado and chave not in criadas_nesta_execucao:
            ignoradas.add(ref)
            continue
        if not periodo:
            if ordem == 1:
                inicio, fim = date(ano, mes, 1), date(ano, mes, 15)
            else:
                import calendar
                inicio = date(ano, mes, 16)
                fim = date(ano, mes, calendar.monthrange(ano, mes)[1])
            periodo = PeriodoGorjeta(
                referencia=ref, ordem_quinzena=ordem, mes=mes, ano=ano,
                data_inicio=inicio, data_fim=fim, status="fechado",
            )
            db.session.add(periodo)
            db.session.flush()
            quinzenas_criadas += 1
            criadas_nesta_execucao.add(chave)

        nome = str(row[1]).strip()
        setor_nome = str(row[2]).strip() if len(row) > 2 and row[2] else "Salão"
        funcao_nome = str(row[3]).strip() if len(row) > 3 and row[3] else "Garçom"
        registro = str(row[4]).strip() if len(row) > 4 and row[4] else "CLT"
        liquido = para_decimal(row[5]) or Decimal("0")

        colaborador = _get_colaborador(nome, setor_nome, funcao_nome, setores)
        pontos = colaborador.pontos if colaborador else Decimal("2")

        db.session.add(FechamentoGorjeta(
            periodo_id=periodo.id,
            colaborador_id=colaborador.id if colaborador else None,
            nome=nome, setor=setor_nome, funcao=funcao_nome, registro=registro,
            pontos=pontos, dias_trabalhados=0, desconto=Decimal("0"),
            liquido_a_pagar=liquido,
        ))
        fechamentos += 1

    db.session.flush()
    relatorio = [
        f"Gorjetas: {quinzenas_criadas} quinzenas criadas, {fechamentos} fechamentos importados."
    ]
    if ignoradas:
        relatorio.append(f"Ignoradas (já fechadas): {', '.join(sorted(ignoradas))}.")
    return relatorio


def _get_colaborador(nome, setor_nome, funcao_nome, setores) -> Colaborador | None:
    colab = db.session.execute(
        db.select(Colaborador).filter(Colaborador.nome.ilike(nome))
    ).scalar_one_or_none()
    if colab:
        return colab

    setor = setores.get(setor_nome)
    if not setor:
        return None
    funcao = db.session.execute(
        db.select(Funcao).filter_by(nome=funcao_nome, setor_id=setor.id)
    ).scalar_one_or_none()
    if not funcao:
        funcao = db.session.execute(
            db.select(Funcao).filter_by(setor_id=setor.id)
        ).scalars().first()
    if not funcao:
        return None

    colab = Colaborador(
        nome=nome, funcao_id=funcao.id, setor_id=setor.id,
        pontos=funcao.pontos_padrao, registro="CLT", ativo=True,
    )
    db.session.add(colab)
    db.session.flush()
    return colab
