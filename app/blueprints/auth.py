# -*- coding: utf-8 -*-
"""Autenticação: login/logout, troca e recuperação de senha, limite de tentativas."""
import hashlib
import time

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_mail import Message
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db, mail
from app.models.usuario import Usuario
from app.services import auditoria
from app.utils import seguranca
from app.utils.datas import agora_sp

bp = Blueprint("auth", __name__)

MENSAGEM_BLOQUEIO = "Muitas tentativas. Aguarde alguns minutos e tente de novo."


def iniciar_sessao(usuario: Usuario) -> None:
    """Sessão nova a cada login: nada do visitante anônimo passa para a sessão autenticada."""
    session.clear()
    login_user(usuario)
    session.permanent = True
    agora = int(time.time())
    session["login_em"] = agora
    session["visto_em"] = agora


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:255]
        senha = (request.form.get("senha") or "")[:200]

        if seguranca.bloqueado("login", email):
            auditoria.registrar("login_bloqueado", "usuario", None, depois={"email": email})
            db.session.commit()
            flash(MENSAGEM_BLOQUEIO, "error")
            return render_template("auth/login.html"), 429

        usuario = db.session.execute(
            db.select(Usuario).filter_by(email=email)
        ).scalar_one_or_none()
        senha_ok = seguranca.conferir_senha_em_tempo_constante(usuario, senha)

        if usuario and usuario.ativo and senha_ok:
            seguranca.limpar_tentativas("login", email)
            destino = seguranca.destino_seguro(request.args.get("next"), url_for("dashboard.home"))
            iniciar_sessao(usuario)
            usuario.ultimo_login = agora_sp().replace(tzinfo=None)
            auditoria.registrar("login", "usuario", usuario.id)
            db.session.commit()
            return redirect(destino)

        seguranca.registrar_tentativa("login", email)
        auditoria.registrar("login_falhou", "usuario", usuario.id if usuario else None,
                            depois={"email": email, "motivo": "inativo" if usuario and not usuario.ativo and senha_ok
                                    else "credenciais"})
        db.session.commit()
        # mesma mensagem para e-mail inexistente, senha errada ou conta inativa
        flash("E-mail ou senha inválidos.", "error")
        return render_template("auth/login.html"), 401

    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    auditoria.registrar("logout", "usuario", current_user.id)
    db.session.commit()
    logout_user()
    session.clear()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/conta/senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    """O próprio usuário troca a senha (pede a atual). Encerra as sessões em outros aparelhos."""
    if request.method == "POST":
        atual = request.form.get("atual") or ""
        nova = request.form.get("senha") or ""
        confirmar = request.form.get("confirmar") or ""
        usuario = db.session.get(Usuario, current_user.id)
        if seguranca.bloqueado("troca_senha", usuario.email):
            flash(MENSAGEM_BLOQUEIO, "error")
            return render_template("auth/trocar_senha.html"), 429
        if not usuario.conferir_senha(atual):
            seguranca.registrar_tentativa("troca_senha", usuario.email)
            db.session.commit()
            flash("A senha atual não confere.", "error")
            return render_template("auth/trocar_senha.html"), 400
        problema = seguranca.problema_na_senha(nova, nome=usuario.nome, email=usuario.email)
        if not problema and nova == atual:
            problema = "A nova senha precisa ser diferente da atual."
        if not problema and nova != confirmar:
            problema = "As senhas não conferem."
        if problema:
            flash(problema, "error")
            return render_template("auth/trocar_senha.html"), 400
        usuario.definir_senha(nova)  # muda a versão da sessão: outros aparelhos saem
        seguranca.limpar_tentativas("troca_senha", usuario.email)
        auditoria.registrar("senha_alterada", "usuario", usuario.id, depois={"por": "proprio usuario"})
        db.session.commit()
        iniciar_sessao(usuario)  # este aparelho continua logado com a versão nova
        flash("Senha alterada. As sessões em outros aparelhos foram encerradas.", "success")
        return redirect(url_for("dashboard.home"))
    return render_template("auth/trocar_senha.html")


# ---------------------------- recuperação de senha ----------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="recuperar-senha")


def _impressao_da_senha(usuario: Usuario) -> str:
    """Muda quando a senha muda: o link de recuperação só funciona uma vez."""
    return hashlib.sha256(usuario.senha_hash.encode()).hexdigest()[:20]


def _link_redefinicao(token: str) -> str:
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    caminho = url_for("auth.redefinir_senha", token=token)
    # com APP_BASE_URL o link não depende do cabeçalho Host da requisição
    return f"{base}{caminho}" if base else url_for("auth.redefinir_senha", token=token, _external=True)


@bp.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:255]
        if seguranca.bloqueado("recuperacao", email):
            flash(MENSAGEM_BLOQUEIO, "error")
            return render_template("auth/esqueci_senha.html"), 429
        seguranca.registrar_tentativa("recuperacao", email)  # conta todo pedido: evita bombardeio de e-mails
        usuario = db.session.execute(
            db.select(Usuario).filter_by(email=email)
        ).scalar_one_or_none()
        # resposta idêntica com ou sem cadastro — não vaza quais e-mails existem
        if usuario and usuario.ativo and current_app.config.get("MAIL_USERNAME"):
            token = _serializer().dumps({"id": usuario.id, "h": _impressao_da_senha(usuario)})
            msg = Message(
                subject="Bangalô — redefinição de senha",
                recipients=[usuario.email],
                body=(
                    f"Olá {usuario.nome},\n\n"
                    f"Para redefinir sua senha, acesse (válido por 1 hora e uma única vez):\n"
                    f"{_link_redefinicao(token)}\n\n"
                    "Se você não pediu a redefinição, ignore este e-mail. Sua senha atual continua valendo."
                ),
            )
            try:
                mail.send(msg)
            except Exception:  # noqa: BLE001
                current_app.logger.exception("Falha ao enviar e-mail de recuperação")
            auditoria.registrar("senha_pedido", "usuario", usuario.id)
        db.session.commit()
        flash("Se o e-mail estiver cadastrado, você receberá o link de redefinição.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/esqueci_senha.html")


def _usuario_do_token(token: str) -> Usuario | None:
    try:
        dados = _serializer().loads(token, max_age=3600)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(dados, dict):
        return None
    usuario = db.session.get(Usuario, dados.get("id")) if isinstance(dados.get("id"), int) else None
    if not usuario or not usuario.ativo or dados.get("h") != _impressao_da_senha(usuario):
        return None
    return usuario


@bp.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token: str):
    usuario = _usuario_do_token(token)
    if usuario is None:
        flash("Link inválido, expirado ou já usado. Peça uma nova redefinição.", "error")
        return redirect(url_for("auth.esqueci_senha"))

    if request.method == "POST":
        senha = request.form.get("senha") or ""
        confirmar = request.form.get("confirmar") or ""
        problema = seguranca.problema_na_senha(senha, nome=usuario.nome, email=usuario.email)
        if not problema and senha != confirmar:
            problema = "As senhas não conferem."
        if problema:
            flash(problema, "error")
        else:
            usuario.definir_senha(senha)  # também invalida este link e as sessões abertas
            seguranca.limpar_tentativas("login", usuario.email)
            auditoria.registrar("senha_alterada", "usuario", usuario.id, depois={"por": "link de recuperacao"})
            db.session.commit()
            flash("Senha redefinida. Faça login.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/redefinir_senha.html", token=token)
