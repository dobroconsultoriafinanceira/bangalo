# -*- coding: utf-8 -*-
"""Conciliação bancária Itaú: extrato classificado (visão CFO) × fluxo."""
from datetime import date

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models.banco import MovimentoBancario
from app.models.fluxo import Fornecedor
from app.services import classificacao_bancaria as cls
from app.services import fluxo_caixa
from app.services import itau_sync
from app.utils import periodo as periodo_global
from app.utils.datas import hoje_sp, primeiro_dia_mes, ultimo_dia_mes
from app.utils.filtros import format_brl
from app.utils.permissoes import requer

bp = Blueprint("conciliacao", __name__)

STATUS = {
    "todos": "Todos",
    "classificar": "A classificar (tudo que ainda não foi revisado)",
    "revisar": "Sem classificação (precisa de você)",
    "pendentes": "Pendentes de conciliação",
    "conciliados": "Conciliados",
}

# classificar/revisar é o trabalho do dia a dia; sincronizar fica com a consultoria


def _data(texto: str | None, padrao: date) -> date:
    try:
        return date.fromisoformat(texto) if texto else padrao
    except ValueError:
        return padrao


def _filtros(origem) -> dict:
    hoje = hoje_sp()
    ano, mes = periodo_global.atual()
    inicio = _data(origem.get("inicio"), primeiro_dia_mes(ano, mes))
    fim = _data(origem.get("fim"), min(hoje, ultimo_dia_mes(ano, mes)) if (ano, mes) <= (hoje.year, hoje.month)
                else ultimo_dia_mes(ano, mes))
    if fim < inicio:
        inicio, fim = fim, inicio
    status = origem.get("status") if origem.get("status") in STATUS else "todos"
    bloco = origem.get("bloco") if origem.get("bloco") in cls.BLOCOS else ""
    busca = (origem.get("q") or "").strip()[:80]
    return {"inicio": inicio, "fim": fim, "status": status, "bloco": bloco, "q": busca}


def _url_tela(f: dict, **extra) -> str:
    return url_for("conciliacao.itau", inicio=f["inicio"].isoformat(), fim=f["fim"].isoformat(),
                   status=f["status"], bloco=f["bloco"] or None, q=f["q"] or None, **extra)


@bp.route("/itau")
@login_required
def itau():
    f = _filtros(request.args)
    return render_template(
        "conciliacao/itau.html",
        **f,
        status_opcoes=STATUS,
        blocos=cls.BLOCOS,
        categorias=cls.CATEGORIAS,
        categorias_por_bloco=cls.categorias_por_bloco(),
        linhas_do_caixa=fluxo_caixa.linhas_da_planilha(),
        grupo_compras=fluxo_caixa.GRUPO_COMPRAS,
        fornecedores=db.session.execute(
            db.select(Fornecedor).filter_by(ativo=True).order_by(Fornecedor.nome)
        ).scalars().all(),
        fora_conciliacao=itau_sync.BLOCOS_FORA_CONCILIACAO,
        configurado=itau_sync.configurado(),
        pode_classificar=current_user.pode("movimentos.classificar"),
        saldo=itau_sync.ultimo_saldo(),
        ultima_sync=itau_sync.ultima_sincronizacao(),
        demo=itau_sync.demonstrativo(f["inicio"], f["fim"]),
        resumo=itau_sync.resumo(f["inicio"], f["fim"]),
        contagens=itau_sync.contagens(f["inicio"], f["fim"], f["q"]),
        movimentos=(movs := itau_sync.movimentos(f["inicio"], f["fim"], f["status"],
                                                 f["bloco"], f["q"])),
        regra_de=itau_sync.regras_dos_movimentos(movs),
        derivados=itau_sync.linhas_derivadas(movs),
        itens_do_movimento=itau_sync.itens_conciliados(movs),
        abrir_revisao=request.args.get("revisar", type=int),
        fornecedor_retorno=request.args.get("fornecedor", type=int),
        linha_retorno=request.args.get("linha", type=int),
        sem_banco=itau_sync.lancamentos_sem_banco(f["inicio"], f["fim"]),
    )


@bp.route("/itau/sincronizar", methods=["POST"])
@requer("movimentos.sincronizar")
def itau_sincronizar():
    f = _filtros(request.form)
    try:
        rel = itau_sync.sincronizar(f["inicio"])
    except (RuntimeError, ValueError) as exc:  # inclui ItauErroAPI
        db.session.rollback()
        flash(f"Itaú: {exc}", "error")
        return redirect(_url_tela(f))
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Falha na sincronização Itaú")
        flash(f"Falha na sincronização Itaú: {exc}", "error")
        return redirect(_url_tela(f))

    db.session.commit()
    flash(
        f"Itaú sincronizado desde {f['inicio']:%d/%m/%Y}: {rel['inseridos']} novos, "
        f"{rel['atualizados']} atualizados, {rel['conciliacao']['conciliados']} conciliados automaticamente.",
        "success",
    )
    return redirect(_url_tela(f))


@bp.route("/itau/revisar", methods=["POST"])
@requer("movimentos.classificar")
def revisar():
    """Marca como revisado tudo o que está no filtro atual (ou um movimento)."""
    f = _filtros(request.form)
    mov_id = request.form.get("mov_id", type=int)
    if mov_id:
        alvos = [db.session.get(MovimentoBancario, mov_id) or abort(404)]
    else:
        alvos = itau_sync.movimentos(f["inicio"], f["fim"], f["status"], f["bloco"], f["q"])
    total = itau_sync.marcar_revisados(alvos)
    db.session.commit()
    flash(f"{total} movimento(s) marcados como revisados.", "success")
    return redirect(_url_tela(f))


@bp.route("/itau/movimentos/<int:mov_id>/revisao", methods=["POST"])
@requer("movimentos.classificar")
def revisar_movimento(mov_id: int):
    """Painel "Revisar movimento": categoria + alcance + revisado, numa transação."""
    movimento = db.session.get(MovimentoBancario, mov_id) or abort(404)
    f = _filtros(request.form)
    criar_regra = request.form.get("alcance") == "regra"
    fornecedor_id = request.form.get("fornecedor_id", type=int)
    # no repasse da Stone a gaveta não pergunta categoria: ela edita quais linhas
    # do caixa formam o crédito, e isso vem junto na mesma confirmação
    linhas = request.form.getlist("lancamento_ids")
    try:
        alterados = itau_sync.revisar_movimento(
            movimento, request.form.get("categoria", ""), criar_regra,
            fornecedor_id=fornecedor_id, categoria_id=request.form.get("categoria_id", type=int))
        if request.form.get("editar_linhas") == "1":
            itau_sync.conciliar_manual(movimento, [int(i) for i in linhas],
                                       usuario_id=current_user.id)
    except ValueError as exc:
        db.session.rollback()
        flash(f"Revisão não salva: {exc}", "error")
        return redirect(_url_tela(f, revisar=mov_id))
    db.session.commit()
    nome = movimento.contraparte or movimento.descricao or "Movimento"
    if criar_regra:
        flash(f"{nome}: regra salva — {alterados} movimento(s) desta contraparte ficaram "
              f"classificados e revisados, inclusive os anteriores.", "success")
    else:
        flash(f"{nome}: revisado.", "success")
    return redirect(_url_tela(f))


@bp.route("/itau/movimentos/<int:mov_id>/conciliacao")
@requer("movimentos.classificar")
def conciliacao_candidatos(mov_id: int):
    """Lançamentos do fluxo que podem compor este movimento (painel de conciliação)."""
    movimento = db.session.get(MovimentoBancario, mov_id) or abort(404)
    candidatos = itau_sync.candidatos_conciliacao(movimento)
    ligados = {i.lancamento_id for i in movimento.itens_conciliacao}
    return jsonify({
        "movimento": {
            "id": movimento.id,
            "data": movimento.data.isoformat(),
            "valor": str(movimento.valor),
            "tipo": movimento.tipo,
            "nome": movimento.contraparte or movimento.descricao or "Movimento",
        },
        "ligados": sorted(ligados),
        "sugestao": itau_sync.sugerir_combinacao(movimento, candidatos) if not ligados else [],
        "candidatos": [{
            "id": l.id,
            "data": l.data.isoformat(),
            "data_br": l.data.strftime("%d/%m"),
            "valor": str(l.valor),
            "categoria": l.categoria.nome if l.categoria else "—",
            "descricao": l.descricao or (l.fornecedor.nome if l.fornecedor else ""),
            "ligado": l.id in ligados,
        } for l in candidatos],
    })


@bp.route("/itau/movimentos/<int:mov_id>/conciliacao", methods=["POST"])
@requer("movimentos.classificar")
def conciliar_movimento(mov_id: int):
    """Concilia o movimento com um ou vários lançamentos do fluxo."""
    movimento = db.session.get(MovimentoBancario, mov_id) or abort(404)
    f = _filtros(request.form)
    ids = sorted({i for i in request.form.getlist("lancamento_ids", type=int) if i})
    nome = movimento.contraparte or movimento.descricao or "Movimento"
    try:
        rel = itau_sync.conciliar_manual(movimento, ids, usuario_id=current_user.id)
    except ValueError as exc:
        db.session.rollback()
        flash(f"Conciliação não salva: {exc}", "error")
        return redirect(_url_tela(f, revisar=mov_id))
    db.session.commit()
    if not rel["lancamentos"]:
        flash(f"{nome}: conciliação desfeita.", "success")
    else:
        texto = f"{nome}: conciliado com {rel['lancamentos']} lançamento(s)"
        if rel["diferenca"]:
            flash(f"{texto} — diferença de {format_brl(rel['diferenca'])} em relação ao extrato.", "warning")
        else:
            flash(f"{texto}, fechando o valor do extrato.", "success")
    return redirect(_url_tela(f))


@bp.route("/itau/revisar-lote", methods=["POST"])
@requer("movimentos.classificar")
def revisar_lote():
    """Marca como revisados EXATAMENTE os movimentos selecionados (por id)."""
    f = _filtros(request.form)
    ids = sorted({i for i in request.form.getlist("mov_ids", type=int) if i})
    if not ids:
        flash("Selecione ao menos um movimento para revisar.", "warning")
        return redirect(_url_tela(f))
    alvos = db.session.execute(
        db.select(MovimentoBancario).filter(MovimentoBancario.id.in_(ids))
    ).scalars().all()
    if len(alvos) != len(ids):
        flash("Alguns movimentos selecionados não existem mais. Nada foi revisado; atualize a lista.", "error")
        return redirect(_url_tela(f))
    total = itau_sync.marcar_revisados(alvos)
    db.session.commit()
    ja = len(alvos) - total
    flash(f"{total} movimento(s) revisado(s)." + (f" {ja} já estavam revisados." if ja else ""), "success")
    return redirect(_url_tela(f))


@bp.route("/itau/movimentos/<int:mov_id>/classificar", methods=["POST"])
@requer("movimentos.classificar")
def classificar(mov_id: int):
    movimento = db.session.get(MovimentoBancario, mov_id) or abort(404)
    f = _filtros(request.form)
    try:
        alterados = itau_sync.reclassificar(
            movimento, request.form.get("categoria", ""),
            para_contraparte=bool(request.form.get("contraparte")),
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(_url_tela(f))
    db.session.commit()
    flash(f"Classificação salva — {alterados} movimento(s) atualizado(s).", "success")
    return redirect(_url_tela(f))


@bp.route("/itau/movimentos/<int:mov_id>/desfazer", methods=["POST"])
@requer("movimentos.classificar")
def desfazer(mov_id: int):
    movimento = db.session.get(MovimentoBancario, mov_id) or abort(404)
    itau_sync.desfazer_conciliacao(movimento)
    db.session.commit()
    flash("Conciliação desfeita — o movimento voltou para pendente.", "success")
    return redirect(_url_tela(_filtros(request.form)))
