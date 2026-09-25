# -*- coding: utf-8 -*-
"""Central de Relatórios: monta, gera (PDF) e guarda os relatórios do sistema.

Cada tipo produz um `Documento` neutro (título, período, status, tabelas e
notas) usado tanto na prévia da tela quanto no PDF — o que a pessoa vê é o que
sai no arquivo. Gorjetas reaproveitam os PDFs próprios da quinzena.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape, portrait
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.extensions import db
from app.models.documentos import ExportacaoRelatorio
from app.models.gorjetas import PeriodoGorjeta
from app.services import arquivos, dre_gerencial, dre_previsao, gorjetas_relatorios, metas_consultas
from app.services import fluxo_caixa as fluxo
from app.services.gorjetas_relatorios import (
    BORDA,
    CINZA,
    CREME,
    ESTILO_CEL,
    ESTILO_NOTA,
    ESTILO_SECAO,
    ESTILO_SUB,
    ESTILO_TITULO,
    LARANJA,
    MARROM,
    TINTA,
)
from app.utils.datas import hoje_sp
from app.utils.filtros import MESES_PT, format_brl

TIPOS = {
    "dre_gerencial": {"titulo": "DRE gerencial", "descricao": "Resultado do mês por linha, com comparação e composição."},
    "caixa": {"titulo": "Fluxo de caixa", "descricao": "Saldos, resumo semanal e totais por grupo do mês."},
    "gorjetas": {"titulo": "Gorjetas", "descricao": "PDFs da quinzena: completa, resumida ou férias."},
    "metas": {"titulo": "Metas", "descricao": "Meta × realizado mês a mês do ano."},
    "contabil": {"titulo": "Conferência contábil", "descricao": "Prévia do sistema × DRE da contabilidade."},
}
VARIANTES_GORJETA = {"completo": "Gorjeta completa", "resumido": "Gorjeta (sem por fora)", "ferias": "Férias"}


@dataclass
class Linha:
    celulas: list
    estilo: str = ""                     # "" | grupo | total
    itens: list = field(default_factory=list)  # composição: [(nome, valor)]


@dataclass
class Tabela:
    titulo: str
    colunas: list
    linhas: list
    numericas: tuple = ()                # índices das colunas numéricas


@dataclass
class Documento:
    titulo: str
    periodo: str
    base: str
    status: str                          # Preliminar | Fechado
    tabelas: list
    notas: list = field(default_factory=list)


def _brl(v) -> str:
    return "—" if v is None else format_brl(Decimal(v), sinal=True)


def _pct(v) -> str:
    return "—" if v is None else f"{Decimal(v) * 100:.1f}%".replace(".", ",")


def _status_mes(ano: int, mes: int) -> str:
    hoje = hoje_sp()
    return "Preliminar" if (ano, mes) >= (hoje.year, hoje.month) else "Fechado"


# ------------------------------- documentos -------------------------------

def documento_dre_gerencial(ano: int, mes: int, regime: str = "competencia", composicao: bool = True) -> Documento:
    analise = dre_gerencial.analise(ano, mes, regime)
    linhas = [Linha([l["rotulo"], _brl(l["valor"]), _pct(l["pct"]), _brl(l["anterior"]), _brl(l["media"])],
                    itens=list(l["linha"].itens) if composicao else [])
              for l in analise["linhas"]]
    linhas += [Linha([t["rotulo"], _brl(t["valor"]), _pct(t["pct"]), _brl(t["anterior"]), _brl(t["media"])], "total")
               for t in analise["totais"]]
    ant = MESES_PT[12 if mes == 1 else mes - 1]
    return Documento(
        titulo="DRE gerencial", periodo=f"{MESES_PT[mes]} {ano}",
        base="Regime de competência (ajustado)" if regime == "competencia" else "Regime de caixa",
        status=_status_mes(ano, mes),
        tabelas=[Tabela("Demonstrativo", ["Conta", f"{MESES_PT[mes]}", "% RL", ant, f"Média {analise['janela']}m"], linhas, (1, 2, 3, 4))],
        notas=["Receita bruta do faturamento diário (PDV); gorjeta das quinzenas fechadas; demais linhas dos lançamentos do fluxo e do extrato classificado."],
    )


def documento_caixa(ano: int, mes: int, composicao: bool = True) -> Documento:
    visao = fluxo.visao_mensal(ano, mes)
    resumo = fluxo.resumo_do_mes(visao, ano, mes, hoje_sp())
    saldos = Tabela("Saldos", ["Medida", "Valor"], [
        Linha(["Saldo inicial do mês", _brl(visao["saldo_inicial"])]),
        Linha(["Saldo atual" if resumo["mes_atual"] else "Saldo final", _brl(resumo["saldo_atual"] if resumo["mes_atual"] else resumo["saldo_final"])]),
        Linha(["Saldo previsto no fim do mês", _brl(resumo["saldo_final"])]) if resumo["mes_atual"] else Linha(["Resultado do mês", _brl(resumo["entradas"] - resumo["saidas"])]),
    ], (1,))
    semanas = Tabela("Resumo semanal", ["Semana", "Entradas", "Saídas", "Resultado"], [
        Linha([s["rotulo"] + (" · previsto" if s["previsto"] else ""), _brl(s["entradas"]), _brl(s["saidas"]), _brl(s["resultado"])])
        for s in resumo["semanas"]
    ] + [Linha(["Mês", _brl(resumo["entradas"]), _brl(resumo["saidas"]), _brl(resumo["entradas"] - resumo["saidas"])], "total")], (1, 2, 3))
    grupos = Tabela("Totais por grupo", ["Grupo", "Tipo", "Total"], [
        Linha([nome, "Entrada" if g["tipo"] == "entrada" else "Saída", _brl(g["total"])],
              itens=[(c["nome"], c["total"]) for c in g["categorias"].values() if c["total"]] if composicao else [])
        for nome, g in visao["grupos"].items()
    ], (2,))
    return Documento(titulo="Fluxo de caixa", periodo=f"{MESES_PT[mes]} {ano}", base="Regime de caixa (lançamentos do fluxo)",
                     status=_status_mes(ano, mes), tabelas=[saldos, semanas, grupos],
                     notas=["Dias depois de hoje são previstos (lançamentos programados)."])


def documento_metas(ano: int, **_) -> Documento:
    tabela = metas_consultas.tabela_do_ano(ano)
    linhas = [Linha([MESES_PT[l.mes], _brl(l.meta), _brl(l.realizado), _pct(l.pct_meta), _brl(l.meta_acumulada), _brl(l.realizado_acumulado)])
              for l in tabela]
    meta = sum((l.meta for l in tabela if l.meta), Decimal("0"))
    real = sum((l.realizado for l in tabela if l.realizado), Decimal("0"))
    linhas.append(Linha(["Total", _brl(meta), _brl(real), _pct(real / meta if meta else None), "", ""], "total"))
    hoje = hoje_sp()
    return Documento(titulo="Metas de faturamento", periodo=str(ano), base="Registro diário e faturamento histórico",
                     status="Preliminar" if ano >= hoje.year else "Fechado",
                     tabelas=[Tabela("Meta × realizado", ["Mês", "Meta", "Realizado", "% da meta", "Meta acum.", "Realiz. acum."], linhas, (1, 2, 3, 4, 5))])


def documento_contabil(ano: int, mes: int, composicao: bool = True) -> Documento:
    comp = dre_previsao.comparar(ano, mes)
    linhas = [Linha([l["rotulo"], _brl(l["previsto"]), _brl(l["real"]), _brl(l["diferenca"])],
                    itens=[(f'{c["classificacao"]} · {c["nome"]}', c["valor"]) for c in l["contas"]] if composicao else [])
              for l in comp["linhas"]]
    linhas += [Linha([t["rotulo"], _brl(t["previsto"]), _brl(t["real"]), _brl(t["diferenca"])], "total") for t in comp["totais"]]
    notas = [f"Prévia calibrada com {comp['previsto']['calibrado_com']}."]
    if comp["real"] is None:
        notas.append("A contabilidade ainda não enviou o razão deste mês: coluna Contabilidade vazia.")
    return Documento(titulo="Conferência contábil", periodo=f"{MESES_PT[mes]} {ano}", base="Competência (contabilidade) × prévia do sistema",
                     status="Fechado" if comp["real"] else "Preliminar",
                     tabelas=[Tabela("Prévia × contabilidade", ["Conta", "Prévia interna", "Contabilidade", "Diferença"], linhas, (1, 2, 3))],
                     notas=notas)


def documento(tipo: str, p: dict) -> Documento | None:
    if tipo == "dre_gerencial":
        return documento_dre_gerencial(p["ano"], p["mes"], p.get("regime", "competencia"), p.get("composicao", True))
    if tipo == "caixa":
        return documento_caixa(p["ano"], p["mes"], p.get("composicao", True))
    if tipo == "metas":
        return documento_metas(p["ano"])
    if tipo == "contabil":
        return documento_contabil(p["ano"], p["mes"], p.get("composicao", True))
    return None


# ------------------------------- PDF genérico -------------------------------

def _pdf(doc: Documento) -> bytes:
    buffer = BytesIO()
    largo = any(len(t.colunas) > 4 for t in doc.tabelas)
    tamanho = landscape(A4) if largo else portrait(A4)
    gerado = datetime.now().strftime("%d/%m/%Y às %H:%M")

    def cabecalho(canvas, d):
        canvas.saveState()
        larg, alt = tamanho
        canvas.setStrokeColor(BORDA)
        canvas.line(15 * mm, alt - 13 * mm, larg - 15 * mm, alt - 13 * mm)
        canvas.setFont("Helvetica-Bold", 8); canvas.setFillColor(LARANJA)
        canvas.drawString(15 * mm, alt - 11 * mm, "BANGALÔ")
        canvas.setFont("Helvetica", 8); canvas.setFillColor(CINZA)
        canvas.drawString(15 * mm + canvas.stringWidth("BANGALÔ", "Helvetica-Bold", 8) + 5, alt - 11 * mm,
                          f"· {doc.titulo} · {doc.periodo} · {doc.status}")
        canvas.drawRightString(larg - 15 * mm, alt - 11 * mm, f"Gerado em {gerado}")
        canvas.setFont("Helvetica", 7)
        canvas.drawString(15 * mm, 8 * mm, f"Restaurante Bangalô · {doc.base}")
        canvas.drawRightString(larg - 15 * mm, 8 * mm, f"Página {d.page}")
        canvas.restoreState()

    modelo = BaseDocTemplate(buffer, pagesize=tamanho, leftMargin=15 * mm, rightMargin=15 * mm,
                             topMargin=18 * mm, bottomMargin=14 * mm, title=f"{doc.titulo} — {doc.periodo}",
                             author="Sistema Bangalô")
    modelo.addPageTemplates([PageTemplate(frames=[Frame(modelo.leftMargin, modelo.bottomMargin, modelo.width, modelo.height)],
                                          onPage=cabecalho)])
    elementos = [Paragraph(doc.titulo, ESTILO_TITULO),
                 Paragraph(f"{doc.periodo} · {doc.base} · {doc.status}", ESTILO_SUB), Spacer(1, 10)]
    for t in doc.tabelas:
        dados = [[Paragraph(f"<b>{c}</b>", ESTILO_CEL) for c in t.colunas]]
        estilos = [("BACKGROUND", (0, 0), (-1, 0), MARROM), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.4, BORDA),
                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                   ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for i in t.numericas:
            estilos.append(("ALIGN", (i, 1), (i, -1), "RIGHT"))
        for linha in t.linhas:
            idx = len(dados)
            dados.append([Paragraph(str(linha.celulas[0]), ESTILO_CEL)] + [str(c) for c in linha.celulas[1:]])
            if linha.estilo == "total":
                estilos += [("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#F6DCCB")), ("FONTNAME", (0, idx), (-1, idx), "Helvetica-Bold")]
            for nome, valor in linha.itens:
                sub = len(dados)
                dados.append([Paragraph(f"&nbsp;&nbsp;&nbsp;{nome}", ESTILO_NOTA), _brl(valor)] + [""] * (len(t.colunas) - 2))
                estilos += [("TEXTCOLOR", (1, sub), (1, sub), CINZA), ("FONTSIZE", (1, sub), (1, sub), 7),
                            ("BACKGROUND", (0, sub), (-1, sub), CREME)]
        tabela = Table(dados, repeatRows=1, colWidths=[modelo.width * (0.4 if len(t.colunas) > 2 else 0.7)]
                       + [modelo.width * ((0.6 if len(t.colunas) > 2 else 0.3) / (len(t.colunas) - 1))] * (len(t.colunas) - 1))
        tabela.setStyle(TableStyle(estilos))
        elementos += [KeepTogether([Paragraph(t.titulo.upper(), ESTILO_SECAO)]), tabela, Spacer(1, 12)]
    for nota in doc.notas:
        elementos.append(Paragraph(nota, ESTILO_NOTA))
    modelo.build(elementos)
    return buffer.getvalue()


# ------------------------------- geração e histórico -------------------------------

def _descricao_periodo(tipo: str, p: dict) -> str:
    if tipo == "gorjetas":
        periodo = db.session.get(PeriodoGorjeta, p.get("periodo_id") or 0)
        return periodo.referencia if periodo else "Quinzena removida"
    if tipo == "metas":
        return str(p["ano"])
    return f"{MESES_PT[p['mes']]} {p['ano']}"


def gerar(tipo: str, p: dict, usuario_id: int | None) -> ExportacaoRelatorio:
    """Gera o PDF, guarda em instance/arquivos/relatorios e registra no histórico.

    Falha também fica registrada (com o motivo) para poder tentar de novo.
    Não faz commit.
    """
    if tipo not in TIPOS:
        raise ValueError("Tipo de relatório inválido.")
    titulo = TIPOS[tipo]["titulo"]
    if tipo == "gorjetas":
        titulo = VARIANTES_GORJETA.get(p.get("variante"), titulo)
    registro = ExportacaoRelatorio(tipo=tipo, titulo=f"{titulo} · {_descricao_periodo(tipo, p)}", parametros=p,
                                   status="falhou", gerado_por_id=usuario_id)
    try:
        if tipo == "gorjetas":
            periodo = db.session.get(PeriodoGorjeta, p.get("periodo_id") or 0)
            if periodo is None:
                raise ValueError("Quinzena não encontrada.")
            geradores = {"completo": gorjetas_relatorios.pdf_completo, "resumido": gorjetas_relatorios.pdf_resumido,
                         "ferias": gorjetas_relatorios.pdf_ferias}
            conteudo = geradores[p.get("variante", "completo")](periodo)
            if conteudo is None:
                raise ValueError("Ninguém está de férias nesta quinzena.")
            nome = gorjetas_relatorios.nome_arquivo(periodo, p.get("variante", "completo"))
        else:
            doc = documento(tipo, p)
            conteudo = _pdf(doc)
            nome = f"{doc.titulo} - {doc.periodo}.pdf"
        caminho, tamanho = arquivos.guardar_bytes(conteudo, f"relatorios/{datetime.now():%Y-%m}", nome)
        registro.caminho, registro.nome_arquivo, registro.tamanho, registro.status = caminho, nome, tamanho, "gerado"
    except Exception as exc:  # noqa: BLE001 — registra a falha com mensagem legível
        registro.erro = str(exc)[:300] or exc.__class__.__name__
    db.session.add(registro)
    db.session.flush()
    return registro


def historico(limite: int = 100) -> list[ExportacaoRelatorio]:
    return db.session.execute(
        db.select(ExportacaoRelatorio).order_by(ExportacaoRelatorio.gerado_em.desc()).limit(limite)
    ).scalars().all()
