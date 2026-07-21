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

    return render_template("admin/importacao.html")


def _executar_importer(tipo: str, caminho: Path) -> list[str]:
    from app.importers import fluxo_importer, gorjetas_importer, metas_importer

    if tipo == "fluxo":
        return fluxo_importer.importar(caminho)
    if tipo == "gorjetas":
        return gorjetas_importer.importar(caminho)
    if tipo == "metas":
        return metas_importer.importar(caminho)
    raise ValueError("Tipo de importação desconhecido.")
