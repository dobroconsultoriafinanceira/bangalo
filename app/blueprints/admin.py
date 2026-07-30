# -*- coding: utf-8 -*-
"""Admin (só consultoria): usuários, auditoria e importação de planilhas."""
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.auditoria import LogAuditoria
from app.models.usuario import ROLES, Usuario
from app.services import auditoria
from app.utils.decoradores import role_required

bp = Blueprint("admin", __name__)


@bp.route("/usuarios")
@role_required("consultoria")
def usuarios():
    itens = db.session.execute(db.select(Usuario).order_by(Usuario.nome)).scalars().all()
    return render_template("admin/usuarios.html", usuarios=itens, roles=ROLES)


@bp.route("/usuarios/salvar", methods=["POST"])
@role_required("consultoria")
def salvar_usuario():
    user_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    role = request.form.get("role")
    senha = request.form.get("senha") or ""

    if not nome or not email or role not in ROLES:
        flash("Preencha nome, e-mail e perfil.", "error")
        return redirect(url_for("admin.usuarios"))

    if user_id:
        usuario = db.session.get(Usuario, user_id) or abort(404)
        auditoria.registrar("update", "usuario", usuario.id,
                            antes={"nome": usuario.nome, "email": usuario.email, "role": usuario.role})
    else:
        if not senha or len(senha) < 8:
            flash("Novo usuário precisa de senha com pelo menos 8 caracteres.", "error")
            return redirect(url_for("admin.usuarios"))
        usuario = Usuario()
        db.session.add(usuario)

    usuario.nome, usuario.email, usuario.role = nome, email, role
    usuario.ativo = "ativo" in request.form
    if senha:
        if len(senha) < 8:
            flash("Senha precisa de pelo menos 8 caracteres.", "error")
            return redirect(url_for("admin.usuarios"))
        usuario.definir_senha(senha)

    db.session.flush()
    if not user_id:
        auditoria.registrar("create", "usuario", usuario.id,
                            depois={"nome": nome, "email": email, "role": role})
    db.session.commit()
    flash("Usuário salvo.", "success")
    return redirect(url_for("admin.usuarios"))


@bp.route("/auditoria")
@role_required("consultoria")
def logs_auditoria():
    pagina = request.args.get("pagina", type=int, default=1)
    entidade = request.args.get("entidade")
    query = db.select(LogAuditoria).order_by(LogAuditoria.timestamp.desc())
    if entidade:
        query = query.filter(LogAuditoria.entidade == entidade)
    paginacao = db.paginate(query, page=pagina, per_page=50, error_out=False)
    entidades = [e[0] for e in db.session.query(LogAuditoria.entidade).distinct().all()]
    return render_template(
        "admin/auditoria.html", paginacao=paginacao, entidades=entidades, entidade=entidade
    )


@bp.route("/importacao", methods=["GET", "POST"])
@role_required("consultoria")
def importacao():
    """Upload das planilhas originais + execução dos importers one-shot."""
    if request.method == "POST":
        arquivo = request.files.get("arquivo")
        tipo = request.form.get("tipo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um arquivo.", "error")
            return redirect(url_for("admin.importacao"))

        sufixo = Path(arquivo.filename).suffix.lower()
        if sufixo not in current_app.config["UPLOAD_EXTENSOES"]:
            flash("Só são aceitos arquivos .xlsx ou .csv.", "error")
            return redirect(url_for("admin.importacao"))

        destino_dir = Path(current_app.instance_path) / "uploads"
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / secure_filename(arquivo.filename)
        arquivo.save(destino)

        try:
            relatorio = _executar_importer(tipo, destino)
        except Exception as exc:  # noqa: BLE001 — mostra erro amigável, loga o resto
            current_app.logger.exception("Falha na importação")
            flash(f"Falha na importação: {exc}", "error")
            return redirect(url_for("admin.importacao"))

        auditoria.registrar("import", tipo or "planilha", None,
                            depois={"arquivo": arquivo.filename})
        db.session.commit()
        for linha in relatorio:
            flash(linha, "success")
        return redirect(url_for("admin.importacao"))

    from app.importers.stone_adapter import StoneConfig
    from app.services import google_sync

    stone_config = StoneConfig.from_app(current_app)
    return render_template(
        "admin/importacao.html",
        stone_configurada=stone_config.configurada,
        google_configurado=google_sync.configurado(current_app),
        google_ativo=current_app.config.get("GOOGLE_SYNC_ENABLED"),
        google_intervalo=current_app.config.get("GOOGLE_SYNC_INTERVAL_MIN", 15),
        google_ultima_sync=google_sync.ultima_sincronizacao(),
    )


@bp.route("/google/sincronizar", methods=["POST"])
@role_required("consultoria")
def google_sincronizar():
    """Botão 'Sincronizar agora': puxa a planilha do Google e reimporta o fluxo."""
    from app.services import google_sync

    try:
        rel = google_sync.sincronizar_fluxo(current_app, forcar=True)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na sincronização Google")
        flash(f"Falha na sincronização com o Google: {exc}", "error")
        return redirect(url_for("admin.importacao"))

    if not rel.get("ok"):
        flash(rel["relatorio"][0], "warning")
    else:
        flash("Fluxo sincronizado com a planilha do Google.", "success")
        for linha in rel.get("relatorio", []):
            flash(linha, "success")
    return redirect(url_for("admin.importacao"))


@bp.route("/stone/csv", methods=["POST"])
@role_required("consultoria")
def stone_csv():
    """Importa um CSV de conciliação Stone exportado (upload manual)."""
    from flask_login import current_user

    from app.services import stone_import

    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        flash("Selecione o arquivo CSV da conciliação Stone.", "error")
        return redirect(url_for("admin.importacao"))
    if Path(arquivo.filename).suffix.lower() != ".csv":
        flash("O arquivo de conciliação Stone deve ser .csv.", "error")
        return redirect(url_for("admin.importacao"))

    try:
        rel = stone_import.importar_arquivo_csv(arquivo.read(), usuario_id=current_user.id)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na importação Stone (CSV)")
        flash(f"Falha ao importar conciliação Stone: {exc}", "error")
        return redirect(url_for("admin.importacao"))

    db.session.commit()
    flash(
        f"Stone: {rel['inseridos']} novos, {rel['atualizados']} atualizados "
        f"({rel['lancamentos_gerados']} de {rel['transacoes_recebidas']} transações).",
        "success",
    )
    if rel["ignorados_sem_categoria"]:
        flash(f"{rel['ignorados_sem_categoria']} transações sem categoria correspondente.", "warning")
    return redirect(url_for("admin.importacao"))


@bp.route("/stone/sincronizar", methods=["POST"])
@role_required("consultoria")
def stone_sincronizar():
    """Baixa a conciliação Stone de um período pela API (exige credenciais)."""
    from datetime import date

    from flask_login import current_user

    from app.services import stone_import

    inicio = request.form.get("inicio")
    fim = request.form.get("fim")
    if not inicio or not fim:
        flash("Informe início e fim do período.", "error")
        return redirect(url_for("admin.importacao"))

    try:
        rel = stone_import.importar_periodo(
            date.fromisoformat(inicio), date.fromisoformat(fim), usuario_id=current_user.id
        )
    except (RuntimeError, NotImplementedError) as exc:
        flash(str(exc), "warning")
        return redirect(url_for("admin.importacao"))
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na sincronização Stone")
        flash(f"Falha na sincronização Stone: {exc}", "error")
        return redirect(url_for("admin.importacao"))

    db.session.commit()
    flash(f"Stone: {rel['inseridos']} novos, {rel['atualizados']} atualizados.", "success")
    return redirect(url_for("admin.importacao"))


def _executar_importer(tipo: str, caminho: Path) -> list[str]:
    from app.importers import fluxo_importer, gorjetas_importer, metas_importer

    if tipo == "fluxo":
        return fluxo_importer.importar(caminho)
    if tipo == "gorjetas":
        return gorjetas_importer.importar(caminho)
    if tipo == "metas":
        return metas_importer.importar(caminho)
    raise ValueError("Tipo de importação desconhecido.")
