# -*- coding: utf-8 -*-
"""Fluxo de caixa normalizado: a grade dia × conta da planilha vira linhas.

Regra de ouro: saldos e subtotais são DERIVADOS (services/fluxo_caixa.py),
nunca colunas. Só o saldo inicial de abertura é guardado (ConfigSistema).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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

TIPOS_CATEGORIA = ("entrada", "saida")

# Formas de pagamento das entradas operacionais (espelha o PDV/planilha)
FORMAS_PAGAMENTO = (
    "Visa Crédito", "Master Card Crédito", "ELO Crédito", "Amex Crédito",
    "ELO Débito", "Visa Eletron Débito", "Maestro Débito", "Dinheiro",
    "IFOOD", "99 FOOD", "PIX", "Outros/Acertos",
)


class Categoria(db.Model):
    """Plano de contas hierárquico (grupo › subgrupo › categoria)."""

    __tablename__ = "categoria"
    __table_args__ = (UniqueConstraint("nome", "grupo", name="uq_categoria_nome_grupo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum(*TIPOS_CATEGORIA, name="tipo_categoria", native_enum=False), nullable=False
    )
    grupo: Mapped[str] = mapped_column(String(80), nullable=False)  # ex.: "Operacional", "Impostos"
    subgrupo: Mapped[str | None] = mapped_column(String(80))
    ordem: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    codigo: Mapped[str | None] = mapped_column(String(20))

    lancamentos = relationship("Lancamento", back_populates="categoria")

    def __repr__(self) -> str:
        return f"<Categoria {self.grupo}/{self.nome}>"


class Fornecedor(db.Model):
    __tablename__ = "fornecedor"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    apelido: Mapped[str | None] = mapped_column(String(160))  # nomes compostos "MJM/Pagcerto/Cave"
    categoria_padrao_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    observacao: Mapped[str | None] = mapped_column(Text)

    categoria_padrao = relationship("Categoria")
    lancamentos = relationship("Lancamento", back_populates="fornecedor")


class Lancamento(db.Model):
    """Coração do fluxo de caixa: cada célula preenchida da planilha é uma linha."""

    __tablename__ = "lancamento"
    __table_args__ = (
        # dedupe de importações externas: um mesmo id de origem não duplica.
        # (SQLite/Postgres permitem múltiplos NULL, então lançamentos manuais
        #  com origem_id NULL não conflitam entre si.)
        UniqueConstraint("origem", "origem_id", name="uq_lancamento_origem"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    categoria_id: Mapped[int] = mapped_column(ForeignKey("categoria.id"), nullable=False, index=True)
    fornecedor_id: Mapped[int | None] = mapped_column(ForeignKey("fornecedor.id"), index=True)
    forma_pagamento: Mapped[str | None] = mapped_column(String(40))  # só entradas operacionais
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    descricao: Mapped[str | None] = mapped_column(String(255))
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    # procedência do lançamento: "manual", "importacao", "stone", ...
    origem: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    origem_id: Mapped[str | None] = mapped_column(String(80))  # id da transação na origem
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    categoria = relationship("Categoria", back_populates="lancamentos")
    fornecedor = relationship("Fornecedor", back_populates="lancamentos")
    usuario = relationship("Usuario")


TIPOS_APLICACAO = ("entrada", "rendimento", "resgate")


class AplicacaoFinanceira(db.Model):
    """Bloco "Saldo Aplicação" da planilha. Saldo = saldo inicial + acumulado."""

    __tablename__ = "aplicacao_financeira"

    id: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    tipo: Mapped[str] = mapped_column(
        Enum(*TIPOS_APLICACAO, name="tipo_aplicacao", native_enum=False), nullable=False
    )
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    descricao: Mapped[str | None] = mapped_column(String(255))


class ConfigSistema(db.Model):
    """Configurações pontuais (ex.: saldo_inicial_abertura, data_abertura)."""

    __tablename__ = "config_sistema"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str] = mapped_column(String(255), nullable=False)

    @staticmethod
    def obter(chave: str, default: str | None = None) -> str | None:
        reg = db.session.get(ConfigSistema, chave)
        return reg.valor if reg else default

    @staticmethod
    def definir(chave: str, valor: str) -> None:
        reg = db.session.get(ConfigSistema, chave)
        if reg:
            reg.valor = valor
        else:
            db.session.add(ConfigSistema(chave=chave, valor=valor))
