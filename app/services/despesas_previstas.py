# -*- coding: utf-8 -*-
"""Despesas previstas: cadastro, projeção no caixa e baixa automática.

Fluxo: a Manu cadastra o que está combinado (ex.: Get Distribuidora, R$ 801,79
em 25/09). O sistema cria um lançamento previsto no caixa (origem 'previsto')
e, quando o pagamento aparece no extrato, casa os dois, apaga a previsão e
marca a despesa como baixada — avisando na tela que a baixa foi automática.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.extensions import db
from app.models.banco import ConciliacaoItem, MovimentoBancario
from app.models.fluxo import Categoria, Lancamento
from app.models.previsao import DespesaPrevista
from app.services import auditoria
from app.utils.datas import hoje_sp

ORIGEM = "previsto"
ZERO = Decimal("0.00")

# janela em que o pagamento real pode cair em relação à data prevista
DIAS_ANTES = 5
DIAS_DEPOIS = 10


def _origem_id(despesa: DespesaPrevista) -> str:
    return f"dp:{despesa.id}"


def lancamento_da(despesa: DespesaPrevista) -> Lancamento | None:
    return db.session.execute(
        db.select(Lancamento).filter_by(origem=ORIGEM, origem_id=_origem_id(despesa))
    ).scalar_one_or_none()


def _sincronizar_lancamento(despesa: DespesaPrevista) -> None:
    """A previsão só existe no caixa enquanto está em aberto."""
    lanc = lancamento_da(despesa)
    if not despesa.em_aberto:
        if lanc:
            db.session.execute(db.delete(ConciliacaoItem).where(
                ConciliacaoItem.lancamento_id == lanc.id))
            db.session.delete(lanc)
        return
    if lanc is None:
        lanc = Lancamento(origem=ORIGEM, origem_id=_origem_id(despesa))
        db.session.add(lanc)
    lanc.data = despesa.data_prevista
    lanc.categoria_id = despesa.categoria_id
    lanc.fornecedor_id = despesa.fornecedor_id
    lanc.valor = despesa.valor
    lanc.descricao = (despesa.descricao or "Pagamento combinado") + " · previsto"
    lanc.usuario_id = despesa.criado_por_id


def salvar(dados: dict, despesa: DespesaPrevista | None = None,
           usuario_id: int | None = None) -> DespesaPrevista:
    """Cria ou edita uma despesa prevista. Não faz commit."""
    valor = Decimal(str(dados["valor"]))
    if valor <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    categoria = db.session.get(Categoria, int(dados["categoria_id"]))
    if categoria is None or categoria.tipo != "saida":
        raise ValueError("Escolha uma categoria de saída.")

    if despesa is None:
        despesa = DespesaPrevista(criado_por_id=usuario_id)
        db.session.add(despesa)
    elif not despesa.em_aberto:
        raise ValueError("Esta despesa já foi baixada ou cancelada.")

    despesa.data_prevista = dados["data_prevista"]
    despesa.categoria_id = categoria.id
    despesa.fornecedor_id = dados.get("fornecedor_id") or None
    despesa.descricao = (dados.get("descricao") or "").strip() or None
    despesa.valor = valor
    db.session.flush()
    _sincronizar_lancamento(despesa)
    db.session.flush()
    auditoria.registrar("update" if despesa.id else "create", "despesa_prevista", despesa.id,
                        depois={"data": str(despesa.data_prevista), "valor": str(valor)})
    return despesa


def cancelar(despesa: DespesaPrevista) -> None:
    if not despesa.em_aberto:
        raise ValueError("Só dá para cancelar uma despesa ainda em aberto.")
    despesa.situacao = "cancelada"
    _sincronizar_lancamento(despesa)
    db.session.flush()
    auditoria.registrar("update", "despesa_prevista", despesa.id, depois={"situacao": "cancelada"})


def baixar(despesa: DespesaPrevista, movimento: MovimentoBancario | None = None,
           automatica: bool = False) -> None:
    """Marca como paga: sai da previsão do caixa (o pagamento real já está lá)."""
    despesa.situacao = "baixada"
    despesa.movimento_id = movimento.id if movimento else None
    despesa.baixada_em = datetime.utcnow()
    despesa.baixa_automatica = automatica
    despesa.aviso_pendente = automatica
    _sincronizar_lancamento(despesa)
    db.session.flush()


def _combina(despesa: DespesaPrevista, mov: MovimentoBancario) -> bool:
    if Decimal(mov.valor) != Decimal(despesa.valor):
        return False
    if not (despesa.data_prevista - timedelta(days=DIAS_ANTES)
            <= mov.data <= despesa.data_prevista + timedelta(days=DIAS_DEPOIS)):
        return False
    if despesa.fornecedor_id:
        # com fornecedor definido, o pagamento tem de ser daquele fornecedor
        lanc = mov.lancamento
        if lanc is not None and lanc.fornecedor_id == despesa.fornecedor_id:
            return True
        nome = (despesa.fornecedor.nome or "").strip().upper()
        alvo = f"{mov.contraparte or ''} {mov.descricao or ''}".upper()
        return bool(nome) and (nome[:12] in alvo or alvo[:12] in nome)
    return True


def baixar_automatico(inicio: date | None = None, fim: date | None = None) -> dict:
    """Casa as previsões em aberto com débitos do extrato. Não faz commit."""
    hoje = hoje_sp()
    inicio = inicio or hoje - timedelta(days=60)
    fim = fim or hoje

    abertas = db.session.execute(
        db.select(DespesaPrevista).filter_by(situacao="prevista").order_by(DespesaPrevista.data_prevista)
    ).scalars().all()
    if not abertas:
        return {"baixadas": 0, "total": ZERO}

    movimentos = db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.tipo == "debito",
            MovimentoBancario.data >= inicio - timedelta(days=DIAS_ANTES),
            MovimentoBancario.data <= fim + timedelta(days=DIAS_DEPOIS),
        ).order_by(MovimentoBancario.data)
    ).scalars().all()
    ja_usados = {d.movimento_id for d in db.session.execute(
        db.select(DespesaPrevista).filter(DespesaPrevista.movimento_id.isnot(None))
    ).scalars()}

    baixadas, total = 0, ZERO
    for despesa in abertas:
        for mov in movimentos:
            if mov.id in ja_usados or not _combina(despesa, mov):
                continue
            baixar(despesa, mov, automatica=True)
            ja_usados.add(mov.id)
            baixadas += 1
            total += Decimal(despesa.valor)
            break

    if baixadas:
        auditoria.registrar("update", "despesa_prevista", None,
                            depois={"baixa_automatica": baixadas, "total": str(total)})
    return {"baixadas": baixadas, "total": total}


def listar(situacao: str = "prevista", limite: int = 300) -> list[DespesaPrevista]:
    query = db.select(DespesaPrevista).order_by(DespesaPrevista.data_prevista, DespesaPrevista.id)
    if situacao in ("prevista", "baixada", "cancelada"):
        query = query.filter(DespesaPrevista.situacao == situacao)
    return db.session.execute(query.limit(limite)).scalars().all()


def avisos() -> list[DespesaPrevista]:
    """Baixas automáticas que ninguém viu ainda."""
    return db.session.execute(
        db.select(DespesaPrevista).filter_by(aviso_pendente=True)
        .order_by(DespesaPrevista.baixada_em.desc())
    ).scalars().all()


def marcar_avisos_vistos() -> int:
    pendentes = avisos()
    for despesa in pendentes:
        despesa.aviso_pendente = False
    db.session.flush()
    return len(pendentes)


def total_em_aberto(ate: date | None = None) -> Decimal:
    query = db.select(db.func.coalesce(db.func.sum(DespesaPrevista.valor), 0)).filter(
        DespesaPrevista.situacao == "prevista")
    if ate:
        query = query.filter(DespesaPrevista.data_prevista <= ate)
    return Decimal(db.session.execute(query).scalar_one())
