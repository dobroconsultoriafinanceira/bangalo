# -*- coding: utf-8 -*-
"""Helpers de data — tudo em America/Sao_Paulo."""
import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_SP = ZoneInfo("America/Sao_Paulo")


def agora_sp() -> datetime:
    return datetime.now(TZ_SP)


def hoje_sp() -> date:
    return agora_sp().date()


def primeiro_dia_mes(ano: int, mes: int) -> date:
    return date(ano, mes, 1)


def ultimo_dia_mes(ano: int, mes: int) -> date:
    return date(ano, mes, calendar.monthrange(ano, mes)[1])
