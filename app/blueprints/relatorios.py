# -*- coding: utf-8 -*-
"""Central de Relatórios: prévia, geração de PDF e histórico de exportações."""
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user

from app.extensions import db
from app.models.documentos import ExportacaoRelatorio
from app.models.gorjetas import PeriodoGorjeta
from app.services import arquivos, relatorios_central
from app.utils import periodo as periodo_global
from app.utils.datas import hoje_sp
from app.utils.filtros import MESES_PT
from app.utils.permissoes import requer

bp = Blueprint("relatorios", __name__)


def _parametros(origem) -> tuple[str, dict]:
    """Lê e valida os parâmetros do relatório (GET da prévia ou POST da geração)."""
    ano_padrao, mes_padrao = periodo_global.atual()
    tipo = origem.get("tipo") if origem.get("tipo") in relatorios_central.TIPOS else "dre_gerencial"
    ano = origem.get("ano", type=int) or ano_padrao
    mes = origem.get("mes", type=int) or mes_padrao
    if not 2000 <= ano <= 2100:
        ano = ano_padrao
    if not 1 <= mes <= 12:
        mes = mes_padrao
    p = {"ano": ano, "mes": mes,
         "composicao": origem.get("composicao", "1") in ("1", "on", "true")}
    if tipo == "dre_gerencial":
        p["regime"] = origem.get("regime") if origem.get("regime") in ("competencia", "caixa") else "competencia"
    if tipo == "gorjetas":
        p = {"periodo_id": origem.get("periodo_id", type=int),
             "variante": origem.get("variante") if origem.get("variante") in relatorios_central.VARIANTES_GORJETA else "completo"}
    if tipo == "metas":
        p = {"ano": ano}
    return tipo, p


@bp.route("/")
@requer("relatorios.gerar")
def index():
    tipo, p = _parametros(request.args)
    quinzenas = db.session.execute(
        db.select(PeriodoGorjeta).order_by(PeriodoGorjeta.data_inicio.desc())
    ).scalars().all()
    if tipo == "gorjetas" and not p["periodo_id"] and quinzenas:
        p["periodo_id"] = quinzenas[0].id
    previa, erro = None, None
    if tipo != "gorjetas":
        try:
            previa = relatorios_central.documento(tipo, p)
        except Exception:  # noqa: BLE001 — falha só na prévia, a página continua
            erro = "Não foi possível montar a prévia deste relatório com os dados atuais."
    return render_template(
        "relatorios/index.html",
        tipo=tipo, p=p, tipos=relatorios_central.TIPOS, variantes=relatorios_central.VARIANTES_GORJETA,
        previa=previa, erro=erro, quinzenas=quinzenas,
        quinzena=next((q for q in quinzenas if q.id == p.get("periodo_id")), None),
        meses=MESES_PT, recentes=relatorios_central.historico(5),
        # o PDF recém-gerado vem destacado na lista (o Jinja não converte tipos)
        destaque=request.args.get("destaque", type=int),
    )


@bp.route("/gerar", methods=["POST"])
@requer("relatorios.gerar")
def gerar():
    tipo, p = _parametros(request.form)
    registro = relatorios_central.gerar(tipo, p, current_user.id)
    db.session.commit()
    if registro.status == "gerado":
        flash(f"{registro.titulo}: PDF pronto.", "success")
    else:
        flash(f"{registro.titulo}: não foi possível gerar — {registro.erro}", "error")
    return redirect(url_for("relatorios.index", tipo=tipo, destaque=registro.id, **p))


@bp.route("/exportacoes")
@requer("relatorios.gerar")
def exportacoes():
    return render_template("relatorios/exportacoes.html", itens=relatorios_central.historico(200),
                           tipos=relatorios_central.TIPOS)


@bp.route("/exportacoes/<int:exp_id>/arquivo")
@requer("relatorios.gerar")
def arquivo(exp_id: int):
    registro = db.session.get(ExportacaoRelatorio, exp_id) or abort(404)
    if registro.status != "gerado" or not registro.caminho:
        abort(404)
    try:
        caminho = arquivos.caminho_absoluto(registro.caminho)
    except ValueError:
        abort(404)
    if not caminho.exists():
        flash("O arquivo deste relatório não está mais disponível. Gere novamente.", "warning")
        return redirect(url_for("relatorios.exportacoes"))
    resp = send_file(caminho, mimetype="application/pdf", as_attachment=request.args.get("baixar") is not None,
                     download_name=registro.nome_arquivo)
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; object-src 'self'"
    return resp


@bp.route("/exportacoes/<int:exp_id>/refazer", methods=["POST"])
@requer("relatorios.gerar")
def refazer(exp_id: int):
    """Tentar de novo / gerar novamente com os mesmos parâmetros."""
    anterior = db.session.get(ExportacaoRelatorio, exp_id) or abort(404)
    registro = relatorios_central.gerar(anterior.tipo, dict(anterior.parametros or {}), current_user.id)
    db.session.commit()
    if registro.status == "gerado":
        flash(f"{registro.titulo}: PDF gerado novamente.", "success")
    else:
        flash(f"{registro.titulo}: falhou de novo — {registro.erro}", "error")
    return redirect(url_for("relatorios.exportacoes"))
