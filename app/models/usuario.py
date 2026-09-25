# -*- coding: utf-8 -*-
"""Usuários, papéis/permissões e tentativas de login.

Papéis: `consultoria` (administrador total), `gerencia` (operação) e
`leitura` (só visualiza). Detalhes em app/utils/permissoes.py.
"""
from datetime import datetime

from flask_login import AnonymousUserMixin, UserMixin
from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.utils import permissoes as perm

ROLES = tuple(perm.PAPEIS)


class Usuario(UserMixin, db.Model):
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum(*ROLES, name="role_usuario", native_enum=False), nullable=False, default="gerencia"
    )
    # None = padrão do papel; lista = ajuste feito pelo administrador
    permissoes: Mapped[list | None] = mapped_column(JSON)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # muda a cada troca de senha/papel/permissão/desativação: derruba sessões abertas
    sessao_versao: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")
    senha_alterada_em: Mapped[datetime | None] = mapped_column(DateTime)
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def definir_senha(self, senha: str) -> None:
        self.senha_hash = generate_password_hash(senha)
        self.senha_alterada_em = datetime.utcnow()
        self.encerrar_sessoes()

    def conferir_senha(self, senha: str) -> bool:
        return check_password_hash(self.senha_hash, senha)

    def encerrar_sessoes(self) -> None:
        self.sessao_versao = (self.sessao_versao or 1) + 1

    def get_id(self) -> str:  # Flask-Login: sessão presa à versão atual do usuário
        return f"{self.id}:{self.sessao_versao or 1}"

    @property
    def is_active(self) -> bool:  # Flask-Login: usuário desativado não loga
        return self.ativo

    @property
    def eh_consultoria(self) -> bool:
        return self.role == "consultoria"

    @property
    def papel_rotulo(self) -> str:
        return perm.PAPEIS.get(self.role, {}).get("rotulo", self.role)

    @property
    def permissoes_efetivas(self) -> frozenset[str]:
        return perm.efetivas(self.role, self.permissoes)

    @property
    def permissoes_personalizadas(self) -> bool:
        return self.role != "consultoria" and self.permissoes is not None and \
            self.permissoes_efetivas != perm.efetivas(self.role, None)

    def pode(self, permissao: str) -> bool:
        return self.ativo and permissao in self.permissoes_efetivas


class Anonimo(AnonymousUserMixin):
    eh_consultoria = False
    role = None

    def pode(self, _permissao: str) -> bool:
        return False


class TentativaLogin(db.Model):
    """Falhas de login/recuperação por e-mail e IP — limite vale entre workers do Gunicorn."""
    __tablename__ = "tentativa_login"

    id: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False, default="login")  # login | recuperacao
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(45), index=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
