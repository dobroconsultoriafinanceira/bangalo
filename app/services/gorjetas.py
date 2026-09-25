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
    em_ferias: bool = False  # de férias: o setor iguala o apurado dele ao dos colegas


@dataclass
class ExtraRateio:
    """Pessoa de fora contratada por diária num turno.

    O restaurante paga a diária; o setor devolve apenas o quanto o extra teria
    recebido pelo rateio daquele turno (decisão da cliente em 16/09/2026).
    """

    data: date
    setor: str
    pontos: Decimal
    comissao_turno: Decimal
    pago_pelo_restaurante: Decimal = Decimal("0")
    turno: str = ""


@dataclass
class ResultadoExtra:
    data: date
    setor: str
    turno: str
    pontos: Decimal
    comissao_turno: Decimal
    devido_pelo_rateio: Decimal
    pago_pelo_restaurante: Decimal
    reposto_pelo_setor: Decimal
    parte_restaurante: Decimal
    excedente_do_setor: Decimal


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
    desconto_extra: Decimal = Decimal("0")     # reposição da diária do extra
    reembolso_ferias: Decimal = Decimal("0")   # acerto de férias: + recebe / − paga
    em_ferias: bool = False
    credito_saldo: Decimal = Decimal("0")      # centavos devolvidos do saldo do setor


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
    extras: list[ResultadoExtra] = field(default_factory=list)
    total_extras_reposto: Decimal = Decimal("0")     # volta ao restaurante
    total_extras_restaurante: Decimal = Decimal("0")  # custo que fica com o restaurante
    saldo_setor: dict = field(default_factory=dict)   # centavos guardados/devolvidos por setor

    def status_setor(self, setor: str) -> str:
        diferenca = abs(self.pools.get(setor, Decimal("0")) - self.pago_por_setor.get(setor, Decimal("0")))
        return "OK" if diferenca <= TOLERANCIA else "DIVERGE"


def total_liquido_a_ratear(comissao_bruta: Decimal, percentual_encargos: Decimal) -> Decimal:
    return comissao_bruta * (Decimal("1") - percentual_encargos)


def _reembolso_de_ferias(
    colaboradores: list[ColaboradorRateio], bases: dict[int, Decimal]
) -> dict[int, Decimal]:
    """Quem sai de férias recebe como se tivesse trabalhado a quinzena inteira.

    Política interna do Bangalô: TODO o setor termina a quinzena com o mesmo
    valor. O alvo é o líquido do setor dividido pelo número de pessoas do setor
    (contando quem está de férias), e cada um acerta a diferença entre o próprio
    apurado e esse alvo — quem apurou mais paga, quem saiu de férias recebe.
    Fecha em zero por construção.

    A base é o apurado JÁ com descontos e extras (coluna "Líquido apurado" da
    planilha), não o bruto rateado. Confere com as abas "2ª Q Agosto-26" (dois
    colaboradores de férias, um deles tendo trabalhado parte da quinzena) e
    "1ª Q Setembro-26".
    """
    ajustes: dict[int, Decimal] = {}
    setores = {c.setor for c in colaboradores if c.em_ferias}
    for setor in setores:
        time = [c for c in colaboradores if c.setor == setor]
        if not time:
            continue
        total = sum((bases.get(c.id, Decimal("0")) for c in time), Decimal("0"))
        # o alvo já vai arredondado: assim todo mundo do setor termina com
        # exatamente o mesmo valor, sem sobrar meio centavo em ninguém
        alvo = (total / Decimal(len(time))).quantize(CENTAVOS)
        for c in time:
            ajustes[c.id] = alvo - bases.get(c.id, Decimal("0"))
    return ajustes


def _finalizar(
    modo: str,
    total_liquido: Decimal,
    brutos: dict[int, Decimal],
    dias: dict[int, int],
    colaboradores: list[ColaboradorRateio],
    pools: dict[str, Decimal],
    redistribuido: Decimal,
    descontos_setor: dict[str, Decimal] | None = None,
    descontos_extra: dict[int, Decimal] | None = None,
    extras: list[ResultadoExtra] | None = None,
) -> ResultadoRateio:
    # Soma dos brutos por setor (antes de quantizar) para distribuição proporcional
    bruto_setor_raw: dict[str, Decimal] = {}
    for c in colaboradores:
        b = brutos.get(c.id, Decimal("0"))
        bruto_setor_raw[c.setor] = bruto_setor_raw.get(c.setor, Decimal("0")) + b

    # 1ª passada: o apurado de cada um já com descontos — é essa a base do
    # reembolso de férias (a planilha rateia sobre o líquido, não sobre o bruto)
    apurado: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    bases: dict[int, Decimal] = {}
    for c in colaboradores:
        bruto = brutos.get(c.id, Decimal("0")).quantize(CENTAVOS)
        desc_individual = (c.desconto or Decimal("0")).quantize(CENTAVOS)
        desc_extra = (descontos_extra or {}).get(c.id, Decimal("0")).quantize(CENTAVOS)

        desc_setor = Decimal("0")
        if descontos_setor:
            total_desc_s = descontos_setor.get(c.setor, Decimal("0"))
            if total_desc_s > 0:
                total_bruto_s = bruto_setor_raw.get(c.setor, Decimal("0"))
                if total_bruto_s > 0:
                    desc_setor = (brutos.get(c.id, Decimal("0")) / total_bruto_s * total_desc_s).quantize(CENTAVOS)

        apurado[c.id] = (bruto, desc_individual, desc_setor, desc_extra)
        bases[c.id] = bruto - desc_individual - desc_setor - desc_extra

    reembolsos = _reembolso_de_ferias(colaboradores, bases)

    resultados = []
    pago_por_setor: dict[str, Decimal] = {s: Decimal("0") for s in pools}
    total_descontos = Decimal("0")
    total_a_pagar = Decimal("0")

    for c in colaboradores:
        bruto, desc_individual, desc_setor, desc_extra = apurado[c.id]
        reembolso = reembolsos.get(c.id, Decimal("0")).quantize(CENTAVOS)

        liquido = bases[c.id] + reembolso
        pago_por_setor[c.setor] = pago_por_setor.get(c.setor, Decimal("0")) + bruto
        total_descontos += desc_individual + desc_setor + desc_extra
        total_a_pagar += liquido

        resultados.append(ResultadoColaborador(
            id=c.id, nome=c.nome, setor=c.setor, funcao=c.funcao, registro=c.registro,
            pontos=c.pontos, dias_trabalhados=dias.get(c.id, 0),
            bruto_rateado=bruto,
            desconto_setor=desc_setor,
            desconto=desc_individual,
            liquido=liquido,
            desconto_extra=desc_extra,
            reembolso_ferias=reembolso,
            em_ferias=c.em_ferias,
        ))

    extras = extras or []
    return ResultadoRateio(
        modo=modo,
        total_liquido=total_liquido.quantize(CENTAVOS),
        colaboradores=resultados,
        pools={s: v.quantize(CENTAVOS) for s, v in pools.items()},
        pago_por_setor={s: v.quantize(CENTAVOS) for s, v in pago_por_setor.items()},
        redistribuido=redistribuido.quantize(CENTAVOS),
        total_descontos=total_descontos.quantize(CENTAVOS),
        total_a_pagar=total_a_pagar.quantize(CENTAVOS),
        extras=extras,
        total_extras_reposto=sum((e.reposto_pelo_setor for e in extras), Decimal("0")).quantize(CENTAVOS),
        total_extras_restaurante=sum((e.parte_restaurante for e in extras), Decimal("0")).quantize(CENTAVOS),
    )


def _calcular_extras(
    extras: list[ExtraRateio],
    fator_liquido: Decimal,
    percentuais_setor: dict[str, Decimal],
    colaboradores: list[ColaboradorRateio],
) -> tuple[list[ResultadoExtra], dict[int, Decimal]]:
    """Diária de extra: o setor repõe só o que o extra renderia no rateio.

    O divisor conta os pontos presentes no dia MAIS os de quem está de férias e
    ausente: como o time vai repor o valor de quem saiu de férias, esses pontos
    continuam sendo do setor. Sem isso o extra fica com uma fatia maior e o time
    devolve ao restaurante mais do que deveria.

    O valor reposto é dividido entre quem estava presente naquele dia, por
    ponto (a planilha arredonda o valor por ponto — seguimos igual).
    """
    resultados: list[ResultadoExtra] = []
    descontos: dict[int, Decimal] = {}
    for extra in extras:
        presentes = [c for c in colaboradores if c.setor == extra.setor and extra.data in c.presencas]
        pontos_presentes = sum((c.pontos for c in presentes), Decimal("0"))
        pontos_ferias = sum(
            (c.pontos for c in colaboradores
             if c.setor == extra.setor and c.em_ferias and extra.data not in c.presencas),
            Decimal("0"),
        )
        divisor = pontos_presentes + pontos_ferias + extra.pontos
        devido = (
            Decimal(extra.comissao_turno) * fator_liquido
            * percentuais_setor.get(extra.setor, Decimal("0")) * extra.pontos / divisor
        ) if divisor else Decimal("0")
        pago = Decimal(extra.pago_pelo_restaurante or 0)
        reposto = min(devido, pago) if pago else devido
        excedente = max(devido - pago, Decimal("0")) if pago else Decimal("0")

        if pontos_presentes > 0 and reposto:
            por_ponto = (reposto / pontos_presentes).quantize(CENTAVOS)
            for c in presentes:
                descontos[c.id] = descontos.get(c.id, Decimal("0")) + por_ponto * c.pontos

        resultados.append(ResultadoExtra(
            data=extra.data, setor=extra.setor, turno=extra.turno, pontos=extra.pontos,
            comissao_turno=Decimal(extra.comissao_turno).quantize(CENTAVOS),
            devido_pelo_rateio=devido.quantize(CENTAVOS),
            pago_pelo_restaurante=pago.quantize(CENTAVOS),
            reposto_pelo_setor=reposto.quantize(CENTAVOS),
            parte_restaurante=(pago - reposto).quantize(CENTAVOS),
            excedente_do_setor=excedente.quantize(CENTAVOS),
        ))
    return resultados, descontos


def ratear_diario(
    comissao_por_dia: dict[date, Decimal],
    percentual_encargos: Decimal,
    percentuais_setor: dict[str, Decimal],
    colaboradores: list[ColaboradorRateio],
    redistribuir_setor_vazio: bool = True,
    descontos_setor: dict[str, Decimal] | None = None,
    extras: list[ExtraRateio] | None = None,
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
    resultados_extras, descontos_extra = _calcular_extras(
        extras or [], fator_liquido, percentuais_setor, colaboradores
    )
    return _finalizar("diario", total_liquido, brutos, dias, colaboradores, pools, redistribuido,
                      descontos_setor, descontos_extra, resultados_extras)


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
    extras: list[ExtraRateio] | None = None,
) -> ResultadoRateio:
    """Escolhe a engine: diária quando há comissão por dia + presenças."""
    tem_comissao_diaria = bool(comissao_por_dia) and any(v for v in comissao_por_dia.values())
    tem_presenca = any(c.presencas for c in colaboradores)
    if tem_comissao_diaria and tem_presenca:
        return ratear_diario(
            comissao_por_dia, percentual_encargos, percentuais_setor,
            colaboradores, redistribuir_setor_vazio, descontos_setor, extras,
        )
    return ratear_simplificado(
        comissao_bruta, percentual_encargos, percentuais_setor,
        colaboradores, redistribuir_setor_vazio, descontos_setor,
    )


def validar_percentuais_setor(percentuais_setor: dict[str, Decimal]) -> bool:
    """A soma dos percentuais dos setores ativos deve dar exatamente 1."""
    return sum(percentuais_setor.values()) == Decimal("1")
