# -*- coding: utf-8 -*-
"""DRE Gerencial — Demonstrativo de Resultado do Exercício."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models.documentos import DocumentoContabil
from app.services import arquivos, documentos_contabeis, dre_contabil, dre_gerencial, dre_previsao
from app.services import fluxo_caixa as srv
from app.services import indicadores
from app.utils import periodo as periodo_global
from app.utils.datas import hoje_sp, primeiro_dia_mes, ultimo_dia_mes
from app.utils.filtros import MESES_PT
from app.utils.permissoes import requer

bp = Blueprint("dre", __name__)

EXTENSOES_RAZAO = (".xls", ".xlsx")


@bp.route("/")
@login_required
def index():
    hoje = hoje_sp()
    ano_padrao, mes_padrao = periodo_global.atual()
    ano = request.args.get("ano", ano_padrao, type=int)
    mes = request.args.get("mes", mes_padrao, type=int)
    if not 1 <= mes <= 12:
        mes = mes_padrao

    if mes == 1:
        ano_ant, mes_ant = ano - 1, 12
    else:
        ano_ant, mes_ant = ano, mes - 1

    if mes == 12:
        ano_prox, mes_prox = ano + 1, 1
    else:
        ano_prox, mes_prox = ano, mes + 1

    # Mês em andamento: por padrão só o REALIZADO (até hoje), comparado com o
    # mesmo intervalo de dias do mês anterior; "completo" inclui o previsto.
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    em_andamento = inicio <= hoje < fim
    futuro = hoje < inicio
    visao = request.args.get("visao") if request.args.get("visao") in ("realizado", "completo") else "realizado"
    parcial = em_andamento and visao == "realizado"

    dia_ant = min(hoje.day, ultimo_dia_mes(ano_ant, mes_ant).day)
    if parcial:
        atual = srv.dre_mensal(ano, mes, ate=hoje)
        anterior = srv.dre_mensal(ano_ant, mes_ant, ate=date(ano_ant, mes_ant, dia_ant))
        previsto = indicadores.calcular(hoje + timedelta(days=1), fim)
    else:
        atual = srv.dre_mensal(ano, mes)
        anterior = srv.dre_mensal(ano_ant, mes_ant)
        previsto = None

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
        em_andamento=em_andamento,
        futuro=futuro,
        parcial=parcial,
        corte=hoje,
        dia_ant=dia_ant,
        previsto=previsto,
    )


def _mes_ano(padrao_mes_anterior: bool = True) -> tuple[int, int]:
    hoje = hoje_sp()
    if periodo_global.escolhido():
        padrao_ano, padrao_mes = periodo_global.escolhido()
    elif padrao_mes_anterior:
        padrao_mes = hoje.month - 1 or 12
        padrao_ano = hoje.year if hoje.month > 1 else hoje.year - 1
    else:
        padrao_mes, padrao_ano = hoje.month, hoje.year
    ano = request.values.get("ano", padrao_ano, type=int)
    mes = request.values.get("mes", padrao_mes, type=int)
    return ano, (mes if 1 <= mes <= 12 else padrao_mes)


@bp.route("/gerencial")
@login_required
def gerencial():
    """DRE Gerencial apurada com os dados do sistema (modelo da consultoria)."""
    ano, mes = _mes_ano(padrao_mes_anterior=False)
    regime = request.args.get("regime") if request.args.get("regime") in ("competencia", "caixa") else "competencia"
    ano_ant, mes_ant = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    ano_prox, mes_prox = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return render_template(
        "dre/gerencial.html",
        ano=ano, mes=mes, mes_nome=MESES_PT[mes], regime=regime,
        ano_ant=ano_ant, mes_ant=mes_ant, mes_ant_nome=MESES_PT[mes_ant],
        ano_prox=ano_prox, mes_prox=mes_prox,
        analise=dre_gerencial.analise(ano, mes, regime),
    )


ABAS_CONTABIL = ("comparacao", "documentos", "razao")


@bp.route("/contabil")
@login_required
def contabil():
    """Fechamento contábil: comparação, documentos recebidos e razão detalhado."""
    ano, mes = _mes_ano()
    aba = request.args.get("aba") if request.args.get("aba") in ABAS_CONTABIL else "comparacao"
    ano_ant, mes_ant = (ano - 1, 12) if mes == 1 else (ano, mes - 1)
    ano_prox, mes_prox = (ano + 1, 1) if mes == 12 else (ano, mes + 1)

    contexto = {}
    if aba == "comparacao":
        contexto["comparacao"] = dre_previsao.comparar(ano, mes)
    elif aba == "documentos":
        contexto["documentos"] = documentos_contabeis.listar(ano, mes)
    else:
        contas = dre_contabil.contas_do_mes(ano, mes)
        conta = request.args.get("conta") or (contas[0].conta if contas else None)
        contexto.update(
            contas=contas, conta_sel=next((c for c in contas if c.conta == conta), None),
            lancamentos=dre_contabil.lancamentos_da_conta(ano, mes, conta) if conta else [],
        )

    # prévia do razão enviado (antes de substituir a competência)
    previa, token = None, request.args.get("previa")
    if token:
        tmp = arquivos.temporario_existente(token, request.args.get("ext", ".xls"))
        if tmp is None:
            flash("A prévia do razão expirou. Selecione o arquivo de novo.", "warning")
        else:
            try:
                previa = dre_contabil.previa_razao(tmp)
                previa["token"] = token
                previa["nome"] = request.args.get("nome", "razao.xls")[:200]
            except (ValueError, KeyError) as exc:
                tmp.unlink(missing_ok=True)
                flash(f"Não consegui ler o razão: {exc}", "error")

    return render_template(
        "dre/contabil.html",
        aba=aba, ano=ano, mes=mes, mes_nome=MESES_PT[mes],
        ano_ant=ano_ant, mes_ant=mes_ant, ano_prox=ano_prox, mes_prox=mes_prox,
        faturamento_informado=dre_previsao.faturamento_informado(ano, mes),
        meses_importados=dre_contabil.meses_importados(),
        parametros=dre_previsao.parametros(),
        previa=previa,
        meses_pt=MESES_PT,
        **contexto,
    )


@bp.route("/contabil/faturamento", methods=["POST"])
@requer("fechamento.conferir")
def contabil_faturamento():
    """Informa o faturamento bruto do mês (PDV) — base da previsão da receita."""
    ano, mes = _mes_ano()
    texto = (request.form.get("faturamento") or "").strip().replace(".", "").replace(",", ".")
    try:
        valor = Decimal(texto)
    except (InvalidOperation, ValueError):
        flash("Informe o faturamento do mês (ex.: 442.913,13).", "error")
        return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    dre_previsao.definir_faturamento(ano, mes, valor)
    db.session.commit()
    flash(f"Faturamento de {MESES_PT[mes]}/{ano} registrado.", "success")
    return redirect(url_for("dre.contabil", ano=ano, mes=mes))


@bp.route("/contabil/razao/previa", methods=["POST"])
@requer("fechamento.importar_razao")
def contabil_razao_previa():
    """1º passo: guarda o arquivo temporariamente e mostra a competência e o que será substituído."""
    ano, mes = _mes_ano()
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        flash("Selecione o arquivo do razão (.xls ou .xlsx).", "error")
        return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    ext = Path(arquivo.filename).suffix.lower()
    if ext not in EXTENSOES_RAZAO:
        flash("O razão da contabilidade deve ser .xls (como a Marsal envia) ou .xlsx.", "error")
        return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    token, destino = arquivos.temporario(ext)
    arquivo.save(destino)
    return redirect(url_for("dre.contabil", ano=ano, mes=mes, previa=token, nome=arquivo.filename, ext=ext))


@bp.route("/contabil/razao", methods=["POST"])
@requer("fechamento.importar_razao")
def contabil_razao():
    """Importa o razão (.xls/.xlsx) e recalibra a previsão.

    Aceita o token da prévia (fluxo da tela) ou o arquivo direto. O arquivo
    original fica guardado nos documentos da competência.
    """
    ano, mes = _mes_ano()
    token = request.form.get("previa")
    nome_original = (request.form.get("nome") or "razao.xls")[:200]
    ext = Path(nome_original).suffix.lower()
    if ext not in EXTENSOES_RAZAO:
        ext = ".xls"
    if token:
        destino = arquivos.temporario_existente(token, ext)
        if destino is None:
            flash("A prévia do razão expirou. Selecione o arquivo de novo.", "error")
            return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    else:
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione o arquivo do razão (.xls ou .xlsx).", "error")
            return redirect(url_for("dre.contabil", ano=ano, mes=mes))
        ext = Path(arquivo.filename).suffix.lower()
        if ext not in EXTENSOES_RAZAO:
            flash("O razão da contabilidade deve ser .xls (como a Marsal envia) ou .xlsx.", "error")
            return redirect(url_for("dre.contabil", ano=ano, mes=mes))
        nome_original = arquivo.filename
        destino = arquivos.temporario(ext)[1]
        arquivo.save(destino)
    try:
        rel = dre_contabil.importar_razao(destino)
        dre_previsao.calibrar(rel["ano"], rel["mes"])
        documentos_contabeis.registrar(rel["ano"], rel["mes"], "razao", nome_original, origem=destino)
    except (ValueError, KeyError) as exc:
        db.session.rollback()
        flash(f"Não consegui ler o razão: {exc}", "error")
        return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Falha ao importar o razão")
        flash(f"Falha ao importar o razão: {exc}", "error")
        return redirect(url_for("dre.contabil", ano=ano, mes=mes))
    finally:
        destino.unlink(missing_ok=True)

    db.session.commit()
    flash(
        f"Razão de {MESES_PT[rel['mes']]}/{rel['ano']} importado: {rel['lancamentos']} lançamentos, "
        f"{rel['contas']} contas. Previsão recalibrada.",
        "success",
    )
    return redirect(url_for("dre.contabil", ano=rel["ano"], mes=rel["mes"]))


# ---------------------------- documentos do fechamento ----------------------------

@bp.route("/contabil/documentos", methods=["POST"])
@requer("fechamento.conferir")
def contabil_documento_enviar():
    """Anexa o DRE em PDF (ou outro documento) recebido da contabilidade."""
    ano, mes = _mes_ano()
    tipo = request.form.get("tipo") if request.form.get("tipo") in ("dre_pdf", "outro") else "dre_pdf"
    arquivo = request.files.get("arquivo")
    voltar = url_for("dre.contabil", ano=ano, mes=mes, aba="documentos")
    if not arquivo or not arquivo.filename:
        flash("Selecione o arquivo do documento.", "error")
        return redirect(voltar)
    try:
        documentos_contabeis.registrar(ano, mes, tipo, arquivo.filename, conteudo=arquivo.read(),
                                       observacao=request.form.get("observacao"))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(voltar)
    db.session.commit()
    flash(f"Documento “{arquivo.filename}” anexado a {MESES_PT[mes]}/{ano}.", "success")
    return redirect(voltar)


@bp.route("/contabil/documentos/<int:doc_id>/arquivo")
@login_required
def contabil_documento_arquivo(doc_id: int):
    doc = db.session.get(DocumentoContabil, doc_id) or abort(404)
    try:
        caminho = arquivos.caminho_absoluto(doc.caminho)
    except ValueError:
        abort(404)
    if not caminho.exists():
        abort(404)
    # só PDF abre no navegador; o resto sempre baixa (arquivo enviado nunca roda como página)
    eh_pdf = doc.nome_arquivo.lower().endswith(".pdf")
    resp = send_file(caminho, as_attachment=request.args.get("baixar") is not None or not eh_pdf,
                     download_name=doc.nome_arquivo, mimetype="application/pdf" if eh_pdf else None)
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; object-src 'self'"
    return resp


@bp.route("/contabil/documentos/<int:doc_id>/situacao", methods=["POST"])
@requer("fechamento.conferir")
def contabil_documento_situacao(doc_id: int):
    doc = db.session.get(DocumentoContabil, doc_id) or abort(404)
    voltar = url_for("dre.contabil", ano=doc.ano, mes=doc.mes, aba="documentos")
    try:
        documentos_contabeis.mudar_situacao(doc, request.form.get("situacao", ""),
                                            pode_aprovar=current_user.pode("fechamento.aprovar"))
    except PermissionError:
        abort(403)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(voltar)
    db.session.commit()
    flash(f"“{doc.nome_arquivo}” marcado como {doc.situacao}.", "success")
    return redirect(voltar)
