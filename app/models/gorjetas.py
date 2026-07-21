# -*- coding: utf-8 -*-
"""Gorjetas/comissões: quinzenas, presença diária e fechamento imutável.

A engine de rateio (services/gorjetas.py) é DIÁRIA — validada centavo a
centavo contra a aba "1ª Q Maio-26" da planilha real. O modo simplificado
(pontos × dias no período) existe como fallback quando a quinzena não tem
comissão diária/grade de presença.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

REGISTROS_COLABORADOR = (
    "CLT", "Por fora", "CLT (rescisão)", "CLT (férias)", "Experiência",
)
MOTIVOS_DESCONTO = ("Perda", "Avaria", "Vale", "Descontos")
STATUS_PERIODO = ("aberto", "fechado")


class Setor(db.Model):
    __tablename__ = "setor"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    # Cozinha 0.25 · Salão 0.73 · Caixa 0.02 — a soma dos setores ativos deve dar 1
    percentual_rateio: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    funcoes = relationship("Funcao", back_populates="setor")
    colaboradores = relationship("Colaborador", back_populates="setor")


class Funcao(db.Model):
    __tablename__ = "funcao"
    __table_args__ = (UniqueConstraint("nome", "setor_id", name="uq_funcao_nome_setor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(60), nullable=False)
    setor_id: Mapped[int] = mapped_column(ForeignKey("setor.id"), nullable=False)
    pontos_padrao: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)

    setor = relationship("Setor", back_populates="funcoes")
    colaboradores = relationship("Colaborador", back_populates="funcao")


class Colaborador(db.Model):
    __tablename__ = "colaborador"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    funcao_id: Mapped[int] = mapped_column(ForeignKey("funcao.id"), nullable=False)
    setor_id: Mapped[int] = mapped_column(ForeignKey("setor.id"), nullable=False)
    # default = pontos da função; editável por colaborador
    pontos: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    registro: Mapped[str] = mapped_column(
        Enum(*REGISTROS_COLABORADOR, name="registro_colaborador", native_enum=False),
        nullable=False, default="CLT",
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    observacao: Mapped[str | None] = mapped_column(Text)

    funcao = relationship("Funcao", back_populates="colaboradores")
    setor = relationship("Setor", back_populates="colaboradores")


class PeriodoGorjeta(db.Model):
    """Quinzena. `comissao_bruta` é input manual (12% do faturamento/couvert do PDV)."""

    __tablename__ = "periodo_gorjeta"
    __table_args__ = (
        UniqueConstraint("ano", "mes", "ordem_quinzena", name="uq_periodo_quinzena"),
        CheckConstraint("ordem_quinzena IN (1, 2)", name="ck_ordem_quinzena"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referencia: Mapped[str] = mapped_column(String(60), nullable=False)  # "1ª Quinzena Julho/26"
    ordem_quinzena: Mapped[int] = mapped_column(Integer, nullable=False)
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    data_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    data_fim: Mapped[date] = mapped_column(Date, nullable=False)
    comissao_bruta: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    percentual_encargos: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0.20"), nullable=False)
    meta_mes: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    meta_quinzena: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimativa_couvert_mes: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimativa_couvert_quinzena: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(
        Enum(*STATUS_PERIODO, name="status_periodo", native_enum=False),
        nullable=False, default="aberto",
    )

    participacoes = relationship("ParticipacaoPeriodo", back_populates="periodo", cascade="all, delete-orphan")
    presencas = relationship("Presenca", back_populates="periodo", cascade="all, delete-orphan")
    comissoes_diarias = relationship("ComissaoDiaria", back_populates="periodo", cascade="all, delete-orphan")
    fechamentos = relationship("FechamentoGorjeta", back_populates="periodo")

    @property
    def fechado(self) -> bool:
        return self.status == "fechado"


class ParticipacaoPeriodo(db.Model):
    """Colaborador dentro de uma quinzena: descontos e dias manuais.

    `dias_trabalhados_manual` permite o modo sem grade de presença
    (rateio simplificado). Com grade, os dias vêm de `presenca`.
    """

    __tablename__ = "participacao_periodo"
    __table_args__ = (
        UniqueConstraint("periodo_id", "colaborador_id", name="uq_participacao"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    periodo_id: Mapped[int] = mapped_column(ForeignKey("periodo_gorjeta.id"), nullable=False)
    colaborador_id: Mapped[int] = mapped_column(ForeignKey("colaborador.id"), nullable=False)
    dias_trabalhados_manual: Mapped[int | None] = mapped_column(Integer)
    desconto: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    desconto_motivo: Mapped[str | None] = mapped_column(
        Enum(*MOTIVOS_DESCONTO, name="motivo_desconto", native_enum=False)
    )
    desconto_observacao: Mapped[str | None] = mapped_column(String(255))

    periodo = relationship("PeriodoGorjeta", back_populates="participacoes")
    colaborador = relationship("Colaborador")


class Presenca(db.Model):
    """Grade colaborador × dia (1 = trabalhou)."""

    __tablename__ = "presenca"
    __table_args__ = (
        UniqueConstraint("periodo_id", "colaborador_id", "data", name="uq_presenca"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    periodo_id: Mapped[int] = mapped_column(ForeignKey("periodo_gorjeta.id"), nullable=False, index=True)
    colaborador_id: Mapped[int] = mapped_column(ForeignKey("colaborador.id"), nullable=False)
    data: Mapped[date] = mapped_column(Date, nullable=False)
    presente: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    periodo = relationship("PeriodoGorjeta", back_populates="presencas")
    colaborador = relationship("Colaborador")


class ComissaoDiaria(db.Model):
    """Comissão por dia vinda do PDV — insumo da engine diária."""

    __tablename__ = "comissao_diaria"
    __table_args__ = (UniqueConstraint("periodo_id", "data", name="uq_comissao_dia"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    periodo_id: Mapped[int] = mapped_column(ForeignKey("periodo_gorjeta.id"), nullable=False, index=True)
    data: Mapped[date] = mapped_column(Date, nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)

    periodo = relationship("PeriodoGorjeta", back_populates="comissoes_diarias")


class FechamentoGorjeta(db.Model):
    """Snapshot IMUTÁVEL gravado ao fechar a quinzena (aba Histórico).

    Campos denormalizados de propósito: o histórico não pode mudar se o
    colaborador trocar de função/setor depois.
    """

    __tablename__ = "fechamento_gorjeta"

    id: Mapped[int] = mapped_column(primary_key=True)
    periodo_id: Mapped[int] = mapped_column(ForeignKey("periodo_gorjeta.id"), nullable=False, index=True)
    colaborador_id: Mapped[int | None] = mapped_column(ForeignKey("colaborador.id"))
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    setor: Mapped[str] = mapped_column(String(60), nullable=False)
    funcao: Mapped[str] = mapped_column(String(60), nullable=False)
    registro: Mapped[str] = mapped_column(String(30), nullable=False)
    pontos: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    dias_trabalhados: Mapped[int] = mapped_column(Integer, nullable=False)
    desconto: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), nullable=False)
    liquido_a_pagar: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    data_fechamento: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    periodo = relationship("PeriodoGorjeta", back_populates="fechamentos")
