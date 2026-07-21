# -*- coding: utf-8 -*-
"""Metas: tabela anual com semáforo, premissas ajustáveis e registro diário."""
import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.extensions import db
from app.models.metas import FaturamentoDiario, FaturamentoHistorico, PremissaMeta
from app.services import auditoria, metas_consultas
from app.services.metas import metas_diarias
from app.utils.datas import hoje_sp
from app.utils.decoradores import role_required

bp = Blueprint("metas", __name__)


def _parse_valor(texto: str) -> Decimal | None:
    texto = (texto or "").strip().replace("R$", "").strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


@bp.route("/")
@login_required
def painel():
    hoje = hoje_sp()
    ano = request.args.get("ano", type=int, default=hoje.year)
    tabela = metas_consultas.tabela_do_ano(ano)
    premissa = db.session.execute(
        db.select(PremissaMeta).filter_by(ano=ano)
    ).scalar_one_or_none()

    totais = {
        "meta": sum((l.meta for l in tabela if l.meta), Decimal("0")),
        "realizado": sum((l.realizado for l in tabela if l.realizado), Decimal("0")),
    }
    totais["pct"] = (totais["realizado"] / totais["meta"]) if totais["meta"] else None

    return render_template(
        "metas/painel.html", tabela=tabela, ano=ano, premissa=premissa, totais=totais
    )


@bp.route("/premissas/<int:ano>", methods=["POST"])
@role_required("consultoria")
def salvar_premissas(ano: int):
    """Premissas mudam a meta de todos os meses — restrito à consultoria."""

    def pct(campo: str, default: str) -> Decimal:
        bruto = (request.form.get(campo) or default).replace(",", ".")
        valor = Decimal(bruto)
        return valor / 100 if valor > 1 else valor

    peso_anterior = pct("peso_mes_anterior", "0.60")
    peso_media = pct("peso_media_historica", "0.40")
    if peso_anterior + peso_media != Decimal("1"):
        flash("Os pesos precisam somar 100%.", "error")
        return redirect(url_for("metas.painel", ano=ano))

    premissa = db.session.execute(
        db.select(PremissaMeta).filter_by(ano=ano)
    ).scalar_one_or_none()
    antes = None
    if not premissa:
        premissa = PremissaMeta(ano=ano)
        db.session.add(premissa)
    else:
        antes = {
            "peso_mes_anterior": premissa.peso_mes_anterior,
            "peso_media_historica": premissa.peso_media_historica,
            "crescimento_alvo": premissa.crescimento_alvo,
        }

    premissa.peso_mes_anterior = peso_anterior
    premissa.peso_media_historica = peso_media
    premissa.crescimento_alvo = pct("crescimento_alvo", "0.08")
    premissa.peso_ter_qua = Decimal((request.form.get("peso_ter_qua") or "1.0").replace(",", "."))
    premissa.peso_qui_sex_dom = Decimal((request.form.get("peso_qui_sex_dom") or "1.4").replace(",", "."))
    premissa.peso_sabado = Decimal((request.form.get("peso_sabado") or "1.6").replace(",", "."))

    auditoria.registrar("update", "premissa_meta", premissa.id, antes=antes, depois={
        "peso_mes_anterior": premissa.peso_mes_anterior,
        "peso_media_historica": premissa.peso_media_historica,
        "crescimento_alvo": premissa.crescimento_alvo,
    })
    db.session.commit()
    flash("Premissas atualizadas — metas recalculadas.", "success")
    return redirect(url_for("metas.painel", ano=ano))


@bp.route("/registro-diario")
@login_required
def registro_diario():
    hoje = hoje_sp()
    ano = request.args.get("ano", type=int, default=hoje.year)
    mes = request.args.get("mes", type=int, default=hoje.month)
    if not 1 <= mes <= 12:
        abort(400)

    ultimo = calendar.monthrange(ano, mes)[1]
    datas = [date(ano, mes, d) for d in range(1, ultimo + 1)]
    registros = {
        r.data: r
        for r in db.session.execute(
            db.select(FaturamentoDiario).filter(
                FaturamentoDiario.data >= datas[0], FaturamentoDiario.data <= datas[-1]
            )
        ).scalars()
    }

    linha = metas_consultas.tabela_do_ano(ano)[mes - 1]
    premissas = metas_consultas.premissas_do_ano(ano)
    fechados_extra = {d for d, r in registros.items() if not r.aberto and d.weekday() != 0}
    metas_dia = (
        metas_diarias(ano, mes, linha.meta, premissas, fechados_extra) if linha.meta else {}
    )

    # acumulados dia a dia (como a aba de acompanhamento)
    linhas_tabela = []
    meta_acum = Decimal("0")
    real_acum = Decimal("0")
    for d in datas:
        reg = registros.get(d)
        aberto = reg.aberto if reg else d.weekday() != 0
        meta_dia = metas_dia.get(d, Decimal("0")) if aberto else Decimal("0")
        realizado = reg.faturamento if reg else None
        meta_acum += meta_dia
        if realizado:
            real_acum += realizado
        linhas_tabela.append({
            "data": d,
            "aberto": aberto,
            "meta_dia": meta_dia,
            "realizado": realizado,
            "diferenca": (realizado - meta_dia) if realizado is not None else None,
            "pct": (realizado / meta_dia) if (realizado is not None and meta_dia) else None,
            "meta_acum": meta_acum,
            "real_acum": real_acum,
        })

    return render_template(
        "metas/registro_diario.html",
        ano=ano, mes=mes, linhas=linhas_tabela, linha_meta=linha,
    )


@bp.route("/registro-diario/salvar", methods=["POST"])
@login_required
def salvar_registro_diario():
    ano = request.form.get("ano", type=int)
    mes = request.form.get("mes", type=int)
    if not ano or not 1 <= (mes or 0) <= 12:
        abort(400)

    ultimo = calendar.monthrange(ano, mes)[1]
    for dia in range(1, ultimo + 1):
        d = date(ano, mes, dia)
        valor = _parse_valor(request.form.get(f"faturamento_{d.isoformat()}", ""))
        aberto = f"aberto_{d.isoformat()}" in request.form
        reg = db.session.execute(
            db.select(FaturamentoDiario).filter_by(data=d)
        ).scalar_one_or_none()
        if reg:
            reg.faturamento = valor
            reg.aberto = aberto
        elif valor is not None or not aberto:
            db.session.add(FaturamentoDiario(data=d, faturamento=valor, aberto=aberto))

    auditoria.registrar("update", "faturamento_diario", None,
                        depois={"ano": ano, "mes": mes})
    db.session.commit()
    flash("Registro diário salvo.", "success")
    return redirect(url_for("metas.registro_diario", ano=ano, mes=mes))


@bp.route("/historico", methods=["GET", "POST"])
@login_required
def historico():
    if request.method == "POST":
        ano = request.form.get("ano", type=int)
        mes = request.form.get("mes", type=int)
        valor = _parse_valor(request.form.get("valor", ""))
        if not ano or not 1 <= (mes or 0) <= 12 or valor is None:
            flash("Informe ano, mês e valor válidos.", "error")
        else:
            reg = db.session.execute(
                db.select(FaturamentoHistorico).filter_by(ano=ano, mes=mes)
            ).scalar_one_or_none()
            if reg:
                auditoria.registrar("update", "faturamento_historico", reg.id,
                                    antes={"valor": reg.valor}, depois={"valor": valor})
                reg.valor = valor
            else:
                reg = FaturamentoHistorico(ano=ano, mes=mes, valor=valor)
                db.session.add(reg)
                db.session.flush()
                auditoria.registrar("create", "faturamento_historico", reg.id,
                                    depois={"ano": ano, "mes": mes, "valor": valor})
            db.session.commit()
            flash("Faturamento histórico salvo.", "success")
        return redirect(url_for("metas.historico"))

    historico_matriz = metas_consultas.carregar_historico()
    anos = sorted(historico_matriz)
    return render_template("metas/historico.html", historico=historico_matriz, anos=anos)
