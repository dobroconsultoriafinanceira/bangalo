# -*- coding: utf-8 -*-
"""Consolidação do fluxo de caixa — subtotais e saldos sempre derivados.

Espelha os blocos da planilha:
  Total Entradas = Operacional + Patrocínio + Financeiras
  Receita Líquida = Total Entradas − Impostos
  Total Saídas = Impostos + Folha + Despesas Gerais + Compras (CPV)
               + Despesas Fixas + Outros/Financeiro
  Saldo Final(dia) = Saldo Inicial(dia) + Entradas − Saídas
  Saldo Inicial(dia) = Saldo Final(dia−1); o 1º dia vem de
  ConfigSistema('saldo_inicial_abertura').
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.fluxo import Categoria, ConfigSistema, Lancamento

CENTAVOS = Decimal("0.01")

CHAVE_SALDO_ABERTURA = "saldo_inicial_abertura"
CHAVE_DATA_ABERTURA = "data_abertura"

# Grupos do plano de contas (seed) — usados nos agrupamentos das telas
GRUPOS_ENTRADA = ("Operacional", "Patrocínio", "Financeiras")
GRUPOS_SAIDA = (
    "Impostos", "Folha/Salários", "Despesas Gerais", "Compras (CPV)",
    "Despesas Fixas", "Outros/Financeiro",
)


def saldo_abertura() -> tuple[Decimal, date | None]:
    valor = Decimal(ConfigSistema.obter(CHAVE_SALDO_ABERTURA, "0"))
    data_txt = ConfigSistema.obter(CHAVE_DATA_ABERTURA)
    return valor, date.fromisoformat(data_txt) if data_txt else None


def totais_por_tipo(inicio: date, fim: date) -> dict[str, Decimal]:
    """{'entrada': X, 'saida': Y} no intervalo (inclusive)."""
    linhas = (
        db.session.query(Categoria.tipo, func.coalesce(func.sum(Lancamento.valor), 0))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Categoria.tipo)
        .all()
    )
    resultado = {"entrada": Decimal("0"), "saida": Decimal("0")}
    for tipo, total in linhas:
        resultado[tipo] = Decimal(total)
    return resultado


def totais_por_grupo(inicio: date, fim: date) -> dict[str, Decimal]:
    linhas = (
        db.session.query(Categoria.grupo, func.coalesce(func.sum(Lancamento.valor), 0))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Categoria.grupo)
        .all()
    )
    return {grupo: Decimal(total) for grupo, total in linhas}


def saldo_ate(dia: date) -> Decimal:
    """Saldo final acumulado até `dia` = abertura + entradas − saídas até lá."""
    abertura, data_abertura = saldo_abertura()
    query = (
        db.session.query(Categoria.tipo, func.coalesce(func.sum(Lancamento.valor), 0))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data <= dia)
    )
    if data_abertura:
        query = query.filter(Lancamento.data >= data_abertura)
    totais = {"entrada": Decimal("0"), "saida": Decimal("0")}
    for tipo, total in query.group_by(Categoria.tipo).all():
        totais[tipo] = Decimal(total)
    return (abertura + totais["entrada"] - totais["saida"]).quantize(CENTAVOS)


def consolidacao_mensal(ano: int, mes_inicio: int = 1, mes_fim: int = 12) -> list[dict]:
    """Entradas, saídas e resultado por mês do ano (para tabela/gráfico)."""
    linhas = (
        db.session.query(
            func.extract("month", Lancamento.data).label("mes"),
            Categoria.tipo,
            func.coalesce(func.sum(Lancamento.valor), 0),
        )
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(func.extract("year", Lancamento.data) == ano)
        .group_by("mes", Categoria.tipo)
        .all()
    )
    por_mes: dict[int, dict[str, Decimal]] = {}
    for mes, tipo, total in linhas:
        por_mes.setdefault(int(mes), {"entrada": Decimal("0"), "saida": Decimal("0")})[tipo] = Decimal(total)
    resultado = []
    for mes in range(mes_inicio, mes_fim + 1):
        t = por_mes.get(mes, {"entrada": Decimal("0"), "saida": Decimal("0")})
        resultado.append({
            "mes": mes,
            "entradas": t["entrada"].quantize(CENTAVOS),
            "saidas": t["saida"].quantize(CENTAVOS),
            "resultado": (t["entrada"] - t["saida"]).quantize(CENTAVOS),
        })
    return resultado


def visao_mensal(ano: int, mes: int) -> dict:
    """Grade grupo × dia do mês (tabela colapsável da tela de fluxo)."""
    import calendar

    ultimo = calendar.monthrange(ano, mes)[1]
    inicio, fim = date(ano, mes, 1), date(ano, mes, ultimo)

    linhas = (
        db.session.query(
            Lancamento.data,
            Categoria.tipo,
            Categoria.grupo,
            Categoria.nome,
            Categoria.id,
            func.coalesce(func.sum(Lancamento.valor), 0),
        )
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Lancamento.data, Categoria.tipo, Categoria.grupo, Categoria.nome, Categoria.id)
        .order_by(Categoria.grupo, Categoria.nome)
        .all()
    )

    grupos: dict[str, dict] = {}
    total_dia: dict[int, Decimal] = {d: Decimal("0") for d in range(1, ultimo + 1)}
    for data_l, tipo, grupo, nome, cat_id, total in linhas:
        g = grupos.setdefault(grupo, {"tipo": tipo, "categorias": {}, "total_por_dia": {}, "total": Decimal("0")})
        cat = g["categorias"].setdefault(cat_id, {"nome": nome, "por_dia": {}, "total": Decimal("0")})
        valor = Decimal(total)
        dia = data_l.day
        cat["por_dia"][dia] = cat["por_dia"].get(dia, Decimal("0")) + valor
        cat["total"] += valor
        g["total_por_dia"][dia] = g["total_por_dia"].get(dia, Decimal("0")) + valor
        g["total"] += valor
        sinal = valor if tipo == "entrada" else -valor
        total_dia[dia] += sinal

    # saldo dia a dia do mês (inicial = saldo final do dia anterior ao mês)
    from datetime import timedelta

    saldo_inicial_mes = saldo_ate(inicio - timedelta(days=1))
    saldos: dict[int, Decimal] = {}
    acumulado = saldo_inicial_mes
    for d in range(1, ultimo + 1):
        acumulado += total_dia[d]
        saldos[d] = acumulado.quantize(CENTAVOS)

    return {
        "ano": ano,
        "mes": mes,
        "dias": list(range(1, ultimo + 1)),
        "grupos": grupos,
        "saldo_inicial": saldo_inicial_mes.quantize(CENTAVOS),
        "saldos_por_dia": saldos,
    }


def ranking_despesas(inicio: date, fim: date, limite: int = 10) -> list[tuple[str, Decimal]]:
    """Maiores grupos de despesa no período (para o dashboard)."""
    linhas = (
        db.session.query(Categoria.grupo, func.coalesce(func.sum(Lancamento.valor), 0).label("total"))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Categoria.tipo == "saida", Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Categoria.grupo)
        .order_by(func.sum(Lancamento.valor).desc())
        .limit(limite)
        .all()
    )
    return [(grupo, Decimal(total).quantize(CENTAVOS)) for grupo, total in linhas]
