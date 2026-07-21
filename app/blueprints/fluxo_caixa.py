# -*- coding: utf-8 -*-
"""Fluxo de caixa: visão mensal, lançamentos (CRUD), filtros e paginação."""
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
from app.utils.datas import hoje_sp

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
    ano = request.args.get("ano", type=int, default=hoje.year)
    mes = request.args.get("mes", type=int, default=hoje.month)
    if not 1 <= mes <= 12:
        abort(400)
    visao = srv.visao_mensal(ano, mes)
    categorias = db.session.execute(
        db.select(Categoria).filter_by(ativo=True).order_by(Categoria.grupo, Categoria.ordem, Categoria.nome)
    ).scalars().all()
    fornecedores = db.session.execute(
        db.select(Fornecedor).filter_by(ativo=True).order_by(Fornecedor.nome)
    ).scalars().all()
    return render_template(
        "fluxo/visao_mensal.html",
        visao=visao, ano=ano, mes=mes,
        categorias=categorias, fornecedores=fornecedores,
        formas_pagamento=FORMAS_PAGAMENTO,
    )


@bp.route("/lancamentos")
@login_required
def lancamentos():
    pagina = request.args.get("pagina", type=int, default=1)
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")
    categoria_id = request.args.get("categoria_id", type=int)
    fornecedor_id = request.args.get("fornecedor_id", type=int)
    grupo = request.args.get("grupo")

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
                 "fornecedor_id": fornecedor_id, "grupo": grupo},
    )


@bp.route("/lancamentos/novo", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def excluir_lancamento(lanc_id: int):
    lanc = db.session.get(Lancamento, lanc_id) or abort(404)
    auditoria.registrar("delete", "lancamento", lanc.id, antes=_dados_lancamento(lanc))
    db.session.delete(lanc)
    db.session.commit()
    flash("Lançamento excluído.", "success")
    return redirect(request.referrer or url_for("fluxo_caixa.lancamentos"))
