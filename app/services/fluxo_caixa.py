# -*- coding: utf-8 -*-
"""Consolidação do fluxo de caixa — subtotais e saldos sempre derivados.

Espelha os blocos da planilha:
  Total Entradas = vendas (Stone/PIX/delivery/dinheiro) + outras entradas
  Receita Líquida = Total Entradas − Impostos
  Total Saídas = Impostos + Folha/Salários + Demais Salários + Compras
               + Despesas Fixas + Outras despesas
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

# Categorias do plano de contas — a ordem aqui é a ordem das telas
# (a subcategoria de Compras é o fornecedor cadastrado)
# o que conta como receita da operação e o que é "outras entradas" no demonstrativo
GRUPOS_RECEITA = ("Vendas - Repasse Stone", "Pix Itau", "IFOOD", "99 FOOD", "Dinheiro")
GRUPOS_OUTRAS_ENTRADAS = ("Outros/Acertos", "Patrocínio", "Empréstimo", "Resgate", "RENDIMENTO")
# ordem das telas — a pedida pela consultoria; as não citadas vêm depois
GRUPOS_ENTRADA = (
    "Vendas - Repasse Stone", "Pix Itau", "Outros/Acertos", "Patrocínio", "Empréstimo",
    "Resgate", "IFOOD", "99 FOOD", "Dinheiro", "RENDIMENTO",
)
assert set(GRUPOS_ENTRADA) == set(GRUPOS_RECEITA) | set(GRUPOS_OUTRAS_ENTRADAS)
GRUPOS_SAIDA = (
    "Impostos", "Folha/Salários", "Demais Salários", "Compras",
    "Despesas Fixas", "Outras despesas",
)
GRUPO_COMPRAS = "Compras"
GRUPO_A_CLASSIFICAR = "A classificar"

GRUPO_ORDEM = {g: i for i, g in enumerate(GRUPOS_ENTRADA + GRUPOS_SAIDA)}


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


def dre_mensal(ano: int, mes: int, ate: date | None = None) -> dict:
    """Estrutura do DRE para o mês/ano informado, derivada dos grupos de categorias.

    `ate` limita ao realizado (ex.: até hoje): a planilha traz recebíveis e
    despesas programadas para datas futuras (previstos).
    """
    from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

    inicio = primeiro_dia_mes(ano, mes)
    fim = ultimo_dia_mes(ano, mes)
    if ate is not None:
        fim = min(fim, ate)

    g = totais_por_grupo(inicio, fim) if fim >= inicio else {}
    z = Decimal("0")

    soma_grupos = lambda nomes: sum((g.get(n, z) for n in nomes), z)
    receita_bruta   = soma_grupos(GRUPOS_RECEITA).quantize(CENTAVOS)
    impostos        = g.get("Impostos", z).quantize(CENTAVOS)
    receita_liquida = (receita_bruta - impostos).quantize(CENTAVOS)

    cpv         = g.get(GRUPO_COMPRAS, z).quantize(CENTAVOS)
    lucro_bruto = (receita_liquida - cpv).quantize(CENTAVOS)

    folha       = g.get("Folha/Salários", z).quantize(CENTAVOS)
    demais_sal  = g.get("Demais Salários", z).quantize(CENTAVOS)
    desp_fixas  = g.get("Despesas Fixas", z).quantize(CENTAVOS)
    desp_gerais = g.get("Outras despesas", z).quantize(CENTAVOS)

    total_pessoal   = (folha + demais_sal).quantize(CENTAVOS)
    total_desp_op   = (desp_fixas + desp_gerais).quantize(CENTAVOS)
    total_despesas  = (total_pessoal + total_desp_op).quantize(CENTAVOS)

    resultado_op     = (lucro_bruto - total_despesas).quantize(CENTAVOS)
    outras_entradas  = soma_grupos(GRUPOS_OUTRAS_ENTRADAS).quantize(CENTAVOS)
    resultado        = (resultado_op + outras_entradas).quantize(CENTAVOS)

    return {
        "inicio":                 inicio,
        "fim":                    fim,
        "prime_cost":             (cpv + total_pessoal).quantize(CENTAVOS),  # CMV + pessoal
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

    def grupo_da_grade(nome: str, tipo: str) -> dict:
        """Grupo fora da ordem da planilha (categoria nova) entra no fim, sem quebrar a grade."""
        return grupos.setdefault(nome, {"tipo": tipo, "categorias": {}, "total_por_dia": {},
                                        "total": Decimal("0")})

    for cat in todas_cats:
        if cat.grupo not in GRUPO_ORDEM:
            continue
        g = grupo_da_grade(cat.grupo, cat.tipo)
        if cat.grupo != GRUPO_COMPRAS:
            g["categorias"].setdefault(str(cat.id), {"nome": cat.nome, "por_dia": {}, "total": Decimal("0")})

    total_dia: dict[int, Decimal] = {d: Decimal("0") for d in range(1, ultimo + 1)}
    for data_l, tipo, grupo, nome, cat_id, forn_id, forn_nome, total in linhas:
        g = grupo_da_grade(grupo, tipo)
        if grupo == GRUPO_COMPRAS and forn_id:
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

    # a grade é lida no vocabulário da planilha; o plano de contas não muda
    from app.services import grade_planilha

    fornecedores = db.session.execute(
        db.select(Fornecedor.nome).order_by(Fornecedor.id)).scalars().all()

    return {
        "ano": ano,
        "mes": mes,
        "dias": list(range(1, ultimo + 1)),
        "grupos": grade_planilha.aplicar(grupos, fornecedores),
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


def resumo_do_mes(visao: dict, ano: int, mes: int, hoje: date) -> dict:
    """Resumo do Caixa: saldo atual × previsto, série diária e semanas.

    Usa só o que a visão mensal já calculou (mesmos saldos da grade). Dias
    depois de hoje são previstos — lançamentos programados, não realizados.
    """
    dias = visao["dias"]
    entradas = {d: Decimal("0") for d in dias}
    saidas = {d: Decimal("0") for d in dias}
    for g in visao["grupos"].values():
        alvo = entradas if g["tipo"] == "entrada" else saidas
        for d, v in g["total_por_dia"].items():
            alvo[d] += v

    mes_atual = (ano, mes) == (hoje.year, hoje.month)
    futuro = (ano, mes) > (hoje.year, hoje.month)
    corte = hoje.day if mes_atual else (0 if futuro else dias[-1])
    saldos = visao["saldos_por_dia"]
    saldo_atual = saldos[corte] if corte else visao["saldo_inicial"]
    saldo_final = saldos[dias[-1]]

    semanas = []
    for inicio in range(1, dias[-1] + 1, 7):
        fim = min(inicio + 6, dias[-1])
        faixa = range(inicio, fim + 1)
        e = sum((entradas[d] for d in faixa), Decimal("0"))
        s_ = sum((saidas[d] for d in faixa), Decimal("0"))
        semanas.append({
            "rotulo": f"{inicio:02d} a {fim:02d}/{mes:02d}",
            "entradas": e, "saidas": s_, "resultado": e - s_,
            "previsto": inicio > corte,
            "parcial": inicio <= corte < fim,
        })

    serie = [{
        "dia": f"{d:02d}/{mes:02d}",
        "realizado": float(saldos[d]) if d <= corte else None,
        "previsto": float(saldos[d]) if d >= corte and corte < dias[-1] else None,
    } for d in dias]

    def ate_corte(valores: dict[int, Decimal], realizado: bool) -> Decimal:
        return sum((v for d, v in valores.items() if (d <= corte) == realizado), Decimal("0"))

    total_entradas = sum(entradas.values(), Decimal("0"))
    total_saidas = sum(saidas.values(), Decimal("0"))
    return {
        "corte": corte, "mes_atual": mes_atual, "futuro": futuro,
        "saldo_atual": saldo_atual, "saldo_final": saldo_final,
        "variacao": saldo_final - saldo_atual,
        "entradas": total_entradas,
        "saidas": total_saidas,
        "resultado": total_entradas - total_saidas,
        # realizado = até hoje; previsto = lançamentos programados do resto do mês
        "entradas_realizadas": ate_corte(entradas, True),
        "entradas_previstas": ate_corte(entradas, False),
        "saidas_realizadas": ate_corte(saidas, True),
        "saidas_previstas": ate_corte(saidas, False),
        "resultado_realizado": ate_corte(entradas, True) - ate_corte(saidas, True),
        "semanas": semanas, "serie": serie,
    }


# ordem dos seletores: as categorias do plano, e as técnicas por último
ORDEM_GRUPOS = GRUPOS_ENTRADA + GRUPOS_SAIDA + (GRUPO_A_CLASSIFICAR,)

# categorias que o sistema usa por baixo e ninguém escolhe na revisão
GRUPOS_AUTOMATICOS = (GRUPO_A_CLASSIFICAR,)


# linhas que só o sistema alimenta — não entram nos seletores de revisão
LINHAS_AUTOMATICAS = ("Recebíveis de cartão (previsto)",)


def linhas_da_planilha(tipo: str | None = None) -> list[dict]:
    """Linhas do caixa para os seletores da tela, na ordem dos blocos.

    É o vocabulário da planilha — o mesmo que a Bárbara e a Manu usam — e não a
    taxonomia bancária interna, que continua servindo só para DRE e relatórios.
    """
    consulta = db.select(Categoria).filter_by(ativo=True)
    if tipo:
        consulta = consulta.filter_by(tipo=tipo)
    linhas = db.session.execute(
        consulta.order_by(Categoria.grupo, Categoria.ordem, Categoria.nome)
    ).scalars().all()
    ordem = {g: i for i, g in enumerate(ORDEM_GRUPOS)}
    linhas.sort(key=lambda c: (ordem.get(c.grupo, len(ORDEM_GRUPOS)), c.grupo, c.ordem, c.nome))
    return [{"id": c.id, "nome": c.nome, "grupo": c.grupo, "tipo": c.tipo}
            for c in linhas
            if c.nome not in LINHAS_AUTOMATICAS and c.grupo not in GRUPOS_AUTOMATICOS]
