# -*- coding: utf-8 -*-
"""Dashboard consolidado + endpoints JSON dos gráficos (Chart.js)."""
import calendar
from datetime import date, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from app.extensions import db
from app.models.banco import MovimentoBancario
from app.models.gorjetas import PeriodoGorjeta
from app.models.metas import FaturamentoDiario
from app.services import fluxo_caixa as fluxo_srv
from app.services import indicadores, itau_sync, metas_consultas
from app.services.metas import metas_diarias
from app.utils import periodo as periodo_global
from app.utils.seguranca import destino_seguro
from app.utils.datas import hoje_sp, primeiro_dia_mes, ultimo_dia_mes

bp = Blueprint("dashboard", __name__)


def _delta_pct(atual: Decimal, anterior: Decimal):
    """Variação percentual vs período anterior; None se sem base."""
    if not anterior:
        return None
    return float((atual - anterior) / anterior * 100)


@bp.route("/periodo", methods=["POST"])
@login_required
def trocar_periodo():
    """Seletor de período do topo: vale como padrão para todas as telas por mês."""
    ano = request.form.get("ano", type=int)
    mes = request.form.get("mes", type=int)
    if request.form.get("acao") == "atual" or not ano or not mes:
        periodo_global.limpar()
    elif 2000 <= ano <= 2100 and 1 <= mes <= 12:
        periodo_global.definir(ano, mes)
    # volta para a mesma tela, sem os parâmetros de mês que mandariam no lugar do global
    return redirect(destino_seguro(request.form.get("proximo"), url_for("dashboard.home")))


@bp.route("/")
@login_required
def home():
    hoje = hoje_sp()
    ano_sel, mes_sel = periodo_global.atual()
    inicio_mes = primeiro_dia_mes(ano_sel, mes_sel)
    fim_mes = ultimo_dia_mes(ano_sel, mes_sel)

    # Realizado (até hoje) × previsto (resto do mês): a planilha já traz
    # recebíveis de cartão e despesas programadas para os próximos dias.
    # Mês passado: tudo realizado; mês futuro: tudo previsto.
    periodo = indicadores.mes_realizado_previsto(ano_sel, mes_sel, hoje)
    real, previsto = periodo["realizado"], periodo["previsto"]
    # deltas comparam com o mesmo intervalo de dias do mês anterior
    anterior = indicadores.mesmo_periodo_mes_anterior(max(periodo["corte"], inicio_mes))

    linha_meta = metas_consultas.tabela_do_ano(ano_sel)[mes_sel - 1]

    ranking = fluxo_srv.ranking_despesas(inicio_mes, periodo["corte"], limite=6)
    ranking_json = [{"grupo": g, "total": float(t)} for g, t in ranking]

    proxima_quinzena = db.session.execute(
        db.select(PeriodoGorjeta)
        .filter_by(status="aberto")
        .order_by(PeriodoGorjeta.data_inicio)
    ).scalars().first()

    # Extrato Itaú gravado (não entra nos totais do fluxo — só referência)
    saldo_itau = itau_sync.ultimo_saldo()
    itau_mes = itau_sync.totais_periodo(inicio_mes, fim_mes) if saldo_itau else None

    # "Para hoje": o que falta fazer no extrato, com o mesmo recorte e as mesmas
    # contas da tela de Movimentações (mês corrente até hoje), para os números baterem
    fim_recorte = min(hoje, fim_mes)
    movs_mes = itau_sync.movimentos(inicio_mes, fim_recorte)
    pendencias = {
        # mesma fila da tela de Movimentações (repasse da Stone sem o arquivo
        # do dia não entra: não há o que revisar enquanto ele não chega)
        "classificar": len(itau_sync.movimentos(inicio_mes, fim_recorte, status="classificar")),
        "sem_fornecedor": sum(
            1 for m in movs_mes
            if m.categoria_gerencial == "pag_fornecedores"
            and m.lancamento is not None and m.lancamento.fornecedor_id is None),
        "inicio": inicio_mes,
        "fim": fim_recorte,
    }
    ultimos_movimentos = db.session.execute(
        db.select(MovimentoBancario)
        .order_by(MovimentoBancario.data.desc(), MovimentoBancario.id.desc())
        .limit(5)
    ).scalars().all()

    return render_template(
        "dashboard/home.html",
        hoje=hoje,
        saldo_itau=saldo_itau,
        itau_mes=itau_mes,
        itau_ultima_sync=itau_sync.ultima_sincronizacao(),
        real=real,
        previsto=previsto,
        corte=periodo["corte"],
        fim_mes=fim_mes,
        delta_entradas=_delta_pct(real.entradas, anterior.entradas),
        delta_saidas=_delta_pct(real.saidas, anterior.saidas),
        linha_meta=linha_meta,
        ranking=ranking,
        ranking_json=ranking_json,
        proxima_quinzena=proxima_quinzena,
        pendencias=pendencias,
        ultimos_movimentos=ultimos_movimentos,
        serie_faturamento=_serie_faturamento_meta(ano_sel, mes_sel, linha_meta),
        ano_sel=ano_sel, mes_sel=mes_sel,
    )


def _serie_faturamento_meta(ano: int, mes: int, linha_meta) -> list[dict]:
    """Acumulado do mês: registro diário × meta diária (pesos por dia da semana).

    Dias ainda sem registro ficam sem valor realizado (lacuna, não zero).
    """
    datas = [date(ano, mes, d) for d in range(1, calendar.monthrange(ano, mes)[1] + 1)]
    registros = {
        r.data: r for r in db.session.execute(
            db.select(FaturamentoDiario).filter(
                FaturamentoDiario.data >= datas[0], FaturamentoDiario.data <= datas[-1])
        ).scalars()
    }
    fechados, abertos = metas_consultas.excecoes_do_mes(registros)
    metas = (metas_diarias(ano, mes, linha_meta.meta, metas_consultas.premissas_do_ano(ano),
                           fechados, abertos)
             if linha_meta.meta else {})
    ultimo_registro = max((d for d, r in registros.items() if r.faturamento is not None), default=None)

    serie, meta_acum, real_acum = [], Decimal("0"), Decimal("0")
    for d in datas:
        meta_acum += metas.get(d, Decimal("0"))
        reg = registros.get(d)
        if reg and reg.faturamento is not None:
            real_acum += reg.faturamento
        serie.append({
            "dia": d.strftime("%d/%m"),
            "meta": float(meta_acum) if metas else None,
            "realizado": float(real_acum) if ultimo_registro and d <= ultimo_registro else None,
        })
    return serie


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
