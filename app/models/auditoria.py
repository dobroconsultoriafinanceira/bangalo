# -*- coding: utf-8 -*-
"""Trilha de auditoria — obrigatória para dados de folha e financeiros (LGPD)."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class LogAuditoria(db.Model):
    __tablename__ = "log_auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    acao: Mapped[str] = mapped_column(String(20), nullable=False)  # create/update/delete/login...
    entidade: Mapped[str] = mapped_column(String(60), nullable=False)
    entidade_id: Mapped[int | None] = mapped_column(Integer)
    dados_antes: Mapped[dict | None] = mapped_column(JSON)
    dados_depois: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(45))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    usuario = relationship("Usuario")
