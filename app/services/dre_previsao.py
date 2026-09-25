# -*- coding: utf-8 -*-
"""Previsão do DRE contábil a partir dos dados do próprio sistema.

O objetivo é saber, antes de a contabilidade enviar (entre os dias 15 e 20),
como o DRE do mês fechado deve vir — e depois comparar.

Como cada linha é prevista:

| Linha                | Método                                                   |
|----------------------|----------------------------------------------------------|
| Receita bruta        | faturamento do mês (PDV) informado ou estimado do caixa   |
| Deduções             | % do faturamento: gorjeta faturada, Simples e ICMS        |
| CMV                  | % do faturamento (a contabilidade usa estoque, não caixa) |
| Pessoal              | nossa folha paga × fator de calibração + pró-labore fixo  |
| Gerais               | nossas despesas pagas × fator de calibração               |
| Tributárias          | nosso IPTU pago × fator                                   |
| Financeiras          | taxa de cartão (% do faturamento) + juros + IOF/tarifas   |
| Depreciação          | valor fixo do último razão (não passa pelo caixa)         |
| Receitas financeiras | rendimento das aplicações (razão anterior + extrato)      |
| Outras receitas      | bonificações: média dos razões importados                 |

Os percentuais e fatores saem de `calibrar()`, que compara o razão importado
com os nossos números do mesmo mês. Ficam guardados em ConfigSistema, então a
previsão melhora a cada mês que a contabilidade envia.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import extract, func

from app.extensions import db
from app.models.fluxo import Categoria, ConfigSistema, Lancamento
from app.services import dre_contabil as dc
from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

CHAVE_PARAMETROS = "dre_previsao_parametros"
CHAVE_FATURAMENTO = "faturamento_bruto"  # + "_AAAA_MM"
CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")

# Pró-labore fixo da sócia (o excedente é adiantamento de lucros, não despesa)
PRO_LABORE_MENSAL = Decimal("8475.55")

# Categorias do nosso fluxo que compõem cada base de comparação
GRUPO_PESSOAL = "Folha/Salários"
GRUPO_GERAIS = ("Despesas Fixas",)
CATS_GERAIS_EXTRA = ("Músicos", "Técnico Som", "Segurança", "Outros Acertos", "Multas")
CATS_FORA_DE_GERAIS = ("IPTU", "Aplicação", "Empréstimos Heitor", "Reembolso Barbara")
CAT_IPTU = "IPTU"

PARAMETROS_PADRAO = {
    "pct_gorjeta": "0.09677",
    "pct_simples": "0.08584",
    "pct_icms": "0.02064",
    "pct_cmv": "0.27176",
    "pct_taxa_cartao": "0.01254",
    "fator_pessoal": "1",
    "fator_pessoal_operacional": "0.5384",  # pessoal contábil (sem pró-labore) ÷ folha paga
    "fator_gerais": "1",
    "fator_tributarias": "1",
    "juros_mes": "4302.81",
    "depreciacao_mes": "1418.59",
    "outras_receitas_mes": "989.82",
    "receitas_financeiras_mes": "5323.09",
    "calibrado_com": "",
}


@dataclass
class LinhaPrevista:
    chave: str
    rotulo: str
    valor: Decimal = ZERO
    metodo: str = ""
    detalhe: list[tuple[str, Decimal]] = field(default_factory=list)


def _q(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVOS)


# ------------------------------- parâmetros -------------------------------

def parametros() -> dict:
    guardado = ConfigSistema.obter(CHAVE_PARAMETROS)
    valores = dict(PARAMETROS_PADRAO)
    if guardado:
        try:
            valores.update(json.loads(guardado))
        except ValueError:
            pass
    return valores


def salvar_parametros(valores: dict) -> None:
    atual = parametros()
    atual.update({k: str(v) for k, v in valores.items()})
    ConfigSistema.definir(CHAVE_PARAMETROS, json.dumps(atual))


def faturamento_informado(ano: int, mes: int) -> Decimal | None:
    valor = ConfigSistema.obter(f"{CHAVE_FATURAMENTO}_{ano}_{mes:02d}")
    return _q(valor) if valor else None


def definir_faturamento(ano: int, mes: int, valor: Decimal) -> None:
    ConfigSistema.definir(f"{CHAVE_FATURAMENTO}_{ano}_{mes:02d}", str(_q(valor)))


# --------------------------- nossos números do mês ---------------------------

def _totais_por_categoria(ano: int, mes: int) -> dict[tuple[str, str], Decimal]:
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    linhas = db.session.execute(
        db.select(Categoria.grupo, Categoria.nome, func.sum(Lancamento.valor))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Categoria.grupo, Categoria.nome)
    ).all()
    return {(grupo, nome): _q(total) for grupo, nome, total in linhas}


def bases_do_mes(ano: int, mes: int) -> dict:
    """Nossos números de caixa que alimentam a previsão."""
    totais = _totais_por_categoria(ano, mes)
    soma = lambda itens: sum(itens, ZERO)  # noqa: E731

    pessoal = soma(v for (grupo, _), v in totais.items() if grupo == GRUPO_PESSOAL)
    gerais = soma(
        v for (grupo, nome), v in totais.items()
        if (grupo in GRUPO_GERAIS or nome in CATS_GERAIS_EXTRA) and nome not in CATS_FORA_DE_GERAIS
    )
    from app.services.fluxo_caixa import GRUPOS_RECEITA

    entradas_operacionais = soma(v for (grupo, _), v in totais.items() if grupo in GRUPOS_RECEITA)
    return {
        "pessoal": pessoal,
        "gerais": gerais,
        "iptu": totais.get(("Despesas Fixas", CAT_IPTU), ZERO),
        "entradas_operacionais": entradas_operacionais,
        "cmv_caixa": soma(v for (grupo, _), v in totais.items() if grupo == "Compras"),
    }


def faturamento_do_mes(ano: int, mes: int, params: dict):
    """Faturamento do mês pela regra única (services/faturamento.py).

    Primeiro o registro diário (PDV), que é o número real; só quando não há
    nada lançado é que o caixa entra como estimativa — o caixa recebe o líquido
    (sem a taxa de cartão) e com atraso.
    """
    from app.services import faturamento as fonte

    def pelo_caixa():
        recebido = bases_do_mes(ano, mes)["entradas_operacionais"]
        taxa = Decimal(params["pct_taxa_cartao"])
        estimado = _q(recebido / (Decimal("1") - taxa)) if taxa < 1 else recebido
        return estimado, "estimado pelos recebimentos — lance o faturamento diário para o número real ⚠"

    return fonte.do_mes(ano, mes, estimativa=pelo_caixa)


def faturamento_estimado(ano: int, mes: int, params: dict) -> tuple[Decimal, str]:
    f = faturamento_do_mes(ano, mes, params)
    return f.valor, f.origem


# ------------------------------- calibração -------------------------------

def calibrar(ano: int, mes: int) -> dict | None:
    """Ajusta percentuais e fatores comparando o razão importado com os nossos
    números do mesmo mês. Não faz commit."""
    real = dc.dre_importado(ano, mes)
    if not real:
        return None
    bases = bases_do_mes(ano, mes)
    faturamento = real["receita_bruta"]
    linhas = real["linhas"]

    def conta(chave: str, prefixo: str) -> Decimal:
        return sum((c["valor"] for c in linhas[chave].contas
                    if c["classificacao"].startswith(prefixo)), ZERO)

    def pct(valor: Decimal) -> str:
        return str((valor / faturamento).quantize(Decimal("0.00001"))) if faturamento else "0"

    def fator(valor: Decimal, base: Decimal) -> str:
        return str((valor / base).quantize(Decimal("0.0001"))) if base else "1"

    novos = {
        "pct_gorjeta": pct(conta("deducoes", "4.1.20.100")),
        "pct_simples": pct(conta("deducoes", "4.1.20.300.08")),
        "pct_icms": pct(conta("deducoes", "4.1.20.300.02")),
        "pct_cmv": pct(linhas["cmv"].valor),
        "pct_taxa_cartao": pct(conta("financeiras", "3.2.20.500.11")),
        "fator_pessoal": fator(linhas["pessoal"].valor, bases["pessoal"] + PRO_LABORE_MENSAL),
        "fator_pessoal_operacional": fator(linhas["pessoal"].valor - PRO_LABORE_MENSAL, bases["pessoal"]),
        "fator_gerais": fator(linhas["gerais"].valor, bases["gerais"]),
        "fator_tributarias": fator(linhas["tributarias"].valor, bases["iptu"]),
        "juros_mes": str(conta("financeiras", "3.2.20.500.05")),
        "depreciacao_mes": str(linhas["depreciacao"].valor),
        "outras_receitas_mes": str(linhas["outras_receitas"].valor),
        "receitas_financeiras_mes": str(linhas["receitas_financeiras"].valor),
        "calibrado_com": f"{mes:02d}/{ano}",
    }
    salvar_parametros(novos)
    return novos


# ------------------------------- previsão -------------------------------

def prever(ano: int, mes: int) -> dict:
    params = parametros()
    bases = bases_do_mes(ano, mes)
    fonte_faturamento = faturamento_do_mes(ano, mes, params)
    faturamento, origem_faturamento = fonte_faturamento.valor, fonte_faturamento.origem
    p = lambda chave: Decimal(params[chave])  # noqa: E731

    gorjeta = _q(faturamento * p("pct_gorjeta"))
    simples = _q(faturamento * p("pct_simples"))
    icms = _q(faturamento * p("pct_icms"))
    taxa_cartao = _q(faturamento * p("pct_taxa_cartao"))
    juros = _q(p("juros_mes"))
    pessoal = _q((bases["pessoal"] + PRO_LABORE_MENSAL) * p("fator_pessoal"))
    gerais = _q(bases["gerais"] * p("fator_gerais"))
    tributarias = _q(bases["iptu"] * p("fator_tributarias"))

    linhas = {
        "receita_bruta": LinhaPrevista(
            "receita_bruta", "Receita bruta", faturamento, origem_faturamento,
            [("Faturamento do mês", faturamento)]),
        "deducoes": LinhaPrevista(
            "deducoes", "(–) Deduções", gorjeta + simples + icms, "% do faturamento (calibrado)",
            [("Gorjeta faturada e estornada", gorjeta), ("Simples Nacional", simples), ("ICMS", icms)]),
        "cmv": LinhaPrevista(
            "cmv", "(–) CMV e insumos", _q(faturamento * p("pct_cmv")),
            "% do faturamento — a contabilidade usa estoque, não o pagamento",
            [("CMV estimado", _q(faturamento * p("pct_cmv"))),
             ("(nosso caixa de compras no mês)", bases["cmv_caixa"])]),
        "pessoal": LinhaPrevista(
            "pessoal", "(–) Despesas com pessoal", pessoal,
            "folha paga × fator de calibração (inclui provisões) + pró-labore fixo",
            [("Folha paga no mês", bases["pessoal"]), ("Pró-labore fixo", PRO_LABORE_MENSAL),
             ("Ajuste de competência", _q(pessoal - bases["pessoal"] - PRO_LABORE_MENSAL))]),
        "gerais": LinhaPrevista(
            "gerais", "(–) Despesas gerais", gerais, "despesas pagas × fator de calibração",
            [("Despesas pagas no mês", bases["gerais"]),
             ("Ajuste de competência", _q(gerais - bases["gerais"]))]),
        "tributarias": LinhaPrevista(
            "tributarias", "(–) Despesas tributárias", tributarias, "IPTU pago × fator",
            [("IPTU pago", bases["iptu"])]),
        "financeiras": LinhaPrevista(
            "financeiras", "(–) Despesas financeiras", _q(taxa_cartao + juros),
            "taxa de cartão (% do faturamento) + juros do empréstimo",
            [("Taxa de administração de cartão", taxa_cartao), ("Juros", juros)]),
        "depreciacao": LinhaPrevista(
            "depreciacao", "(–) Depreciação e amortização", _q(p("depreciacao_mes")),
            "valor do último razão (não passa pelo caixa)",
            [("Depreciação/amortização", _q(p("depreciacao_mes")))]),
        "receitas_financeiras": LinhaPrevista(
            "receitas_financeiras", "(+) Receitas financeiras", _q(p("receitas_financeiras_mes")),
            "rendimento das aplicações (último razão)",
            [("Rendimento de aplicação", _q(p("receitas_financeiras_mes")))]),
        "outras_receitas": LinhaPrevista(
            "outras_receitas", "(+) Outras receitas", _q(p("outras_receitas_mes")),
            "bonificações (último razão)",
            [("Bonificações e brindes", _q(p("outras_receitas_mes")))]),
    }

    receita_liquida = linhas["receita_bruta"].valor - linhas["deducoes"].valor
    lucro_bruto = receita_liquida - linhas["cmv"].valor
    despesas = sum((linhas[c].valor for c in
                    ("pessoal", "gerais", "tributarias", "financeiras", "depreciacao")), ZERO)
    resultado_operacional = lucro_bruto - despesas
    return {
        "ano": ano, "mes": mes, "linhas": linhas,
        "faturamento_origem": origem_faturamento,
        "calibrado_com": params.get("calibrado_com") or "valores iniciais (julho/2026)",
        "receita_bruta": linhas["receita_bruta"].valor,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "total_despesas": despesas,
        "resultado_operacional": resultado_operacional,
        "resultado": (resultado_operacional + linhas["receitas_financeiras"].valor
                      + linhas["outras_receitas"].valor),
    }


def comparar(ano: int, mes: int) -> dict:
    """Previsão × contabilidade (quando o razão do mês já foi importado)."""
    previsto = prever(ano, mes)
    real = dc.dre_importado(ano, mes)
    linhas = []
    for chave, rotulo, _, _ in dc.ESTRUTURA:
        p = previsto["linhas"][chave]
        r = real["linhas"][chave].valor if real else None
        linhas.append({
            "chave": chave, "rotulo": rotulo, "previsto": p.valor, "real": r,
            "diferenca": (r - p.valor) if r is not None else None,
            "metodo": p.metodo, "detalhe": p.detalhe,
            "contas": real["linhas"][chave].contas if real else [],
        })
    totais = []
    for chave, rotulo in (("receita_liquida", "Receita líquida"), ("lucro_bruto", "Lucro bruto"),
                          ("resultado_operacional", "Resultado operacional"), ("resultado", "Lucro líquido")):
        totais.append({
            "chave": chave, "rotulo": rotulo, "previsto": previsto[chave],
            "real": real[chave] if real else None,
            "diferenca": (real[chave] - previsto[chave]) if real else None,
        })
    return {"ano": ano, "mes": mes, "previsto": previsto, "real": real,
            "linhas": linhas, "totais": totais}
