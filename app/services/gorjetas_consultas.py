# -*- coding: utf-8 -*-
"""Cola entre o banco e a engine pura de gorjetas."""
from decimal import Decimal

from app.extensions import db
from app.models.gorjetas import (
    Colaborador,
    DescontoQuinzena,
    FechamentoGorjeta,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Setor,
)
from app.services import gorjetas as engine


def percentuais_setores() -> dict[str, Decimal]:
    setores = db.session.execute(db.select(Setor).filter_by(ativo=True)).scalars().all()
    return {s.nome: Decimal(s.percentual_rateio) for s in setores}


def montar_entrada(periodo: PeriodoGorjeta) -> tuple[list[engine.ColaboradorRateio], dict]:
    """Monta os insumos da engine a partir do período (presenças + participações)."""
    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    presencas_por_colab: dict[int, set] = {}
    for pres in periodo.presencas:
        if pres.presente:
            presencas_por_colab.setdefault(pres.colaborador_id, set()).add(pres.data)

    # participantes: quem tem participação registrada OU presença marcada;
    # se nada existe ainda, todos os colaboradores ativos entram zerados
    ids = set(participacoes) | set(presencas_por_colab)
    if ids:
        colaboradores_db = db.session.execute(
            db.select(Colaborador).filter(Colaborador.id.in_(ids))
        ).scalars().all()
    else:
        colaboradores_db = db.session.execute(
            db.select(Colaborador).filter_by(ativo=True).order_by(Colaborador.nome)
        ).scalars().all()

    entrada = []
    for c in colaboradores_db:
        part = participacoes.get(c.id)
        # a quinzena guarda a função/setor vigentes nela; o cadastro é só o padrão
        funcao = (part.funcao if part and part.funcao else c.funcao)
        setor = (part.setor if part and part.setor else c.setor)
        entrada.append(engine.ColaboradorRateio(
            id=c.id,
            nome=c.nome,
            setor=setor.nome,
            funcao=funcao.nome,
            registro=c.registro,
            # os pontos da quinzena mandam (a tabela muda com o tempo)
            pontos=Decimal(part.pontos) if part and part.pontos is not None else Decimal(c.pontos),
            presencas=presencas_por_colab.get(c.id, set()),
            dias_trabalhados_manual=part.dias_trabalhados_manual if part else None,
            desconto=Decimal(part.desconto) if part else Decimal("0"),
            em_ferias=bool(part.em_ferias) if part else False,
        ))

    comissao_por_dia = {cd.data: Decimal(cd.valor) for cd in periodo.comissoes_diarias}
    return entrada, comissao_por_dia


def descontos_setor_dict(periodo: PeriodoGorjeta) -> dict[str, Decimal]:
    """Soma dos descontos por setor para este período: {nome_setor: total}."""
    total: dict[str, Decimal] = {}
    for d in periodo.descontos_setor:
        nome = d.setor.nome
        total[nome] = total.get(nome, Decimal("0")) + Decimal(d.valor)
    return total


def extras_do_periodo(periodo: PeriodoGorjeta) -> list[engine.ExtraRateio]:
    """Diárias de extras: o setor repõe apenas o rateio do extra."""
    return [
        engine.ExtraRateio(
            data=e.data, setor=e.setor.nome, pontos=Decimal(e.pontos),
            comissao_turno=Decimal(e.comissao_turno),
            pago_pelo_restaurante=Decimal(e.valor_pago), turno=e.turno or "",
        )
        for e in periodo.extras
    ]


def calcular(periodo: PeriodoGorjeta) -> engine.ResultadoRateio:
    """Rateio da quinzena, já com a devolução do saldo de centavos do setor.

    Aqui é só projeção: o saldo só é gravado no fechamento.
    """
    resultado = _calcular_sem_saldo(periodo)
    resultado.saldo_setor = aplicar_saldo_setor(periodo, resultado, persistir=False)
    return resultado


def _calcular_sem_saldo(periodo: PeriodoGorjeta) -> engine.ResultadoRateio:
    colaboradores, comissao_por_dia = montar_entrada(periodo)
    descontos_s = descontos_setor_dict(periodo)
    return engine.calcular_rateio(
        comissao_bruta=Decimal(periodo.comissao_bruta),
        percentual_encargos=Decimal(periodo.percentual_encargos),
        percentuais_setor=percentuais_setores(),
        colaboradores=colaboradores,
        comissao_por_dia=comissao_por_dia or None,
        descontos_setor=descontos_s or None,
        extras=extras_do_periodo(periodo) or None,
    )


def fechar(periodo: PeriodoGorjeta) -> engine.ResultadoRateio:
    """Fecha a quinzena: grava snapshot imutável e muda o status.

    Depois de fechado NÃO recalcula (histórico auditável). O commit fica
    com o chamador.
    """
    resultado = _calcular_sem_saldo(periodo)
    # o saldo de centavos do setor é acertado e gravado junto do fechamento
    resultado.saldo_setor = aplicar_saldo_setor(periodo, resultado, persistir=True)
    for r in resultado.colaboradores:
        db.session.add(FechamentoGorjeta(
            periodo_id=periodo.id,
            colaborador_id=r.id,
            nome=r.nome,
            setor=r.setor,
            funcao=r.funcao,
            registro=r.registro,
            pontos=r.pontos,
            dias_trabalhados=r.dias_trabalhados,
            desconto=r.desconto,
            desconto_setor=r.desconto_setor,
            desconto_extra=r.desconto_extra,
            reembolso_ferias=r.reembolso_ferias,
            liquido_a_pagar=r.liquido,
        ))
    periodo.status = "fechado"
    return resultado


def garantir_participacoes(periodo: PeriodoGorjeta) -> None:
    """Pré-popula com colaboradores da última quinzena fechada (ou todos ativos se não houver)."""
    existentes = {p.colaborador_id for p in periodo.participacoes}

    ultima_fechada = db.session.execute(
        db.select(PeriodoGorjeta)
        .filter(PeriodoGorjeta.status == "fechado", PeriodoGorjeta.id != periodo.id)
        .order_by(PeriodoGorjeta.data_fim.desc())
    ).scalars().first()

    anterior: dict[int, ParticipacaoPeriodo] = {}
    if ultima_fechada:
        anterior = {p.colaborador_id: p for p in ultima_fechada.participacoes}
        base = db.session.execute(
            db.select(Colaborador).filter(
                Colaborador.id.in_(set(anterior)),
                Colaborador.ativo == True,
            )
        ).scalars().all()
    else:
        base = db.session.execute(
            db.select(Colaborador).filter_by(ativo=True)
        ).scalars().all()

    for c in base:
        if c.id in existentes:
            continue
        # a equipe começa como estava na quinzena anterior (função, setor e pontos),
        # não como está o cadastro hoje — quem mudou de função depois não reescreve o passado
        antes = anterior.get(c.id)
        db.session.add(ParticipacaoPeriodo(
            periodo_id=periodo.id, colaborador_id=c.id,
            funcao_id=(antes.funcao_id if antes and antes.funcao_id else c.funcao_id),
            setor_id=(antes.setor_id if antes and antes.setor_id else c.setor_id),
            pontos=(antes.pontos if antes and antes.pontos is not None else c.pontos),
        ))


def reabrir(periodo: PeriodoGorjeta) -> int:
    """Desfaz o fechamento: apaga o snapshot e devolve a quinzena para edição.

    Os valores pagos voltam a ser recalculados a partir de presenças, descontos
    e extras — é isso que permite corrigir um erro descoberto depois do
    fechamento. O commit fica com o chamador.
    """
    if not periodo.fechado:
        raise ValueError("A quinzena já está aberta.")
    apagados = db.session.execute(
        db.delete(FechamentoGorjeta).where(FechamentoGorjeta.periodo_id == periodo.id)
    ).rowcount
    periodo.status = "aberto"
    return apagados


def excluir(periodo: PeriodoGorjeta) -> None:
    """Apaga a quinzena inteira, aberta ou fechada.

    Presenças, participações, descontos e extras saem por cascata; o snapshot
    do fechamento não tem cascata e é removido aqui. O commit fica com o
    chamador.
    """
    db.session.execute(
        db.delete(FechamentoGorjeta).where(FechamentoGorjeta.periodo_id == periodo.id))
    db.session.delete(periodo)


def aplicar_equipe(periodo: PeriodoGorjeta, escolhidos: dict[int, dict]) -> dict:
    """Define quem participa da quinzena e com qual função, setor e pontos.

    `escolhidos` = {colaborador_id: {"funcao_id", "setor_id", "pontos"}}. Quem
    ficar de fora perde a participação e as presenças daquela quinzena — sem
    isso a pessoa voltaria pelo rateio, já que presença também conta como
    participação. Não mexe no cadastro do colaborador: a mesma pessoa pode ter
    função diferente em quinzenas diferentes.
    """
    from app.models.gorjetas import Presenca

    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    entraram, sairam, mudaram = 0, 0, 0

    for colaborador_id, dados in escolhidos.items():
        part = participacoes.get(colaborador_id)
        if part is None:
            part = ParticipacaoPeriodo(periodo_id=periodo.id, colaborador_id=colaborador_id)
            db.session.add(part)
            entraram += 1
        antes = (part.funcao_id, part.setor_id, part.pontos)
        part.funcao_id = dados.get("funcao_id") or None
        part.setor_id = dados.get("setor_id") or None
        if dados.get("pontos") is not None:
            part.pontos = dados["pontos"]
        if antes != (part.funcao_id, part.setor_id, part.pontos):
            mudaram += 1

    for colaborador_id, part in participacoes.items():
        if colaborador_id in escolhidos:
            continue
        db.session.execute(db.delete(Presenca).where(
            Presenca.periodo_id == periodo.id, Presenca.colaborador_id == colaborador_id))
        db.session.delete(part)
        sairam += 1

    db.session.flush()
    return {"entraram": entraram, "sairam": sairam, "mudaram": mudaram}


def equipe_da_quinzena(periodo: PeriodoGorjeta) -> list[dict]:
    """Linhas da etapa Equipe: quem está na quinzena e quem pode entrar."""
    participacoes = {p.colaborador_id: p for p in periodo.participacoes}
    candidatos = db.session.execute(
        db.select(Colaborador).filter(
            db.or_(Colaborador.ativo == True, Colaborador.id.in_(set(participacoes) or {0}))
        ).order_by(Colaborador.nome)
    ).scalars().all()

    linhas = []
    for c in candidatos:
        part = participacoes.get(c.id)
        linhas.append({
            "colaborador": c,
            "participa": part is not None,
            "funcao_id": (part.funcao_id if part and part.funcao_id else c.funcao_id),
            "setor_id": (part.setor_id if part and part.setor_id else c.setor_id),
            "pontos": (part.pontos if part and part.pontos is not None else c.pontos),
            # objetos resolvidos: a grade de presença mostra a realidade da quinzena
            "funcao": (part.funcao if part and part.funcao else c.funcao),
            "setor": (part.setor if part and part.setor else c.setor),
        })
    return linhas


CENTAVO = Decimal("0.01")


def _sobra_do_setor(resultado, setor: str) -> Decimal:
    """Quanto do pool do setor não chegou a ninguém nesta quinzena.

    pool = líquidos pagos + descontos + extras repostos + sobra. Positivo = o
    restaurante deve ao setor; negativo = o setor recebeu a mais.
    """
    pessoas = [c for c in resultado.colaboradores if c.setor == setor]
    if not pessoas:
        return Decimal("0")
    pago = sum((c.liquido + c.desconto + c.desconto_setor for c in pessoas), Decimal("0"))
    extras = sum((e.reposto_pelo_setor for e in resultado.extras if e.setor == setor), Decimal("0"))
    return (Decimal(resultado.pools.get(setor, 0)) - pago - extras).quantize(CENTAVO)


def saldos_anteriores() -> dict[int, Decimal]:
    """Saldo acumulado de cada setor (último registro de cada um)."""
    from app.models.gorjetas import SaldoSetorQuinzena

    saldos: dict[int, Decimal] = {}
    linhas = db.session.execute(
        db.select(SaldoSetorQuinzena)
        .join(PeriodoGorjeta, PeriodoGorjeta.id == SaldoSetorQuinzena.periodo_id)
        .order_by(PeriodoGorjeta.data_fim, SaldoSetorQuinzena.id)
    ).scalars().all()
    for linha in linhas:
        saldos[linha.setor_id] = Decimal(linha.saldo)
    return saldos


def aplicar_saldo_setor(periodo: PeriodoGorjeta, resultado, persistir: bool = False) -> dict:
    """Devolve ao setor os centavos acumulados, em partes iguais.

    A sobra de cada quinzena entra no saldo do setor. Quando o saldo dá para
    dividir em partes iguais entre as pessoas do setor, cada uma recebe a mesma
    quantia e o resto continua guardado — nunca alguém recebe um centavo a mais
    que o colega de mesma carga, e nada fica com o restaurante.
    """
    from app.models.gorjetas import SaldoSetorQuinzena, Setor

    setores = {s.nome: s for s in db.session.execute(db.select(Setor)).scalars()}
    anteriores = saldos_anteriores()
    movimento: dict[str, dict] = {}

    for nome, setor in setores.items():
        pessoas = [c for c in resultado.colaboradores if c.setor == nome]
        if not pessoas:
            continue
        anterior = anteriores.get(setor.id, Decimal("0"))
        gerado = _sobra_do_setor(resultado, nome)
        acumulado = (anterior + gerado).quantize(CENTAVO)

        # em centavos inteiros: quanto dá para dar igual a cada pessoa
        centavos = int((acumulado / CENTAVO).to_integral_value())
        por_pessoa_centavos = centavos // len(pessoas) if centavos > 0 else 0
        por_pessoa = (Decimal(por_pessoa_centavos) * CENTAVO).quantize(CENTAVO)
        distribuido = (por_pessoa * len(pessoas)).quantize(CENTAVO)

        if por_pessoa:
            for c in pessoas:
                c.credito_saldo = por_pessoa
                c.liquido = (c.liquido + por_pessoa).quantize(CENTAVO)
            resultado.total_a_pagar = (resultado.total_a_pagar + distribuido).quantize(CENTAVO)

        saldo = (acumulado - distribuido).quantize(CENTAVO)
        movimento[nome] = {"anterior": anterior, "gerado": gerado,
                           "distribuido": distribuido, "saldo": saldo, "pessoas": len(pessoas)}

        if persistir:
            registro = db.session.execute(
                db.select(SaldoSetorQuinzena).filter_by(periodo_id=periodo.id, setor_id=setor.id)
            ).scalar_one_or_none()
            if registro is None:
                registro = SaldoSetorQuinzena(periodo_id=periodo.id, setor_id=setor.id)
                db.session.add(registro)
            registro.anterior, registro.gerado = anterior, gerado
            registro.distribuido, registro.saldo = distribuido, saldo
            registro.pessoas = len(pessoas)

    return movimento
