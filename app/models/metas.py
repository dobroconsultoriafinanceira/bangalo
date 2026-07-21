# -*- coding: utf-8 -*-
"""Metas/planejamento de faturamento.

Meta mensal e acumulados são DERIVADOS pela engine (services/metas.py) a
partir de premissa_meta + faturamento_historico — recalculados sempre que
as premissas mudam, nunca armazenados (evita valor velho no banco).

Realizado do mês (decisão do cliente, item 14.1): soma dos lançamentos do
Registro Diário (`faturamento_diario`); para meses sem registro diário
(jan–jun/2026 vieram só com o total mensal) vale o valor de
`faturamento_historico` do próprio ano.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class PremissaMeta(db.Model):
    """1 registro por ano. Pesos ajustáveis pela consultoria."""

    __tablename__ = "premissa_meta"
    __table_args__ = (
        CheckConstraint(
            "peso_mes_anterior + peso_media_historica = 1.0", name="ck_pesos_somam_1"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    peso_mes_anterior: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.60"), nullable=False)
    peso_media_historica: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.40"), nullable=False)
    crescimento_alvo: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.08"), nullable=False)
    # Pesos do rateio da meta mensal em metas diárias (segunda = fechado, peso 0)
    peso_ter_qua: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("1.0"), nullable=False)
    peso_qui_sex_dom: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("1.4"), nullable=False)
    peso_sabado: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("1.6"), nullable=False)


class FaturamentoHistorico(db.Model):
    """Faturamento bruto mensal por ano (matriz 2022–2026 da planilha)."""

    __tablename__ = "faturamento_historico"
    __table_args__ = (
        UniqueConstraint("ano", "mes", name="uq_faturamento_ano_mes"),
        CheckConstraint("mes BETWEEN 1 AND 12", name="ck_mes_valido"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class FaturamentoDiario(db.Model):
    """Registro Diário — fonte do Realizado. Restaurante fecha às segundas."""

    __tablename__ = "faturamento_diario"

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    aberto: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    faturamento: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
