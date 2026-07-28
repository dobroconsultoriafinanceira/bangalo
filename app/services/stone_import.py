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
