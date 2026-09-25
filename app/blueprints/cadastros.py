# -*- coding: utf-8 -*-
"""Cadastros: plano de contas, fornecedores, setores/funções/colaboradores."""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.extensions import db
from app.models.fluxo import TIPOS_CATEGORIA, Categoria, Fornecedor
from app.models.gorjetas import (
    REGISTROS_COLABORADOR,
    Colaborador,
    Funcao,
    Setor,
)
from app.services import auditoria
from app.utils.permissoes import requer
from app.utils.seguranca import destino_seguro

bp = Blueprint("cadastros", __name__)


def _decimal(texto: str, default: str = "0") -> Decimal:
    try:
        return Decimal((texto or default).replace(",", "."))
    except InvalidOperation:
        return Decimal(default)


# ---------- Plano de contas ----------

@bp.route("/categorias")
@login_required
def categorias():
    itens = db.session.execute(
        db.select(Categoria).order_by(Categoria.tipo, Categoria.grupo, Categoria.ordem, Categoria.nome)
    ).scalars().all()
    grupos: dict[str, list] = {}
    for c in itens:
        grupos.setdefault(f"{'Entradas' if c.tipo == 'entrada' else 'Saídas'} › {c.grupo}", []).append(c)
    return render_template("cadastros/categorias.html", grupos=grupos, tipos=TIPOS_CATEGORIA)


@bp.route("/categorias/salvar", methods=["POST"])
@requer("cadastros.editar")
def salvar_categoria():
    cat_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    tipo = request.form.get("tipo")
    grupo = (request.form.get("grupo") or "").strip()
    if not nome or tipo not in TIPOS_CATEGORIA or not grupo:
        flash("Preencha nome, tipo e grupo.", "error")
        return redirect(url_for("cadastros.categorias"))

    if cat_id:
        cat = db.session.get(Categoria, cat_id) or abort(404)
        auditoria.registrar("update", "categoria", cat.id, antes={"nome": cat.nome})
    else:
        cat = Categoria(ordem=0)
        db.session.add(cat)
    cat.nome, cat.tipo, cat.grupo = nome, tipo, grupo
    cat.subgrupo = (request.form.get("subgrupo") or "").strip() or None
    cat.ativo = "ativo" in request.form
    db.session.commit()
    flash("Categoria salva.", "success")
    return redirect(url_for("cadastros.categorias"))


# ---------- Fornecedores ----------

@bp.route("/fornecedores")
@login_required
def fornecedores():
    busca = (request.args.get("q") or "").strip()
    query = db.select(Fornecedor).order_by(Fornecedor.nome)
    if busca:
        query = query.filter(Fornecedor.nome.ilike(f"%{busca}%"))
    itens = db.session.execute(query).scalars().all()
    return render_template("cadastros/fornecedores.html", fornecedores=itens, busca=busca)


@bp.route("/fornecedores/salvar", methods=["POST"])
@requer("cadastros.editar")
def salvar_fornecedor():
    forn_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    # cadastro contextual (ex.: durante a revisão de um movimento) volta para a origem
    destino = destino_seguro(request.form.get("proximo"), url_for("cadastros.fornecedores"))
    if not nome:
        flash("Informe o nome do fornecedor.", "error")
        return redirect(destino)

    duplicado = db.session.execute(
        db.select(Fornecedor).filter(Fornecedor.nome.ilike(nome), Fornecedor.id != (forn_id or 0))
    ).scalar_one_or_none()
    if duplicado:
        flash(f"Já existe o fornecedor “{duplicado.nome}”. Edite o cadastro existente em vez de criar outro.", "error")
        return redirect(destino)

    if forn_id:
        forn = db.session.get(Fornecedor, forn_id) or abort(404)
        auditoria.registrar("update", "fornecedor", forn.id, antes={"nome": forn.nome})
    else:
        forn = Fornecedor()
        db.session.add(forn)
    forn.nome = nome
    forn.apelido = (request.form.get("apelido") or "").strip() or None
    forn.observacao = (request.form.get("observacao") or "").strip() or None
    forn.ativo = "ativo" in request.form
    db.session.commit()
    flash(f"Fornecedor “{forn.nome}” salvo.", "success")
    if "conciliacao" in destino:
        # voltou da revisão de um movimento: já deixa o fornecedor escolhido
        destino += ("&" if "?" in destino else "?") + f"fornecedor={forn.id}"
    return redirect(destino)


# ---------- Equipe (setores, funções, colaboradores) ----------

@bp.route("/equipe")
@login_required
def equipe():
    setores = db.session.execute(db.select(Setor).order_by(Setor.nome)).scalars().all()
    funcoes = db.session.execute(db.select(Funcao).order_by(Funcao.nome)).scalars().all()
    colaboradores = db.session.execute(
        db.select(Colaborador).order_by(Colaborador.nome)
    ).scalars().all()
    soma_percentuais = sum((Decimal(s.percentual_rateio) for s in setores if s.ativo), Decimal("0"))
    aba = request.args.get("aba") if request.args.get("aba") in ("colaboradores", "funcoes", "setores") else "colaboradores"
    busca = (request.args.get("q") or "").strip()
    if busca:
        termo = busca.casefold()
        colaboradores = [c for c in colaboradores if termo in c.nome.casefold()]
    return render_template(
        "cadastros/equipe.html",
        setores=setores, funcoes=funcoes, colaboradores=colaboradores,
        registros=REGISTROS_COLABORADOR, soma_percentuais=soma_percentuais,
        aba=aba, busca=busca,
    )


@bp.route("/setores/salvar", methods=["POST"])
@requer("cadastros.editar")
def salvar_setor():
    setor_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    pct = _decimal(request.form.get("percentual_rateio"), "0")
    if pct > 1:
        pct = pct / 100  # aceita "25" como 25%
    if not nome:
        flash("Informe o nome do setor.", "error")
        return redirect(url_for("cadastros.equipe"))

    if setor_id:
        setor = db.session.get(Setor, setor_id) or abort(404)
        auditoria.registrar("update", "setor", setor.id,
                            antes={"percentual_rateio": setor.percentual_rateio})
    else:
        setor = Setor()
        db.session.add(setor)
    setor.nome = nome
    setor.percentual_rateio = pct
    setor.ativo = "ativo" in request.form
    db.session.commit()
    flash("Setor salvo. Confira se a soma dos percentuais dá 100%.", "success")
    return redirect(url_for("cadastros.equipe", aba="setores"))


@bp.route("/funcoes/salvar", methods=["POST"])
@requer("cadastros.editar")
def salvar_funcao():
    funcao_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    setor_id = request.form.get("setor_id", type=int)
    if not nome or not setor_id:
        flash("Informe nome e setor da função.", "error")
        return redirect(url_for("cadastros.equipe"))

    if funcao_id:
        funcao = db.session.get(Funcao, funcao_id) or abort(404)
    else:
        funcao = Funcao()
        db.session.add(funcao)
    funcao.nome = nome
    funcao.setor_id = setor_id
    funcao.pontos_padrao = _decimal(request.form.get("pontos_padrao"), "1")
    db.session.commit()
    flash("Função salva.", "success")
    return redirect(url_for("cadastros.equipe", aba="funcoes"))


@bp.route("/colaboradores/salvar", methods=["POST"])
@requer("cadastros.editar")
def salvar_colaborador():
    colab_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    funcao_id = request.form.get("funcao_id", type=int)
    if not nome or not funcao_id:
        flash("Informe nome e função.", "error")
        return redirect(url_for("cadastros.equipe"))

    funcao = db.session.get(Funcao, funcao_id) or abort(400)
    if colab_id:
        colab = db.session.get(Colaborador, colab_id) or abort(404)
        auditoria.registrar("update", "colaborador", colab.id, antes={"nome": colab.nome})
    else:
        colab = Colaborador()
        db.session.add(colab)
    colab.nome = nome
    colab.funcao_id = funcao.id
    colab.setor_id = funcao.setor_id
    pontos = request.form.get("pontos")
    colab.pontos = _decimal(pontos, str(funcao.pontos_padrao)) if pontos else Decimal(funcao.pontos_padrao)
    registro = request.form.get("registro")
    colab.registro = registro if registro in REGISTROS_COLABORADOR else "CLT"
    colab.ativo = "ativo" in request.form
    colab.observacao = (request.form.get("observacao") or "").strip() or None
    db.session.commit()
    flash("Colaborador salvo.", "success")
    return redirect(url_for("cadastros.equipe"))
