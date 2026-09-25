# -*- coding: utf-8 -*-
"""De onde sai o faturamento (receita bruta) de um mês.

Regra única, usada pelo DRE gerencial e pela prévia do fechamento contábil:

1. **Registro diário (PDV)** — o que a equipe lança dia a dia. É o número real;
   quando todos os dias abertos do mês estão lançados, o mês está completo.
2. **Histórico de faturamento** — total do mês vindo da planilha de metas
   (meses antigos, anteriores ao registro diário).
3. **Informado manualmente** — só vale enquanto não há registro diário nem
   histórico do mês.
4. **Estimativa pelos recebimentos do caixa** — último recurso, sempre marcado
   como estimativa.

Dia fechado (segunda, folga trocada) não precisa de lançamento: a contagem de
"dias que faltam" olha só os dias abertos.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal


from app.extensions import db
from app.models.metas import FaturamentoDiario, FaturamentoHistorico
from app.utils.datas import primeiro_dia_mes, ultimo_dia_mes

CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass
class Faturamento:
    valor: Decimal
    origem: str          # texto curto para a tela
    fonte: str           # diario | historico | informado | estimativa
    completo: bool       # dá para fechar o mês com este número?
    dias_abertos: int = 0
    dias_lancados: int = 0

    @property
    def dias_faltando(self) -> int:
        return max(0, self.dias_abertos - self.dias_lancados)


def _q(valor) -> Decimal:
    return Decimal(valor or 0).quantize(CENTAVOS)


def do_mes(ano: int, mes: int, estimativa=None) -> Faturamento:
    """Faturamento do mês pela melhor fonte disponível.

    `estimativa`: função sem argumentos que devolve (valor, texto) para o caso
    de não haver nenhum número real — cada tela passa a sua.
    """
    inicio, fim = primeiro_dia_mes(ano, mes), ultimo_dia_mes(ano, mes)
    registros = db.session.execute(
        db.select(FaturamentoDiario).filter(FaturamentoDiario.data >= inicio,
                                            FaturamentoDiario.data <= fim)
    ).scalars().all()

    por_data = {r.data: r for r in registros}
    dias_do_mes = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    # dia sem registro conta como aberto, menos segunda-feira (fechado padrão da casa)
    dias_abertos = sum(1 for d in dias_do_mes
                       if (por_data[d].aberto if d in por_data else d.weekday() != 0))
    lancados = [r for r in registros if r.aberto and r.faturamento is not None]
    total = sum((Decimal(r.faturamento) for r in registros if r.faturamento is not None), ZERO)

    if lancados:
        completo = len(lancados) >= dias_abertos
        if completo:
            origem = f"registro diário (PDV) · mês completo, {len(lancados)} dias abertos lançados"
        else:
            faltam = dias_abertos - len(lancados)
            origem = (f"registro diário (PDV) · parcial, {len(lancados)} de {dias_abertos} dias abertos "
                      f"({faltam} {'dia' if faltam == 1 else 'dias'} sem lançar) ⚠")
        return Faturamento(_q(total), origem, "diario", completo, dias_abertos, len(lancados))

    historico = db.session.execute(
        db.select(FaturamentoHistorico.valor).filter_by(ano=ano, mes=mes)
    ).scalar_one_or_none()
    if historico:
        return Faturamento(_q(historico), "histórico de faturamento (planilha de metas)", "historico", True)

    from app.services import dre_previsao

    informado = dre_previsao.faturamento_informado(ano, mes)
    if informado:
        return Faturamento(_q(informado), "faturamento informado manualmente", "informado", True)

    if estimativa is None:
        return Faturamento(ZERO, "sem faturamento lançado no mês ⚠", "estimativa", False)
    valor, texto = estimativa()
    return Faturamento(_q(valor), texto, "estimativa", False)
