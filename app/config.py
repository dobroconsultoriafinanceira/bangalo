# -*- coding: utf-8 -*-
"""Configuração por ambiente. Nenhum segredo hardcoded — tudo via .env."""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool_env(nome: str, default: str = "false") -> bool:
    return os.environ.get(nome, default).strip().lower() in ("1", "true", "yes", "sim")


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-inseguro-trocar")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # endereço público (links de e-mail não dependem do cabeçalho Host)
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "")
    # quantos proxies reversos confiáveis na frente do app (Caddy = 1)
    TRUST_PROXY = int(os.environ.get("TRUST_PROXY", "0"))

    # Fuso e localidade — todo o sistema opera em America/Sao_Paulo, moeda BRL
    TIMEZONE = "America/Sao_Paulo"

    # Sessão / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # sessão expira de vez após 12 h e por inatividade (padrão 2 h)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get("SESSAO_MAXIMA_HORAS", "12")))
    SESSAO_INATIVIDADE_MIN = int(os.environ.get("SESSAO_INATIVIDADE_MIN", "120"))
    # token CSRF vale enquanto a sessão valer (telas longas, como a de gorjetas)
    WTF_CSRF_TIME_LIMIT = 12 * 3600

    # E-mail (SMTP Hostinger)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.hostinger.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "465"))
    MAIL_USE_SSL = _bool_env("MAIL_USE_SSL", "true")
    MAIL_USE_TLS = _bool_env("MAIL_USE_TLS", "false")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER")

    # Admin inicial (seed)
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@dobroconsultoria.com.br")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    # Jobs agendados
    SCHEDULER_ENABLED = _bool_env("SCHEDULER_ENABLED", "false")

    # Sincronização automática do fluxo com o Google Sheets (só leitura)
    GOOGLE_SYNC_ENABLED = _bool_env("GOOGLE_SYNC_ENABLED", "false")
    GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GOOGLE_SHEETS_FLUXO_ID = os.environ.get("GOOGLE_SHEETS_FLUXO_ID", "")
    GOOGLE_SYNC_INTERVAL_MIN = int(os.environ.get("GOOGLE_SYNC_INTERVAL_MIN", "15"))

    # Upload de planilhas: só .xlsx/.csv, no máximo 20 MB
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    UPLOAD_EXTENSOES = {".xlsx", ".csv", ".xls"}  # .xls: razão da contabilidade

    # Rate limit do login: N tentativas por janela (minutos)
    LOGIN_MAX_TENTATIVAS = 5       # por e-mail
    LOGIN_MAX_TENTATIVAS_IP = 20   # por IP (varredura de várias contas)
    LOGIN_JANELA_MINUTOS = 15

    # Stone: busca diária dos repasses e da agenda (o arquivo do dia sai depois das 5h)
    STONE_SYNC_ENABLED = _bool_env("STONE_SYNC_ENABLED", "false")
    STONE_SYNC_HORA = int(os.environ.get("STONE_SYNC_HORA", "6"))

    # Integração Stone (Conciliação) — credenciais atreladas ao Stone Code
    STONE_BASE_URL = os.environ.get("STONE_BASE_URL", "")
    STONE_CLIENT_APPLICATION_KEY = os.environ.get("STONE_CLIENT_APPLICATION_KEY", "")
    STONE_SECRET_KEY = os.environ.get("STONE_SECRET_KEY", "")
    STONE_CODES = os.environ.get("STONE_CODES", "")  # separados por vírgula
    # registrar a taxa (MDR) como saída em Despesas Bancárias?
    STONE_LANCAR_TAXA = _bool_env("STONE_LANCAR_TAXA", "false")

    # Integração Itaú PJ (API de Extrato) — OAuth2 client_credentials + mTLS
    ITAU_AMBIENTE = os.environ.get("ITAU_AMBIENTE", "producao")  # producao | homologacao
    ITAU_CLIENT_ID = os.environ.get("ITAU_CLIENT_ID", "")
    ITAU_CLIENT_SECRET = os.environ.get("ITAU_CLIENT_SECRET", "")
    ITAU_CERT_DIR = os.environ.get("ITAU_CERT_DIR") or str(BASE_DIR / "instance" / "itau")
    ITAU_CERT_PATH = os.environ.get("ITAU_CERT_PATH", "")  # padrão: ITAU_CERT_DIR/itau.crt
    ITAU_KEY_PATH = os.environ.get("ITAU_KEY_PATH", "")    # padrão: ITAU_CERT_DIR/itau.key
    ITAU_CONTAS = os.environ.get("ITAU_CONTAS", "")        # agência-conta-DAC, separadas por vírgula
    # sincronização automática do extrato (APScheduler)
    ITAU_SYNC_ENABLED = _bool_env("ITAU_SYNC_ENABLED", "false")
    ITAU_SYNC_INTERVAL_MIN = int(os.environ.get("ITAU_SYNC_INTERVAL_MIN", "60"))
    ITAU_SYNC_DIAS = int(os.environ.get("ITAU_SYNC_DIAS", "35"))  # janela buscada a cada sync
    # raiz do CNPJ da empresa (8 dígitos): identifica contas próprias (Stone etc.)
    EMPRESA_CNPJ_RAIZ = os.environ.get("EMPRESA_CNPJ_RAIZ", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or (
        "sqlite:///" + str(BASE_DIR / "instance" / "bangalo_dev.db")
    )


class ProductionConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SESSION_COOKIE_SECURE = True  # HTTPS obrigatório atrás do Caddy
    SESSION_COOKIE_NAME = "__Host-bangalo"  # só HTTPS, só este domínio, path /
    PREFERRED_URL_SCHEME = "https"
    TRUST_PROXY = int(os.environ.get("TRUST_PROXY", "1"))  # Caddy na frente


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


def obter_config(nome: str | None = None):
    # sem FLASK_ENV definido, assume produção (falha fechada: exige SECRET_KEY forte, sem debug)
    nome = nome or os.environ.get("FLASK_ENV", "production")
    return {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
        "testing": TestingConfig,
    }.get(nome, ProductionConfig)
