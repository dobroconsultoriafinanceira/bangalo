# -*- coding: utf-8 -*-
"""Autenticação: login/logout, recuperação de senha e rate limiting."""
import time

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_mail import Message
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db, mail
from app.models.usuario import Usuario
from app.services import auditoria
from app.utils.datas import agora_sp

bp = Blueprint("auth", __name__)

# Rate limiting simples em memória: {chave: [timestamps de falhas]}
# Suficiente para poucos usuários; um WAF/Caddy cobre o resto.
_tentativas_falhas: dict[str, list[float]] = {}


def _bloqueado(chave: str) -> bool:
    janela = current_app.config["LOGIN_JANELA_MINUTOS"] * 60
    maximo = current_app.config["LOGIN_MAX_TENTATIVAS"]
    agora = time.time()
    falhas = [t for t in _tentativas_falhas.get(chave, []) if agora - t < janela]
    _tentativas_falhas[chave] = falhas
    return len(falhas) >= maximo


def _registrar_falha(chave: str) -> None:
    _tentativas_falhas.setdefault(chave, []).append(time.time())


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        senha = request.form.get("senha") or ""
        chave = f"{email}|{request.remote_addr}"

        if _bloqueado(chave):
            flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "error")
            return render_template("auth/login.html"), 429

        usuario = db.session.execute(
            db.select(Usuario).filter_by(email=email)
        ).scalar_one_or_none()

        if usuario and usuario.ativo and usuario.conferir_senha(senha):
            login_user(usuario)
            usuario.ultimo_login = agora_sp().replace(tzinfo=None)
            auditoria.registrar("login", "usuario", usuario.id)
            db.session.commit()
            destino = request.args.get("next")
            # evita open redirect: só caminhos internos
            if destino and destino.startswith("/") and not destino.startswith("//"):
                return redirect(destino)
            return redirect(url_for("dashboard.home"))

        _registrar_falha(chave)
        flash("E-mail ou senha inválidos.", "error")

    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("auth.login"))


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="recuperar-senha")


@bp.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        usuario = db.session.execute(
            db.select(Usuario).filter_by(email=email)
        ).scalar_one_or_none()
        # resposta idêntica com ou sem cadastro — não vaza quais e-mails existem
        if usuario and usuario.ativo and current_app.config.get("MAIL_USERNAME"):
            token = _serializer().dumps(usuario.email)
            link = url_for("auth.redefinir_senha", token=token, _external=True)
            msg = Message(
                subject="Bangalô — redefinição de senha",
                recipients=[usuario.email],
                body=(
                    f"Olá {usuario.nome},\n\n"
                    f"Para redefinir sua senha, acesse (válido por 1 hora):\n{link}\n\n"
                    "Se você não pediu a redefinição, ignore este e-mail."
                ),
            )
            try:
                mail.send(msg)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Falha ao enviar e-mail de recuperação")
        flash("Se o e-mail estiver cadastrado, você receberá o link de redefinição.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/esqueci_senha.html")


@bp.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token: str):
    try:
        email = _serializer().loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        flash("Link inválido ou expirado. Peça uma nova redefinição.", "error")
        return redirect(url_for("auth.esqueci_senha"))

    usuario = db.session.execute(db.select(Usuario).filter_by(email=email)).scalar_one_or_none()
    if not usuario:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        senha = request.form.get("senha") or ""
        confirmar = request.form.get("confirmar") or ""
        if len(senha) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "error")
        elif senha != confirmar:
            flash("As senhas não conferem.", "error")
        else:
            usuario.definir_senha(senha)
            auditoria.registrar("update", "usuario", usuario.id, depois={"evento": "senha redefinida"})
            db.session.commit()
            flash("Senha redefinida. Faça login.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/redefinir_senha.html", token=token)
