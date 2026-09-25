# -*- coding: utf-8 -*-
"""Filtros Jinja centralizados: moeda BRL, datas e percentuais."""
from datetime import date, datetime
from decimal import Decimal

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]
MESES_ABREV_PT = ["", "JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
                  "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]
DIAS_SEMANA_PT = ["Seg.", "Ter.", "Qua.", "Qui.", "Sex.", "Sáb.", "Dom."]


def format_brl(valor, com_simbolo: bool = True, sinal: bool = False) -> str:
    """1234567.8 -> 'R$ 1.234.567,80'.

    Negativos entre parênteses (relatórios em PDF) ou com sinal `-R$` (telas
    Clareza: `sinal=True`)."""
    if valor is None:
        return "—"
    valor = Decimal(str(valor))
    negativo = valor < 0
    inteiro, _, centavos = f"{abs(valor):.2f}".partition(".")
    grupos = []
    while inteiro:
        grupos.append(inteiro[-3:])
        inteiro = inteiro[:-3]
    texto = ".".join(reversed(grupos)) + "," + centavos
    if com_simbolo:
        texto = "R$ " + texto
    if negativo:
        return f"-{texto}" if sinal else f"({texto})"
    return texto


def format_brl_abreviado(valor) -> str:
    """Eixos e rótulos curtos: 15 mil, 1,2 mi."""
    if valor is None:
        return "—"
    v = float(valor)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} mi".replace(".", ",")
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f} mil"
    return f"R$ {v:.0f}"


def format_data(valor, formato: str = "%d/%m/%Y") -> str:
    if valor is None:
        return "—"
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor)
    return valor.strftime(formato)


def format_data_curta(valor) -> str:
    return format_data(valor, "%d/%m")


def format_pct(valor, casas: int = 0) -> str:
    """0.98 -> '98%'. None -> '—'."""
    if valor is None:
        return "—"
    return f"{Decimal(str(valor)) * 100:.{casas}f}".replace(".", ",") + "%"


def nome_mes(mes: int, abreviado: bool = False) -> str:
    tabela = MESES_ABREV_PT if abreviado else MESES_PT
    return tabela[mes] if 1 <= mes <= 12 else ""


def dia_semana_pt(valor: date) -> str:
    return DIAS_SEMANA_PT[valor.weekday()]


def registrar(app):
    app.jinja_env.filters["brl"] = lambda valor, com_simbolo=True: format_brl(valor, com_simbolo, sinal=True)
    app.jinja_env.filters["brl_abrev"] = format_brl_abreviado
    app.jinja_env.filters["data"] = format_data
    app.jinja_env.filters["data_curta"] = format_data_curta
    app.jinja_env.filters["pct"] = format_pct
    app.jinja_env.filters["nome_mes"] = nome_mes
    app.jinja_env.filters["dia_semana"] = dia_semana_pt
