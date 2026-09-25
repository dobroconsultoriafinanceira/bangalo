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
from flask_login import current_user
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.auditoria import LogAuditoria
from app.models.banco import RegraClassificacaoBancaria
from app.models.usuario import Usuario
from app.services import auditoria, regras_bancarias
from app.services import classificacao_bancaria as cls
from app.blueprints.auth import iniciar_sessao
from app.utils import permissoes as perm
from app.utils import seguranca
from app.utils.filtros import format_brl
from app.utils.permissoes import requer

bp = Blueprint("admin", __name__)


@bp.route("/usuarios")
@requer("admin.acessar")
def usuarios():
    itens = db.session.execute(db.select(Usuario).order_by(Usuario.ativo.desc(), Usuario.nome)).scalars().all()
    return render_template(
        "admin/usuarios.html", usuarios=itens, papeis=perm.PAPEIS, permissoes=perm.PERMISSOES,
        so_admin=perm.SO_ADMIN, senha_minima=seguranca.SENHA_MINIMA,
        padroes={p: sorted(d["padrao"]) for p, d in perm.PAPEIS.items()},
    )


def _admins_ativos(exceto_id: int | None = None) -> int:
    query = db.select(db.func.count(Usuario.id)).filter(Usuario.role == "consultoria", Usuario.ativo.is_(True))
    if exceto_id:
        query = query.filter(Usuario.id != exceto_id)
    return db.session.execute(query).scalar_one()


def _dados_usuario(u: Usuario) -> dict:
    return {"nome": u.nome, "email": u.email, "role": u.role, "ativo": u.ativo,
            "permissoes": sorted(u.permissoes_efetivas)}


@bp.route("/usuarios/salvar", methods=["POST"])
@requer("admin.acessar")
def salvar_usuario():
    user_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()[:120]
    email = (request.form.get("email") or "").strip().lower()[:255]
    role = request.form.get("role")
    senha = request.form.get("senha") or ""
    ativo = "ativo" in request.form
    voltar = redirect(url_for("admin.usuarios"))

    if not nome or not email or role not in perm.PAPEIS:
        flash("Preencha nome, e-mail e papel.", "error")
        return voltar
    if "@" not in email or " " in email:
        flash("Informe um e-mail válido.", "error")
        return voltar
    duplicado = db.session.execute(
        db.select(Usuario).filter(Usuario.email == email, Usuario.id != (user_id or 0))
    ).scalar_one_or_none()
    if duplicado:
        flash("Já existe um usuário com esse e-mail.", "error")
        return voltar

    marcadas = {p for p in request.form.getlist("permissoes") if p in perm.PERMISSOES and p not in perm.SO_ADMIN}
    if role == "consultoria" or marcadas == set(perm.PAPEIS[role]["padrao"]):
        personalizadas = None
    else:
        personalizadas = sorted(marcadas)

    if user_id:
        usuario = db.session.get(Usuario, user_id) or abort(404)
        antes = _dados_usuario(usuario)
        # proteções: ninguém se tranca para fora e sempre sobra um administrador total
        if usuario.id == current_user.id and (role != "consultoria" or not ativo):
            flash("Você não pode tirar o seu próprio acesso de administrador nem se desativar.", "error")
            return voltar
        if usuario.role == "consultoria" and usuario.ativo and (role != "consultoria" or not ativo) \
                and _admins_ativos(exceto_id=usuario.id) == 0:
            flash("Precisa existir pelo menos um administrador total ativo.", "error")
            return voltar
    else:
        if not senha:
            flash("Defina uma senha inicial para o novo usuário.", "error")
            return voltar
        usuario = Usuario(sessao_versao=1)
        antes = None
        db.session.add(usuario)

    if senha:
        problema = seguranca.problema_na_senha(senha, nome=nome, email=email)
        if problema:
            db.session.rollback()
            flash(problema, "error")
            return voltar

    mudou_acesso = antes is not None and (
        antes["role"] != role or antes["ativo"] != ativo or antes["email"] != email
        or set(antes["permissoes"]) != set(perm.efetivas(role, personalizadas))
    )
    usuario.nome, usuario.email, usuario.role, usuario.ativo = nome, email, role, ativo
    usuario.permissoes = personalizadas
    if senha:
        usuario.definir_senha(senha)  # também encerra as sessões abertas
    elif mudou_acesso:
        usuario.encerrar_sessoes()  # permissões novas valem já, sem esperar novo login

    db.session.flush()
    depois = _dados_usuario(usuario)
    if senha:
        depois["senha"] = "definida pelo administrador"
    auditoria.registrar("update" if antes else "create", "usuario", usuario.id, antes=antes, depois=depois)
    db.session.commit()
    if usuario.id == current_user.id and (senha or mudou_acesso):
        iniciar_sessao(usuario)  # quem se editou continua logado
    flash("Usuário salvo." + (" As sessões abertas dele foram encerradas." if antes and (senha or mudou_acesso)
                              and usuario.id != current_user.id else ""), "success")
    return voltar


@bp.route("/usuarios/<int:user_id>/encerrar-sessoes", methods=["POST"])
@requer("admin.acessar")
def encerrar_sessoes(user_id: int):
    """Derruba todas as sessões do usuário (ex.: celular perdido)."""
    usuario = db.session.get(Usuario, user_id) or abort(404)
    usuario.encerrar_sessoes()
    auditoria.registrar("sessoes_encerradas", "usuario", usuario.id)
    db.session.commit()
    if usuario.id == current_user.id:
        iniciar_sessao(usuario)
        flash("Suas sessões em outros aparelhos foram encerradas.", "success")
    else:
        flash(f"Sessões de {usuario.nome} encerradas. Será preciso entrar de novo.", "success")
    return redirect(url_for("admin.usuarios"))


@bp.route("/auditoria")
@requer("admin.acessar")
def logs_auditoria():
    pagina = request.args.get("pagina", type=int, default=1)
    entidade = request.args.get("entidade") or None
    acao = request.args.get("acao") or None
    usuario_id = request.args.get("usuario_id", type=int)
    query = db.select(LogAuditoria).order_by(LogAuditoria.timestamp.desc())
    if entidade:
        query = query.filter(LogAuditoria.entidade == entidade)
    if acao:
        query = query.filter(LogAuditoria.acao == acao)
    if usuario_id:
        query = query.filter(LogAuditoria.usuario_id == usuario_id)
    paginacao = db.paginate(query, page=pagina, per_page=50, error_out=False)
    entidades = sorted(e[0] for e in db.session.query(LogAuditoria.entidade).distinct().all())
    acoes = sorted(a[0] for a in db.session.query(LogAuditoria.acao).distinct().all())
    usuarios = db.session.execute(db.select(Usuario).order_by(Usuario.nome)).scalars().all()
    return render_template(
        "admin/auditoria.html", paginacao=paginacao, entidades=entidades, entidade=entidade,
        acoes=acoes, acao=acao, usuarios=usuarios, usuario_id=usuario_id,
    )


@bp.route("/importacao", methods=["GET", "POST"])
@requer("admin.acessar")
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
@requer("admin.acessar")
def google_sincronizar():
    """Botão 'Sincronizar agora': puxa a planilha do Google e reimporta o fluxo."""
    from app.services import google_sync

    try:
        rel = google_sync.sincronizar_fluxo(current_app, forcar=True)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na sincronização Google")
        flash(f"Falha na sincronização com o Google: {exc}", "error")
        return redirect(url_for("admin.integracoes"))

    if not rel.get("ok"):
        flash(rel["relatorio"][0], "warning")
    else:
        flash("Fluxo sincronizado com a planilha do Google.", "success")
        for linha in rel.get("relatorio", []):
            flash(linha, "success")
    return redirect(url_for("admin.integracoes"))


@bp.route("/stone/csv", methods=["POST"])
@requer("admin.acessar")
def stone_csv():
    """Importa um CSV de conciliação Stone exportado (upload manual)."""
    from flask_login import current_user

    from app.services import stone_import

    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        flash("Selecione o arquivo CSV da conciliação Stone.", "error")
        return redirect(url_for("admin.integracoes"))
    if Path(arquivo.filename).suffix.lower() != ".csv":
        flash("O arquivo de conciliação Stone deve ser .csv.", "error")
        return redirect(url_for("admin.integracoes"))

    try:
        rel = stone_import.importar_arquivo_csv(arquivo.read(), usuario_id=current_user.id)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na importação Stone (CSV)")
        flash(f"Falha ao importar conciliação Stone: {exc}", "error")
        return redirect(url_for("admin.integracoes"))

    db.session.commit()
    flash(
        f"Stone: {rel['inseridos']} novos, {rel['atualizados']} atualizados "
        f"({rel['lancamentos_gerados']} de {rel['transacoes_recebidas']} transações).",
        "success",
    )
    if rel["ignorados_sem_categoria"]:
        flash(f"{rel['ignorados_sem_categoria']} transações sem categoria correspondente.", "warning")
    return redirect(url_for("admin.integracoes"))


@bp.route("/stone/importar", methods=["POST"])
@requer("admin.acessar")
def stone_importar():
    """Traz as entradas de cartão do período pelos repasses da Stone (fonte oficial)."""
    from datetime import date

    from flask_login import current_user

    from app.services import stone_import

    try:
        i = date.fromisoformat(request.form.get("inicio") or "")
        f = date.fromisoformat(request.form.get("fim") or "")
    except ValueError:
        flash("Informe início e fim do período.", "error")
        return redirect(url_for("admin.integracoes"))
    if f < i or (f - i).days > 62:
        flash("Importe no máximo dois meses por vez.", "error")
        return redirect(url_for("admin.integracoes"))

    try:
        rel = stone_import.importar_repasses(i, f, usuario_id=current_user.id)
    except RuntimeError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("admin.integracoes"))
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Falha ao importar repasses Stone")
        flash(f"Falha ao importar da Stone: {exc}", "error")
        return redirect(url_for("admin.integracoes"))

    db.session.commit()
    flash(f"Stone: {rel['inseridos']} entradas novas, {rel['atualizados']} atualizadas "
          f"({format_brl(rel['total'])}). {rel['removidos']} linha(s) de cartão da planilha foram "
          f"substituídas no período.", "success")
    if rel["erros"]:
        flash(f"{len(rel['erros'])} dia(s) não puderam ser consultados na Stone.", "warning")
    if rel["sem_categoria"]:
        flash("Sem categoria no plano de contas: " + ", ".join(rel["sem_categoria"]), "warning")
    return redirect(url_for("admin.integracoes"))


@bp.route("/stone/agenda", methods=["POST"])
@requer("admin.acessar")
def stone_agenda():
    """Atualiza a previsão de recebíveis de cartão pela agenda da Stone."""
    from flask_login import current_user

    from app.services import stone_import
    from app.utils.datas import hoje_sp

    try:
        rel = stone_import.importar_agenda(hoje_sp(), usuario_id=current_user.id)
    except RuntimeError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("admin.integracoes"))
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Falha ao atualizar a agenda da Stone")
        flash(f"Falha ao falar com a Stone: {exc}", "error")
        return redirect(url_for("admin.integracoes"))

    db.session.commit()
    flash(f"Agenda da Stone atualizada: {format_brl(rel['total'])} a receber em {rel['dias']} "
          f"data(s)." + (f" {rel['removidos_da_planilha']} previsão(ões) da planilha substituída(s)."
                         if rel["removidos_da_planilha"] else ""), "success")
    return redirect(url_for("admin.integracoes"))


@bp.route("/stone/conferir", methods=["POST"])
@requer("admin.acessar")
def stone_conferir():
    """Conferência Stone × fluxo do período (só leitura — não cria lançamentos).

    As linhas de cartão já entram no fluxo pela planilha; importar os repasses
    da Stone contaria o mesmo dinheiro duas vezes. Aqui a API serve para achar
    diferença entre o que a Stone depositou e o que foi lançado.
    """
    from datetime import date

    from app.services import stone_import

    inicio = request.form.get("inicio")
    fim = request.form.get("fim")
    if not inicio or not fim:
        flash("Informe início e fim do período.", "error")
        return redirect(url_for("admin.integracoes"))
    try:
        i, f = date.fromisoformat(inicio), date.fromisoformat(fim)
    except ValueError:
        flash("Datas inválidas.", "error")
        return redirect(url_for("admin.integracoes"))
    if (f - i).days > 62 or f < i:
        flash("Confira no máximo dois meses por vez.", "error")
        return redirect(url_for("admin.integracoes"))

    try:
        rel = stone_import.conferir_periodo(i, f)
    except RuntimeError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("admin.integracoes"))
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Falha na conferência Stone")
        flash(f"Falha ao falar com a Stone: {exc}", "error")
        return redirect(url_for("admin.integracoes"))

    auditoria.registrar("consulta", "stone", None, depois={"inicio": inicio, "fim": fim,
                                                           "linhas": len(rel["linhas"])})
    db.session.commit()
    return render_template("admin/stone_conferencia.html", rel=rel, inicio=i, fim=f)


def _executar_importer(tipo: str, caminho: Path) -> list[str]:
    from app.importers import fluxo_importer, gorjetas_importer, metas_importer

    if tipo == "fluxo":
        return fluxo_importer.importar(caminho)
    if tipo == "gorjetas":
        return gorjetas_importer.importar(caminho)
    if tipo == "metas":
        return metas_importer.importar(caminho)
    raise ValueError("Tipo de importação desconhecido.")


# ---------------------------- Integrações ----------------------------

@bp.route("/integracoes")
@requer("admin.acessar")
def integracoes():
    """Status real de cada integração: configuração, rotina e último sucesso."""
    from sqlalchemy import func

    from app.importers.stone_adapter import StoneConfig
    from app.models.fluxo import Lancamento
    from app.services import google_sync, itau_sync

    cfg = current_app.config
    stone_cfg = StoneConfig.from_app(current_app)
    stone_ultimo = db.session.execute(
        db.select(func.max(Lancamento.data), func.count(Lancamento.id)).filter(Lancamento.origem == "stone")
    ).one()
    return render_template(
        "admin/integracoes.html",
        itau={
            "configurado": itau_sync.configurado(),
            "automatico": cfg.get("ITAU_SYNC_ENABLED"),
            "intervalo": cfg.get("ITAU_SYNC_INTERVAL_MIN", 60),
            "janela": cfg.get("ITAU_SYNC_DIAS", 35),
            "ambiente": cfg.get("ITAU_AMBIENTE"),
            "contas": len([c for c in (cfg.get("ITAU_CONTAS") or "").split(",") if c.strip()]),
            "ultima": itau_sync.ultima_sincronizacao(),
            "saldo": itau_sync.ultimo_saldo(),
        },
        google={
            "configurado": google_sync.configurado(current_app),
            "automatico": cfg.get("GOOGLE_SYNC_ENABLED"),
            "intervalo": cfg.get("GOOGLE_SYNC_INTERVAL_MIN", 15),
            "ultima": google_sync.ultima_sincronizacao(),
        },
        stone={
            "configurado": stone_cfg.configurada,
            "faltando": stone_cfg.faltando,
            "recebidas": [nome for nome, valor in (
                ("STONE_BASE_URL", stone_cfg.base_url),
                ("STONE_SECRET_KEY", stone_cfg.secret_key),
                ("STONE_CODES", ", ".join(stone_cfg.stone_codes)),
            ) if valor],
            "ultimo_dia": stone_ultimo[0],
            "lancamentos": stone_ultimo[1],
        },
    )


# ---------------------------- Regras bancárias ----------------------------

def _dados_regra(form) -> dict:
    return {
        "campo": form.get("campo", ""), "valor": form.get("valor", ""), "tipo": form.get("tipo", ""),
        "categoria": form.get("categoria", ""), "ativo": form.get("ativo") in ("1", "on", "true"),
        "revisar": form.get("revisar") in ("1", "on", "true"), "observacao": form.get("observacao", ""),
        "regra_id": form.get("id", type=int),
    }


def _tela_regras(**extra):
    return render_template(
        "admin/regras.html",
        regras=regras_bancarias.listar(),
        campos=regras_bancarias.ROTULOS_CAMPO,
        tipos=regras_bancarias.TIPOS,
        categorias_por_bloco=cls.categorias_por_bloco(),
        **extra,
    )


@bp.route("/regras")
@requer("admin.acessar")
def regras():
    return _tela_regras()


@bp.route("/regras/previa", methods=["POST"])
@requer("admin.acessar")
def regras_previa():
    """Mostra o alcance antes de gravar (não altera nada)."""
    dados = _dados_regra(request.form)
    try:
        campo, valor, tipo, categoria = regras_bancarias.validar(
            dados["campo"], dados["valor"], dados["tipo"], dados["categoria"])
    except ValueError as exc:
        flash(str(exc), "error")
        return _tela_regras(rascunho=dados, abrir="dlg-regra")
    previa = regras_bancarias.previa(campo, valor, tipo, categoria, dados["regra_id"]) if dados["ativo"] else None
    dados.update(valor=valor)
    return _tela_regras(rascunho=dados, previa=previa, categoria_nome=cls.CATEGORIAS[categoria].nome,
                        abrir="dlg-alcance")


@bp.route("/regras/salvar", methods=["POST"])
@requer("admin.acessar")
def regras_salvar():
    dados = _dados_regra(request.form)
    try:
        regra, alterados = regras_bancarias.salvar(
            dados["campo"], dados["valor"], dados["tipo"], dados["categoria"], regra_id=dados["regra_id"],
            ativo=dados["ativo"], revisar=dados["revisar"], observacao=dados["observacao"])
    except LookupError:
        abort(404)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return _tela_regras(rascunho=dados, abrir="dlg-regra")
    db.session.commit()
    flash(f"Regra salva — {alterados} movimento(s) reclassificado(s).", "success")
    return redirect(url_for("admin.regras"))


@bp.route("/regras/<int:regra_id>/excluir", methods=["POST"])
@requer("admin.acessar")
def regras_excluir(regra_id: int):
    regra = db.session.get(RegraClassificacaoBancaria, regra_id) or abort(404)
    alterados = regras_bancarias.excluir(regra)
    db.session.commit()
    flash(f"Regra excluída — {alterados} movimento(s) voltaram à classificação padrão.", "success")
    return redirect(url_for("admin.regras"))
