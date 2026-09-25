# -*- coding: utf-8 -*-
"""Application factory do dashboard financeiro do Bangalô."""
from pathlib import Path

import time

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError

from app.config import obter_config
from app.extensions import csrf, db, login_manager, mail, migrate


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(obter_config(config_name))
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _conferir_segredos(app)

    if app.config.get("TRUST_PROXY"):
        # IP real do cliente (limite de login, auditoria) e esquema https atrás do Caddy
        from werkzeug.middleware.proxy_fix import ProxyFix

        hops = app.config["TRUST_PROXY"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

    # --- extensões ---
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    csrf.init_app(app)

    # --- models precisam estar importados para migrations/login ---
    from app import models  # noqa: F401
    from app.models.usuario import Anonimo, Usuario

    login_manager.anonymous_user = Anonimo
    login_manager.session_protection = "basic"

    @login_manager.user_loader
    def carregar_usuario(user_id: str):
        # formato "id:versao" — trocar senha/papel/permissões ou desativar derruba a sessão
        try:
            uid, versao = (int(x) for x in str(user_id).split(":"))
        except ValueError:
            return None
        usuario = db.session.get(Usuario, uid)
        if not usuario or not usuario.ativo or (usuario.sessao_versao or 1) != versao:
            return None
        return usuario

    rotas_publicas = {"auth.login", "auth.esqueci_senha", "auth.redefinir_senha", "static", "healthz"}

    @app.before_request
    def exigir_login():
        """Toda rota exige login, salvo as públicas — rota nova esquecida não fica exposta."""
        from flask_login import current_user, logout_user

        if request.endpoint is None or request.endpoint in rotas_publicas:
            return None
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        agora = int(time.time())
        inatividade = app.config["SESSAO_INATIVIDADE_MIN"] * 60
        visto_em = session.get("visto_em") or 0
        if agora - visto_em > inatividade:
            logout_user()
            session.clear()
            flash("Sua sessão expirou por inatividade. Entre de novo.", "warning")
            if request.method == "GET":
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            return redirect(url_for("auth.login"))
        if agora - visto_em > 60:  # evita regravar o cookie a cada requisição
            session["visto_em"] = agora
        return None

    # --- filtros Jinja (moeda BRL, datas) ---
    from app.utils import filtros

    filtros.registrar(app)

    # --- contexto do shell Clareza (menu e status do Itaú no rodapé da sidebar) ---
    @app.context_processor
    def contexto_shell():
        from flask_login import current_user

        dados = {"shell_endpoints": set(app.view_functions), "shell_itau": {}}
        if not getattr(current_user, "is_authenticated", False):
            return dados
        from app.utils import periodo as periodo_global

        dados["shell_periodo"] = {"atual": periodo_global.atual(), "escolhido": periodo_global.escolhido()}
        try:
            from datetime import timedelta

            from app.services import itau_sync
            from app.utils.datas import agora_sp

            ultima = itau_sync.ultima_sincronizacao()
            intervalo = int(app.config.get("ITAU_SYNC_INTERVAL_MIN", 60))
            limite_h = max(1, round(intervalo * 3 / 60))  # três ciclos sem sucesso = atrasado
            atrasado = bool(
                ultima and agora_sp().replace(tzinfo=None) - ultima > timedelta(hours=limite_h)
            )
            dados["shell_itau"] = {"ultima": ultima, "atrasado": atrasado, "limite_h": limite_h}
        except Exception:  # o menu nunca pode derrubar a página
            app.logger.exception("contexto do shell: status do Itaú indisponível")
        return dados

    # --- blueprints ---
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.dre import bp as dre_bp
    from app.blueprints.fluxo_caixa import bp as fluxo_bp
    from app.blueprints.gorjetas import bp as gorjetas_bp
    from app.blueprints.metas import bp as metas_bp
    from app.blueprints.cadastros import bp as cadastros_bp
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.conciliacao import bp as conciliacao_bp
    from app.blueprints.relatorios import bp as relatorios_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(dre_bp, url_prefix="/dre")
    app.register_blueprint(fluxo_bp, url_prefix="/fluxo")
    app.register_blueprint(gorjetas_bp, url_prefix="/gorjetas")
    app.register_blueprint(metas_bp, url_prefix="/metas")
    app.register_blueprint(cadastros_bp, url_prefix="/cadastros")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(conciliacao_bp, url_prefix="/conciliacao")
    app.register_blueprint(relatorios_bp, url_prefix="/relatorios")

    # --- comandos CLI (flask seed / flask importar) ---
    from app.cli import registrar_cli

    registrar_cli(app)

    # --- healthcheck (sem auth — usado pelo compose/monitoramento) ---
    @app.route("/healthz")
    def healthz():
        return jsonify(status="ok")

    # --- headers de segurança ---
    @app.after_request
    def headers_seguranca(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # CSP: fontes locais (Clareza); Chart.js e Lucide com versão fixa no jsDelivr
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        # dados financeiros: não guardar páginas autenticadas no cache do navegador/proxy
        if request.endpoint not in ("static", "healthz"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers.setdefault("Pragma", "no-cache")
        if not app.debug:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp

    # --- páginas de erro sem stack trace ---
    @app.errorhandler(404)
    def nao_encontrado(_e):
        return render_template("erro.html", codigo=404, mensagem="Página não encontrada."), 404

    @app.errorhandler(403)
    def proibido(_e):
        return render_template("erro.html", codigo=403, mensagem="Acesso negado."), 403

    @app.errorhandler(CSRFError)
    def csrf_invalido(_e):
        return render_template(
            "erro.html", codigo=400,
            mensagem="O formulário expirou ou veio de outra página. Recarregue a página e tente de novo.",
        ), 400

    @app.errorhandler(413)
    def grande_demais(_e):
        return render_template("erro.html", codigo=413, mensagem="Arquivo grande demais (máximo 20 MB)."), 413

    @app.errorhandler(500)
    def erro_interno(_e):
        return render_template("erro.html", codigo=500, mensagem="Erro interno. Tente novamente."), 500

    # --- scheduler opcional (backup, sync Google e/ou extrato Itaú) ---
    if (app.config.get("SCHEDULER_ENABLED") or app.config.get("GOOGLE_SYNC_ENABLED")
            or app.config.get("ITAU_SYNC_ENABLED") or app.config.get("STONE_SYNC_ENABLED")):
        from app.utils.agendador import iniciar_agendador

        iniciar_agendador(app)

    return app


SEGREDOS_FRACOS = {"", "dev-inseguro-trocar", "changeme", "trocar", "secret"}


def _conferir_segredos(app: Flask) -> None:
    """Em produção, não sobe com SECRET_KEY padrão/curta (sessões e links de senha seriam forjáveis)."""
    if app.config.get("TESTING") or app.debug:
        return
    chave = app.config.get("SECRET_KEY") or ""
    if chave in SEGREDOS_FRACOS or len(chave) < 32:
        raise RuntimeError(
            "SECRET_KEY ausente ou fraca para produção. Gere uma com "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))' e coloque no .env."
        )
