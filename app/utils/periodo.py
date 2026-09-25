# -*- coding: utf-8 -*-
"""Período global escolhido no topo (mês/ano), guardado na sessão.

Regra: parâmetros explícitos na URL continuam mandando na tela; o período
global só substitui o PADRÃO de cada tela (antes: "mês de hoje"). Sem escolha
na sessão, cada tela mantém o padrão de sempre.
"""
from flask import session

from app.utils.datas import hoje_sp

CHAVE = "periodo_global"


def escolhido() -> tuple[int, int] | None:
    valor = session.get(CHAVE)
    try:
        ano, mes = int(valor[0]), int(valor[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (ano, mes) if 2000 <= ano <= 2100 and 1 <= mes <= 12 else None


def atual() -> tuple[int, int]:
    """Período em vigor: o escolhido ou o mês de hoje."""
    hoje = hoje_sp()
    return escolhido() or (hoje.year, hoje.month)


def definir(ano: int, mes: int) -> None:
    hoje = hoje_sp()
    if (ano, mes) == (hoje.year, hoje.month):
        session.pop(CHAVE, None)  # mês corrente = acompanhar o calendário
    else:
        session[CHAVE] = [ano, mes]


def limpar() -> None:
    session.pop(CHAVE, None)
