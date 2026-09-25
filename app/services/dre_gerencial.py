# -*- coding: utf-8 -*-
"""DRE Gerencial do Bangalô — apurada com os dados do próprio sistema.

Espelha o modelo da planilha "DRE_Gerencial_Bangalo": reclassifica as despesas
por COMPORTAMENTO (o que a gestão decide), não por natureza contábil. As três
reclassificações que a consultoria já adotava continuam valendo:

  1. Taxa de cartão é custo operacional (não despesa financeira);
  2. IPTU entra em Ocupação, junto de aluguel e condomínio;
  3. Pró-labore sai de Pessoal Operacional (remuneração de sócio); o que passa
     do pró-labore fixo é adiantamento de lucros e NÃO entra na DRE.

Fonte dos números (decisão do cliente em 16/09/2026): o próprio sistema.
  - Receita Bruta: faturamento diário (PDV) > histórico de metas > valor
    informado > estimativa pelos recebimentos — sempre com a origem declarada.
  - Devoluções (gorjeta faturada e estornada): comissão bruta das quinzenas de
    gorjeta; se o mês não estiver fechado, estimativa por % do faturamento.
  - Demais linhas: lançamentos do fluxo (regime de CAIXA) reclassificados.
  - Depreciação: não passa pelo caixa — vem do último razão importado.

Como é regime de caixa, o resultado difere do contábil (competência). A tela de
DRE Contábil faz a ponte entre os dois.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.fluxo import Categoria, Lancamento
from app.models.gorjetas import PeriodoGorjeta
from app.models.metas import FaturamentoDiario, FaturamentoHistorico
from app.services import dre_contabil as dc
from app.services import dre_previsao as dp
from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

ZERO = Decimal("0.00")
CENTAVOS = Decimal("0.01")

# (chave, rótulo, papel) — papel: receita | deducao | custo | despesa | outros
LINHAS = [
    ("receita_bruta", "Receita Bruta de Vendas", "receita"),
    ("devolucoes", "(–) Devoluções e Cancelamentos [Gorjeta]", "deducao"),
    ("impostos", "(–) Impostos sobre Vendas (ICMS + Simples Nacional)", "deducao"),
    ("cmv", "(–) CMV (Custo de Mercadoria Vendida + Insumos)", "custo"),
    ("pessoal", "Pessoal Operacional", "despesa"),
    ("prolabore", "Pró-labore (Sócios)", "despesa"),
    ("ocupacao", "Ocupação (Aluguel + Condomínio + IPTU)", "despesa"),
    ("utilidades", "Utilidades (Energia/Água/Gás/Internet)", "despesa"),
    ("entretenimento", "Entretenimento (Música ao Vivo)", "despesa"),
    ("servicos_terceiros", "Serviços de Terceiros (PJ)", "despesa"),
    ("taxas_cartao", "Taxas de Cartão/Adquirência", "despesa"),
    ("administrativas", "Despesas Administrativas", "despesa"),
    ("cartao_credito", "Cartão de Crédito (a detalhar)", "despesa"),
    ("marketing", "Marketing e Publicidade", "despesa"),
    ("material_consumo", "Material de Uso e Consumo", "despesa"),
    ("manutencao", "Manutenção e Limpeza", "despesa"),
    ("locacao_equip", "Locação de Equipamentos", "despesa"),
    ("seguros", "Seguros", "despesa"),
    ("deslocamento", "Despesas com Deslocamento", "despesa"),
    ("medicas", "Despesas Médicas", "despesa"),
    ("depreciacao", "(–) Depreciação e Amortização", "outros"),
    ("financeiro", "(+/–) Resultado Financeiro Líquido", "outros"),
    ("outras_receitas", "(+) Outras Receitas Operacionais", "outros"),
]
ROTULOS = {chave: rotulo for chave, rotulo, _ in LINHAS}
DESPESAS_OPERACIONAIS = [chave for chave, _, papel in LINHAS if papel == "despesa"]

# Categoria do nosso plano de contas -> linha da DRE Gerencial
MAPA_CATEGORIA = {
    # entradas
    "IFOOD": "receita_bruta", "99 FOOD": "receita_bruta",
    "Patrocínio": "outras_receitas", "Outros/Acertos": "outras_receitas",
    # impostos
    "DAS": "impostos", "ICMS": "impostos",
    "Dívidas Receita (Simples/PERT)": "impostos",
    # pessoal
    "Pro Labore/Lucro": "prolabore",
    "Medicina Trabalho": "medicas",
    # ocupação
    "Aluguel": "ocupacao", "Condomínio": "ocupacao", "IPTU": "ocupacao",
    # utilidades
    "IGUA": "utilidades", "Light": "utilidades", "Gedisa (luz)": "utilidades",
    "CEG": "utilidades", "Net": "utilidades",
    # entretenimento e terceiros
    "Músicos": "entretenimento", "Técnico Som": "entretenimento",
    "Segurança": "servicos_terceiros", "Nutricionista": "servicos_terceiros",
    "Clauwan/C.Villela": "servicos_terceiros", "Força Ambiental": "manutencao",
    # administrativas
    "Contabilidade": "administrativas", "Jurídico": "administrativas",
    "NixConsultoria": "administrativas", "Assessoria Financeira": "administrativas",
    "Abrasel": "administrativas", "Control ID": "administrativas",
    "Virtual Market": "administrativas", "Emantec/iNova": "administrativas",
    "Ecad": "administrativas", "Falae/Experiência B2S": "administrativas",
    "Tuap": "administrativas", "Taxa Inspeção Sanitária": "administrativas",
    "Taxa Incêndio": "administrativas", "Outros DARJ/DARM/DIFAL": "administrativas",
    "Darf iFood": "administrativas", "GRDJ/FREST": "administrativas",
    "Multas": "administrativas", "Outros Acertos": "administrativas",
    "Reembolso Barbara": "administrativas",
    # cartão de crédito (fatura mista — a detalhar)
    "Cartão de Crédito 15 Itaú": "cartao_credito",
    "Cartão de Crédito 26 Itaú": "cartao_credito",
    "Cartão de Crédito 5 Itaú": "cartao_credito",
    # demais
    "Marketing/Fotografias/Gráfica": "marketing",
    "Manutenção/Obras/Equip.": "manutencao", "Aster (Dedetização)": "manutencao",
    "Fiel Limpeza Caixa d'Água": "manutencao", "Atto Service": "manutencao",
    "Seguro": "seguros", "Enincêndio": "seguros",
    "Despesas Bancárias": "financeiro",
}
# fallback por grupo do plano de contas
MAPA_GRUPO = {
    "Entradas": "receita_bruta",
    "Outros/Acertos": "outras_receitas",
    "Patrocínio": "outras_receitas",
    "Empréstimo": "outras_receitas",
    "Resgate": "outras_receitas",
    "RENDIMENTO": "outras_receitas",
    "Impostos": "impostos",
    "Folha/Salários": "pessoal",
    "Demais Salários": "pessoal",
    "Compras": "cmv",
    "Despesas Fixas": "administrativas",
    "Outras despesas": "administrativas",
}
# não entram na DRE (tesouraria e sócios/financiamento)
FORA_DA_DRE = {"Aplicação", "Resgate", "RENDIMENTO", "Empréstimo", "Empréstimos Heitor"}


@dataclass
class LinhaGerencial:
    chave: str
    rotulo: str
    valor: Decimal = ZERO
    origem: str = ""
    itens: list[tuple[str, Decimal]] = field(default_factory=list)  # composição (hover)

    @property
    def pct(self):  # preenchido na apuração (sobre a receita líquida)
        return getattr(self, "_pct", None)


def _q(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVOS)


def _linha_da_categoria(grupo: str, nome: str) -> str | None:
    if nome in FORA_DA_DRE:
        return None
    return MAPA_CATEGORIA.get(nome) or MAPA_GRUPO.get(grupo)


# ------------------------------- fontes de receita -------------------------------

def receita_bruta(ano: int, mes: int) -> tuple[Decimal, str]:
    """Faturamento do mês e de onde ele veio (regra em services/faturamento.py)."""
    from app.services import faturamento as fonte

    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)

    def pelo_caixa():
        recebido = db.session.execute(
            db.select(func.coalesce(func.sum(Lancamento.valor), 0))
            .join(Categoria, Lancamento.categoria_id == Categoria.id)
            .filter(Categoria.grupo == "Entradas", Lancamento.data >= inicio, Lancamento.data <= fim)
        ).scalar_one()
        return recebido, "estimado pelos recebimentos (sem faturamento do PDV) ⚠"

    f = fonte.do_mes(ano, mes, estimativa=pelo_caixa)
    return f.valor, f.origem


def gorjeta_do_mes(ano: int, mes: int, faturamento: Decimal) -> tuple[Decimal, str]:
    """Gorjeta faturada e estornada: comissão bruta das quinzenas do mês."""
    periodos = db.session.execute(
        db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes)
    ).scalars().all()
    fechados = [p for p in periodos if p.status == "fechado"]
    total = sum((_q(p.comissao_bruta) for p in fechados), ZERO)
    if fechados and len(fechados) == len(periodos) and total:
        return total, f"comissão das {len(fechados)} quinzenas fechadas"
    pct = Decimal(dp.parametros()["pct_gorjeta"])
    estimado = _q(faturamento * pct)
    if total:
        return total, f"só {len(fechados)} de {len(periodos)} quinzenas fechadas ⚠ (estimativa: {estimado})"
    return estimado, f"estimado em {pct * 100:.2f}% do faturamento (quinzenas não fechadas) ⚠"


# ------------------------------- apuração -------------------------------

def _pagamentos_do_grupo(ano: int, mes: int, grupo: str) -> Decimal:
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    return _q(db.session.execute(
        db.select(func.coalesce(func.sum(Lancamento.valor), 0))
        .join(Categoria, Lancamento.categoria_id == Categoria.id)
        .filter(Categoria.grupo == grupo, Lancamento.data >= inicio, Lancamento.data <= fim)
    ).scalar_one())


def _ajustes_de_competencia(ano: int, mes: int, linhas: dict, faturamento: Decimal) -> None:
    """Tira do caixa e coloca na competência as três linhas que mais distorcem:

    - Impostos: o DAS/ICMS pagos no mês seguinte são do faturamento deste mês;
    - CMV: a contabilidade mede consumo (estoque), não o pagamento ao fornecedor;
    - Pessoal: férias e 13º são provisionados ao longo do ano.
    """
    params = dp.parametros()
    prox_ano, prox_mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)

    pago_no_mes_seguinte = _pagamentos_do_grupo(prox_ano, prox_mes, "Impostos")
    caixa_impostos = linhas["impostos"].valor
    if pago_no_mes_seguinte:
        linhas["impostos"].valor = pago_no_mes_seguinte
        linhas["impostos"].origem = f"imposto do faturamento deste mês, pago em {prox_mes:02d}/{prox_ano}"
    else:
        estimado = _q(faturamento * (Decimal(params["pct_simples"]) + Decimal(params["pct_icms"])))
        linhas["impostos"].valor = estimado
        linhas["impostos"].origem = "estimado em % do faturamento (pagamento ainda não lançado)"
    linhas["impostos"].itens = [("Competência do mês", linhas["impostos"].valor),
                                ("(pago em caixa neste mês)", caixa_impostos)]

    caixa_cmv = linhas["cmv"].valor
    consumo = _q(faturamento * Decimal(params["pct_cmv"]))
    linhas["cmv"].valor = consumo
    linhas["cmv"].origem = "consumo estimado em % do faturamento (a contabilidade usa estoque)"
    linhas["cmv"].itens = [("Consumo do mês", consumo), ("(pago a fornecedores no mês)", caixa_cmv)]

    caixa_pessoal = linhas["pessoal"].valor
    competencia = _q(caixa_pessoal * Decimal(params["fator_pessoal_operacional"]))
    linhas["pessoal"].valor = competencia
    linhas["pessoal"].origem = "folha com provisões (férias/13º), calibrada pelo razão"
    linhas["pessoal"].itens = [("Folha de competência", competencia),
                               ("(pago em caixa no mês)", caixa_pessoal)]


def apurar(ano: int, mes: int, regime: str = "competencia") -> dict:
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    linhas = {chave: LinhaGerencial(chave, rotulo) for chave, rotulo, _ in LINHAS}

    faturamento, origem_fat = receita_bruta(ano, mes)
    linhas["receita_bruta"].valor = faturamento
    linhas["receita_bruta"].origem = origem_fat

    gorjeta, origem_gorjeta = gorjeta_do_mes(ano, mes, faturamento)
    linhas["devolucoes"].valor = gorjeta
    linhas["devolucoes"].origem = origem_gorjeta

    # lançamentos do fluxo (caixa) reclassificados
    movimentos = db.session.execute(
        db.select(Categoria.grupo, Categoria.nome, Categoria.tipo, func.sum(Lancamento.valor))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .filter(Lancamento.data >= inicio, Lancamento.data <= fim)
        .group_by(Categoria.grupo, Categoria.nome, Categoria.tipo)
    ).all()

    fora = []
    prolabore_pago = ZERO
    for grupo, nome, tipo, total in movimentos:
        valor = _q(total)
        chave = _linha_da_categoria(grupo, nome)
        if chave is None:
            fora.append((f"{grupo} › {nome}", valor))
            continue
        if chave == "receita_bruta" and tipo == "entrada":
            continue  # a receita vem do faturamento, não dos recebimentos
        if chave == "prolabore":
            prolabore_pago += valor
            continue
        linhas[chave].valor += valor
        linhas[chave].itens.append((nome, valor))

    # pró-labore fixo; o excedente é adiantamento de lucros (fora da DRE)
    if prolabore_pago:
        fixo = min(prolabore_pago, dp.PRO_LABORE_MENSAL)
        linhas["prolabore"].valor = fixo
        linhas["prolabore"].origem = "pró-labore fixo do contrato"
        linhas["prolabore"].itens.append(("Pró-labore fixo", fixo))
        excedente = prolabore_pago - fixo
        if excedente:
            fora.append(("Adiantamento de lucros (sócios)", excedente))

    # taxa de cartão: já vem descontada do repasse, então é estimada pelo %
    pct_taxa = Decimal(dp.parametros()["pct_taxa_cartao"])
    taxa = _q(faturamento * pct_taxa)
    linhas["taxas_cartao"].valor = taxa
    linhas["taxas_cartao"].origem = f"estimada em {pct_taxa * 100:.2f}% do faturamento (vem descontada no repasse)"
    linhas["taxas_cartao"].itens.append(("Taxa de adquirência", taxa))

    # depreciação não passa pelo caixa: vem do razão
    razao = dc.dre_importado(ano, mes)
    if razao:
        linhas["depreciacao"].valor = razao["linhas"]["depreciacao"].valor
        linhas["depreciacao"].origem = "razão da contabilidade do mês"
    else:
        linhas["depreciacao"].valor = _q(dp.parametros()["depreciacao_mes"])
        linhas["depreciacao"].origem = "último razão importado (não passa pelo caixa)"

    for chave in ("pessoal", "cmv", "impostos", "ocupacao", "utilidades", "entretenimento",
                  "servicos_terceiros", "administrativas", "cartao_credito", "marketing",
                  "material_consumo", "manutencao", "locacao_equip", "seguros",
                  "deslocamento", "medicas", "financeiro", "outras_receitas"):
        if linhas[chave].valor and not linhas[chave].origem:
            linhas[chave].origem = "lançamentos do fluxo (caixa)"
        linhas[chave].itens.sort(key=lambda i: -i[1])

    if regime == "competencia":
        _ajustes_de_competencia(ano, mes, linhas, faturamento)

    receita_liquida = faturamento - linhas["devolucoes"].valor - linhas["impostos"].valor
    lucro_bruto = receita_liquida - linhas["cmv"].valor
    total_despesas = sum((linhas[c].valor for c in DESPESAS_OPERACIONAIS), ZERO)
    ebitda = lucro_bruto - total_despesas
    ebit = ebitda - linhas["depreciacao"].valor
    resultado = ebit - linhas["financeiro"].valor + linhas["outras_receitas"].valor

    base = receita_liquida or None
    for linha in linhas.values():
        linha._pct = float(linha.valor / base) if base else None  # noqa: SLF001

    return {
        "ano": ano, "mes": mes, "regime": regime,
        "linhas": linhas, "fora_da_dre": sorted(fora, key=lambda i: -i[1]),
        "receita_bruta": faturamento,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "total_despesas": total_despesas,
        "ebitda": ebitda,
        "ebit": ebit,
        "resultado": resultado,
        "margem_bruta": float(lucro_bruto / base) if base else None,
        "margem_ebitda": float(ebitda / base) if base else None,
        "margem_liquida": float(resultado / base) if base else None,
        "prime_cost": linhas["cmv"].valor + linhas["pessoal"].valor + linhas["prolabore"].valor,
    }


def serie(ano: int, ate_mes: int = 12, regime: str = "competencia") -> list[dict]:
    """Apura os meses do ano (para a tabela mês a mês e as médias)."""
    return [apurar(ano, mes, regime) for mes in range(1, ate_mes + 1)]


MATERIALIDADE_VALOR = Decimal("1000")
MATERIALIDADE_PCT = 0.10


def analise(ano: int, mes: int, regime: str = "competencia", janela: int = 3) -> dict:
    """Mês escolhido × mês anterior × média dos meses anteriores, com as linhas
    que estouraram a materialidade (R$ 1.000 e 10%) sinalizadas."""
    meses = serie(ano, mes, regime)
    atual = meses[-1]
    anterior = meses[-2] if len(meses) > 1 else None
    linhas = []
    for chave, rotulo, papel in LINHAS:
        valor = atual["linhas"][chave].valor
        valor_ant = anterior["linhas"][chave].valor if anterior else None
        media = media_movel(meses, chave, mes, janela)
        variacao = (valor - valor_ant) if valor_ant is not None else None
        var_pct = float(variacao / valor_ant) if valor_ant else None
        desvio = (valor - media) if media is not None else None
        desvio_pct = float(desvio / media) if media else None
        sinalizar = bool(
            desvio is not None and abs(desvio) >= MATERIALIDADE_VALOR
            and desvio_pct is not None and abs(desvio_pct) >= MATERIALIDADE_PCT
        )
        linhas.append({
            "chave": chave, "rotulo": rotulo, "papel": papel,
            "linha": atual["linhas"][chave], "valor": valor, "pct": atual["linhas"][chave].pct,
            "anterior": valor_ant, "variacao": variacao, "var_pct": var_pct,
            "media": media, "desvio": desvio, "desvio_pct": desvio_pct, "sinalizar": sinalizar,
        })
    totais = []
    for chave, rotulo in (("receita_liquida", "RECEITA LÍQUIDA"), ("lucro_bruto", "LUCRO BRUTO"),
                          ("total_despesas", "TOTAL DESPESAS OPERACIONAIS"), ("ebitda", "EBITDA"),
                          ("ebit", "EBIT (Resultado Operacional)"), ("resultado", "LUCRO LÍQUIDO GERENCIAL")):
        totais.append({
            "chave": chave, "rotulo": rotulo, "valor": atual[chave],
            "anterior": anterior[chave] if anterior else None,
            "media": media_movel(meses, chave, mes, janela),
            "pct": float(atual[chave] / atual["receita_liquida"]) if atual["receita_liquida"] else None,
        })
    return {"atual": atual, "anterior": anterior, "meses": meses, "linhas": linhas, "totais": totais,
            "janela": janela, "materialidade": MATERIALIDADE_VALOR}


def media_movel(meses: list[dict], chave: str, ate: int, janela: int = 3) -> Decimal | None:
    """Média dos `janela` meses anteriores a `ate` (1-based) para a linha/total."""
    anteriores = [m for m in meses[max(0, ate - 1 - janela):ate - 1] if m["receita_bruta"]]
    if not anteriores:
        return None
    valores = [(m["linhas"][chave].valor if chave in m["linhas"] else m[chave]) for m in anteriores]
    return _q(sum(valores, ZERO) / len(valores))
