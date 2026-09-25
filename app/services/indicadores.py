# -*- coding: utf-8 -*-
"""Indicadores gerenciais do fluxo (visão CFO), separando realizado × previsto.

A planilha do cliente é um fluxo PROJETADO: além do que já aconteceu, traz
recebíveis de cartão e despesas programadas para datas futuras. Por isso o mês
corrente é medido até hoje (realizado) e o restante do mês aparece à parte
(previsto).

- CMV: compras de insumos (categoria "Compras").
- Pessoal: Folha/Salários + Demais Salários.
- Prime Cost: CMV + pessoal — o principal indicador de custo de restaurante.
Percentuais sobre as entradas do período.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.services import fluxo_caixa as fluxo_srv
from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

ZERO = Decimal("0.00")
CENTAVOS = Decimal("0.01")
GRUPO_CMV = "Compras"
GRUPOS_PESSOAL = ("Folha/Salários", "Demais Salários")


def _razao(valor: Decimal, base: Decimal):
    return float(valor / base) if base else None


@dataclass(frozen=True)
class Indicadores:
    inicio: date
    fim: date
    entradas: Decimal = ZERO
    saidas: Decimal = ZERO
    impostos: Decimal = ZERO
    cmv: Decimal = ZERO
    pessoal: Decimal = ZERO

    @property
    def vazio(self) -> bool:
        return self.fim < self.inicio

    @property
    def resultado(self) -> Decimal:
        return self.entradas - self.saidas

    @property
    def prime_cost(self) -> Decimal:
        return self.cmv + self.pessoal

    @property
    def pct_cmv(self):
        return _razao(self.cmv, self.entradas)

    @property
    def pct_pessoal(self):
        return _razao(self.pessoal, self.entradas)

    @property
    def pct_prime_cost(self):
        return _razao(self.prime_cost, self.entradas)

    @property
    def margem_bruta(self):
        return _razao(self.entradas - self.cmv, self.entradas)

    @property
    def margem_liquida(self):
        return _razao(self.resultado, self.entradas)


def _q(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVOS)


def calcular(inicio: date, fim: date) -> Indicadores:
    if fim < inicio:
        return Indicadores(inicio, fim)
    tipos = fluxo_srv.totais_por_tipo(inicio, fim)
    grupos = fluxo_srv.totais_por_grupo(inicio, fim)
    return Indicadores(
        inicio=inicio,
        fim=fim,
        entradas=_q(tipos.get("entrada")),
        saidas=_q(tipos.get("saida")),
        impostos=_q(grupos.get("Impostos")),
        cmv=_q(grupos.get(GRUPO_CMV)),
        pessoal=_q(sum((_q(grupos.get(g)) for g in GRUPOS_PESSOAL), ZERO)),
    )


def mes_realizado_previsto(ano: int, mes: int, hoje: date) -> dict:
    """Realizado = do dia 1 até hoje; previsto = de amanhã até o fim do mês.
    Mês passado: tudo realizado. Mês futuro: tudo previsto."""
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    corte = min(max(hoje, inicio - timedelta(days=1)), fim)
    return {
        "inicio": inicio,
        "fim": fim,
        "corte": corte,
        "realizado": calcular(inicio, corte),
        "previsto": calcular(corte + timedelta(days=1), fim),
    }


def mesmo_periodo_mes_anterior(hoje: date) -> Indicadores:
    """Do dia 1 ao mesmo dia do mês anterior (comparação justa com o mês em curso)."""
    mes = hoje.month - 1 or 12
    ano = hoje.year if hoje.month > 1 else hoje.year - 1
    ultimo = ultimo_dia_mes(ano, mes)
    return calcular(primeiro_dia_mes(ano, mes), date(ano, mes, min(hoje.day, ultimo.day)))
