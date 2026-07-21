# -*- coding: utf-8 -*-
"""Dashboard consolidado + endpoints JSON dos gráficos (Chart.js)."""
from datetime import timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from app.extensions import db
from app.models.gorjetas import PeriodoGorjeta
from app.services import fluxo_caixa as fluxo_srv
from app.services import metas_consultas
from app.utils.datas import hoje_sp, primeiro_dia_mes, ultimo_dia_mes

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def home():
    hoje = hoje_sp()
    inicio_mes = primeiro_dia_mes(hoje.year, hoje.month)
    fim_mes = ultimo_dia_mes(hoje.year, hoje.month)

    totais_mes = fluxo_srv.totais_por_tipo(inicio_mes, fim_mes)
    saldo_atual = fluxo_srv.saldo_ate(hoje)

    linha_meta = metas_consultas.tabela_do_ano(hoje.year)[hoje.month - 1]

    ranking = fluxo_srv.ranking_despesas(inicio_mes, fim_mes, limite=6)

    proxima_quinzena = db.session.execute(
        db.select(PeriodoGorjeta)
        .filter_by(status="aberto")
        .order_by(PeriodoGorjeta.data_inicio)
    ).scalars().first()

    return render_template(
        "dashboard/home.html",
        hoje=hoje,
        saldo_atual=saldo_atual,
        entradas_mes=totais_mes["entrada"],
        saidas_mes=totais_mes["saida"],
        linha_meta=linha_meta,
        ranking=ranking,
        proxima_quinzena=proxima_quinzena,
    )


# ---------- APIs JSON para os gráficos ----------

@bp.route("/api/fluxo/mensal/<int:ano>")
@login_required
def api_fluxo_mensal(ano: int):
    dados = fluxo_srv.consolidacao_mensal(ano)
    return jsonify([
        {
            "mes": item["mes"],
            "entradas": float(item["entradas"]),
            "saidas": float(item["saidas"]),
            "resultado": float(item["resultado"]),
        }
        for item in dados
    ])


@bp.route("/api/metas/anual/<int:ano>")
@login_required
def api_metas_anual(ano: int):
    tabela = metas_consultas.tabela_do_ano(ano)
    return jsonify([
        {
            "mes": linha.mes,
            "meta": float(linha.meta) if linha.meta is not None else None,
            "realizado": float(linha.realizado) if linha.realizado is not None else None,
        }
        for linha in tabela
    ])


@bp.route("/api/fluxo/saldo-diario")
@login_required
def api_saldo_diario():
    """Saldo dos últimos 30 dias para o sparkline do dashboard."""
    hoje = hoje_sp()
    dias = [hoje - timedelta(days=i) for i in range(29, -1, -1)]
    # saldo_ate por dia: uma consulta por dia seria cara; deriva incremental
    inicio = dias[0]
    saldo_base = fluxo_srv.saldo_ate(inicio - timedelta(days=1))
    from sqlalchemy import func

    from app.models.fluxo import Categoria, Lancamento

    linhas = (
        db.session.query(
            Lancamento.data,
            Categoria.tipo,
            func.coalesce(func.sum(Lancamento.valor), 0),
        )
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= hoje)
        .group_by(Lancamento.data, Categoria.tipo)
        .all()
    )
    delta_por_dia: dict = {}
    for data_l, tipo, total in linhas:
        valor = Decimal(total)
        delta_por_dia[data_l] = delta_por_dia.get(data_l, Decimal("0")) + (
            valor if tipo == "entrada" else -valor
        )

    serie = []
    acumulado = saldo_base
    for dia in dias:
        acumulado += delta_por_dia.get(dia, Decimal("0"))
        serie.append({"data": dia.isoformat(), "saldo": float(acumulado)})
    return jsonify(serie)
