# -*- coding: utf-8 -*-
"""Cola entre o banco e a engine pura de gorjetas."""
from decimal import Decimal

from app.extensions import db
from app.models.gorjetas import (
    Colaborador,
    DescontoQuinzena,
    FechamentoGorjeta,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Setor,
)
from app.services import gorjetas as engine


def percentuais_setores() -> dict[str, Decimal]:
    setores = db.session.execute(db.select(Setor).filter_by(ativo=True)).scalars().all()
    return {s.nome: Decimal(s.percentual_rateio) for s in setores}


def montar_entrada(periodo: PeriodoGorjeta) -> tuple[list[engine.ColaboradorRateio], dict]:
    """Monta os insumos da engine a partir do período (presenças + participações)."""
    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    presencas_por_colab: dict[int, set] = {}
    for pres in periodo.presencas:
        if pres.presente:
            presencas_por_colab.setdefault(pres.colaborador_id, set()).add(pres.data)

    # participantes: quem tem participação registrada OU presença marcada;
    # se nada existe ainda, todos os colaboradores ativos entram zerados
    ids = set(participacoes) | set(presencas_por_colab)
    if ids:
        colaboradores_db = db.session.execute(
            db.select(Colaborador).filter(Colaborador.id.in_(ids))
        ).scalars().all()
    else:
        colaboradores_db = db.session.execute(
            db.select(Colaborador).filter_by(ativo=True).order_by(Colaborador.nome)
        ).scalars().all()

    entrada = []
    for c in colaboradores_db:
        part = participacoes.get(c.id)
        entrada.append(engine.ColaboradorRateio(
            id=c.id,
            nome=c.nome,
            setor=c.setor.nome,
            funcao=c.funcao.nome,
            registro=c.registro,
            pontos=Decimal(c.pontos),
            presencas=presencas_por_colab.get(c.id, set()),
            dias_trabalhados_manual=part.dias_trabalhados_manual if part else None,
            desconto=Decimal(part.desconto) if part else Decimal("0"),
        ))

    comissao_por_dia = {cd.data: Decimal(cd.valor) for cd in periodo.comissoes_diarias}
    return entrada, comissao_por_dia


def descontos_setor_dict(periodo: PeriodoGorjeta) -> dict[str, Decimal]:
    """Soma dos descontos por setor para este período: {nome_setor: total}."""
    total: dict[str, Decimal] = {}
    for d in periodo.descontos_setor:
        nome = d.setor.nome
        total[nome] = total.get(nome, Decimal("0")) + Decimal(d.valor)
    return total


def calcular(periodo: PeriodoGorjeta) -> engine.ResultadoRateio:
    colaboradores, comissao_por_dia = montar_entrada(periodo)
    descontos_s = descontos_setor_dict(periodo)
    return engine.calcular_rateio(
        comissao_bruta=Decimal(periodo.comissao_bruta),
        percentual_encargos=Decimal(periodo.percentual_encargos),
        percentuais_setor=percentuais_setores(),
        colaboradores=colaboradores,
        comissao_por_dia=comissao_por_dia or None,
        descontos_setor=descontos_s or None,
    )


def fechar(periodo: PeriodoGorjeta) -> engine.ResultadoRateio:
    """Fecha a quinzena: grava snapshot imutável e muda o status.

    Depois de fechado NÃO recalcula (histórico auditável). O commit fica
    com o chamador.
    """
    resultado = calcular(periodo)
    for r in resultado.colaboradores:
        db.session.add(FechamentoGorjeta(
            periodo_id=periodo.id,
            colaborador_id=r.id,
            nome=r.nome,
            setor=r.setor,
            funcao=r.funcao,
            registro=r.registro,
            pontos=r.pontos,
            dias_trabalhados=r.dias_trabalhados,
            desconto=r.desconto,
            liquido_a_pagar=r.liquido,
        ))
    periodo.status = "fechado"
    return resultado


def garantir_participacoes(periodo: PeriodoGorjeta) -> None:
    """Pré-popula com colaboradores da última quinzena fechada (ou todos ativos se não houver)."""
    existentes = {p.colaborador_id for p in periodo.participacoes}

    ultima_fechada = db.session.execute(
        db.select(PeriodoGorjeta)
        .filter(PeriodoGorjeta.status == "fechado", PeriodoGorjeta.id != periodo.id)
        .order_by(PeriodoGorjeta.data_fim.desc())
    ).scalars().first()

    if ultima_fechada:
        ids_base = {p.colaborador_id for p in ultima_fechada.participacoes}
        base = db.session.execute(
            db.select(Colaborador).filter(
                Colaborador.id.in_(ids_base),
                Colaborador.ativo == True,
            )
        ).scalars().all()
    else:
        base = db.session.execute(
            db.select(Colaborador).filter_by(ativo=True)
        ).scalars().all()

    for c in base:
        if c.id not in existentes:
            db.session.add(ParticipacaoPeriodo(periodo_id=periodo.id, colaborador_id=c.id))
