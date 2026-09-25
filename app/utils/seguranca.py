# -*- coding: utf-8 -*-
"""Peças de segurança compartilhadas: redirecionamento seguro, política de senha
e limite de tentativas (guardado no banco para valer entre os workers)."""
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import current_app, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db

SENHA_MINIMA = 10

# senhas óbvias recusadas mesmo com tamanho suficiente
_SENHAS_FRACAS = {
    "1234567890", "12345678910", "0123456789", "senha12345", "senhasenha", "password123",
    "qwertyuiop", "bangalo123", "bangalo2026", "restaurante", "abcdefghij", "1111111111",
    "admin12345", "mudar12345", "trocar1234",
}

_HASH_FALSO = None


def destino_seguro(destino: str | None, padrao: str) -> str:
    """Só aceita caminhos internos (evita open redirect, inclusive `/\\evil.com`)."""
    if not destino or not destino.startswith("/") or destino.startswith("//"):
        return padrao
    if "\\" in destino or any(ord(c) < 32 for c in destino):
        return padrao
    partes = urlsplit(destino)
    if partes.scheme or partes.netloc:
        return padrao
    return destino


def problema_na_senha(senha: str, *, nome: str = "", email: str = "") -> str | None:
    """Retorna a mensagem do problema ou None se a senha é aceitável."""
    if len(senha) < SENHA_MINIMA:
        return f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres."
    if len(senha) > 200:
        return "A senha pode ter no máximo 200 caracteres."
    baixa = senha.lower()
    if baixa in _SENHAS_FRACAS or len(set(baixa)) < 4:
        return "Essa senha é fácil de adivinhar. Escolha outra."
    usuario_email = (email or "").split("@")[0].lower()
    if usuario_email and len(usuario_email) >= 4 and usuario_email in baixa:
        return "A senha não pode conter o seu e-mail."
    primeiro_nome = (nome or "").split(" ")[0].lower()
    if len(primeiro_nome) >= 4 and primeiro_nome in baixa and len(baixa) - len(primeiro_nome) < 6:
        return "A senha não pode ser basicamente o seu nome."
    if not any(c.isalpha() for c in senha) or not any(not c.isalpha() for c in senha):
        return "Use letras e também números ou símbolos."
    return None


def conferir_senha_em_tempo_constante(usuario, senha: str) -> bool:
    """Confere a senha; sem usuário, gasta o mesmo tempo (não revela quais e-mails existem)."""
    global _HASH_FALSO
    if usuario is None:
        if _HASH_FALSO is None:
            _HASH_FALSO = generate_password_hash("senha-inexistente-para-tempo-constante")
        check_password_hash(_HASH_FALSO, senha)
        return False
    return usuario.conferir_senha(senha)


# ---------------------------- limite de tentativas ----------------------------

def ip_cliente() -> str:
    return (request.remote_addr or "")[:45]


def _janela() -> datetime:
    return datetime.utcnow() - timedelta(minutes=current_app.config["LOGIN_JANELA_MINUTOS"])


def bloqueado(tipo: str, email: str) -> bool:
    """Bloqueia por e-mail (força bruta numa conta) e por IP (varredura de contas)."""
    from app.models.usuario import TentativaLogin

    desde = _janela()
    base = db.select(db.func.count(TentativaLogin.id)).filter(
        TentativaLogin.tipo == tipo, TentativaLogin.criado_em >= desde)
    por_email = db.session.execute(base.filter(TentativaLogin.email == email)).scalar_one()
    por_ip = db.session.execute(base.filter(TentativaLogin.ip == ip_cliente())).scalar_one()
    cfg = current_app.config
    return por_email >= cfg["LOGIN_MAX_TENTATIVAS"] or por_ip >= cfg["LOGIN_MAX_TENTATIVAS_IP"]


def registrar_tentativa(tipo: str, email: str) -> None:
    from app.models.usuario import TentativaLogin

    db.session.add(TentativaLogin(tipo=tipo, email=email[:255], ip=ip_cliente()))
    # limpeza: nada de acumular registros antigos
    db.session.execute(db.delete(TentativaLogin).where(
        TentativaLogin.criado_em < datetime.utcnow() - timedelta(days=1)))


def limpar_tentativas(tipo: str, email: str) -> None:
    from app.models.usuario import TentativaLogin

    db.session.execute(db.delete(TentativaLogin).where(
        TentativaLogin.tipo == tipo, TentativaLogin.email == email))
