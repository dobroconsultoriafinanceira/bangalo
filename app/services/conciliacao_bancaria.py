# -*- coding: utf-8 -*-
"""Motor de conciliação bancária — puro e testável.

Casa transações do banco (extrato) com os lançamentos do fluxo de caixa por
tipo (entrada/saída), valor e data dentro de uma janela de tolerância.
Independe de origem: serve para Itaú (API), OFX ou qualquer extrato
normalizado em `TransacaoBancaria`.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from app.importers.itau_adapter import CREDITO, TransacaoBancaria

# tipo do banco -> tipo do fluxo
TIPO_FLUXO = {CREDITO: "entrada"}  # débito -> "saida" (default)


@dataclass
class LancamentoRef:
    """Lançamento do fluxo reduzido ao necessário para conciliar."""

    id: int
    data: date
    valor: Decimal
    tipo: str  # entrada | saida


@dataclass
class Par:
    transacao: TransacaoBancaria
    lancamento: LancamentoRef
    dias_diferenca: int


@dataclass
class ResultadoConciliacao:
    conciliados: list[Par] = field(default_factory=list)
    so_no_banco: list[TransacaoBancaria] = field(default_factory=list)   # falta lançar no fluxo
    so_no_fluxo: list[LancamentoRef] = field(default_factory=list)       # não achou no extrato

    @property
    def total_conciliado(self) -> Decimal:
        return sum((p.transacao.valor for p in self.conciliados), Decimal("0"))

    def resumo(self) -> dict:
        return {
            "conciliados": len(self.conciliados),
            "so_no_banco": len(self.so_no_banco),
            "so_no_fluxo": len(self.so_no_fluxo),
            "total_conciliado": str(self.total_conciliado),
        }


def _tipo_fluxo(t: TransacaoBancaria) -> str:
    return TIPO_FLUXO.get(t.tipo, "saida")


def conciliar(
    transacoes: list[TransacaoBancaria],
    lancamentos: list[LancamentoRef],
    tolerancia_dias: int = 3,
) -> ResultadoConciliacao:
    """Casa 1-para-1 por tipo + valor exato + data dentro da janela.

    Estratégia gulosa: para cada transação do banco, escolhe o lançamento
    compatível ainda livre com a menor diferença de dias. Determinístico.
    """
    livres = list(lancamentos)
    res = ResultadoConciliacao()

    # processa em ordem estável (data, valor) para resultado determinístico
    for t in sorted(transacoes, key=lambda x: (x.data, x.valor, x.id)):
        tipo = _tipo_fluxo(t)
        candidatos = [
            (abs((t.data - l.data).days), l)
            for l in livres
            if l.tipo == tipo
            and l.valor == t.valor
            and abs((t.data - l.data).days) <= tolerancia_dias
        ]
        if candidatos:
            candidatos.sort(key=lambda c: (c[0], c[1].data, c[1].id))
            dias, escolhido = candidatos[0]
            res.conciliados.append(Par(transacao=t, lancamento=escolhido, dias_diferenca=dias))
            livres.remove(escolhido)
        else:
            res.so_no_banco.append(t)

    res.so_no_fluxo = livres
    return res


# ----------------------------- combinação (1 movimento → N lançamentos) -----------------------------

LIMITE_CANDIDATOS = 16   # acima disso a busca exata fica cara demais
LIMITE_PARCELAS = 8      # ninguém junta mais que isso num repasse


def combinar(alvo: Decimal, candidatos: list[LancamentoRef]) -> list[LancamentoRef] | None:
    """Menor conjunto de lançamentos cuja soma dá exatamente `alvo`.

    É o caso do repasse de cartão: um crédito do banco junta as vendas do dia
    (Visa, Master, ELO, débito, PIX...). Busca exaustiva, em centavos, limitada
    a LIMITE_CANDIDATOS lançamentos — determinística e sem depender de ordem.
    """
    from itertools import combinations

    if alvo <= 0 or not candidatos:
        return None
    # os maiores primeiro: entra rápido no valor do repasse
    itens = sorted(candidatos, key=lambda l: (-l.valor, l.data, l.id))[:LIMITE_CANDIDATOS]
    centavos = {id(l): int((l.valor * 100).to_integral_value()) for l in itens}
    alvo_c = int((alvo * 100).to_integral_value())

    for tamanho in range(2, min(LIMITE_PARCELAS, len(itens)) + 1):
        melhor = None
        for combinacao in combinations(itens, tamanho):
            if sum(centavos[id(l)] for l in combinacao) == alvo_c:
                # entre as de mesmo tamanho, a mais próxima da data do movimento
                chave = tuple(sorted(l.id for l in combinacao))
                if melhor is None or chave < melhor[0]:
                    melhor = (chave, list(combinacao))
        if melhor:
            return melhor[1]
    return None
