# -*- coding: utf-8 -*-
"""Sincronização do extrato Itaú: grava, classifica (visão CFO) e concilia.

- O extrato NÃO vira `Lancamento` (o fluxo vem da planilha; seria contar o
  mesmo dinheiro duas vezes). Fica em `movimento_bancario`.
- Cada movimento recebe uma categoria gerencial (classificacao_bancaria):
  operação × sócios/financiamentos × tesouraria × transferências próprias.
- A conciliação com o fluxo ignora tesouraria e transferências entre contas
  próprias, que não têm par na planilha.
Nada aqui faz commit — quem chama decide.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import current_app
from sqlalchemy import or_, case, func

from app.extensions import db
from app.importers import itau_adapter as itau
from app.models.banco import (
    ConciliacaoItem,
    MovimentoBancario,
    RegraClassificacaoBancaria,
    SaldoBancario,
)
from app.models.fluxo import Categoria, ConfigSistema, Lancamento
from app.services import auditoria
from app.services import classificacao_bancaria as cls
from app.services import conciliacao_bancaria as conc
from app.utils.datas import agora_sp, hoje_sp

BANCO = "itau"
ORIGEM_STONE = "stone"   # lançamentos que nascem do arquivo da Stone
CHAVE_ULTIMA_SYNC = "itau_ultima_sincronizacao"
TOLERANCIA_DIAS = 3
CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")
BLOCOS_FORA_CONCILIACAO = ("tesouraria", "transferencia")


def configurado(app=None) -> bool:
    cfg = itau.ItauConfig.from_app(app or current_app)
    return cfg.configurada and bool(cfg.contas)


def ultima_sincronizacao() -> datetime | None:
    txt = ConfigSistema.obter(CHAVE_ULTIMA_SYNC)
    try:
        return datetime.fromisoformat(txt) if txt else None
    except ValueError:
        return None


def _agora() -> datetime:
    return agora_sp().replace(tzinfo=None, microsecond=0)


def _dinheiro(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVOS)


# ------------------------------- classificação -------------------------------

def _cnpj_raiz() -> str:
    return current_app.config.get("EMPRESA_CNPJ_RAIZ") or ""


# movimentos cujas linhas do caixa nascem de outra fonte, já quebradas: o
# repasse da Stone (o arquivo diz quanto foi de cada bandeira) e o pagamento da
# sócia (pró-labore + distribuição de lucros). Escolher uma subcategoria neles
# não descreve a realidade — e ainda arriscaria duplicar valor no caixa.
LINHAS_DERIVADAS = ("rec_stone", "rec_cartao", "soc_a_detalhar", "soc_retirada", "pag_prolabore")


def _filtro_stone_com_arquivo():
    """Deixa de fora o repasse da Stone cujo arquivo do dia ainda não chegou.

    Enquanto o arquivo não sai (depois das 5h do dia seguinte), não há venda por
    bandeira para conciliar e não há o que revisar: o crédito entra inteiro como
    "Pix Stone" e só se divide quando o arquivo chega. Por decisão da consultoria
    ele não aparece na tela de Movimentações até lá — o dinheiro continua no caixa
    e no demonstrativo do período, que espelham o extrato.
    """
    tem_arquivo = (
        db.select(Lancamento.id)
        .filter(Lancamento.origem == ORIGEM_STONE, Lancamento.data == MovimentoBancario.data)
        .exists()
    )
    return db.or_(MovimentoBancario.categoria_gerencial != "rec_stone",
                  MovimentoBancario.tipo != "credito",
                  tem_arquivo)


def linhas_derivadas(movimentos) -> set[int]:
    """Ids dos movimentos cuja quebra em linhas já veio pronta de outra fonte."""
    return {m.id for m in movimentos if m.categoria_gerencial in LINHAS_DERIVADAS}


def itens_conciliados(movimentos) -> dict[int, list[dict]]:
    """Para cada movimento, as linhas do caixa que ele amarra.

    É o que a gaveta de revisão mostra quando a quebra vem pronta de outra
    fonte (repasse da Stone, pró-labore da sócia): em vez de perguntar a
    categoria, ela exibe o que já está lançado.
    """
    from app.models.fluxo import Categoria

    saida: dict[int, list[dict]] = {}
    for mov in movimentos:
        linhas = []
        for item in mov.itens_conciliacao:
            lanc = item.lancamento
            if lanc is None:
                continue
            cat = db.session.get(Categoria, lanc.categoria_id)
            linhas.append({"id": lanc.id, "nome": cat.nome if cat else "—",
                           "valor": str(lanc.valor)})
        if linhas:
            saida[mov.id] = sorted(linhas, key=lambda x: Decimal(x["valor"]), reverse=True)
    return saida


def regras_dos_movimentos(movimentos) -> dict[int, RegraClassificacaoBancaria]:
    """Para cada movimento, a regra do usuário que o alcança (se houver).

    A tela usa isto para reabrir a revisão já com "criar ou atualizar regra"
    marcado — foi assim que a pessoa classificou aquela contraparte.
    """
    regras = _regras_ativas()
    achados = {}
    for mov in movimentos:
        for regra in regras:
            if cls.regra_casa(regra, mov):
                achados[mov.id] = regra
                break
    return achados


def _regras_ativas() -> list[RegraClassificacaoBancaria]:
    return db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(ativo=True)
        .order_by(RegraClassificacaoBancaria.id)
    ).scalars().all()


def _definir_categoria(mov: MovimentoBancario, categoria: str, revisar: bool, regra: str) -> bool:
    bloco = cls.CATEGORIAS[categoria].bloco
    # categoria que "pede revisão" (ex.: Sócios – a detalhar) não pode arrastar de
    # volta para a fila o que uma pessoa já revisou: só volta se a classificação mudar
    if revisar and mov.revisado and categoria == mov.categoria_gerencial:
        revisar = False
    novo = (categoria, bloco, bool(revisar), (regra or "")[:120] or None)
    if novo == (mov.categoria_gerencial, mov.bloco, mov.revisar, mov.regra_aplicada):
        return False
    mov.categoria_gerencial, mov.bloco, mov.revisar, mov.regra_aplicada = novo
    if bloco in BLOCOS_FORA_CONCILIACAO and mov.itens_conciliacao:
        _limpar_conciliacao(mov)
    return True


def aplicar_classificacao(mov: MovimentoBancario, regras, cnpj_raiz: str) -> bool:
    """Classifica um movimento (exceto os manuais). True se mudou."""
    if mov.classificacao_manual:
        return False
    r = cls.classificar(mov, regras, cnpj_raiz)
    mudou = _definir_categoria(mov, r.categoria, r.revisar, r.regra)
    if r.categoria_id and mov.categoria_id != r.categoria_id:
        mov.categoria_id, mudou = r.categoria_id, True
    return mudou


def reaplicar_regras() -> int:
    regras, raiz = _regras_ativas(), _cnpj_raiz()
    alterados = 0
    for mov in db.session.execute(
        db.select(MovimentoBancario).filter_by(banco=BANCO, classificacao_manual=False)
    ).scalars():
        alterados += aplicar_classificacao(mov, regras, raiz)
    db.session.flush()
    return alterados


def aprender_da_conciliacao() -> dict:
    """Aprende a classificação a partir do que já foi conciliado com a planilha.

    Quando um movimento ainda está "a classificar" mas casou com um lançamento
    do fluxo, a categoria da planilha diz o que ele é (ex.: PIX para uma pessoa
    que na planilha é "Músicos"). O sistema cria a regra da contraparte e passa
    a classificar sozinho os próximos — a Manu só revisa.
    """
    candidatos = db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.banco == BANCO,
            MovimentoBancario.id.in_(_ids_movimentos_conciliados()),
            MovimentoBancario.classificacao_manual.is_(False),
            MovimentoBancario.bloco == "a_classificar",
        )
    ).scalars().all()

    regras_criadas = 0
    for mov in candidatos:
        # só aprende de conciliação 1 para 1: em grupo a categoria é ambígua
        lanc = mov.lancamento
        if lanc is None:
            continue
        categoria_fluxo = db.session.get(Categoria, lanc.categoria_id)
        alvo = cls.categoria_do_lancamento(categoria_fluxo.grupo, categoria_fluxo.nome) if categoria_fluxo else None
        if not alvo or alvo == mov.categoria_gerencial:
            continue
        campo, valor = cls.chave_regra(mov)
        if campo == "descricao" or not valor:
            continue  # só aprende por contraparte identificada
        existente = db.session.execute(
            db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=mov.tipo)
        ).scalar_one_or_none()
        if existente:
            continue
        db.session.add(RegraClassificacaoBancaria(
            campo=campo, valor=valor, tipo=mov.tipo, categoria=alvo, revisar=False,
            observacao=f"aprendido da planilha: {categoria_fluxo.grupo} › {categoria_fluxo.nome}",
        ))
        regras_criadas += 1

    if not regras_criadas:
        return {"regras": 0, "movimentos": 0}
    db.session.flush()
    return {"regras": regras_criadas, "movimentos": reaplicar_regras()}


def marcar_revisados(movimentos) -> int:
    agora = _agora()
    total = 0
    for mov in movimentos:
        if not mov.revisado:
            mov.revisado = True
            mov.revisar = False
            if not mov.conciliado_em and mov.itens_conciliacao:
                mov.conciliado_em = agora
            total += 1
    db.session.flush()
    return total


def reclassificar(movimento: MovimentoBancario, categoria: str, para_contraparte: bool = False,
                  fornecedor_id: int | None = None, categoria_id: int | None = None) -> int:
    """Classificação feita pelo usuário.

    - só este movimento: fica manual (regras não sobrescrevem);
    - `para_contraparte`: cria/atualiza regra pela contraparte (documento,
      nome ou descrição) e reaplica em todos os movimentos não manuais.
    Retorna quantos movimentos mudaram.
    """
    if categoria not in cls.CATEGORIAS:
        raise ValueError("Categoria inválida.")
    if not para_contraparte:
        movimento.classificacao_manual = True
        alterados = int(_definir_categoria(movimento, categoria, False, "manual"))
    else:
        campo, valor = cls.chave_regra(movimento)
        if not valor:
            raise ValueError("Movimento sem contraparte ou descrição para criar regra.")
        regra = db.session.execute(
            db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=movimento.tipo)
        ).scalar_one_or_none()
        if regra is None:
            regra = RegraClassificacaoBancaria(campo=campo, valor=valor, tipo=movimento.tipo)
            db.session.add(regra)
        regra.categoria, regra.revisar, regra.ativo = categoria, False, True
        regra.categoria_id = categoria_id
        if fornecedor_id:
            regra.fornecedor_id = fornecedor_id
        movimento.classificacao_manual = False
        db.session.flush()
        alterados = reaplicar_regras()
    auditoria.registrar("update", "movimento_bancario", movimento.id, depois={
        "categoria": categoria, "regra_por_contraparte": para_contraparte, "alterados": alterados,
    })
    db.session.flush()
    return alterados


# ------------------------------- gravação -------------------------------

def _gravar_movimentos(txns: list[itau.TransacaoBancaria], conta: str, regras, raiz: str) -> tuple[int, int]:
    """Upsert por id_externo + classificação. Retorna (inseridos, atualizados)."""
    ids = [t.id for t in txns]
    existentes: dict[str, MovimentoBancario] = {}
    for i in range(0, len(ids), 500):  # limite de variáveis do SQLite
        for m in db.session.execute(
            db.select(MovimentoBancario).filter(
                MovimentoBancario.banco == BANCO,
                MovimentoBancario.conta == conta,
                MovimentoBancario.id_externo.in_(ids[i:i + 500]),
            )
        ).scalars():
            existentes[m.id_externo] = m

    inseridos = atualizados = 0
    for t in txns:
        campos = {
            "data": t.data,
            "tipo": t.tipo,
            "valor": t.valor,
            "descricao": (t.descricao or "")[:255] or None,
            "origem": (t.origem or "")[:40] or None,
            "contraparte": (t.contraparte or "")[:160] or None,
            "contraparte_documento": (t.contraparte_documento or "")[:20] or None,
            "contraparte_instituicao": (t.contraparte_instituicao or "")[:80] or None,
            "estorno": bool(t.estorno),
        }
        m = existentes.get(t.id)
        if m is None:
            m = MovimentoBancario(banco=BANCO, conta=conta, id_externo=t.id, **campos)
            db.session.add(m)
            existentes[t.id] = m
            inseridos += 1
        else:
            mudou = {k: v for k, v in campos.items() if getattr(m, k) != v}
            if mudou:
                if mudou.keys() & {"data", "tipo", "valor"}:
                    # a base do casamento mudou: volta a ficar pendente
                    _limpar_conciliacao(m)
                for k, v in mudou.items():
                    setattr(m, k, v)
                atualizados += 1
        aplicar_classificacao(m, regras, raiz)
    return inseridos, atualizados


def sincronizar(inicio: date, fim: date | None = None, client=None, config=None) -> dict:
    """Busca o extrato de cada conta de ITAU_CONTAS desde `inicio`, grava e
    classifica os movimentos, guarda o saldo e concilia o período."""
    config = config or itau.ItauConfig.from_app(current_app)
    if not config.contas:
        raise RuntimeError("Nenhuma conta configurada em ITAU_CONTAS.")
    client = client or itau.ItauClient(config)
    fim_efetivo = fim or hoje_sp()
    regras, raiz = _regras_ativas(), _cnpj_raiz()

    rel = {"inicio": inicio.isoformat(), "fim": fim_efetivo.isoformat(),
           "contas": {}, "inseridos": 0, "atualizados": 0}
    for conta in config.contas:
        conta_id = itau.normalizar_conta(conta)
        txns = client.extrato(conta_id, inicio, fim)
        inseridos, atualizados = _gravar_movimentos(txns, conta_id, regras, raiz)
        saldo = client.saldo(conta_id)
        db.session.add(SaldoBancario(
            banco=BANCO, conta=conta_id, consultado_em=_agora(), data=saldo.data,
            disponivel=saldo.saldo, bloqueado=saldo.saldo_bloqueado,
            aplicacao_automatica=saldo.saldo_aplicacao_automatica,
        ))
        rel["contas"][conta_id] = {"movimentos": len(txns), "inseridos": inseridos,
                                   "atualizados": atualizados, "saldo": str(saldo.saldo)}
        rel["inseridos"] += inseridos
        rel["atualizados"] += atualizados

    db.session.flush()
    rel["conciliacao"] = conciliar_periodo(inicio, fim_efetivo)
    rel["aprendizado"] = aprender_da_conciliacao()
    # o extrato alimenta o caixa: movimento novo já vira lançamento
    from app.services import itau_fluxo

    # o banco é a fonte do período que ele cobre: a previsão que veio da planilha
    # para esses dias sai, senão a compra conta duas vezes (a real e a prevista).
    # "Dinheiro" é preservado — é o único lançamento que a Manu faz à mão.
    rel["fluxo"] = itau_fluxo.importar_periodo(inicio, fim_efetivo, substituir_planilha=True)
    # pagamento combinado que caiu na conta sai da projeção
    from app.services import despesas_previstas

    rel["previstas"] = despesas_previstas.baixar_automatico(inicio, fim_efetivo)
    ConfigSistema.definir(CHAVE_ULTIMA_SYNC, _agora().isoformat())
    auditoria.registrar("import", "movimento_bancario", None, depois={
        "origem": BANCO, "inseridos": rel["inseridos"], "atualizados": rel["atualizados"],
        "conciliados": rel["conciliacao"]["conciliados"],
    })
    return rel


# ------------------------------- conciliação -------------------------------

def _ids_lancamentos_conciliados():
    return db.select(ConciliacaoItem.lancamento_id)


def _ids_movimentos_conciliados():
    return db.select(ConciliacaoItem.movimento_id)


def _limpar_conciliacao(movimento: MovimentoBancario) -> None:
    movimento.itens_conciliacao.clear()
    # o flush precisa vir antes de gravar os novos itens: sem ele o SQLAlchemy
    # insere o par (movimento, lançamento) antes de apagar o antigo e bate na
    # restrição de unicidade quando o lançamento continua na conciliação
    db.session.flush()
    movimento.conciliado_em = None
    movimento.diferenca_conciliacao = Decimal("0.00")


def _gravar_conciliacao(movimento: MovimentoBancario, lancamentos: list, *,
                        automatica: bool, usuario_id: int | None = None) -> None:
    """Liga o movimento aos lançamentos escolhidos e guarda a diferença."""
    _limpar_conciliacao(movimento)
    for lanc in lancamentos:
        movimento.itens_conciliacao.append(ConciliacaoItem(
            lancamento_id=lanc.id if hasattr(lanc, "id") else int(lanc),
            automatica=automatica, criado_por_id=usuario_id,
        ))
    if lancamentos:
        movimento.conciliado_em = _agora()
        soma = sum((Decimal(getattr(l, "valor", 0)) for l in lancamentos), Decimal("0.00"))
        movimento.diferenca_conciliacao = (Decimal(movimento.valor) - soma).quantize(CENTAVOS)


def conciliar_periodo(inicio: date, fim: date, tolerancia_dias: int = TOLERANCIA_DIAS) -> dict:
    """Casa movimentos pendentes do período com lançamentos ainda livres."""
    pendentes = db.session.execute(
        db.select(MovimentoBancario).filter(
            MovimentoBancario.banco == BANCO,
            MovimentoBancario.bloco.not_in(BLOCOS_FORA_CONCILIACAO),
            MovimentoBancario.id.not_in(_ids_movimentos_conciliados()),
            MovimentoBancario.data >= inicio,
            MovimentoBancario.data <= fim,
        )
    ).scalars().all()
    if not pendentes:
        return {"conciliados": 0, "pendentes": 0}

    livres = db.session.execute(
        db.select(Lancamento.id, Lancamento.data, Lancamento.valor, Categoria.tipo)
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(
            Lancamento.data >= inicio - timedelta(days=tolerancia_dias),
            Lancamento.data <= fim + timedelta(days=tolerancia_dias),
            Lancamento.id.not_in(_ids_lancamentos_conciliados()),
        )
    ).all()

    por_id = {str(m.id): m for m in pendentes}
    resultado = conc.conciliar(
        [itau.TransacaoBancaria(id=str(m.id), conta=m.conta, data=m.data,
                                valor=Decimal(m.valor), tipo=m.tipo) for m in pendentes],
        [conc.LancamentoRef(id=l.id, data=l.data, valor=Decimal(l.valor), tipo=l.tipo) for l in livres],
        tolerancia_dias=tolerancia_dias,
    )
    for par in resultado.conciliados:
        mov = por_id[par.transacao.id]
        _gravar_conciliacao(mov, [db.session.get(Lancamento, par.lancamento.id)], automatica=True)

    # 2º passo: o que sobrou pode ser um repasse que junta vários lançamentos do dia
    livres_ref = {l.id: conc.LancamentoRef(id=l.id, data=l.data, valor=Decimal(l.valor), tipo=l.tipo)
                  for l in livres}
    usados = {par.lancamento.id for par in resultado.conciliados}
    em_grupo = 0
    for transacao in sorted(resultado.so_no_banco, key=lambda t: (t.data, t.id)):
        mov = por_id[transacao.id]
        tipo = "entrada" if mov.tipo == "credito" else "saida"
        candidatos = [ref for lid, ref in livres_ref.items()
                      if lid not in usados and ref.tipo == tipo and ref.data == mov.data]
        combinacao = conc.combinar(Decimal(mov.valor), candidatos)
        if not combinacao:
            continue
        _gravar_conciliacao(mov, [db.session.get(Lancamento, ref.id) for ref in combinacao], automatica=True)
        usados.update(ref.id for ref in combinacao)
        em_grupo += 1

    db.session.flush()
    return {"conciliados": len(resultado.conciliados) + em_grupo,
            "em_grupo": em_grupo,
            "pendentes": len(resultado.so_no_banco) - em_grupo}


def desfazer_conciliacao(movimento: MovimentoBancario) -> None:
    _limpar_conciliacao(movimento)


# ------------------------- conciliação manual (1 → N) -------------------------

def candidatos_conciliacao(movimento: MovimentoBancario, dias: int = TOLERANCIA_DIAS,
                           limite: int = 60) -> list[Lancamento]:
    """Lançamentos do fluxo que podem compor este movimento: mesmo sentido,
    ainda livres (ou já ligados a ele) e dentro da janela de dias."""
    tipo = "entrada" if movimento.tipo == "credito" else "saida"
    ja_ligados = {i.lancamento_id for i in movimento.itens_conciliacao}
    query = (
        db.select(Lancamento).join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(
            Categoria.tipo == tipo,
            Lancamento.data >= movimento.data - timedelta(days=dias),
            Lancamento.data <= movimento.data + timedelta(days=dias),
            db.or_(Lancamento.id.not_in(_ids_lancamentos_conciliados()),
                   Lancamento.id.in_(ja_ligados or [0])),
        )
        .order_by(Lancamento.data, Lancamento.id)
        .limit(limite)
    )
    return db.session.execute(query).scalars().all()


def sugerir_combinacao(movimento: MovimentoBancario, candidatos: list[Lancamento]) -> list[int]:
    """IDs dos lançamentos cuja soma dá exatamente o valor do movimento."""
    refs = [conc.LancamentoRef(id=l.id, data=l.data, valor=Decimal(l.valor),
                               tipo="entrada" if movimento.tipo == "credito" else "saida")
            for l in candidatos if l.data == movimento.data]
    combinacao = conc.combinar(Decimal(movimento.valor), refs)
    return [l.id for l in combinacao] if combinacao else []


def conciliar_manual(movimento: MovimentoBancario, lancamento_ids: list[int],
                     usuario_id: int | None = None) -> dict:
    """Liga o movimento aos lançamentos escolhidos (um ou vários).

    Lista vazia desfaz a conciliação. Só aceita lançamentos do mesmo sentido e
    que não estejam presos a outro movimento.
    """
    if not lancamento_ids:
        desfazer_conciliacao(movimento)
        db.session.flush()
        return {"lancamentos": 0, "diferenca": Decimal("0.00")}

    tipo = "entrada" if movimento.tipo == "credito" else "saida"
    ja_ligados = {i.lancamento_id for i in movimento.itens_conciliacao}
    escolhidos = db.session.execute(
        db.select(Lancamento).join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.id.in_(lancamento_ids), Categoria.tipo == tipo)
    ).scalars().all()
    if len(escolhidos) != len(set(lancamento_ids)):
        raise ValueError("Escolha lançamentos do mesmo sentido do movimento (entrada ou saída).")

    presos = db.session.execute(
        db.select(ConciliacaoItem).filter(
            ConciliacaoItem.lancamento_id.in_([l.id for l in escolhidos]),
            ConciliacaoItem.movimento_id != movimento.id,
        )
    ).scalars().all()
    if presos:
        raise ValueError("Um dos lançamentos já está conciliado com outro movimento do banco.")

    _gravar_conciliacao(movimento, escolhidos, automatica=False, usuario_id=usuario_id)
    if ja_ligados != {l.id for l in escolhidos}:
        movimento.revisado = True
        movimento.revisar = False
    db.session.flush()
    return {"lancamentos": len(escolhidos), "diferenca": movimento.diferenca_conciliacao}


# ------------------------------- consultas -------------------------------

def ultimo_saldo() -> SaldoBancario | None:
    return db.session.execute(
        db.select(SaldoBancario).filter_by(banco=BANCO)
        .order_by(SaldoBancario.consultado_em.desc(), SaldoBancario.id.desc()).limit(1)
    ).scalar_one_or_none()


def _filtro_periodo(inicio: date, fim: date):
    return (MovimentoBancario.banco == BANCO,
            MovimentoBancario.data >= inicio, MovimentoBancario.data <= fim)


def demonstrativo(inicio: date, fim: date) -> dict:
    """Caixa do período por grupo e categoria gerencial (visão CFO)."""
    linhas = db.session.execute(
        db.select(
            MovimentoBancario.categoria_gerencial,
            MovimentoBancario.tipo,
            func.count(MovimentoBancario.id),
            func.coalesce(func.sum(MovimentoBancario.valor), 0),
            func.sum(case((MovimentoBancario.revisar.is_(True), 1), else_=0)),
        )
        .filter(*_filtro_periodo(inicio, fim))
        .group_by(MovimentoBancario.categoria_gerencial, MovimentoBancario.tipo)
    ).all()

    por_categoria: dict[str, dict] = {}
    for categoria, tipo, qtd, soma, revisar in linhas:
        if categoria not in cls.CATEGORIAS:
            categoria = "a_classificar_entrada" if tipo == "credito" else "a_classificar_saida"
        d = por_categoria.setdefault(categoria, {"entradas": ZERO, "saidas": ZERO, "qtd": 0, "revisar": 0})
        d["entradas" if tipo == "credito" else "saidas"] += _dinheiro(soma)
        d["qtd"] += qtd
        d["revisar"] += int(revisar or 0)

    blocos = []
    for codigo, nome in cls.BLOCOS.items():
        categorias = [
            {"codigo": c.codigo, "nome": c.nome, **por_categoria[c.codigo],
             "liquido": por_categoria[c.codigo]["entradas"] - por_categoria[c.codigo]["saidas"]}
            for c in cls.CATEGORIAS.values() if c.bloco == codigo and c.codigo in por_categoria
        ]
        if not categorias:
            continue
        entradas = sum((c["entradas"] for c in categorias), ZERO)
        saidas = sum((c["saidas"] for c in categorias), ZERO)
        blocos.append({
            "codigo": codigo, "nome": nome, "entradas": entradas, "saidas": saidas,
            "liquido": entradas - saidas, "qtd": sum(c["qtd"] for c in categorias),
            "revisar": sum(c["revisar"] for c in categorias), "categorias": categorias,
        })

    por_bloco = {b["codigo"]: b for b in blocos}
    operacao = por_bloco.get("operacional")
    return {
        "blocos": blocos,
        "por_bloco": por_bloco,
        "recebimentos": operacao["entradas"] if operacao else ZERO,
        "pagamentos": operacao["saidas"] if operacao else ZERO,
        "geracao_operacional": operacao["liquido"] if operacao else ZERO,
        "variacao_conta": sum((b["liquido"] for b in blocos), ZERO),
        "revisar": sum(b["revisar"] for b in blocos),
    }


def totais_periodo(inicio: date, fim: date) -> dict[str, Decimal]:
    """Recebimentos e pagamentos da OPERAÇÃO no período (sem sócios,
    aplicações nem transferências)."""
    demo = demonstrativo(inicio, fim)
    return {"recebimentos": demo["recebimentos"], "pagamentos": demo["pagamentos"]}


def _filtro_busca(busca: str):
    termo = f"%{busca.strip()}%"
    return or_(MovimentoBancario.descricao.ilike(termo), MovimentoBancario.contraparte.ilike(termo))


def movimentos(inicio: date, fim: date, status: str = "todos", bloco: str = "",
               busca: str = "") -> list[MovimentoBancario]:
    q = db.select(MovimentoBancario).filter(*_filtro_periodo(inicio, fim),
                                            _filtro_stone_com_arquivo())
    if busca and busca.strip():
        q = q.filter(_filtro_busca(busca))
    if bloco:
        q = q.filter(MovimentoBancario.bloco == bloco)
    if status == "pendentes":
        q = q.filter(MovimentoBancario.id.not_in(_ids_movimentos_conciliados()),
                     MovimentoBancario.bloco.not_in(BLOCOS_FORA_CONCILIACAO))
    elif status == "conciliados":
        q = q.filter(MovimentoBancario.id.in_(_ids_movimentos_conciliados()))
    elif status == "revisar":
        # já revisado por uma pessoa não volta para a fila, mesmo que a regra peça revisão
        q = q.filter(MovimentoBancario.revisar.is_(True), MovimentoBancario.revisado.is_(False))
    elif status == "classificar":
        # a fila de trabalho: sem classificação, com pendência OU só sugerido pelo
        # sistema — tudo que ainda não passou pelos olhos de uma pessoa
        q = q.filter(MovimentoBancario.revisado.is_(False))
    return db.session.execute(
        q.order_by(MovimentoBancario.data.desc(), MovimentoBancario.id.desc())
    ).scalars().all()


def _filtro_sem_banco(inicio: date, fim: date):
    # lançamentos futuros (previstos) ainda não podem estar no extrato
    return (Lancamento.data >= inicio, Lancamento.data <= min(fim, hoje_sp()),
            Lancamento.id.not_in(_ids_lancamentos_conciliados()))


def lancamentos_sem_banco(inicio: date, fim: date, limite: int = 300) -> list[Lancamento]:
    return db.session.execute(
        db.select(Lancamento).join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(*_filtro_sem_banco(inicio, fim))
        .order_by(Lancamento.data.desc(), Lancamento.id.desc()).limit(limite)
    ).scalars().all()


def contagens(inicio: date, fim: date, busca: str = "") -> dict[str, int]:
    """Quantidades para as abas de Movimentações (mesmo período e busca da lista)."""
    base = list(_filtro_periodo(inicio, fim)) + [_filtro_stone_com_arquivo()]
    if busca and busca.strip():
        base.append(_filtro_busca(busca))

    def contar(*filtros):
        return db.session.execute(
            db.select(func.count(MovimentoBancario.id)).filter(*base, *filtros)
        ).scalar_one()

    return {
        "sem_classificacao": contar(MovimentoBancario.bloco == "a_classificar"),
        "revisar": contar(MovimentoBancario.revisar.is_(True), MovimentoBancario.revisado.is_(False)),
        "classificar": contar(MovimentoBancario.revisado.is_(False)),
        "pendentes": contar(MovimentoBancario.id.not_in(_ids_movimentos_conciliados()),
                            MovimentoBancario.bloco.not_in(BLOCOS_FORA_CONCILIACAO)),
        "conciliados": contar(MovimentoBancario.id.in_(_ids_movimentos_conciliados())),
        "todos": contar(),
    }


def linha_do_caixa(categoria_id: int):
    """Linha da planilha escolhida na tela, validada."""
    from app.models.fluxo import Categoria

    linha = db.session.get(Categoria, categoria_id)
    if linha is None:
        raise ValueError("Escolha uma linha do caixa válida.")
    return linha


def categoria_da_linha(linha, tipo: str) -> str:
    """Categoria gerencial equivalente à linha da planilha.

    A tela fala a língua da planilha; os relatórios (DRE, blocos) continuam
    falando a gerencial, então a conversão acontece aqui, num lugar só.
    """
    codigo = cls.categoria_do_lancamento(linha.grupo, linha.nome)
    if codigo in cls.CATEGORIAS:
        return codigo
    return "a_classificar_entrada" if tipo == "credito" else "a_classificar_saida"


def revisar_movimento(movimento: MovimentoBancario, categoria: str = "", criar_regra: bool = False,
                      fornecedor_id: int | None = None, categoria_id: int | None = None) -> int:
    """Revisão completa de UM movimento, numa transação só.

    - categoria igual à atual e sem regra: só confirma (marca revisado, sem
      travar como manual — regras futuras ainda podem corrigir);
    - categoria diferente: classificação manual deste movimento;
    - criar_regra: cria/atualiza a regra da contraparte e reaplica em todos os
      movimentos não manuais, inclusive anteriores.
    Retorna quantos movimentos tiveram a classificação alterada.
    """
    if not categoria and not categoria_id:
        if movimento.categoria_gerencial not in LINHAS_DERIVADAS:
            raise ValueError("Escolha uma linha do caixa válida.")
        # a quebra veio pronta (Stone, sócia): revisar aqui é conferir e seguir
        marcar_revisados([movimento])
        return 0

    linha = None
    if categoria_id:
        linha = linha_do_caixa(categoria_id)
        esperado = "entrada" if movimento.tipo == "credito" else "saida"
        if linha.tipo != esperado:
            raise ValueError(f"“{linha.nome}” é uma linha de {linha.tipo}; "
                             f"este movimento é uma {esperado}.")
        categoria = categoria_da_linha(linha, movimento.tipo)
    if categoria not in cls.CATEGORIAS:
        raise ValueError("Escolha uma linha do caixa válida.")
    anterior = movimento.categoria_id
    movimento.categoria_id = linha.id if linha else None
    alterados = 0
    # trocar so a linha (de "Light" para "Net") tambem e uma decisao de gente
    if criar_regra or categoria != movimento.categoria_gerencial or movimento.categoria_id != anterior:
        alterados = reclassificar(movimento, categoria, para_contraparte=criar_regra,
                                  fornecedor_id=fornecedor_id,
                                  categoria_id=linha.id if linha else None)
    elif fornecedor_id:
        _fixar_fornecedor(movimento, fornecedor_id, criar_regra)
    marcar_revisados([movimento])
    from app.services import itau_fluxo

    itau_fluxo.sincronizar_movimento(movimento, fornecedor_id=fornecedor_id)
    if criar_regra:
        alterados = _propagar_regra(movimento)
    return alterados


def _propagar_regra(movimento: MovimentoBancario) -> int:
    """Leva a regra recem-salva ate o caixa, inclusive nos meses anteriores.

    `reaplicar_regras` acerta a categoria do movimento; sem isto o lancamento
    do fluxo (linha e fornecedor) so acompanharia no proximo sync.
    Retorna quantos movimentos da contraparte foram atualizados.
    """
    from app.services import itau_fluxo

    campo, valor = cls.chave_regra(movimento)
    regra = db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=movimento.tipo)
    ).scalar_one_or_none()
    if regra is None:
        return 0
    alvos = [m for m in db.session.execute(
        db.select(MovimentoBancario).filter_by(banco=BANCO, classificacao_manual=False)
    ).scalars() if cls.regra_casa(regra, m)]
    for alvo in alvos:
        itau_fluxo.sincronizar_movimento(alvo)
    # a regra é decisão de gente: o que ela alcança já está conferido e sai da fila
    marcar_revisados(alvos)
    db.session.flush()
    return len(alvos)


def _fixar_fornecedor(movimento: MovimentoBancario, fornecedor_id: int, criar_regra: bool) -> None:
    """Guarda na regra da contraparte que aqueles pagamentos são deste fornecedor."""
    from app.models.fluxo import Fornecedor

    if not db.session.get(Fornecedor, fornecedor_id):
        raise ValueError("Fornecedor não encontrado.")
    if not criar_regra:
        return
    campo, valor = cls.chave_regra(movimento)
    if not valor:
        raise ValueError("Movimento sem contraparte para criar a regra do fornecedor.")
    regra = db.session.execute(
        db.select(RegraClassificacaoBancaria).filter_by(campo=campo, valor=valor, tipo=movimento.tipo)
    ).scalar_one_or_none()
    if regra is None:
        regra = RegraClassificacaoBancaria(campo=campo, valor=valor, tipo=movimento.tipo,
                                           categoria=movimento.categoria_gerencial)
        db.session.add(regra)
    regra.fornecedor_id = fornecedor_id
    regra.ativo = True
    db.session.flush()


def resumo(inicio: date, fim: date) -> dict:
    def contar(*filtros):
        return db.session.execute(
            db.select(func.count(MovimentoBancario.id)).filter(*_filtro_periodo(inicio, fim), *filtros)
        ).scalar_one()

    # mesmo recorte da lista: repasse da Stone sem o arquivo do dia não entra
    conciliaveis = contar(MovimentoBancario.bloco.not_in(BLOCOS_FORA_CONCILIACAO),
                          _filtro_stone_com_arquivo())
    # mesmo universo de "conciliaveis": senão o painel mostra mais conciliados
    # do que conciliáveis (tesouraria e transferências não entram na conta)
    conciliados = contar(MovimentoBancario.id.in_(_ids_movimentos_conciliados()),
                         MovimentoBancario.bloco.not_in(BLOCOS_FORA_CONCILIACAO),
                         _filtro_stone_com_arquivo())
    sem_banco = db.session.execute(
        db.select(func.count(Lancamento.id)).filter(*_filtro_sem_banco(inicio, fim))
    ).scalar_one()
    return {"conciliaveis": conciliaveis, "conciliados": conciliados,
            "pendentes": conciliaveis - conciliados,
            "revisar": contar(MovimentoBancario.revisar.is_(True), MovimentoBancario.revisado.is_(False),
                              _filtro_stone_com_arquivo()),
            "novos": contar(MovimentoBancario.revisado.is_(False), _filtro_stone_com_arquivo()),
            "lancamentos_sem_banco": sem_banco}
