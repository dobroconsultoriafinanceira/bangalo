# -*- coding: utf-8 -*-
"""Fluxo de caixa: visão mensal, lançamentos (CRUD), filtros e paginação."""
import calendar as _cal
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models.fluxo import FORMAS_PAGAMENTO, Categoria, Fornecedor, Lancamento
from app.services import auditoria
from app.services import fluxo_caixa as srv
from app.utils import periodo as periodo_global
from app.utils.datas import hoje_sp
from app.utils.permissoes import requer

bp = Blueprint("fluxo_caixa", __name__)


def _parse_valor(texto: str) -> Decimal | None:
    """Aceita '1.234,56' (BR) e '1234.56'."""
    texto = (texto or "").strip().replace("R$", "").strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _dados_lancamento(lanc: Lancamento) -> dict:
    return {
        "data": lanc.data,
        "categoria_id": lanc.categoria_id,
        "fornecedor_id": lanc.fornecedor_id,
        "forma_pagamento": lanc.forma_pagamento,
        "valor": lanc.valor,
        "descricao": lanc.descricao,
    }


@bp.route("/")
@login_required
def visao_mensal_view():
    hoje = hoje_sp()
    ano_padrao, mes_padrao = periodo_global.atual()
    ano = request.args.get("ano", type=int, default=ano_padrao)
    mes = request.args.get("mes", type=int, default=mes_padrao)
    if not 1 <= mes <= 12:
        abort(400)
    visao = srv.visao_mensal(ano, mes)
    categorias = db.session.execute(
        db.select(Categoria).filter_by(ativo=True).order_by(Categoria.grupo, Categoria.ordem, Categoria.nome)
    ).scalars().all()
    fornecedores = db.session.execute(
        db.select(Fornecedor).filter_by(ativo=True).order_by(Fornecedor.nome)
    ).scalars().all()
    ultimo_dia = _cal.monthrange(ano, mes)[1]
    # weekday: 0=Seg … 5=Sáb, 6=Dom
    weekdays = {d: date(ano, mes, d).weekday() for d in range(1, ultimo_dia + 1)}
    aba = "grade" if request.args.get("aba") == "grade" else "resumo"
    return render_template(
        "fluxo/visao_mensal.html",
        visao=visao, ano=ano, mes=mes, aba=aba,
        categorias=categorias, fornecedores=fornecedores,
        formas_pagamento=FORMAS_PAGAMENTO,
        weekdays=weekdays,
        hoje=hoje,
        resumo=srv.resumo_do_mes(visao, ano, mes, hoje),
        categoria_dinheiro_id=_categoria_dinheiro_id(),
    )


def _categoria_dinheiro_id() -> int | None:
    """Categoria de entrada "Dinheiro" (atalho "Entrada em dinheiro")."""
    return db.session.execute(
        db.select(Categoria.id).filter_by(tipo="entrada", nome="Dinheiro", ativo=True)
    ).scalars().first()


@bp.route("/lancamentos")
@login_required
def lancamentos():
    pagina = request.args.get("pagina", type=int, default=1)
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")
    categoria_id = request.args.get("categoria_id", type=int)
    fornecedor_id = request.args.get("fornecedor_id", type=int)
    grupo = request.args.get("grupo")
    busca = (request.args.get("q") or "").strip()[:80]

    query = db.select(Lancamento).join(Categoria).order_by(
        Lancamento.data.desc(), Lancamento.id.desc()
    )
    if inicio:
        query = query.filter(Lancamento.data >= date.fromisoformat(inicio))
    if fim:
        query = query.filter(Lancamento.data <= date.fromisoformat(fim))
    if categoria_id:
        query = query.filter(Lancamento.categoria_id == categoria_id)
    if fornecedor_id:
        query = query.filter(Lancamento.fornecedor_id == fornecedor_id)
    if grupo:
        query = query.filter(Categoria.grupo == grupo)
    if busca:
        termo = f"%{busca}%"
        query = query.outerjoin(Fornecedor, Lancamento.fornecedor_id == Fornecedor.id).filter(
            db.or_(Lancamento.descricao.ilike(termo), Categoria.nome.ilike(termo),
                   Fornecedor.nome.ilike(termo), Fornecedor.apelido.ilike(termo))
        )

    paginacao = db.paginate(query, page=pagina, per_page=50, error_out=False)
    categorias = db.session.execute(
        db.select(Categoria).filter_by(ativo=True).order_by(Categoria.grupo, Categoria.ordem, Categoria.nome)
    ).scalars().all()
    fornecedores = db.session.execute(
        db.select(Fornecedor).filter_by(ativo=True).order_by(Fornecedor.nome)
    ).scalars().all()
    grupos = sorted({c.grupo for c in categorias})

    return render_template(
        "fluxo/lancamentos.html",
        paginacao=paginacao,
        categorias=categorias,
        fornecedores=fornecedores,
        grupos=grupos,
        formas_pagamento=FORMAS_PAGAMENTO,
        filtros={"inicio": inicio, "fim": fim, "categoria_id": categoria_id,
                 "fornecedor_id": fornecedor_id, "grupo": grupo, "q": busca or None},
        abrir_novo=request.args.get("novo"),
        categoria_dinheiro_id=_categoria_dinheiro_id(),
        hoje=hoje_sp(),
    )


@bp.route("/lancamentos/novo", methods=["POST"])
@requer("caixa.editar")
def criar_lancamento():
    valor = _parse_valor(request.form.get("valor", ""))
    categoria_id = request.form.get("categoria_id", type=int)
    data_txt = request.form.get("data")
    if not valor or valor <= 0 or not categoria_id or not data_txt:
        flash("Preencha data, categoria e um valor maior que zero.", "error")
        return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))

    lanc = Lancamento(
        data=date.fromisoformat(data_txt),
        categoria_id=categoria_id,
        fornecedor_id=request.form.get("fornecedor_id", type=int) or None,
        forma_pagamento=request.form.get("forma_pagamento") or None,
        valor=valor,
        descricao=(request.form.get("descricao") or "").strip() or None,
        usuario_id=current_user.id,
    )
    db.session.add(lanc)
    db.session.flush()
    auditoria.registrar("create", "lancamento", lanc.id, depois=_dados_lancamento(lanc))
    db.session.commit()
    flash("Lançamento registrado.", "success")
    return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))


@bp.route("/lancamentos/<int:lanc_id>/editar", methods=["POST"])
@requer("caixa.editar")
def editar_lancamento(lanc_id: int):
    lanc = db.session.get(Lancamento, lanc_id) or abort(404)
    antes = _dados_lancamento(lanc)

    valor = _parse_valor(request.form.get("valor", ""))
    if not valor or valor <= 0:
        flash("Valor inválido.", "error")
        return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))

    lanc.data = date.fromisoformat(request.form.get("data"))
    lanc.categoria_id = request.form.get("categoria_id", type=int)
    lanc.fornecedor_id = request.form.get("fornecedor_id", type=int) or None
    lanc.forma_pagamento = request.form.get("forma_pagamento") or None
    lanc.valor = valor
    lanc.descricao = (request.form.get("descricao") or "").strip() or None

    auditoria.registrar("update", "lancamento", lanc.id, antes=antes, depois=_dados_lancamento(lanc))
    db.session.commit()
    flash("Lançamento atualizado.", "success")
    return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))


@bp.route("/lancamentos/<int:lanc_id>/excluir", methods=["POST"])
@requer("caixa.editar")
def excluir_lancamento(lanc_id: int):
    lanc = db.session.get(Lancamento, lanc_id) or abort(404)
    auditoria.registrar("delete", "lancamento", lanc.id, antes=_dados_lancamento(lanc))
    db.session.delete(lanc)
    db.session.commit()
    flash("Lançamento excluído.", "success")
    return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))


# ---------------------------- despesas previstas ----------------------------

@bp.route("/previstas")
@login_required
def previstas():
    """O que já está combinado e ainda não saiu da conta."""
    from app.models.fluxo import Fornecedor
    from app.services import despesas_previstas as dp

    situacao = request.args.get("situacao", "prevista")
    itens = dp.listar(situacao)
    return render_template(
        "fluxo/previstas.html",
        itens=itens, situacao=situacao,
        avisos=dp.avisos(),
        total_aberto=dp.total_em_aberto(),
        categorias=db.session.execute(
            db.select(Categoria).filter_by(tipo="saida", ativo=True).order_by(Categoria.grupo, Categoria.nome)
        ).scalars().all(),
        fornecedores=db.session.execute(
            db.select(Fornecedor).filter_by(ativo=True).order_by(Fornecedor.nome)
        ).scalars().all(),
        hoje=hoje_sp(),
    )


@bp.route("/previstas/salvar", methods=["POST"])
@requer("caixa.editar")
def salvar_prevista():
    from app.models.previsao import DespesaPrevista
    from app.services import despesas_previstas as dp

    prev_id = request.form.get("id", type=int)
    despesa = db.session.get(DespesaPrevista, prev_id) if prev_id else None
    if prev_id and despesa is None:
        abort(404)
    try:
        data = date.fromisoformat(request.form.get("data_prevista") or "")
    except ValueError:
        flash("Informe a data prevista.", "error")
        return redirect(url_for("fluxo_caixa.previstas"))
    try:
        dp.salvar({
            "data_prevista": data,
            "categoria_id": request.form.get("categoria_id", type=int),
            "fornecedor_id": request.form.get("fornecedor_id", type=int),
            "descricao": request.form.get("descricao"),
            "valor": _parse_valor(request.form.get("valor", "")),
        }, despesa, usuario_id=current_user.id)
    except (ValueError, TypeError, KeyError) as exc:
        db.session.rollback()
        flash(f"Despesa prevista não salva: {exc}", "error")
        return redirect(url_for("fluxo_caixa.previstas"))
    db.session.commit()
    flash("Despesa prevista salva — já aparece na projeção do caixa.", "success")
    return redirect(url_for("fluxo_caixa.previstas"))


@bp.route("/previstas/<int:prev_id>/baixar", methods=["POST"])
@requer("caixa.editar")
def baixar_prevista(prev_id: int):
    from app.models.previsao import DespesaPrevista
    from app.services import despesas_previstas as dp

    despesa = db.session.get(DespesaPrevista, prev_id) or abort(404)
    dp.baixar(despesa)
    db.session.commit()
    flash("Despesa marcada como paga e retirada da projeção.", "success")
    return redirect(url_for("fluxo_caixa.previstas"))


@bp.route("/previstas/<int:prev_id>/cancelar", methods=["POST"])
@requer("caixa.editar")
def cancelar_prevista(prev_id: int):
    from app.models.previsao import DespesaPrevista
    from app.services import despesas_previstas as dp

    despesa = db.session.get(DespesaPrevista, prev_id) or abort(404)
    try:
        dp.cancelar(despesa)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("fluxo_caixa.previstas"))
    db.session.commit()
    flash("Despesa prevista cancelada.", "success")
    return redirect(url_for("fluxo_caixa.previstas"))


@bp.route("/previstas/avisos-vistos", methods=["POST"])
@requer("caixa.editar")
def avisos_vistos():
    from app.services import despesas_previstas as dp

    total = dp.marcar_avisos_vistos()
    db.session.commit()
    flash(f"{total} baixa(s) automática(s) conferida(s).", "success")
    return redirect(url_for("fluxo_caixa.previstas"))
