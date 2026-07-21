# -*- coding: utf-8 -*-
"""Engine de metas — validada contra a planilha real.

Ex. JAN/2026: (465.296,65 × 0,60) + (360.926,02 × 0,40) = 423.548,40
             × 1,08 = 457.432,27  ✔ confere com a planilha.

Metas diárias: a meta do mês é rateada pelos dias abertos usando pesos por
dia da semana (Ter/Qua 1,0 · Qui/Sex/Dom 1,4 · Sáb 1,6 · Seg fechado).
Ex. JUL/2026 (soma de pesos 35): Qua = 400.471,21 × 1/35 = 11.442,03 ✔.
"""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENTAVOS = Decimal("0.01")


@dataclass
class Premissas:
    peso_mes_anterior: Decimal = Decimal("0.60")
    peso_media_historica: Decimal = Decimal("0.40")
    crescimento_alvo: Decimal = Decimal("0.08")
    peso_ter_qua: Decimal = Decimal("1.0")
    peso_qui_sex_dom: Decimal = Decimal("1.4")
    peso_sabado: Decimal = Decimal("1.6")

    def peso_do_dia(self, d: date) -> Decimal:
        wd = d.weekday()  # 0=Seg ... 6=Dom
        if wd == 0:
            return Decimal("0")  # segunda: restaurante fechado
        if wd in (1, 2):
            return self.peso_ter_qua
        if wd == 5:
            return self.peso_sabado
        return self.peso_qui_sex_dom  # Qui, Sex, Dom


def media_historica_mes(mes: int, historico: dict[int, dict[int, Decimal]], anos: list[int]) -> Decimal | None:
    """Média do mesmo mês nos anos históricos que têm valor."""
    valores = [historico[a][mes] for a in anos if a in historico and mes in historico[a]]
    if not valores:
        return None
    return sum(valores, Decimal("0")) / Decimal(len(valores))


def base_ponderada(valor_ano_anterior: Decimal | None, media_hist: Decimal | None, p: Premissas) -> Decimal | None:
    if valor_ano_anterior is None or media_hist is None:
        return None
    return valor_ano_anterior * p.peso_mes_anterior + media_hist * p.peso_media_historica


def meta_do_mes(valor_ano_anterior: Decimal | None, media_hist: Decimal | None, p: Premissas) -> Decimal | None:
    base = base_ponderada(valor_ano_anterior, media_hist, p)
    if base is None:
        return None
    return base * (Decimal("1") + p.crescimento_alvo)


@dataclass
class LinhaMeta:
    mes: int
    valor_ano_anterior: Decimal | None
    media_historica: Decimal | None
    base_ponderada: Decimal | None
    meta: Decimal | None
    realizado: Decimal | None
    pct_meta: Decimal | None
    meta_acumulada: Decimal | None
    realizado_acumulado: Decimal | None


def tabela_anual(
    ano: int,
    historico: dict[int, dict[int, Decimal]],
    realizados: dict[int, Decimal],
    p: Premissas,
    anos_historicos: list[int] | None = None,
) -> list[LinhaMeta]:
    """Tabela mês a mês com acumulados — espelho da aba Metas."""
    anos_hist = anos_historicos or sorted(a for a in historico if a < ano)
    linhas: list[LinhaMeta] = []
    meta_acum = Decimal("0")
    real_acum = Decimal("0")
    for mes in range(1, 13):
        anterior = historico.get(ano - 1, {}).get(mes)
        media = media_historica_mes(mes, historico, anos_hist)
        base = base_ponderada(anterior, media, p)
        meta = meta_do_mes(anterior, media, p)
        realizado = realizados.get(mes)
        pct = (realizado / meta) if (realizado is not None and meta) else None
        if meta is not None:
            meta_acum += meta
        if realizado is not None:
            real_acum += realizado
        linhas.append(LinhaMeta(
            mes=mes,
            valor_ano_anterior=anterior,
            media_historica=_q(media),
            base_ponderada=_q(base),
            meta=_q(meta),
            realizado=_q(realizado),
            pct_meta=pct.quantize(Decimal("0.0001")) if pct is not None else None,
            meta_acumulada=_q(meta_acum) if meta is not None else None,
            realizado_acumulado=_q(real_acum) if realizado is not None else None,
        ))
    return linhas


def metas_diarias(ano: int, mes: int, meta_mes: Decimal, p: Premissas, dias_fechados_extra: set[date] | None = None) -> dict[date, Decimal]:
    """Rateia a meta do mês pelos dias abertos conforme pesos de dia da semana."""
    fechados = dias_fechados_extra or set()
    dias = [
        date(ano, mes, d)
        for d in range(1, calendar.monthrange(ano, mes)[1] + 1)
    ]
    pesos = {d: (Decimal("0") if d in fechados else p.peso_do_dia(d)) for d in dias}
    soma = sum(pesos.values(), Decimal("0"))
    if soma == 0:
        return {d: Decimal("0") for d in dias}
    return {d: (meta_mes * pesos[d] / soma).quantize(CENTAVOS) for d in dias}


def meta_quinzena(meta_mes: Decimal | None) -> Decimal | None:
    """A planilha usa metade da meta do mês por quinzena."""
    if meta_mes is None:
        return None
    # a planilha arredonda meio-pra-cima (200235,605 -> 200235,61)
    return (meta_mes / Decimal("2")).quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def _q(v: Decimal | None) -> Decimal | None:
    return v.quantize(CENTAVOS) if v is not None else None
