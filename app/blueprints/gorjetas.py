# -*- coding: utf-8 -*-
"""Gorjetas: quinzenas, presença, comissão diária, rateio e fechamento."""
import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models.gorjetas import (
    MOTIVOS_DESCONTO,
    TIPOS_DESCONTO_SETOR,
    Colaborador,
    ComissaoDiaria,
    DescontoQuinzena,
    ExtraQuinzena,
    FechamentoGorjeta,
    Funcao,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Presenca,
    Setor,
)
from app.services import (
    auditoria,
    gorjetas_consultas,
    gorjetas_relatorios,
    metas_consultas,
)
from app.services.metas import meta_quinzena
from app.utils.filtros import MESES_PT
from app.utils.permissoes import requer

bp = Blueprint("gorjetas", __name__)


# etapas da tela de quinzena (a de Equipe entrou antes das presenças)
ETAPAS = ("1", "2", "3", "4", "5")


def _pontos(texto):
    """Pontos da quinzena: vazio mantém o padrão da função."""
    valor = _parse_valor(texto or "")
    return valor if valor else None


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
    # total: fechada = soma do snapshot pago; aberta = recálculo atual
    totais = {}
    for p in periodos:
        if p.fechado:
            totais[p.id] = sum((f.liquido_a_pagar for f in p.fechamentos), Decimal("0"))
        elif p.comissoes_diarias or p.comissao_bruta:
            totais[p.id] = gorjetas_consultas.calcular(p).total_a_pagar
    hoje = date.today()
    return render_template("gorjetas/listar.html", periodos=periodos, totais=totais, hoje=hoje)


@bp.route("/novo", methods=["POST"])
@requer("gorjetas.editar")
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
        return render_template(
            "gorjetas/fechada.html",
            periodo=periodo,
            fechamentos=fechamentos,
            tem_ferias=any(f.reembolso_ferias > 0 for f in fechamentos),
        )

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
    colaboradores_inativos = db.session.execute(
        db.select(Colaborador).filter_by(ativo=False).order_by(Colaborador.nome)
    ).scalars().all()
    setores = db.session.execute(
        db.select(Setor).filter_by(ativo=True).order_by(Setor.nome)
    ).scalars().all()
    funcoes = db.session.execute(
        db.select(Funcao).order_by(Funcao.setor_id, Funcao.nome)
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
        colaboradores_inativos=colaboradores_inativos,
        setores=setores,
        funcoes=funcoes,
        motivos_desconto=MOTIVOS_DESCONTO,
        tipos_desconto_setor=TIPOS_DESCONTO_SETOR,
        tem_ferias=any(r.em_ferias for r in resultado.colaboradores),
        equipe=gorjetas_consultas.equipe_da_quinzena(periodo),
        etapa=(request.args.get("etapa") if request.args.get("etapa") in ETAPAS else "1"),
    )


@bp.route("/<int:periodo_id>/salvar", methods=["POST"])
@requer("gorjetas.editar")
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

    # equipe: só quando a etapa foi enviada (lista explícita de candidatos)
    candidatos = {i for i in request.form.getlist("equipe_ids", type=int) if i}
    if candidatos:
        escolhidos = {}
        for colaborador_id in candidatos:
            if f"participa_{colaborador_id}" not in request.form:
                continue
            escolhidos[colaborador_id] = {
                "funcao_id": request.form.get(f"funcao_{colaborador_id}", type=int),
                "setor_id": request.form.get(f"setor_{colaborador_id}", type=int),
                "pontos": _pontos(request.form.get(f"pontos_{colaborador_id}")),
            }
        gorjetas_consultas.aplicar_equipe(periodo, escolhidos)
        db.session.flush()
        participacoes = {p.colaborador_id: p for p in periodo.participacoes}

    # férias: só para quem foi exibido na etapa de ajustes (lista explícita de ids)
    ferias_exibidos = {i for i in request.form.getlist("ferias_ids", type=int) if i}
    if ferias_exibidos:
        db.session.flush()  # inclui participações criadas acima pelos descontos
        participacoes = {
            p.colaborador_id: p for p in db.session.execute(
                db.select(ParticipacaoPeriodo).filter_by(periodo_id=periodo.id)).scalars()
        }
    for colab_id in ferias_exibidos:
        part = participacoes.get(colab_id)
        marcado = f"ferias_{colab_id}" in request.form
        if part:
            part.em_ferias = marcado
        elif marcado:
            db.session.add(ParticipacaoPeriodo(periodo_id=periodo.id, colaborador_id=colab_id, em_ferias=True))

    auditoria.registrar("update", "periodo_gorjeta", periodo.id,
                        depois={"comissao_bruta": periodo.comissao_bruta})
    db.session.commit()
    flash("Quinzena salva.", "success")
    etapa = request.form.get("etapa") if request.form.get("etapa") in ETAPAS else None
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id, etapa=etapa))


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
                "bruto": float(r.bruto_rateado),
                "desconto_setor": float(r.desconto_setor),
                "desconto": float(r.desconto),
                "liquido": float(r.liquido),
            }
            for r in resultado.colaboradores
        ],
    })


@bp.route("/<int:periodo_id>/relatorio/<tipo>.pdf")
@requer("relatorios.gerar")
def relatorio(periodo_id: int, tipo: str):
    """PDF da quinzena: completo, resumido (sem "por fora") ou de férias."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)

    geradores = {
        "completo": gorjetas_relatorios.pdf_completo,
        "resumido": gorjetas_relatorios.pdf_resumido,
        "ferias": gorjetas_relatorios.pdf_ferias,
    }
    if tipo not in geradores:
        abort(404)

    conteudo = geradores[tipo](periodo)
    if conteudo is None:  # só acontece em "ferias" sem ninguém de férias
        flash("Ninguém está de férias nesta quinzena — o relatório de férias não se aplica.",
              "warning")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))

    return send_file(
        BytesIO(conteudo),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") is not None,
        download_name=gorjetas_relatorios.nome_arquivo(periodo, tipo),
    )


@bp.route("/<int:periodo_id>/descontos/add", methods=["POST"])
@requer("gorjetas.editar")
def desconto_setor_add(periodo_id: int):
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado:
        flash("Quinzena fechada.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id))

    setor_id = request.form.get("setor_id", type=int)
    tipo = request.form.get("tipo", "").strip()
    valor = _parse_valor(request.form.get("valor", ""))
    obs = (request.form.get("observacao") or "").strip() or None

    if not setor_id or not tipo or not valor:
        flash("Preencha setor, tipo e valor.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id))

    db.session.add(DescontoQuinzena(
        periodo_id=periodo_id, setor_id=setor_id, tipo=tipo, valor=valor, observacao=obs,
    ))
    auditoria.registrar("create", "desconto_quinzena", periodo_id,
                        depois={"setor_id": setor_id, "tipo": tipo, "valor": str(valor)})
    db.session.commit()
    flash("Desconto por setor adicionado.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=3))


@bp.route("/<int:periodo_id>/extras/add", methods=["POST"])
@requer("gorjetas.editar")
def extra_add(periodo_id: int):
    """Diária de pessoa de fora: o setor repõe só o que o extra renderia no rateio."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    voltar = url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=3)
    if periodo.fechado:
        flash("Quinzena fechada.", "error")
        return redirect(voltar)
    setor_id = request.form.get("setor_id", type=int)
    try:
        data_extra = date.fromisoformat(request.form.get("data") or "")
    except ValueError:
        data_extra = None
    pontos = _parse_valor(request.form.get("pontos", ""))
    comissao = _parse_valor(request.form.get("comissao_turno", ""))
    pago = _parse_valor(request.form.get("valor_pago", ""))
    if not setor_id or not data_extra or not pontos or pontos <= 0:
        flash("Preencha setor, dia e pontos do extra.", "error")
        return redirect(voltar)
    if not periodo.data_inicio <= data_extra <= periodo.data_fim:
        flash("O dia do extra precisa estar dentro da quinzena.", "error")
        return redirect(voltar)
    extra = ExtraQuinzena(
        periodo_id=periodo_id, setor_id=setor_id, data=data_extra,
        turno=(request.form.get("turno") or "").strip()[:30] or None,
        pontos=pontos, comissao_turno=comissao, valor_pago=pago,
        observacao=(request.form.get("observacao") or "").strip()[:255] or None,
    )
    db.session.add(extra)
    db.session.flush()
    auditoria.registrar("create", "extra_quinzena", extra.id,
                        depois={"data": data_extra, "setor_id": setor_id, "pontos": pontos,
                                "comissao_turno": comissao, "valor_pago": pago})
    db.session.commit()
    flash("Extra registrado.", "success")
    return redirect(voltar)


@bp.route("/<int:periodo_id>/extras/<int:extra_id>/remover", methods=["POST"])
@requer("gorjetas.editar")
def extra_remover(periodo_id: int, extra_id: int):
    extra = db.session.get(ExtraQuinzena, extra_id) or abort(404)
    if extra.periodo_id != periodo_id:
        abort(400)
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    voltar = url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=3)
    if periodo.fechado:
        flash("Quinzena fechada.", "error")
        return redirect(voltar)
    auditoria.registrar("delete", "extra_quinzena", extra.id,
                        antes={"data": extra.data, "pontos": extra.pontos, "valor_pago": extra.valor_pago})
    db.session.delete(extra)
    db.session.commit()
    flash("Extra removido.", "success")
    return redirect(voltar)


@bp.route("/<int:periodo_id>/descontos/<int:desconto_id>/remover", methods=["POST"])
@requer("gorjetas.editar")
def desconto_setor_remover(periodo_id: int, desconto_id: int):
    d = db.session.get(DescontoQuinzena, desconto_id) or abort(404)
    if d.periodo_id != periodo_id:
        abort(400)
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado:
        flash("Quinzena fechada.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id))
    db.session.delete(d)
    db.session.commit()
    flash("Desconto por setor removido.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=3))


@bp.route("/<int:periodo_id>/colaborador/<int:colab_id>/desativar", methods=["POST"])
@requer("gorjetas.editar")
def desativar_colaborador(periodo_id: int, colab_id: int):
    c = db.session.get(Colaborador, colab_id) or abort(404)
    c.ativo = False
    auditoria.registrar("update", "colaborador", c.id,
                        antes={"ativo": True}, depois={"ativo": False})
    db.session.commit()
    flash(f"{c.nome} desativado.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=3))


@bp.route("/<int:periodo_id>/colaborador/adicionar", methods=["POST"])
@requer("gorjetas.editar")
def adicionar_colaborador(periodo_id: int):
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado:
        flash("Quinzena fechada.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id))

    nome = (request.form.get("nome") or "").strip()
    funcao_id = request.form.get("funcao_id", type=int)

    if not nome or not funcao_id:
        flash("Preencha nome e função.", "error")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id))

    funcao = db.session.get(Funcao, funcao_id) or abort(404)

    # Reativar se já existe (mesmo nome, case-insensitive)
    existente = db.session.execute(
        db.select(Colaborador).filter(Colaborador.nome.ilike(nome))
    ).scalar_one_or_none()

    if existente:
        existente.ativo = True
        existente.funcao_id = funcao_id
        existente.setor_id = funcao.setor_id
        existente.pontos = funcao.pontos_padrao
        c = existente
        auditoria.registrar("update", "colaborador", c.id,
                            depois={"ativo": True, "funcao_id": funcao_id})
    else:
        c = Colaborador(
            nome=nome, funcao_id=funcao_id, setor_id=funcao.setor_id,
            pontos=funcao.pontos_padrao, registro="CLT", ativo=True,
        )
        db.session.add(c)
        db.session.flush()
        auditoria.registrar("create", "colaborador", c.id, depois={"nome": nome})

    # Garantir participação nesta quinzena
    existing_part = db.session.execute(
        db.select(ParticipacaoPeriodo).filter_by(periodo_id=periodo_id, colaborador_id=c.id)
    ).scalar_one_or_none()
    if not existing_part:
        db.session.add(ParticipacaoPeriodo(periodo_id=periodo_id, colaborador_id=c.id))

    db.session.commit()
    flash(f"{c.nome} adicionado à quinzena.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo_id, etapa=2))


@bp.route("/<int:periodo_id>/fechar", methods=["POST"])
@requer("gorjetas.fechar")
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


@bp.route("/<int:periodo_id>/reabrir", methods=["POST"])
@requer("gorjetas.excluir")
def reabrir(periodo_id: int):
    """Volta a quinzena para edição, apagando o snapshot do fechamento."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    try:
        apagados = gorjetas_consultas.reabrir(periodo)
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))

    auditoria.registrar("update", "periodo_gorjeta", periodo.id,
                        antes={"status": "fechado", "linhas_do_fechamento": apagados},
                        depois={"status": "aberto"})
    db.session.commit()
    flash(f"{periodo.referencia} reaberta: os valores voltam a ser calculados. "
          f"Feche de novo quando terminar a correção.", "success")
    return redirect(url_for("gorjetas.detalhe", periodo_id=periodo.id))


@bp.route("/<int:periodo_id>/excluir", methods=["POST"])
@requer("gorjetas.editar")
def excluir(periodo_id: int):
    """Apaga a quinzena. Montar já dá direito de descartar a que está em aberto;
    apagar uma fechada é mais grave e exige a permissão de reabrir/excluir."""
    periodo = db.session.get(PeriodoGorjeta, periodo_id) or abort(404)
    if periodo.fechado and not current_user.pode("gorjetas.excluir"):
        abort(403)

    referencia, era_fechada = periodo.referencia, periodo.fechado
    auditoria.registrar("delete", "periodo_gorjeta", periodo.id,
                        antes={"referencia": referencia,
                               "status": "fechado" if era_fechada else "aberto"})
    gorjetas_consultas.excluir(periodo)
    db.session.commit()
    flash(f"{referencia} excluída.", "success")
    return redirect(url_for("gorjetas.listar"))
