# -*- coding: utf-8 -*-
"""DRE contábil: leitura do razão da contabilidade e montagem do DRE por conta.

A contabilidade (Marsal) envia todo mês, entre os dias 15 e 20, o DRE do mês
anterior em PDF e o **razão** em .xls — este último com TODOS os lançamentos e
a conta contábil de cada um, inclusive a conta "BANCO ITAÚ", que espelha o
extrato que o sistema já importa.

Este módulo lê o razão e monta o DRE a partir das contas de resultado, sem
depender do PDF. Serve para (1) conferir o que a contabilidade entregou e
(2) calibrar a previsão feita com os dados do próprio sistema
(services/dre_previsao.py).

Estrutura do plano de contas da Marsal (prefixos da classificação):
    4.1.10  receita bruta          3.1.10  custos (CMV e insumos)
    4.1.20  deduções               3.2.20.100  despesas com pessoal
    4.1.30  receitas financeiras   3.2.20.300  despesas tributárias
    4.1.50  outras receitas        3.2.20.400  despesas gerais
                                   3.2.20.500  despesas financeiras
                                   3.2.21      depreciação/amortização
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

CENTAVOS = Decimal("0.01")
ZERO = Decimal("0.00")
EPOCA_EXCEL = date(1899, 12, 30)

# (chave, rótulo, prefixos da classificação, sinal: +receita / -despesa)
ESTRUTURA = [
    ("receita_bruta", "Receita bruta", ("4.1.10",), "receita"),
    ("deducoes", "(–) Deduções (gorjeta faturada, ICMS, Simples)", ("4.1.20",), "despesa"),
    ("cmv", "(–) CMV e insumos", ("3.1.10",), "despesa"),
    ("pessoal", "(–) Despesas com pessoal", ("3.2.20.100",), "despesa"),
    ("gerais", "(–) Despesas gerais", ("3.2.20.400",), "despesa"),
    ("tributarias", "(–) Despesas tributárias", ("3.2.20.300",), "despesa"),
    ("financeiras", "(–) Despesas financeiras", ("3.2.20.500",), "despesa"),
    ("depreciacao", "(–) Depreciação e amortização", ("3.2.21",), "despesa"),
    ("receitas_financeiras", "(+) Receitas financeiras", ("4.1.30",), "receita"),
    ("outras_receitas", "(+) Outras receitas", ("4.1.50",), "receita"),
]
CHAVES = [chave for chave, _, _, _ in ESTRUTURA]


@dataclass
class LancamentoRazao:
    conta: str            # código interno da contabilidade (ex.: "712")
    classificacao: str    # ex.: "3.1.10.100.01"
    nome: str             # ex.: "CUSTO DAS MERCADORIAS VENDIDAS"
    data: date | None
    historico: str
    contrapartida: str
    debito: Decimal = ZERO
    credito: Decimal = ZERO

    @property
    def saldo(self) -> Decimal:
        """Débito − crédito (despesa positiva, receita negativa)."""
        return self.debito - self.credito


@dataclass
class LinhaDre:
    chave: str
    rotulo: str
    valor: Decimal = ZERO
    contas: list[dict] = field(default_factory=list)  # detalhe para o hover


def _dec(valor) -> Decimal:
    if isinstance(valor, str):
        valor = valor.strip().replace(".", "").replace(",", ".") if valor else 0
    try:
        return Decimal(str(valor or 0)).quantize(CENTAVOS)
    except Exception:  # noqa: BLE001
        return ZERO


class RazaoIlegivel(ValueError):
    """Arquivo do razão corrompido/incompleto — mensagem pronta para a tela."""


def _data_excel(valor) -> date | None:
    from datetime import datetime

    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    try:
        dias = int(float(valor))
    except (TypeError, ValueError):
        return None
    return EPOCA_EXCEL + timedelta(days=dias) if 30000 < dias < 60000 else None


def _celulas_xls(caminho: Path) -> list[list]:
    import xlrd

    try:
        ws = xlrd.open_workbook(caminho).sheet_by_index(0)
    except Exception as exc:  # noqa: BLE001 — arquivo truncado/corrompido no envio
        raise RazaoIlegivel(
            "não consegui abrir o arquivo .xls (ele chegou incompleto ou corrompido). "
            "Peça o arquivo de novo à contabilidade ou abra no Excel e salve como .xlsx — "
            f"o sistema também importa .xlsx. Detalhe técnico: {exc}"
        ) from exc
    return [[ws.cell_value(l, c) for c in range(ws.ncols)] for l in range(ws.nrows)]


def _celulas_xlsx(caminho: Path) -> list[list]:
    import openpyxl

    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    try:
        return [list(linha) for linha in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def ler_razao(caminho: str | Path) -> list[LancamentoRazao]:
    """Lê o razão da contabilidade (.xls, o formato que a Marsal envia, ou .xlsx).

    Só entram linhas com data: as sem data são "saldo anterior" e os subtotais
    que o arquivo repete ao fim de cada conta (senão os valores triplicam).
    """
    caminho = Path(caminho)
    linhas_planilha = (_celulas_xlsx(caminho) if caminho.suffix.lower() in (".xlsx", ".xlsm")
                       else _celulas_xls(caminho))

    def celula(linha: list, col: int):
        return linha[col] if col < len(linha) and linha[col] is not None else ""

    def texto(linha: list, col: int) -> str:
        return str(celula(linha, col)).strip()

    lancamentos: list[LancamentoRazao] = []
    conta = classificacao = nome = ""
    for linha in linhas_planilha:
        if texto(linha, 0) == "Conta:":
            conta = texto(linha, 1).removesuffix(".0")
            classificacao = texto(linha, 2)
            nome = texto(linha, 5)
            continue
        if not conta:
            continue
        dia = _data_excel(celula(linha, 0))
        if dia is None:
            continue
        debito, credito = _dec(celula(linha, 8)), _dec(celula(linha, 9))
        if not debito and not credito:
            continue
        lancamentos.append(LancamentoRazao(
            conta=conta, classificacao=classificacao, nome=nome, data=dia,
            historico=texto(linha, 2), contrapartida=texto(linha, 7).removesuffix(".0"),
            debito=debito, credito=credito,
        ))
    return lancamentos


def periodo(lancamentos: list[LancamentoRazao]) -> tuple[date | None, date | None]:
    datas = [l.data for l in lancamentos if l.data]
    return (min(datas), max(datas)) if datas else (None, None)


def _grupo_da(classificacao: str) -> str | None:
    for chave, _, prefixos, _ in ESTRUTURA:
        if any(classificacao.startswith(p) for p in prefixos):
            return chave
    return None


def dre_do_razao(lancamentos: list[LancamentoRazao]) -> dict:
    """Monta o DRE do período a partir das contas de resultado do razão."""
    por_chave = {chave: LinhaDre(chave, rotulo) for chave, rotulo, _, _ in ESTRUTURA}
    sinais = {chave: sinal for chave, _, _, sinal in ESTRUTURA}
    contas: dict[tuple[str, str], dict] = {}

    for lanc in lancamentos:
        chave = _grupo_da(lanc.classificacao)
        if chave is None:
            continue  # conta patrimonial (banco, fornecedores, estoque...)
        valor = lanc.saldo if sinais[chave] == "despesa" else -lanc.saldo
        registro = contas.setdefault((chave, lanc.conta), {
            "conta": lanc.conta, "classificacao": lanc.classificacao,
            "nome": lanc.nome, "valor": ZERO, "lancamentos": 0,
        })
        registro["valor"] += valor
        registro["lancamentos"] += 1
        por_chave[chave].valor += valor

    for (chave, _), registro in contas.items():
        por_chave[chave].contas.append(registro)
    for linha in por_chave.values():
        linha.contas.sort(key=lambda c: -abs(c["valor"]))

    receita_bruta = por_chave["receita_bruta"].valor
    receita_liquida = receita_bruta - por_chave["deducoes"].valor
    lucro_bruto = receita_liquida - por_chave["cmv"].valor
    despesas = sum((por_chave[c].valor for c in
                    ("pessoal", "gerais", "tributarias", "financeiras", "depreciacao")), ZERO)
    resultado_operacional = lucro_bruto - despesas
    resultado = (resultado_operacional + por_chave["receitas_financeiras"].valor
                 + por_chave["outras_receitas"].valor)
    inicio, fim = periodo(lancamentos)
    return {
        "inicio": inicio,
        "fim": fim,
        "linhas": por_chave,
        "receita_bruta": receita_bruta,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "total_despesas": despesas,
        "resultado_operacional": resultado_operacional,
        "resultado": resultado,
    }


def lancamentos_do_banco(lancamentos: list[LancamentoRazao], nome_conta: str = "BANCO ITA") -> list[LancamentoRazao]:
    """Lançamentos da conta do banco no razão — base para conferir contra o extrato."""
    return [l for l in lancamentos if l.nome.upper().startswith(nome_conta.upper())]


# ----------------------------- persistência -----------------------------

def importar_razao(caminho: str | Path) -> dict:
    """Lê o razão e grava as contas de resultado do mês (substitui o mês).

    Não faz commit — quem chama decide.
    """
    from app.extensions import db
    from app.models.banco import DreContabilConta

    lancamentos = ler_razao(caminho)
    inicio, fim = periodo(lancamentos)
    if inicio is None:
        raise ValueError("Razão sem lançamentos com data — arquivo inesperado.")
    if (inicio.year, inicio.month) != (fim.year, fim.month):
        raise ValueError(f"O razão cobre mais de um mês ({inicio} a {fim}); envie um arquivo por mês.")

    dre = dre_do_razao(lancamentos)
    ano, mes = inicio.year, inicio.month
    from app.models.documentos import RazaoLancamento

    db.session.query(DreContabilConta).filter_by(ano=ano, mes=mes).delete(synchronize_session=False)
    db.session.query(RazaoLancamento).filter_by(ano=ano, mes=mes).delete(synchronize_session=False)
    db.session.flush()
    # lançamentos um a um: permitem rastrear a conta até o histórico
    db.session.add_all([
        RazaoLancamento(ano=ano, mes=mes, conta=l.conta, classificacao=l.classificacao, nome=l.nome[:120],
                        data=l.data, historico=(l.historico or "")[:300] or None,
                        contrapartida=(l.contrapartida or "")[:20] or None, debito=l.debito, credito=l.credito)
        for l in lancamentos
    ])
    contas = 0
    for chave, linha in dre["linhas"].items():
        for registro in linha.contas:
            db.session.add(DreContabilConta(
                ano=ano, mes=mes, linha=chave, conta=registro["conta"],
                classificacao=registro["classificacao"], nome=registro["nome"],
                valor=registro["valor"], lancamentos=registro["lancamentos"],
            ))
            contas += 1
    db.session.flush()
    return {
        "ano": ano, "mes": mes, "lancamentos": len(lancamentos), "contas": contas,
        "receita_bruta": dre["receita_bruta"], "resultado": dre["resultado"],
        "banco": len(lancamentos_do_banco(lancamentos)),
    }


def previa_razao(caminho: str | Path) -> dict:
    """Lê o razão SEM gravar: competência, totais e o que seria substituído."""
    lancamentos = ler_razao(caminho)
    inicio, fim = periodo(lancamentos)
    if inicio is None:
        raise ValueError("Razão sem lançamentos com data — arquivo inesperado.")
    if (inicio.year, inicio.month) != (fim.year, fim.month):
        raise ValueError(f"O razão cobre mais de um mês ({inicio:%d/%m/%Y} a {fim:%d/%m/%Y}); envie um arquivo por mês.")
    dre = dre_do_razao(lancamentos)
    ano, mes = inicio.year, inicio.month
    existente = dre_importado(ano, mes)
    return {
        "ano": ano, "mes": mes, "inicio": inicio, "fim": fim,
        "lancamentos": len(lancamentos),
        "contas": sum(len(l.contas) for l in dre["linhas"].values()),
        "receita_bruta": dre["receita_bruta"], "resultado": dre["resultado"],
        "existente": existente,
    }


def contas_do_mes(ano: int, mes: int) -> list:
    """Contas de resultado importadas no mês, na ordem da DRE."""
    from app.extensions import db
    from app.models.banco import DreContabilConta

    ordem = {chave: i for i, chave in enumerate(CHAVES)}
    contas = db.session.execute(
        db.select(DreContabilConta).filter_by(ano=ano, mes=mes)
    ).scalars().all()
    return sorted(contas, key=lambda c: (ordem.get(c.linha, 99), c.classificacao))


def lancamentos_da_conta(ano: int, mes: int, conta: str) -> list:
    from app.extensions import db
    from app.models.documentos import RazaoLancamento

    return db.session.execute(
        db.select(RazaoLancamento).filter_by(ano=ano, mes=mes, conta=conta)
        .order_by(RazaoLancamento.data, RazaoLancamento.id)
    ).scalars().all()


def meses_importados() -> list[tuple[int, int]]:
    from app.extensions import db
    from app.models.banco import DreContabilConta

    linhas = db.session.execute(
        db.select(DreContabilConta.ano, DreContabilConta.mes)
        .group_by(DreContabilConta.ano, DreContabilConta.mes)
        .order_by(DreContabilConta.ano.desc(), DreContabilConta.mes.desc())
    ).all()
    return [(ano, mes) for ano, mes in linhas]


def dre_importado(ano: int, mes: int) -> dict | None:
    """Remonta o DRE contábil de um mês já importado."""
    from app.extensions import db
    from app.models.banco import DreContabilConta

    registros = db.session.execute(
        db.select(DreContabilConta).filter_by(ano=ano, mes=mes)
    ).scalars().all()
    if not registros:
        return None

    por_chave = {chave: LinhaDre(chave, rotulo) for chave, rotulo, _, _ in ESTRUTURA}
    for r in registros:
        linha = por_chave.get(r.linha)
        if linha is None:
            continue
        valor = Decimal(r.valor).quantize(CENTAVOS)
        linha.valor += valor
        linha.contas.append({"conta": r.conta, "classificacao": r.classificacao,
                             "nome": r.nome, "valor": valor, "lancamentos": r.lancamentos})
    for linha in por_chave.values():
        linha.contas.sort(key=lambda c: -abs(c["valor"]))

    receita_bruta = por_chave["receita_bruta"].valor
    receita_liquida = receita_bruta - por_chave["deducoes"].valor
    lucro_bruto = receita_liquida - por_chave["cmv"].valor
    despesas = sum((por_chave[c].valor for c in
                    ("pessoal", "gerais", "tributarias", "financeiras", "depreciacao")), ZERO)
    resultado_operacional = lucro_bruto - despesas
    return {
        "ano": ano, "mes": mes, "linhas": por_chave,
        "receita_bruta": receita_bruta,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "total_despesas": despesas,
        "resultado_operacional": resultado_operacional,
        "resultado": (resultado_operacional + por_chave["receitas_financeiras"].valor
                      + por_chave["outras_receitas"].valor),
    }
