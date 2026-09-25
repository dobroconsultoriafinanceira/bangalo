# -*- coding: utf-8 -*-
"""Importa transações da Stone como lançamentos de fluxo (idempotente)."""
from decimal import Decimal

from flask import current_app

from app.extensions import db
from app.importers import stone_adapter as stone
from app.models.fluxo import Categoria, Lancamento
from app.services import auditoria

ORIGEM = "stone"


def _categoria_por_nome(nome: str, tipo: str) -> Categoria | None:
    """Resolve categoria pelo nome (preferindo o tipo esperado)."""
    achadas = db.session.execute(
        db.select(Categoria).filter_by(nome=nome)
    ).scalars().all()
    if not achadas:
        return None
    for c in achadas:
        if c.tipo == tipo:
            return c
    return achadas[0]


def importar_transacoes(
    transacoes: list[stone.TransacaoStone],
    usuario_id: int | None = None,
    lancar_taxa: bool | None = None,
) -> dict:
    """Grava lançamentos a partir de transações Stone. Não faz commit.

    Idempotente: usa (origem='stone', origem_id) — reimportar o mesmo dia
    atualiza em vez de duplicar. Retorna um relatório.
    """
    if lancar_taxa is None:
        lancar_taxa = current_app.config.get("STONE_LANCAR_TAXA", False)

    entradas = stone.to_lancamentos(transacoes, lancar_taxa=lancar_taxa)

    inseridos = 0
    atualizados = 0
    ignorados_sem_categoria = 0
    total_por_categoria: dict[str, Decimal] = {}

    for e in entradas:
        categoria = _categoria_por_nome(e.categoria_nome, e.tipo)
        if not categoria:
            ignorados_sem_categoria += 1
            continue

        existente = db.session.execute(
            db.select(Lancamento).filter_by(origem=ORIGEM, origem_id=e.origem_id)
        ).scalar_one_or_none()

        if existente:
            existente.data = e.data
            existente.categoria_id = categoria.id
            existente.valor = e.valor
            existente.forma_pagamento = e.forma_pagamento or None
            existente.descricao = e.descricao
            atualizados += 1
        else:
            db.session.add(Lancamento(
                data=e.data,
                categoria_id=categoria.id,
                forma_pagamento=e.forma_pagamento or None,
                valor=e.valor,
                descricao=e.descricao,
                usuario_id=usuario_id,
                origem=ORIGEM,
                origem_id=e.origem_id,
            ))
            inseridos += 1

        total_por_categoria[e.categoria_nome] = (
            total_por_categoria.get(e.categoria_nome, Decimal("0")) + e.valor
        )

    db.session.flush()
    auditoria.registrar("import", "lancamento", None, depois={
        "origem": ORIGEM, "inseridos": inseridos, "atualizados": atualizados,
    })

    return {
        "transacoes_recebidas": len(transacoes),
        "lancamentos_gerados": len(entradas),
        "inseridos": inseridos,
        "atualizados": atualizados,
        "ignorados_sem_categoria": ignorados_sem_categoria,
        "total_por_categoria": {k: str(v) for k, v in total_por_categoria.items()},
    }


def importar_arquivo_csv(conteudo, usuario_id: int | None = None) -> dict:
    """Importa um arquivo CSV de conciliação já exportado (upload manual)."""
    transacoes = stone.parse_conciliacao_csv(conteudo)
    return importar_transacoes(transacoes, usuario_id=usuario_id)


def importar_periodo(inicio, fim, usuario_id: int | None = None) -> dict:
    """Baixa da API Stone dia a dia e importa. Exige credenciais no .env."""
    from datetime import timedelta

    config = stone.StoneConfig.from_app(current_app)
    client = stone.StoneClient(config)
    todas: list[stone.TransacaoStone] = []
    dia = inicio
    while dia <= fim:
        todas.extend(client.transacoes_do_dia(dia))
        dia += timedelta(days=1)
    return importar_transacoes(todas, usuario_id=usuario_id)


def conferir_periodo(inicio, fim) -> dict:
    """Compara os REPASSES da Stone com as linhas de cartão do fluxo, sem gravar.

    A planilha já lança essas linhas por dia de recebimento; esta conferência
    mostra, dia a dia e por bandeira, o que a Stone depositou × o que está no
    fluxo. Serve para achar diferença sem correr o risco de contar duas vezes.
    """
    from datetime import timedelta

    from sqlalchemy import func

    from app.models.fluxo import Lancamento

    config = stone.StoneConfig.from_app(current_app)
    client = stone.StoneClient(config)
    linhas, dias_com_erro = [], []
    total_stone = total_fluxo = Decimal("0.00")

    dia = inicio
    while dia <= fim:
        try:
            arquivo = client.arquivo_do_dia(dia)
        except Exception as exc:  # noqa: BLE001 — um dia indisponível não derruba o resto
            dias_com_erro.append((dia, str(exc)))
            dia += timedelta(days=1)
            continue

        por_categoria: dict[str, Decimal] = {}
        for pagamento in arquivo.pagamentos:
            por_categoria[pagamento.categoria_nome] = (
                por_categoria.get(pagamento.categoria_nome, Decimal("0.00")) + pagamento.valor
            )

        for nome, valor_stone in sorted(por_categoria.items()):
            categoria = _categoria_por_nome(nome, "entrada")
            no_fluxo = Decimal("0.00")
            if categoria:
                no_fluxo = Decimal(db.session.execute(
                    db.select(func.coalesce(func.sum(Lancamento.valor), 0)).filter(
                        Lancamento.categoria_id == categoria.id, Lancamento.data == dia)
                ).scalar_one())
            linhas.append({
                "dia": dia, "categoria": nome, "stone": valor_stone,
                "fluxo": no_fluxo, "diferenca": valor_stone - no_fluxo,
                "sem_categoria": categoria is None,
            })
            total_stone += valor_stone
            total_fluxo += no_fluxo
        dia += timedelta(days=1)

    return {
        "linhas": linhas,
        "erros": dias_com_erro,
        "total_stone": total_stone,
        "total_fluxo": total_fluxo,
        "diferenca": total_stone - total_fluxo,
    }


def importar_repasses(inicio, fim, usuario_id: int | None = None, substituir_planilha: bool = True) -> dict:
    """Entradas de cartão a partir dos REPASSES da Stone (fonte oficial).

    Um lançamento por pagamento do arquivo (id do pagamento = origem_id), na
    categoria do arranjo de pagamento. Idempotente: reimportar o período
    atualiza em vez de duplicar.

    `substituir_planilha`: apaga, no período, as linhas de cartão que vieram da
    planilha (origem 'manual') — senão o mesmo dinheiro contaria duas vezes.
    """
    from datetime import timedelta

    config = stone.StoneConfig.from_app(current_app)
    client = stone.StoneClient(config)

    inseridos = atualizados = removidos = 0
    sem_categoria: set[str] = set()
    dias_com_erro: list[tuple] = []
    total = Decimal("0.00")

    dia = inicio
    while dia <= fim:
        try:
            arquivo = client.arquivo_do_dia(dia)
        except Exception as exc:  # noqa: BLE001 — um dia indisponível não derruba o resto
            dias_com_erro.append((dia, str(exc)))
            dia += timedelta(days=1)
            continue

        for pagamento in arquivo.pagamentos:
            categoria = _categoria_por_nome(pagamento.categoria_nome, "entrada")
            if not categoria:
                sem_categoria.add(pagamento.categoria_nome)
                continue
            origem_id = f"pg:{pagamento.id}"
            existente = db.session.execute(
                db.select(Lancamento).filter_by(origem=ORIGEM, origem_id=origem_id)
            ).scalar_one_or_none()
            descricao = f"Repasse Stone · {pagamento.categoria_nome} · code {arquivo.stone_code}"
            if existente:
                existente.data = dia
                existente.categoria_id = categoria.id
                existente.valor = pagamento.valor
                existente.descricao = descricao
                atualizados += 1
            else:
                db.session.add(Lancamento(
                    data=dia, categoria_id=categoria.id, valor=pagamento.valor,
                    forma_pagamento=pagamento.categoria_nome, descricao=descricao,
                    usuario_id=usuario_id, origem=ORIGEM, origem_id=origem_id,
                ))
                inseridos += 1
            total += pagamento.valor
        dia += timedelta(days=1)

    if substituir_planilha:
        removidos = _remover_cartao_da_planilha(inicio, fim)

    db.session.flush()
    auditoria.registrar("import", "lancamento", None, depois={
        "origem": ORIGEM, "tipo": "repasses", "inicio": str(inicio), "fim": str(fim),
        "inseridos": inseridos, "atualizados": atualizados, "removidos_da_planilha": removidos,
    })
    return {
        "inseridos": inseridos, "atualizados": atualizados, "removidos": removidos,
        "total": total, "sem_categoria": sorted(sem_categoria), "erros": dias_com_erro,
    }


def _remover_cartao_da_planilha(inicio, fim) -> int:
    """Tira do período as linhas de cartão que vieram da planilha (origem 'manual')."""
    ids_categorias = db.session.execute(
        db.select(Categoria.id).filter(Categoria.nome.in_(stone.CATEGORIAS_STONE),
                                       Categoria.tipo == "entrada")
    ).scalars().all()
    if not ids_categorias:
        return 0
    alvo = db.select(Lancamento.id).filter(
        Lancamento.categoria_id.in_(ids_categorias), Lancamento.origem != ORIGEM,
        Lancamento.data >= inicio, Lancamento.data <= fim,
    )
    ids = db.session.execute(alvo).scalars().all()
    if not ids:
        return 0
    from app.models.banco import ConciliacaoItem

    db.session.execute(db.delete(ConciliacaoItem).where(ConciliacaoItem.lancamento_id.in_(ids)))
    db.session.execute(db.delete(Lancamento).where(Lancamento.id.in_(ids)))
    return len(ids)


# ─────────────────────── agenda de recebíveis (previsão) ───────────────────────
#
# Cada venda do arquivo traz a data prevista de pagamento da parcela. Somando as
# parcelas ainda não liquidadas, temos a agenda — a mesma que a Bárbara vê no app
# da Stone. Ela entra no caixa como PREVISÃO, na data prevista, e é substituída
# pelo repasse real quando o dinheiro cai.

ORIGEM_AGENDA = "stone_agenda"
JANELA_VENDAS_DIAS = 40   # crédito paga em D+30; 40 dias cobrem a agenda inteira


def agenda_de_recebiveis(hoje, dias_atras: int = JANELA_VENDAS_DIAS) -> dict:
    """{data prevista: valor líquido} a partir dos arquivos dos últimos dias."""
    from datetime import timedelta

    config = stone.StoneConfig.from_app(current_app)
    client = stone.StoneClient(config)
    por_data: dict = {}
    erros = []
    dia = hoje - timedelta(days=dias_atras)
    while dia <= hoje:
        try:
            arquivo = client.arquivo_do_dia(dia)
        except Exception as exc:  # noqa: BLE001 — um dia sem arquivo não derruba a agenda
            erros.append((dia, str(exc)))
            dia += timedelta(days=1)
            continue
        for venda in arquivo.transacoes:
            if venda.data_liquidacao and venda.data_liquidacao > hoje:
                por_data[venda.data_liquidacao] = (
                    por_data.get(venda.data_liquidacao, Decimal("0")) + (venda.valor_liquido or Decimal("0"))
                )
        dia += timedelta(days=1)
    return {"por_data": {d: v.quantize(Decimal("0.01")) for d, v in sorted(por_data.items())},
            "erros": erros}


def importar_agenda(hoje, usuario_id: int | None = None, dias_atras: int = JANELA_VENDAS_DIAS) -> dict:
    """Grava a agenda como lançamentos previstos de cartão no caixa.

    Um lançamento por data prevista (origem 'stone_agenda'). Reimportar
    substitui a agenda inteira: o que já caiu vira repasse real e sai daqui.
    """
    from app.models.fluxo import Categoria

    agenda = agenda_de_recebiveis(hoje, dias_atras)
    categoria = _categoria_por_nome("Recebíveis de cartão (previsto)", "entrada")
    if categoria is None:
        categoria = Categoria(nome="Recebíveis de cartão (previsto)", tipo="entrada",
                              grupo="Entradas", ativo=True)
        db.session.add(categoria)
        db.session.flush()

    # a agenda é sempre reescrita: datas mudam a cada dia
    antigos = db.session.execute(
        db.select(Lancamento).filter(Lancamento.origem == ORIGEM_AGENDA)
    ).scalars().all()
    for lanc in antigos:
        db.session.delete(lanc)
    db.session.flush()

    total = Decimal("0")
    for data_prevista, valor in agenda["por_data"].items():
        if valor <= 0:
            continue
        db.session.add(Lancamento(
            data=data_prevista, categoria_id=categoria.id, valor=valor,
            descricao="Recebíveis de cartão · agenda da Stone",
            usuario_id=usuario_id, origem=ORIGEM_AGENDA,
            origem_id=f"agenda:{data_prevista.isoformat()}",
        ))
        total += valor

    # a previsão de cartão da planilha sai: a agenda da Stone é a fonte agora
    da_planilha = _remover_previsao_da_planilha(hoje)

    db.session.flush()
    auditoria.registrar("import", "lancamento", None, depois={
        "origem": ORIGEM_AGENDA, "dias": len(agenda["por_data"]), "total": str(total),
        "removidos_da_planilha": da_planilha,
    })
    return {"dias": len(agenda["por_data"]), "total": total, "substituidos": len(antigos),
            "removidos_da_planilha": da_planilha, "erros": agenda["erros"]}


def _remover_previsao_da_planilha(hoje) -> int:
    """Tira as linhas de cartão que a planilha projetava para os próximos dias."""
    from app.models.banco import ConciliacaoItem

    ids_cat = db.session.execute(
        db.select(Categoria.id).filter(Categoria.nome.in_(stone.CATEGORIAS_STONE),
                                       Categoria.tipo == "entrada")
    ).scalars().all()
    if not ids_cat:
        return 0
    ids = db.session.execute(
        db.select(Lancamento.id).filter(
            Lancamento.categoria_id.in_(ids_cat), Lancamento.origem == "manual",
            Lancamento.data > hoje)
    ).scalars().all()
    if not ids:
        return 0
    db.session.execute(db.delete(ConciliacaoItem).where(ConciliacaoItem.lancamento_id.in_(ids)))
    db.session.execute(db.delete(Lancamento).where(Lancamento.id.in_(ids)))
    return len(ids)
