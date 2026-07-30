# -*- coding: utf-8 -*-
"""Engine de rateio de gorjetas — funções puras, tudo em Decimal.

ENGINE DIÁRIA (a oficial): validada centavo a centavo contra a aba
"1ª Q Maio-26" da planilha real (19 colaboradores conferidos). A fórmula:

    líquido(colab) = Σ_{dias em que esteve presente}
        comissão_do_dia × (1 − encargos) × %setor × pontos_colab
        ÷ pontos_presentes_do_setor_no_dia
      − desconto_do_colaborador

A fórmula "simplificada" do período (pontos × dias ÷ peso total do setor)
é o caso particular em que todos trabalham nos mesmos dias — usada como
fallback quando a quinzena não tem comissão diária + grade de presença.

Setor sem ninguém presente (decisão do cliente, item 14.2): o pool é
REDISTRIBUÍDO proporcionalmente aos setores com presença.

Arredondamento: só na exibição/fechamento (quantize no resultado final);
os intermediários mantêm precisão total.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

CENTAVOS = Decimal("0.01")
TOLERANCIA = Decimal("0.05")  # tolerância de conferência (centavos de arredondamento)


@dataclass
class ColaboradorRateio:
    """Entrada da engine — desacoplada dos models (testável sem DB)."""

    id: int
    nome: str
    setor: str
    funcao: str
    registro: str
    pontos: Decimal
    presencas: set[date] = field(default_factory=set)  # dias presentes (engine diária)
    dias_trabalhados_manual: int | None = None  # fallback simplificado
    desconto: Decimal = Decimal("0")


@dataclass
class ResultadoColaborador:
    id: int
    nome: str
    setor: str
    funcao: str
    registro: str
    pontos: Decimal
    dias_trabalhados: int
    bruto_rateado: Decimal
    desconto_setor: Decimal   # proporcional ao bruto — vem de DescontoQuinzena
    desconto: Decimal         # desconto individual/manual (ParticipacaoPeriodo)
    liquido: Decimal


@dataclass
class ResultadoRateio:
    modo: str  # "diario" | "simplificado"
    total_liquido: Decimal
    colaboradores: list[ResultadoColaborador]
    pools: dict[str, Decimal]           # pool nominal por setor
    pago_por_setor: dict[str, Decimal]  # soma dos brutos rateados
    redistribuido: Decimal              # total realocado de setores vazios
    total_descontos: Decimal
    total_a_pagar: Decimal

    def status_setor(self, setor: str) -> str:
        diferenca = abs(self.pools.get(setor, Decimal("0")) - self.pago_por_setor.get(setor, Decimal("0")))
        return "OK" if diferenca <= TOLERANCIA else "DIVERGE"


def total_liquido_a_ratear(comissao_bruta: Decimal, percentual_encargos: Decimal) -> Decimal:
    return comissao_bruta * (Decimal("1") - percentual_encargos)


def _finalizar(
    modo: str,
    total_liquido: Decimal,
    brutos: dict[int, Decimal],
    dias: dict[int, int],
    colaboradores: list[ColaboradorRateio],
    pools: dict[str, Decimal],
    redistribuido: Decimal,
    descontos_setor: dict[str, Decimal] | None = None,
) -> ResultadoRateio:
    # Soma dos brutos por setor (antes de quantizar) para distribuição proporcional
    bruto_setor_raw: dict[str, Decimal] = {}
    for c in colaboradores:
        b = brutos.get(c.id, Decimal("0"))
        bruto_setor_raw[c.setor] = bruto_setor_raw.get(c.setor, Decimal("0")) + b

    resultados = []
    pago_por_setor: dict[str, Decimal] = {s: Decimal("0") for s in pools}
    total_descontos = Decimal("0")
    total_a_pagar = Decimal("0")

    for c in colaboradores:
        bruto = brutos.get(c.id, Decimal("0")).quantize(CENTAVOS)
        desc_individual = (c.desconto or Decimal("0")).quantize(CENTAVOS)

        desc_setor = Decimal("0")
        if descontos_setor:
            total_desc_s = descontos_setor.get(c.setor, Decimal("0"))
            if total_desc_s > 0:
                total_bruto_s = bruto_setor_raw.get(c.setor, Decimal("0"))
                if total_bruto_s > 0:
                    desc_setor = (brutos.get(c.id, Decimal("0")) / total_bruto_s * total_desc_s).quantize(CENTAVOS)

        liquido = bruto - desc_individual - desc_setor
        pago_por_setor[c.setor] = pago_por_setor.get(c.setor, Decimal("0")) + bruto
        total_descontos += desc_individual + desc_setor
        total_a_pagar += liquido

        resultados.append(ResultadoColaborador(
            id=c.id, nome=c.nome, setor=c.setor, funcao=c.funcao, registro=c.registro,
            pontos=c.pontos, dias_trabalhados=dias.get(c.id, 0),
            bruto_rateado=bruto,
            desconto_setor=desc_setor,
            desconto=desc_individual,
            liquido=liquido,
        ))

    return ResultadoRateio(
        modo=modo,
        total_liquido=total_liquido.quantize(CENTAVOS),
        colaboradores=resultados,
        pools={s: v.quantize(CENTAVOS) for s, v in pools.items()},
        pago_por_setor={s: v.quantize(CENTAVOS) for s, v in pago_por_setor.items()},
        redistribuido=redistribuido.quantize(CENTAVOS),
        total_descontos=total_descontos.quantize(CENTAVOS),
        total_a_pagar=total_a_pagar.quantize(CENTAVOS),
    )


def ratear_diario(
    comissao_por_dia: dict[date, Decimal],
    percentual_encargos: Decimal,
    percentuais_setor: dict[str, Decimal],
    colaboradores: list[ColaboradorRateio],
    redistribuir_setor_vazio: bool = True,
    descontos_setor: dict[str, Decimal] | None = None,
) -> ResultadoRateio:
    """Engine oficial: rateio dia a dia pelos pontos presentes no dia."""
    fator_liquido = Decimal("1") - percentual_encargos
    brutos: dict[int, Decimal] = {c.id: Decimal("0") for c in colaboradores}
    pools: dict[str, Decimal] = {s: Decimal("0") for s in percentuais_setor}
    redistribuido = Decimal("0")

    for dia, comissao in sorted(comissao_por_dia.items()):
        if not comissao:
            continue
        liquido_dia = Decimal(comissao) * fator_liquido

        # divisor: pontos presentes por setor neste dia
        divisor: dict[str, Decimal] = {s: Decimal("0") for s in percentuais_setor}
        for c in colaboradores:
            if dia in c.presencas:
                divisor[c.setor] += c.pontos

        setores_com_gente = {s for s, d in divisor.items() if d > 0}
        # cota nominal de cada setor no dia
        cotas = {s: liquido_dia * pct for s, pct in percentuais_setor.items()}

        if redistribuir_setor_vazio and setores_com_gente and setores_com_gente != set(percentuais_setor):
            # pool dos setores vazios é redistribuído proporcionalmente
            sobra = sum(cotas[s] for s in cotas if s not in setores_com_gente)
            base = sum(percentuais_setor[s] for s in setores_com_gente)
            for s in setores_com_gente:
                extra = sobra * percentuais_setor[s] / base
                cotas[s] += extra
                redistribuido += extra
            for s in cotas:
                if s not in setores_com_gente:
                    cotas[s] = Decimal("0")

        for s in percentuais_setor:
            pools[s] += cotas[s]
            if s not in setores_com_gente:
                continue
            for c in colaboradores:
                if c.setor == s and dia in c.presencas:
                    brutos[c.id] += cotas[s] * c.pontos / divisor[s]

    total_liquido = sum((Decimal(v) for v in comissao_por_dia.values()), Decimal("0")) * fator_liquido
    dias = {c.id: len(c.presencas) for c in colaboradores}
    return _finalizar("diario", total_liquido, brutos, dias, colaboradores, pools, redistribuido, descontos_setor)


def ratear_simplificado(
    comissao_bruta: Decimal,
    percentual_encargos: Decimal,
    percentuais_setor: dict[str, Decimal],
    colaboradores: list[ColaboradorRateio],
    redistribuir_setor_vazio: bool = True,
    descontos_setor: dict[str, Decimal] | None = None,
) -> ResultadoRateio:
    """Fallback sem grade diária: peso = pontos × dias trabalhados no período."""
    total_liquido = total_liquido_a_ratear(comissao_bruta, percentual_encargos)
    dias = {
        c.id: (c.dias_trabalhados_manual if c.dias_trabalhados_manual is not None else len(c.presencas))
        for c in colaboradores
    }

    pesos_setor: dict[str, Decimal] = {s: Decimal("0") for s in percentuais_setor}
    for c in colaboradores:
        pesos_setor[c.setor] += c.pontos * Decimal(dias[c.id])

    pools = {s: total_liquido * pct for s, pct in percentuais_setor.items()}
    redistribuido = Decimal("0")
    setores_com_gente = {s for s, p in pesos_setor.items() if p > 0}

    if redistribuir_setor_vazio and setores_com_gente and setores_com_gente != set(percentuais_setor):
        sobra = sum(pools[s] for s in pools if s not in setores_com_gente)
        base = sum(percentuais_setor[s] for s in setores_com_gente)
        for s in setores_com_gente:
            extra = sobra * percentuais_setor[s] / base
            pools[s] += extra
            redistribuido += extra
        for s in pools:
            if s not in setores_com_gente:
                pools[s] = Decimal("0")

    brutos: dict[int, Decimal] = {}
    for c in colaboradores:
        peso = c.pontos * Decimal(dias[c.id])
        brutos[c.id] = (
            pools[c.setor] * peso / pesos_setor[c.setor] if pesos_setor[c.setor] > 0 else Decimal("0")
        )

    return _finalizar("simplificado", total_liquido, brutos, dias, colaboradores, pools, redistribuido, descontos_setor)


def calcular_rateio(
    comissao_bruta: Decimal,
    percentual_encargos: Decimal,
    percentuais_setor: dict[str, Decimal],
    colaboradores: list[ColaboradorRateio],
    comissao_por_dia: dict[date, Decimal] | None = None,
    redistribuir_setor_vazio: bool = True,
    descontos_setor: dict[str, Decimal] | None = None,
) -> ResultadoRateio:
    """Escolhe a engine: diária quando há comissão por dia + presenças."""
    tem_comissao_diaria = bool(comissao_por_dia) and any(v for v in comissao_por_dia.values())
    tem_presenca = any(c.presencas for c in colaboradores)
    if tem_comissao_diaria and tem_presenca:
        return ratear_diario(
            comissao_por_dia, percentual_encargos, percentuais_setor,
            colaboradores, redistribuir_setor_vazio, descontos_setor,
        )
    return ratear_simplificado(
        comissao_bruta, percentual_encargos, percentuais_setor,
        colaboradores, redistribuir_setor_vazio, descontos_setor,
    )


def validar_percentuais_setor(percentuais_setor: dict[str, Decimal]) -> bool:
    """A soma dos percentuais dos setores ativos deve dar exatamente 1."""
    return sum(percentuais_setor.values()) == Decimal("1")
