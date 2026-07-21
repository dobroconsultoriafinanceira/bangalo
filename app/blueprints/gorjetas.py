# -*- coding: utf-8 -*-
"""Gorjetas: quinzenas, presença, comissão diária, rateio e fechamento."""
import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required

from app.extensions import db
from app.models.gorjetas import (
    MOTIVOS_DESCONTO,
    Colaborador,
    ComissaoDiaria,
    FechamentoGorjeta,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Presenca,
)
from app.services import auditoria, gorjetas_consultas, metas_consultas
from app.services.metas import meta_quinzena
from app.utils.decoradores import role_required
from app.utils.filtros import MESES_PT

bp = Blueprint("gorjetas", __name__)


def _parse_valor(texto: str) -> Decimal:
    texto = (texto or "").strip().replace("R$", "").strip()
    if not texto:
        return Decimal("0")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0")


@bp.route("/")
@login_required
def listar():
    periodos = db.session.execute(
        db.select(PeriodoGorjeta).order_by(PeriodoGorjeta.data_inicio.desc())
    ).scalars().all()
    return render_template("gorjetas/listar.html", periodos=periodos)


@bp.route("/novo", methods=["POST"])
@login_required
def criar_periodo():
    ano = request.form.get("ano", type=int)
    mes = request.form.get("mes", type=int)
    ordem = request.form.get("ordem_quinzena", type=int)
    if not ano or not mes or ordem not in (1, 2) or not 1 <= mes <= 12:
        flash("Informe ano, mês e quinzena válidos.", "error")
        return redirect(url_for("gorjetas.listar"))

    existente = db.session.execute(
        db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes, ordem_quinzena=ordem)
    ).scalar_one_or_none()
    if existente:
        flash("Essa quinzena já existe.", "warning")
        return redirect(url_for("gorjetas.detalhe", periodo_id=existente.id))

    if ordem == 1:
        inicio, fim = date(ano, mes, 1), date(ano, mes, 15)
    else:
        inicio = date(ano, mes, 16)
        fim = date(ano, mes, calendar.monthrange(ano, mes)[1])

    meta_mes = metas_consultas.meta_do_mes(ano, mes)
    periodo = PeriodoGorjeta(
        referencia=f"{ordem}ª Quinzena {MESES_PT[mes]}/{str(ano)[2:]}",
        ordem_quinzena=ordem,
        mes=mes,
        ano=ano,
        data_inicio=inicio,
        data_fim=fim,
        meta_mes=meta_mes,
        meta_quinzena=meta_quinzena(meta_mes),
    )
    db.session.add(periodo)
    db.session.flush()
    gorjetas_consultas.garantir_participacoes(periodo)
    auditoria.registrar("create", "periodo_gorjeta", periodo.id,
                        depois={"referencia": periodo.referencia})
    db.session.commit()
    flash(f"{periodo.referencia} criada.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))


@bp.route("/<int:periodo_id>")
@login_required
def detalhe(periodo_id: int):
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)

    if periodo.fechado:
        fechamentos = db.session.execute(
            db.select(FechamentoGorjeta)
            .filter_by(periodo_id=periodo.id)
            .order_by(FechamentoGorjeta.setor, FechamentoGorjeta.nome)
        ).scalars().all()
        return render_template("gorjetas/fechada.html", periodo=periodo, fechamentos=fechamentos)

    resultado = gorjetas_consultas.calcular(periodo)

    dias = []
    d = periodo.data_inicio
    while d <= periodo.data_fim:
        dias.append(d)
        d += timedelta(days=1)

    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    presencas = {
        (pres.colaborador_id, pres.data): pres.presente for pres in periodo.presencas
    }
    comissoes = {cd.data: cd.valor for cd in periodo.comissoes_diarias}
    colaboradores = db.session.execute(
        db.select(Colaborador).filter_by(ativo=True).order_by(Colaborador.nome)
    ).scalars().all()

    return render_template(
        "gorjetas/detalhe.html",
        periodo=periodo,
        resultado=resultado,
        dias=dias,
        participacoes=participacoes,
        presencas=presencas,
        comissoes=comissoes,
        colaboradores=colaboradores,
        motivos_desconto=MOTIVOS_DESCONTO,
    )


@bp.route("/<int:periodo_id>/salvar", methods=["POST"])
@login_required
def salvar(periodo_id: int):
    """Salva comissão bruta, comissões diárias, presenças e descontos."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado:
        flash("Quinzena fechada não pode ser alterada.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))

    periodo.comissao_bruta = _parse_valor(request.form.get("comissao_bruta", ""))
    encargos = request.form.get("percentual_encargos")
    if encargos:
        periodo.percentual_encargos = Decimal(encargos.replace(",", ".")) / (
            Decimal("100") if Decimal(encargos.replace(",", ".")) > 1 else Decimal("1")
        )

    colaboradores = db.session.execute(db.select(Colaborador)).scalars().all()
    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    presencas = {(p.colaborador_id, p.data): p for p in periodo.presencas}
    comissoes = {c.data: c for c in periodo.comissoes_diarias}

    d = periodo.data_inicio
    while d <= periodo.data_fim:
        valor = _parse_valor(request.form.get(f"comissao_{d.isoformat()}", ""))
        if d in comissoes:
            comissoes[d].valor = valor
        elif valor:
            db.session.add(ComissaoDiaria(periodo_id=periodo.id, data=d, valor=valor))

        for c in colaboradores:
            campo = f"presenca_{c.id}_{d.isoformat()}"
            presente = campo in request.form
            chave = (c.id, d)
            if chave in presencas:
                presencas[chave].presente = presente
            elif presente:
                db.session.add(Presenca(
                    periodo_id=periodo.id, colaborador_id=c.id, data=d, presente=True
                ))
        d += timedelta(days=1)

    for c in colaboradores:
        desconto = _parse_valor(request.form.get(f"desconto_{c.id}", ""))
        motivo = request.form.get(f"motivo_{c.id}") or None
        obs = (request.form.get(f"obs_desconto_{c.id}") or "").strip() or None
        dias_manual = request.form.get(f"dias_manual_{c.id}", type=int)
        part = participacoes.get(c.id)
        if part:
            part.desconto = desconto
            part.desconto_motivo = motivo if desconto else None
            part.desconto_observacao = obs
            part.dias_trabalhados_manual = dias_manual
        elif desconto or dias_manual is not None:
            db.session.add(ParticipacaoPeriodo(
                periodo_id=periodo.id, colaborador_id=c.id,
                desconto=desconto, desconto_motivo=motivo if desconto else None,
                desconto_observacao=obs, dias_trabalhados_manual=dias_manual,
            ))

    auditoria.registrar("update", "periodo_gorjeta", periodo.id,
                        depois={"comissao_bruta": periodo.comissao_bruta})
    db.session.commit()
    flash("Quinzena salva.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))


@bp.route("/<int:periodo_id>/api/rateio")
@login_required
def api_rateio(periodo_id: int):
    """Resultado do rateio em JSON — recálculo em tempo real na tela."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    resultado = gorjetas_consultas.calcular(periodo)
    return jsonify({
        "modo": resultado.modo,
        "total_liquido": float(resultado.total_liquido),
        "total_descontos": float(resultado.total_descontos),
        "total_a_pagar": float(resultado.total_a_pagar),
        "redistribuido": float(resultado.redistribuido),
        "setores": [
            {
                "nome": setor,
                "pool": float(resultado.pools.get(setor, 0)),
                "pago": float(resultado.pago_por_setor.get(setor, 0)),
                "status": resultado.status_setor(setor),
            }
            for setor in resultado.pools
        ],
        "colaboradores": [
            {
                "id": r.id, "nome": r.nome, "setor": r.setor, "funcao": r.funcao,
                "pontos": float(r.pontos), "dias": r.dias_trabalhados,
                "bruto": float(r.bruto_rateado), "desconto": float(r.desconto),
                "liquido": float(r.liquido),
            }
            for r in resultado.colaboradores
        ],
    })


@bp.route("/<int:periodo_id>/fechar", methods=["POST"])
@role_required("consultoria", "gerencia")
def fechar(periodo_id: int):
    """Fechamento gera snapshot imutável (ação irreversível, com confirmação)."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado:
        flash("Quinzena já está fechada.", "warning")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))

    resultado = gorjetas_consultas.fechar(periodo)
    auditoria.registrar(
        "update", "periodo_gorjeta", periodo.id,
        antes={"status": "aberto"},
        depois={"status": "fechado", "total_a_pagar": resultado.total_a_pagar},
    )
    db.session.commit()
    flash(f"{periodo.referencia} fechada. Total distribuído: R$ {resultado.total_a_pagar}.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))
