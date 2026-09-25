# -*- coding: utf-8 -*-
"""Arquivos guardados pelo sistema: documentos do fechamento contábil,
lançamentos do razão e relatórios exportados.

Os arquivos ficam em `instance/arquivos/` (fora de `static`): só saem por
rotas autenticadas.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

TIPOS_DOCUMENTO = ("razao", "dre_pdf", "outro")
ROTULOS_DOCUMENTO = {"razao": "Razão da contabilidade", "dre_pdf": "DRE da contabilidade (PDF)", "outro": "Outro documento"}
SITUACOES_DOCUMENTO = ("recebido", "conferido", "aprovado")
STATUS_EXPORTACAO = ("gerado", "falhou")


class DocumentoContabil(db.Model):
    """Documento recebido da contabilidade para uma competência."""

    __tablename__ = "documento_contabil"

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo: Mapped[str] = mapped_column(
        Enum(*TIPOS_DOCUMENTO, name="tipo_documento_contabil", native_enum=False), nullable=False)
    nome_arquivo: Mapped[str] = mapped_column(String(200), nullable=False)   # nome original
    caminho: Mapped[str] = mapped_column(String(300), nullable=False)        # relativo a instance/arquivos
    tamanho: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    situacao: Mapped[str] = mapped_column(
        Enum(*SITUACOES_DOCUMENTO, name="situacao_documento_contabil", native_enum=False),
        nullable=False, default="recebido")
    observacao: Mapped[str | None] = mapped_column(Text)
    recebido_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    enviado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    conferido_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    conferido_em: Mapped[datetime | None] = mapped_column(DateTime)
    aprovado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    aprovado_em: Mapped[datetime | None] = mapped_column(DateTime)
    substituido_em: Mapped[datetime | None] = mapped_column(DateTime)  # razão reimportado depois

    enviado_por = relationship("Usuario", foreign_keys=[enviado_por_id])
    conferido_por = relationship("Usuario", foreign_keys=[conferido_por_id])
    aprovado_por = relationship("Usuario", foreign_keys=[aprovado_por_id])

    @property
    def rotulo_tipo(self) -> str:
        return ROTULOS_DOCUMENTO.get(self.tipo, self.tipo)


class RazaoLancamento(db.Model):
    """Lançamento do razão da contabilidade (para rastrear a conta até a origem)."""

    __tablename__ = "razao_lancamento"

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    conta: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    classificacao: Mapped[str] = mapped_column(String(30), nullable=False)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    data: Mapped[date] = mapped_column(Date, nullable=False)
    historico: Mapped[str | None] = mapped_column(String(300))
    contrapartida: Mapped[str | None] = mapped_column(String(20))
    debito: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    credito: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))


class ExportacaoRelatorio(db.Model):
    """Relatório gerado pela Central de Relatórios (histórico para baixar de novo)."""

    __tablename__ = "exportacao_relatorio"

    id: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    parametros: Mapped[dict | None] = mapped_column(JSON)
    nome_arquivo: Mapped[str | None] = mapped_column(String(200))
    caminho: Mapped[str | None] = mapped_column(String(300))
    tamanho: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*STATUS_EXPORTACAO, name="status_exportacao", native_enum=False), nullable=False)
    erro: Mapped[str | None] = mapped_column(String(300))
    gerado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    gerado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    gerado_por = relationship("Usuario")
