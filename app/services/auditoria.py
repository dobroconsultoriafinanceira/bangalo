# -*- coding: utf-8 -*-
"""Registro da trilha de auditoria (LGPD — folha e finanças)."""
from decimal import Decimal

from flask import request
from flask_login import current_user

from app.extensions import db
from app.models.auditoria import LogAuditoria


def _serializavel(dados: dict | None) -> dict | None:
    if dados is None:
        return None
    limpo = {}
    for chave, valor in dados.items():
        if isinstance(valor, Decimal):
            limpo[chave] = str(valor)
        elif hasattr(valor, "isoformat"):
            limpo[chave] = valor.isoformat()
        else:
            limpo[chave] = valor
    return limpo


def registrar(acao: str, entidade: str, entidade_id: int | None = None,
              antes: dict | None = None, depois: dict | None = None) -> None:
    """Adiciona o log à sessão atual — commit fica com o chamador."""
    usuario_id = current_user.id if current_user and current_user.is_authenticated else None
    ip = request.remote_addr if request else None
    db.session.add(LogAuditoria(
        usuario_id=usuario_id,
        acao=acao,
        entidade=entidade,
        entidade_id=entidade_id,
        dados_antes=_serializavel(antes),
        dados_depois=_serializavel(depois),
        ip=ip,
    ))
