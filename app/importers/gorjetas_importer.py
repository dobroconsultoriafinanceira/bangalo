# -*- coding: utf-8 -*-
"""Importa a planilha 'Calculadora de Gorjetas'.

Fontes:
  - Abas individuais ("1ª Q Maio-26", "2ª Q Julho-26", …) — PRIMÁRIA.
    Quinzenas com comissão > 0 → status fechado + FechamentoGorjeta.
    Quinzenas com comissão = 0 → status aberto + ParticipacaoPeriodo.
  - Aba "Histórico" — FALLBACK para quinzenas não presentes nas abas individuais.

Idempotente: quinzenas já existentes no BD são puladas.
"""
import calendar
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
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Setor,
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

MESES_NOME = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def _parse_referencia(texto: str) -> tuple[int, int, int] | None:
    """'1ª Quinzena Maio/26' → (ordem=1, mes=5, ano=2026)."""
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


def _parse_tab_name(nome: str) -> tuple[int, int, int] | None:
    """'2ª Q Julho-26' → (ordem=2, mes=7, ano=2026). Retorna None se não bater."""
    m = re.match(r"^([12])[ªa]?\s*Q\s+([A-Za-zçãõÃÕÇ]+)-(\d{2,4})\s*$", nome.strip(), re.I)
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


def _make_referencia(ordem: int, mes: int, ano: int) -> str:
    return f"{ordem}ª Quinzena {MESES_NOME[mes]}/{str(ano)[-2:]}"


def _datas_quinzena(ordem: int, mes: int, ano: int) -> tuple[date, date]:
    if ordem == 1:
        return date(ano, mes, 1), date(ano, mes, 15)
    return date(ano, mes, 16), date(ano, mes, calendar.monthrange(ano, mes)[1])


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    setores = {s.nome: s for s in db.session.execute(db.select(Setor)).scalars()}
    rel: list[str] = []

    # ── Passo 1: abas individuais (fonte primária) ─────────────────────────────
    qz_criadas = 0
    fechamentos_count = 0
    participacoes_count = 0
    puladas: list[str] = []

    for nome_tab in wb.sheetnames:
        if nome_tab.strip() == "Histórico":
            continue
        if "Planejado" in nome_tab or "Meta" in nome_tab:
            continue
        parsed = _parse_tab_name(nome_tab)
        if not parsed:
            continue

        ordem, mes, ano = parsed

        # Idempotente: pula se já existe
        if db.session.execute(
            db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes, ordem_quinzena=ordem)
        ).scalar_one_or_none():
            puladas.append(nome_tab)
            continue

        ws = wb[nome_tab]
        rows = list(ws.iter_rows(values_only=True))

        # Comissão bruta: linha 5 (índice 4), coluna B (índice 1)
        comissao_bruta = Decimal("0")
        if len(rows) > 4 and rows[4][1] is not None:
            v = para_decimal(rows[4][1])
            if v is not None:
                comissao_bruta = v

        referencia = _make_referencia(ordem, mes, ano)
        inicio, fim = _datas_quinzena(ordem, mes, ano)
        is_fechada = comissao_bruta > Decimal("0")

        periodo = PeriodoGorjeta(
            referencia=referencia,
            ordem_quinzena=ordem, mes=mes, ano=ano,
            data_inicio=inicio, data_fim=fim,
            comissao_bruta=comissao_bruta,
            status="fechado" if is_fechada else "aberto",
        )
        db.session.add(periodo)
        db.session.flush()
        qz_criadas += 1

        # Colaboradores: linha 18 em diante (índice 17+); cabeçalho na linha 17
        for row in rows[17:]:
            if not row or row[0] is None:
                break
            nome_colab = str(row[0]).strip()
            if not nome_colab or nome_colab.startswith("(") or nome_colab == "Nome":
                break

            setor_nome = str(row[1]).strip() if row[1] else "Salão"
            funcao_nome = str(row[2]).strip() if row[2] else "Garçom"
            pontos = para_decimal(row[3]) or Decimal("2")

            dias = 0
            if row[4] is not None and str(row[4]) != "#DIV/0!":
                try:
                    dias = int(row[4])
                except (ValueError, TypeError):
                    dias = 0

            desconto = para_decimal(row[5]) or Decimal("0")
            liquido = para_decimal(row[6]) or Decimal("0")
            registro = str(row[7]).strip() if row[7] else "CLT"

            colab = _get_colaborador(nome_colab, setor_nome, funcao_nome, setores)

            if is_fechada:
                db.session.add(FechamentoGorjeta(
                    periodo_id=periodo.id,
                    colaborador_id=colab.id if colab else None,
                    nome=nome_colab, setor=setor_nome, funcao=funcao_nome,
                    registro=registro, pontos=pontos, dias_trabalhados=dias,
                    desconto=desconto, liquido_a_pagar=liquido,
                ))
                fechamentos_count += 1
            else:
                if colab:
                    db.session.add(ParticipacaoPeriodo(
                        periodo_id=periodo.id, colaborador_id=colab.id,
                    ))
                    participacoes_count += 1

    db.session.flush()

    # ── Passo 2: aba Histórico (fallback / backward compat) ───────────────────
    historico_criadas = 0
    historico_fechamentos = 0
    historico_ignoradas: set[str] = set()
    criadas_nesta_execucao: set[tuple[int, int, int]] = set()

    if "Histórico" in wb.sheetnames:
        ws_hist = wb["Histórico"]
        linhas_hist = list(ws_hist.iter_rows(values_only=True))

        for row in linhas_hist[3:]:
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
            if periodo and periodo.fechado and chave not in criadas_nesta_execucao:
                historico_ignoradas.add(ref)
                continue
            if not periodo:
                inicio, fim = _datas_quinzena(ordem, mes, ano)
                periodo = PeriodoGorjeta(
                    referencia=ref, ordem_quinzena=ordem, mes=mes, ano=ano,
                    data_inicio=inicio, data_fim=fim, status="fechado",
                )
                db.session.add(periodo)
                db.session.flush()
                historico_criadas += 1
                criadas_nesta_execucao.add(chave)

            nome = str(row[1]).strip()
            setor_nome = str(row[2]).strip() if len(row) > 2 and row[2] else "Salão"
            funcao_nome = str(row[3]).strip() if len(row) > 3 and row[3] else "Garçom"
            registro = str(row[4]).strip() if len(row) > 4 and row[4] else "CLT"
            liquido = para_decimal(row[5]) or Decimal("0")

            # Não duplicar FechamentoGorjeta que a aba individual já criou
            existente = db.session.execute(
                db.select(FechamentoGorjeta).filter_by(periodo_id=periodo.id, nome=nome)
            ).scalar_one_or_none()
            if existente:
                continue

            colab = _get_colaborador(nome, setor_nome, funcao_nome, setores)
            pontos = colab.pontos if colab else Decimal("2")

            db.session.add(FechamentoGorjeta(
                periodo_id=periodo.id,
                colaborador_id=colab.id if colab else None,
                nome=nome, setor=setor_nome, funcao=funcao_nome, registro=registro,
                pontos=pontos, dias_trabalhados=0, desconto=Decimal("0"),
                liquido_a_pagar=liquido,
            ))
            historico_fechamentos += 1

    wb.close()
    db.session.flush()

    # ── Relatório ──────────────────────────────────────────────────────────────
    rel.append(f"Abas individuais: {qz_criadas} quinzenas criadas.")
    if fechamentos_count:
        rel.append(f"  Fechadas: {fechamentos_count} FechamentosGorjeta importados.")
    if participacoes_count:
        rel.append(f"  Abertas: {participacoes_count} ParticipacoesPeriodo criadas.")
    if puladas:
        rel.append(f"  Puladas (já existiam): {', '.join(puladas)}.")
    if historico_criadas or historico_fechamentos:
        rel.append(
            f"Histórico: {historico_criadas} quinzenas + {historico_fechamentos} fechamentos (fallback)."
        )
    if historico_ignoradas:
        rel.append(f"Histórico ignoradas (já fechadas pelas abas): {len(historico_ignoradas)}.")
    return rel


def _get_colaborador(nome: str, setor_nome: str, funcao_nome: str, setores: dict) -> Colaborador | None:
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
