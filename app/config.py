# -*- coding: utf-8 -*-
"""Configuração por ambiente. Nenhum segredo hardcoded — tudo via .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool_env(nome: str, default: str = "false") -> bool:
    return os.environ.get(nome, default).strip().lower() in ("1", "true", "yes", "sim")


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-inseguro-trocar")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Fuso e localidade — todo o sistema opera em America/Sao_Paulo, moeda BRL
    TIMEZONE = "America/Sao_Paulo"

    # Sessão / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # E-mail (SMTP Hostinger)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.hostinger.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "465"))
    MAIL_USE_SSL = _bool_env("MAIL_USE_SSL", "true")
    MAIL_USE_TLS = _bool_env("MAIL_USE_TLS", "false")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")

    # Admin inicial (seed)
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@dobroconsultoriafinanceira.com.br")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    # Jobs agendados
    SCHEDULER_ENABLED = _bool_env("SCHEDULER_ENABLED", "false")

    # Upload de planilhas: só .xlsx/.csv, no máximo 20 MB
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    UPLOAD_EXTENSOES = {".xlsx", ".csv"}

    # Rate limit do login: N tentativas por janela (minutos)
    LOGIN_MAX_TENTATIVAS = 5
    LOGIN_JANELA_MINUTOS = 15


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or (
        "sqlite:///" + str(BASE_DIR / "instance" / "bangalo_dev.db")
    )


class ProductionConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SESSION_COOKIE_SECURE = True  # HTTPS obrigatório atrás do Caddy
    PREFERRED_URL_SCHEME = "https"


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


def obter_config(nome: str | None = None):
    nome = nome or os.environ.get("FLASK_ENV", "development")
    return {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
        "testing": TestingConfig,
    }.get(nome, DevelopmentConfig)
