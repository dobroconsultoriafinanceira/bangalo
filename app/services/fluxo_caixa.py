# -*- coding: utf-8 -*-
"""Consolidação do fluxo de caixa — subtotais e saldos sempre derivados.

Espelha os blocos da planilha:
  Total Entradas = Entradas + Outras Entradas
  Receita Líquida = Total Entradas − Impostos
  Total Saídas = Impostos + Folha/Salários + Demais Salários + Compras (CPV)
               + Despesas Fixas + Despesas Gerais
  Saldo Final(dia) = Saldo Inicial(dia) + Entradas − Saídas
  Saldo Inicial(dia) = Saldo Final(dia−1); o 1º dia vem de
  ConfigSistema('saldo_inicial_abertura').
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.fluxo import Categoria, ConfigSistema, Fornecedor, Lancamento

CENTAVOS = Decimal("0.01")

CHAVE_SALDO_ABERTURA = "saldo_inicial_abertura"
CHAVE_DATA_ABERTURA = "data_abertura"

# Grupos do plano de contas (seed) — usados nos agrupamentos das telas
GRUPOS_ENTRADA = ("Entradas", "Outras Entradas")
GRUPOS_SAIDA = (
    "Impostos", "Folha/Salários", "Demais Salários", "Compras (CPV)",
    "Despesas Fixas", "Despesas Gerais",
)

GRUPO_ORDEM = {
    "Entradas": 0, "Outras Entradas": 1, "Impostos": 2,
    "Folha/Salários": 3, "Demais Salários": 4, "Compras (CPV)": 5,
    "Despesas Fixas": 6, "Despesas Gerais": 7,
}


def saldo_abertura() -> tuple[Decimal, date | None]:
    valor = Decimal(ConfigSistema.obter(CHAVE_SALDO_ABERTURA, "0"))
    data_txt = ConfigSistema.obter(CHAVE_DATA_ABERTURA)
    if not data_txt:
        return valor, None
    # tolera tanto "2026-01-01" quanto "2026-01-01T00:00:00"
    return valor, date.fromisoformat(data_txt[:10])


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


def dre_mensal(ano: int, mes: int) -> dict:
    """Estrutura do DRE para o mês/ano informado, derivada dos grupos de categorias."""
    from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

    inicio = primeiro_dia_mes(ano, mes)
    fim = ultimo_dia_mes(ano, mes)

    g = totais_por_grupo(inicio, fim)
    z = Decimal("0")

    receita_bruta   = g.get("Entradas", z).quantize(CENTAVOS)
    impostos        = g.get("Impostos", z).quantize(CENTAVOS)
    receita_liquida = (receita_bruta - impostos).quantize(CENTAVOS)

    cpv         = g.get("Compras (CPV)", z).quantize(CENTAVOS)
    lucro_bruto = (receita_liquida - cpv).quantize(CENTAVOS)

    folha       = g.get("Folha/Salários", z).quantize(CENTAVOS)
    demais_sal  = g.get("Demais Salários", z).quantize(CENTAVOS)
    desp_fixas  = g.get("Despesas Fixas", z).quantize(CENTAVOS)
    desp_gerais = g.get("Despesas Gerais", z).quantize(CENTAVOS)

    total_pessoal   = (folha + demais_sal).quantize(CENTAVOS)
    total_desp_op   = (desp_fixas + desp_gerais).quantize(CENTAVOS)
    total_despesas  = (total_pessoal + total_desp_op).quantize(CENTAVOS)

    resultado_op     = (lucro_bruto - total_despesas).quantize(CENTAVOS)
    outras_entradas  = g.get("Outras Entradas", z).quantize(CENTAVOS)
    resultado        = (resultado_op + outras_entradas).quantize(CENTAVOS)

    return {
        "receita_bruta":          receita_bruta,
        "impostos":               impostos,
        "receita_liquida":        receita_liquida,
        "cpv":                    cpv,
        "lucro_bruto":            lucro_bruto,
        "folha":                  folha,
        "demais_salarios":        demais_sal,
        "total_pessoal":          total_pessoal,
        "despesas_fixas":         desp_fixas,
        "despesas_gerais":        desp_gerais,
        "total_desp_operacionais": total_desp_op,
        "total_despesas":         total_despesas,
        "resultado_operacional":  resultado_op,
        "outras_entradas":        outras_entradas,
        "resultado":              resultado,
    }


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
            Lancamento.fornecedor_id,
            func.coalesce(Fornecedor.nome, "").label("forn_nome"),
            func.coalesce(func.sum(Lancamento.valor), 0),
        )
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .outerjoin(Fornecedor, Lancamento.fornecedor_id == Fornecedor.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(
            Lancamento.data, Categoria.tipo, Categoria.grupo, Categoria.nome,
            Categoria.id, Lancamento.fornecedor_id, Fornecedor.nome,
        )
        .order_by(Categoria.grupo, Categoria.ordem, Fornecedor.nome, Categoria.nome)
        .all()
    )

    # pré-popula todos os grupos/categorias com zeros para sempre exibir todas as linhas
    todas_cats = (
        db.session.query(Categoria)
        .filter(Categoria.ativo == True)
        .order_by(Categoria.grupo, Categoria.ordem, Categoria.nome)
        .all()
    )
    grupos: dict[str, dict] = {}
    for cat in todas_cats:
        if cat.grupo not in GRUPO_ORDEM:
            continue
        g = grupos.setdefault(cat.grupo, {"tipo": cat.tipo, "categorias": {}, "total_por_dia": {}, "total": Decimal("0")})
        if cat.grupo != "Compras (CPV)":
            g["categorias"].setdefault(str(cat.id), {"nome": cat.nome, "por_dia": {}, "total": Decimal("0")})

    total_dia: dict[int, Decimal] = {d: Decimal("0") for d in range(1, ultimo + 1)}
    for data_l, tipo, grupo, nome, cat_id, forn_id, forn_nome, total in linhas:
        g = grupos[grupo]
        if grupo == "Compras (CPV)" and forn_id:
            row_key = f"f{forn_id}"
            row_nome = forn_nome or nome
        else:
            row_key = str(cat_id)
            row_nome = nome
        cat = g["categorias"].setdefault(row_key, {"nome": row_nome, "por_dia": {}, "total": Decimal("0")})
        valor = Decimal(total)
        dia = data_l.day
        cat["por_dia"][dia] = cat["por_dia"].get(dia, Decimal("0")) + valor
        cat["total"] += valor
        g["total_por_dia"][dia] = g["total_por_dia"].get(dia, Decimal("0")) + valor
        g["total"] += valor
        sinal = valor if tipo == "entrada" else -valor
        total_dia[dia] += sinal

    # ordenar grupos conforme planilha
    grupos = dict(sorted(grupos.items(), key=lambda x: GRUPO_ORDEM.get(x[0], 99)))

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
