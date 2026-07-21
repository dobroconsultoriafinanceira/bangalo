# -*- coding: utf-8 -*-
"""Application factory do dashboard financeiro do Bangalô."""
from pathlib import Path

from flask import Flask, jsonify, render_template

from app.config import obter_config
from app.extensions import csrf, db, login_manager, mail, migrate


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(obter_config(config_name))
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    # --- extensões ---
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    csrf.init_app(app)

    # --- models precisam estar importados para migrations/login ---
    from app import models  # noqa: F401
    from app.models.usuario import Usuario

    @login_manager.user_loader
    def carregar_usuario(user_id: str):
        return db.session.get(Usuario, int(user_id))

    # --- filtros Jinja (moeda BRL, datas) ---
    from app.utils import filtros

    filtros.registrar(app)

    # --- blueprints ---
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.fluxo_caixa import bp as fluxo_bp
    from app.blueprints.gorjetas import bp as gorjetas_bp
    from app.blueprints.metas import bp as metas_bp
    from app.blueprints.cadastros import bp as cadastros_bp
    from app.blueprints.admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(fluxo_bp, url_prefix="/fluxo")
    app.register_blueprint(gorjetas_bp, url_prefix="/gorjetas")
    app.register_blueprint(metas_bp, url_prefix="/metas")
    app.register_blueprint(cadastros_bp, url_prefix="/cadastros")
    app.register_blueprint(admin_bp, url_prefix="/admin")

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
        # CSP compatível com os CDNs usados (Tailwind, Chart.js, Google Fonts, Lucide)
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'",
        )
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

    @app.errorhandler(500)
    def erro_interno(_e):
        return render_template("erro.html", codigo=500, mensagem="Erro interno. Tente novamente."), 500

    # --- scheduler opcional (backup/importação) ---
    if app.config.get("SCHEDULER_ENABLED"):
        from app.utils.agendador import iniciar_agendador

        iniciar_agendador(app)

    return app
