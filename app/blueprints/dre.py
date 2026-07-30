# -*- coding: utf-8 -*-
"""DRE Gerencial — Demonstrativo de Resultado do Exercício."""
from flask import Blueprint, render_template, request
from flask_login import login_required

from app.services import fluxo_caixa as srv
from app.utils.datas import hoje_sp
from app.utils.filtros import MESES_PT

bp = Blueprint("dre", __name__)


@bp.route("/")
@login_required
def index():
    hoje = hoje_sp()
    ano = request.args.get("ano", hoje.year, type=int)
    mes = request.args.get("mes", hoje.month, type=int)
    if not 1 <= mes <= 12:
        mes = hoje.month

    atual = srv.dre_mensal(ano, mes)

    if mes == 1:
        ano_ant, mes_ant = ano - 1, 12
    else:
        ano_ant, mes_ant = ano, mes - 1

    if mes == 12:
        ano_prox, mes_prox = ano + 1, 1
    else:
        ano_prox, mes_prox = ano, mes + 1

    anterior = srv.dre_mensal(ano_ant, mes_ant)

    return render_template(
        "dre/index.html",
        ano=ano,
        mes=mes,
        mes_nome=MESES_PT[mes],
        ano_ant=ano_ant,
        mes_ant=mes_ant,
        mes_ant_nome=MESES_PT[mes_ant],
        ano_prox=ano_prox,
        mes_prox=mes_prox,
        atual=atual,
        anterior=anterior,
    )
