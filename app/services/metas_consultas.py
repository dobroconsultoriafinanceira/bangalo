# -*- coding: utf-8 -*-
"""Cola entre o banco e a engine pura de metas (services/metas.py)."""
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.metas import FaturamentoDiario, FaturamentoHistorico, PremissaMeta
from app.services import metas as engine


def premissas_do_ano(ano: int) -> engine.Premissas:
    reg = db.session.execute(
        db.select(PremissaMeta).filter_by(ano=ano)
    ).scalar_one_or_none()
    if not reg:
        return engine.Premissas()
    return engine.Premissas(
        peso_mes_anterior=Decimal(reg.peso_mes_anterior),
        peso_media_historica=Decimal(reg.peso_media_historica),
        crescimento_alvo=Decimal(reg.crescimento_alvo),
        peso_ter_qua=Decimal(reg.peso_ter_qua),
        peso_qui_sex_dom=Decimal(reg.peso_qui_sex_dom),
        peso_sabado=Decimal(reg.peso_sabado),
    )


def excecoes_do_mes(registros) -> tuple[set, set]:
    """Exceções do calendário padrão a partir do registro diário:
    (dias fechados fora de segunda, segundas abertas)."""
    fechados = {d for d, r in registros.items() if not r.aberto and d.weekday() != 0}
    abertos = {d for d, r in registros.items() if r.aberto and d.weekday() == 0}
    return fechados, abertos


def carregar_historico() -> dict[int, dict[int, Decimal]]:
    """{ano: {mes: valor}} com toda a matriz de faturamento histórico."""
    historico: dict[int, dict[int, Decimal]] = {}
    for reg in db.session.execute(db.select(FaturamentoHistorico)).scalars():
        historico.setdefault(reg.ano, {})[reg.mes] = Decimal(reg.valor)
    return historico


def realizados_do_ano(ano: int) -> dict[int, Decimal]:
    """Realizado por mês (decisão 14.1): soma do Registro Diário; meses sem
    registro diário usam o total mensal de faturamento_historico do ano."""
    linhas = (
        db.session.query(
            func.extract("month", FaturamentoDiario.data).label("mes"),
            func.sum(FaturamentoDiario.faturamento),
        )
        .filter(
            func.extract("year", FaturamentoDiario.data) == ano,
            FaturamentoDiario.faturamento.isnot(None),
        )
        .group_by("mes")
        .all()
    )
    realizados = {int(mes): Decimal(total) for mes, total in linhas if total is not None}

    historico_ano = (
        db.session.execute(db.select(FaturamentoHistorico).filter_by(ano=ano)).scalars().all()
    )
    for reg in historico_ano:
        realizados.setdefault(reg.mes, Decimal(reg.valor))
    return realizados


def tabela_do_ano(ano: int) -> list[engine.LinhaMeta]:
    return engine.tabela_anual(
        ano=ano,
        historico=carregar_historico(),
        realizados=realizados_do_ano(ano),
        p=premissas_do_ano(ano),
    )


def meta_do_mes(ano: int, mes: int) -> Decimal | None:
    linha = tabela_do_ano(ano)[mes - 1]
    return linha.meta
