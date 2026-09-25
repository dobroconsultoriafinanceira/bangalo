# -*- coding: utf-8 -*-
"""Relatórios em PDF das gorjetas — reproduzem o que a gerência imprimia da planilha.

Três saídas:

* **completa** — tabelas gerais + presença por dia + resumo + extras (quando houver)
* **resumida** — só as tabelas gerais e sem quem é pago "por fora"
* **férias**   — rateio do reembolso entre o time do setor (só quando há alguém de férias)

Quinzena FECHADA usa o snapshot (o que foi realmente pago); quinzena ABERTA
recalcula pela engine. As duas fontes têm os mesmos campos, então o layout é um só.

Como na planilha, a coluna "Líquido a Pagar" da tabela de colaboradores NÃO
inclui o reembolso de férias — esse acerto sai no relatório de férias.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.extensions import db
from app.models.gorjetas import Funcao, Setor
from app.services import gorjetas_consultas
from app.services.gorjetas import TOLERANCIA, ResultadoExtra
from app.utils.filtros import format_brl

CENTAVOS = Decimal("0.01")
ZERO = Decimal("0")

# paleta do sistema (base.html)
MARROM = colors.HexColor("#6F2F13")
LARANJA = colors.HexColor("#CD5C27")
CREME = colors.HexColor("#FAF7F2")
BORDA = colors.HexColor("#E7E1D8")
TINTA = colors.HexColor("#2B2B2B")
CINZA = colors.HexColor("#6B6B6B")
VERDE = colors.HexColor("#2E7D32")
VERMELHO = colors.HexColor("#B3261E")

LARGURA_UTIL = landscape(A4)[0] - 2 * 12 * mm


def _tolerancia(pessoas: int) -> Decimal:
    """Cada líquido é arredondado ao centavo — a soma pode sobrar uns centavos."""
    return TOLERANCIA + Decimal("0.01") * pessoas


# ───────────────────────────── dados ─────────────────────────────


@dataclass
class LinhaColaborador:
    colaborador_id: int | None
    nome: str
    setor: str
    funcao: str
    registro: str
    pontos: Decimal
    dias: int
    desconto: Decimal          # individual + parte rateada do desconto de setor
    desconto_extra: Decimal    # reposição da diária do extra
    credito: Decimal           # crédito manual de rateio (coluna da planilha)
    liquido: Decimal           # sem o reembolso de férias
    reembolso_ferias: Decimal
    em_ferias: bool

    @property
    def por_fora(self) -> bool:
        return self.registro == "Por fora"


@dataclass
class LinhaSetor:
    nome: str
    percentual: Decimal
    pool: Decimal
    pago: Decimal
    extras: Decimal
    vales: Decimal
    pessoas: int = 0

    @property
    def diferenca(self) -> Decimal:
        return self.pool - (self.pago + self.extras + self.vales)

    @property
    def status(self) -> str:
        return "OK" if abs(self.diferenca) <= _tolerancia(self.pessoas) else "DIVERGE"


@dataclass
class BlocoFerias:
    """Reembolso de férias de um setor: quem paga, quem recebe."""

    setor: str
    liquido_do_setor: Decimal
    pessoas: int
    em_ferias: int
    alvo: Decimal              # todos do setor terminam a quinzena com este valor
    linhas: list[LinhaColaborador]


@dataclass
class Dados:
    periodo: object
    fonte: str                 # "fechamento" | "recálculo"
    linhas: list[LinhaColaborador]
    setores: list[LinhaSetor]
    funcoes: list[tuple[str, str, Decimal]]
    descontos_pessoa: list[tuple[str, str, Decimal, str, str]]
    dias: list[date]
    presencas: dict[int, set[date]]
    comissao_dia: dict[date, Decimal]
    extras: list[ResultadoExtra]
    total_liquido: Decimal
    redistribuido: Decimal
    ordem_setor: dict[str, int] = field(default_factory=dict)

    @property
    def total_pago(self) -> Decimal:
        return sum((s.pago for s in self.setores), ZERO)

    @property
    def total_extras(self) -> Decimal:
        return sum((s.extras for s in self.setores), ZERO)

    @property
    def total_vales(self) -> Decimal:
        return sum((s.vales for s in self.setores), ZERO)

    @property
    def diferenca(self) -> Decimal:
        return self.total_liquido - (self.total_pago + self.total_extras + self.total_vales)

    @property
    def tem_ferias(self) -> bool:
        return any(l.em_ferias for l in self.linhas)

    def blocos_ferias(self) -> list[BlocoFerias]:
        setores = sorted(
            {l.setor for l in self.linhas if l.em_ferias},
            key=lambda s: self.ordem_setor.get(s, 99),
        )
        blocos = []
        for setor in setores:
            time = [l for l in self.linhas if l.setor == setor]
            liquido = sum((l.liquido for l in time), ZERO)
            blocos.append(BlocoFerias(
                setor=setor,
                liquido_do_setor=liquido,
                pessoas=len(time),
                em_ferias=len([l for l in time if l.em_ferias]),
                alvo=(liquido / Decimal(len(time))).quantize(CENTAVOS) if time else ZERO,
                linhas=time,
            ))
        return blocos


def montar(periodo) -> Dados:
    """Junta tudo o que os relatórios precisam, de uma vez só."""
    resultado = gorjetas_consultas.calcular(periodo)
    ordem_setor = {
        s.nome: s.id
        for s in db.session.execute(db.select(Setor).order_by(Setor.id)).scalars()
    }

    # de férias é uma marcação da quinzena: a pessoa pode até ter trabalhado
    # alguns dias e mesmo assim entrar no acerto (ex.: 2ª Q Agosto-26)
    ferias_ids = {p.colaborador_id for p in periodo.participacoes if p.em_ferias}

    if periodo.fechado and periodo.fechamentos:
        linhas = [
            LinhaColaborador(
                colaborador_id=f.colaborador_id,
                nome=f.nome, setor=f.setor, funcao=f.funcao, registro=f.registro,
                pontos=Decimal(f.pontos), dias=f.dias_trabalhados,
                desconto=Decimal(f.desconto) + Decimal(f.desconto_setor or 0),
                desconto_extra=Decimal(f.desconto_extra),
                credito=ZERO,
                liquido=Decimal(f.liquido_a_pagar) - Decimal(f.reembolso_ferias),
                reembolso_ferias=Decimal(f.reembolso_ferias),
                em_ferias=f.colaborador_id in ferias_ids,
            )
            for f in periodo.fechamentos
        ]
        fonte = "fechamento"
    else:
        linhas = [
            LinhaColaborador(
                colaborador_id=r.id,
                nome=r.nome, setor=r.setor, funcao=r.funcao, registro=r.registro,
                pontos=r.pontos, dias=r.dias_trabalhados,
                desconto=r.desconto + r.desconto_setor,
                desconto_extra=r.desconto_extra,
                credito=ZERO,
                liquido=r.liquido - r.reembolso_ferias,
                reembolso_ferias=r.reembolso_ferias,
                em_ferias=r.em_ferias,
            )
            for r in resultado.colaboradores
        ]
        fonte = "recálculo"

    linhas.sort(key=lambda l: (ordem_setor.get(l.setor, 99), l.nome))

    percentuais = gorjetas_consultas.percentuais_setores()
    setores = []
    for nome, _ordem in sorted(ordem_setor.items(), key=lambda kv: kv[1]):
        do_setor = [l for l in linhas if l.setor == nome]
        pool = resultado.pools.get(nome, ZERO)
        if not do_setor and not pool:
            continue
        setores.append(LinhaSetor(
            nome=nome,
            percentual=percentuais.get(nome, ZERO),
            pool=pool,
            pago=sum((l.liquido for l in do_setor), ZERO),
            extras=sum((l.desconto_extra for l in do_setor), ZERO),
            vales=sum((l.desconto for l in do_setor), ZERO),
            pessoas=len(do_setor),
        ))

    funcoes = [
        (f.nome, f.setor.nome, Decimal(f.pontos_padrao))
        for f in db.session.execute(
            db.select(Funcao).join(Setor).order_by(Setor.id, Funcao.pontos_padrao.desc(), Funcao.nome)
        ).scalars()
    ]

    # o nome sai igual ao da tabela de colaboradores (o cadastro pode ter
    # outra grafia da mesma pessoa)
    nome_da_linha = {l.colaborador_id: l.nome for l in linhas}
    descontos_pessoa = []
    for p in periodo.participacoes:
        if Decimal(p.desconto or 0) > 0:
            descontos_pessoa.append((
                nome_da_linha.get(p.colaborador_id, p.colaborador.nome),
                p.colaborador.setor.nome, Decimal(p.desconto),
                p.desconto_motivo or "—", p.desconto_observacao or "",
            ))
    for d in periodo.descontos_setor:
        descontos_pessoa.append((
            f"(todo o setor {d.setor.nome})", d.setor.nome, Decimal(d.valor),
            d.tipo, d.observacao or "rateado proporcionalmente ao apurado",
        ))
    descontos_pessoa.sort(key=lambda d: (ordem_setor.get(d[1], 99), d[0]))

    dias = []
    d = periodo.data_inicio
    while d <= periodo.data_fim:
        dias.append(d)
        d += timedelta(days=1)

    presencas: dict[int, set[date]] = {}
    for pres in periodo.presencas:
        if pres.presente:
            presencas.setdefault(pres.colaborador_id, set()).add(pres.data)

    return Dados(
        periodo=periodo,
        fonte=fonte,
        linhas=linhas,
        setores=setores,
        funcoes=funcoes,
        descontos_pessoa=descontos_pessoa,
        dias=dias,
        presencas=presencas,
        comissao_dia={c.data: Decimal(c.valor) for c in periodo.comissoes_diarias},
        extras=resultado.extras,
        total_liquido=resultado.total_liquido,
        redistribuido=resultado.redistribuido,
        ordem_setor=ordem_setor,
    )


# ────────────────────────── estilos/helpers ──────────────────────────

ESTILO_TITULO = ParagraphStyle(
    "titulo", fontName="Helvetica-Bold", fontSize=13, textColor=MARROM, leading=16,
)
ESTILO_SUB = ParagraphStyle(
    "sub", fontName="Helvetica", fontSize=8.5, textColor=CINZA, leading=11,
)
ESTILO_SECAO = ParagraphStyle(
    "secao", fontName="Helvetica-Bold", fontSize=9, textColor=MARROM,
    leading=12, spaceBefore=2, spaceAfter=4,
)
ESTILO_NOTA = ParagraphStyle(
    "nota", fontName="Helvetica-Oblique", fontSize=7, textColor=CINZA, leading=9,
)
ESTILO_CAB = ParagraphStyle(
    "cab", fontName="Helvetica-Bold", fontSize=6.6, textColor=colors.white,
    alignment=TA_CENTER, leading=7.8,
)
ESTILO_CEL = ParagraphStyle("cel", fontName="Helvetica", fontSize=7, leading=8.5)
ESTILO_CEL_C = ParagraphStyle("celc", parent=ESTILO_CEL, alignment=TA_CENTER)
ESTILO_CEL_D = ParagraphStyle("celd", parent=ESTILO_CEL, alignment=TA_RIGHT)


def _brl(valor) -> str:
    """R$ 1.234,56 — zero vira 'R$ -', como na planilha."""
    valor = Decimal(valor or 0).quantize(CENTAVOS)
    if valor == 0:
        return "R$ -"
    return format_brl(valor)


def _num(valor, casas: int = 1) -> str:
    texto = f"{Decimal(valor or 0):.{casas}f}"
    return texto.replace(".", ",")


def _pct(valor, casas: int = 1) -> str:
    return f"{Decimal(valor or 0) * 100:.{casas}f}".replace(".", ",") + "%"


def _estilo_tabela(
    n_linhas: int,
    colunas_direita: list[int] | None = None,
    total: bool = False,
    fonte: float = 7,
    respiro: float = 2.5,
) -> TableStyle:
    """Cabeçalho marrom, zebra creme, números à direita."""
    comandos = [
        ("BACKGROUND", (0, 0), (-1, 0), MARROM),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), fonte),
        ("TEXTCOLOR", (0, 1), (-1, -1), TINTA),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDA),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, MARROM),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), respiro),
        ("BOTTOMPADDING", (0, 0), (-1, -1), respiro),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for i in range(1, n_linhas):
        if i % 2 == 0:
            comandos.append(("BACKGROUND", (0, i), (-1, i), CREME))
    for col in colunas_direita or []:
        comandos.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    if total and n_linhas > 1:
        comandos += [
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F6DCCB")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, MARROM),
        ]
    return TableStyle(comandos)


def _cab(*textos: str) -> list[Paragraph]:
    """Linha de cabeçalho — Paragraph para o texto quebrar na largura da coluna."""
    return [Paragraph(t, ESTILO_CAB) for t in textos]


def _secao(texto: str) -> Paragraph:
    return Paragraph(texto.upper(), ESTILO_SECAO)


def _rodape(dados: Dados, titulo: str):
    """Cabeçalho/rodapé repetidos em toda página."""
    referencia = dados.periodo.referencia
    gerado = datetime.now().strftime("%d/%m/%Y às %H:%M")

    def desenhar(canvas, doc):
        canvas.saveState()
        largura, altura = landscape(A4)
        canvas.setStrokeColor(BORDA)
        canvas.setLineWidth(0.5)
        canvas.line(12 * mm, altura - 13 * mm, largura - 12 * mm, altura - 13 * mm)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(LARANJA)
        canvas.drawString(12 * mm, altura - 11 * mm, "BANGALÔ")
        depois = 12 * mm + canvas.stringWidth("BANGALÔ", "Helvetica-Bold", 8) + 5
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(CINZA)
        canvas.drawString(depois, altura - 11 * mm, f"· {titulo} · {referencia}")
        canvas.drawRightString(largura - 12 * mm, altura - 11 * mm, f"Gerado em {gerado}")
        canvas.setFont("Helvetica", 7)
        canvas.drawString(12 * mm, 7 * mm, "Sistema Bangalô · relatório gerado automaticamente")
        canvas.drawRightString(largura - 12 * mm, 7 * mm, f"Página {doc.page}")
        canvas.restoreState()

    return desenhar


def _documento(buffer, dados: Dados, titulo: str) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=17 * mm, bottomMargin=11 * mm,
        title=f"{titulo} — {dados.periodo.referencia}",
        author="Sistema Bangalô", subject="Gorjetas",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="corpo")
    doc.addPageTemplates([
        PageTemplate(id="padrao", frames=[frame], onPage=_rodape(dados, titulo))
    ])
    return doc


# ────────────────────────── blocos do relatório ──────────────────────────


def _cabecalho(dados: Dados, titulo: str) -> list:
    periodo = dados.periodo
    situacao = "fechada" if periodo.fechado else "em aberto"
    return [
        Paragraph(titulo, ESTILO_TITULO),
        Paragraph(
            f"{periodo.referencia} · {periodo.data_inicio.strftime('%d/%m/%Y')} a "
            f"{periodo.data_fim.strftime('%d/%m/%Y')} · quinzena {situacao} "
            f"({'valores do fechamento' if dados.fonte == 'fechamento' else 'valores recalculados'})",
            ESTILO_SUB,
        ),
        Spacer(1, 7),
    ]


def _bloco_topo(dados: Dados) -> Table:
    """Parâmetros + rateio por setor à esquerda; tabela de funções à direita."""
    periodo = dados.periodo
    parametros = [
        ["PARÂMETROS DA QUINZENA", ""],
        ["Comissão bruta (12%)", _brl(periodo.comissao_bruta)],
        ["Desconto de encargos", _pct(periodo.percentual_encargos)],
        ["(=) Total líquido a ratear", _brl(dados.total_liquido)],
    ]
    t_parametros = Table(parametros, colWidths=[150, 95], rowHeights=12.5)
    t_parametros.setStyle(_estilo_tabela(len(parametros), colunas_direita=[1], fonte=7.5))

    rateio = [["Setor", "% Rateio", "Pool do setor"]]
    for s in dados.setores:
        rateio.append([s.nome, _pct(s.percentual, 1), _brl(s.pool)])
    rateio.append(["TOTAL", _pct(sum((s.percentual for s in dados.setores), ZERO), 0),
                   _brl(sum((s.pool for s in dados.setores), ZERO))])
    t_rateio = Table(rateio, colWidths=[95, 65, 85], rowHeights=12.5)
    t_rateio.setStyle(_estilo_tabela(len(rateio), colunas_direita=[1, 2], total=True, fonte=7.5))

    esquerda = [
        [t_parametros],
        [Spacer(1, 6)],
        [_secao("Rateio por setor")],
        [t_rateio],
    ]
    t_esquerda = Table(esquerda, colWidths=[250])
    t_esquerda.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    funcoes = [["Função", "Setor", "Pontos"]]
    for nome, setor, pontos in dados.funcoes:
        funcoes.append([nome, setor, _num(pontos)])
    t_funcoes = Table(funcoes, colWidths=[150, 70, 45], repeatRows=1, rowHeights=11)
    t_funcoes.setStyle(_estilo_tabela(len(funcoes), colunas_direita=[2], fonte=6.8))

    direita = [[_secao("Tabela de funções e pontos")], [t_funcoes]]
    t_direita = Table(direita, colWidths=[265])
    t_direita.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    topo = Table([[t_esquerda, "", t_direita]], colWidths=[250, 40, 265])
    topo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return topo


def _tabela_colaboradores(linhas: list[LinhaColaborador]) -> Table:
    dados_tab = [_cab(
        "Nome", "Setor", "Função", "Registro", "Pontos", "Dias trab.",
        "Desconto (R$)", "(-) Extras do setor (R$)", "Crédito rateio (R$)", "Líquido a pagar",
    )]
    for l in linhas:
        dados_tab.append([
            Paragraph(l.nome, ESTILO_CEL), l.setor, Paragraph(l.funcao, ESTILO_CEL),
            l.registro, _num(l.pontos), str(l.dias),
            _brl(l.desconto), _brl(l.desconto_extra), _brl(l.credito), _brl(l.liquido),
        ])
    dados_tab.append([
        "TOTAL", "", "", "", "", "",
        _brl(sum((l.desconto for l in linhas), ZERO)),
        _brl(sum((l.desconto_extra for l in linhas), ZERO)),
        _brl(sum((l.credito for l in linhas), ZERO)),
        _brl(sum((l.liquido for l in linhas), ZERO)),
    ])

    larguras = [128, 52, 104, 50, 36, 38, 66, 76, 66, 80]
    tabela = Table(dados_tab, colWidths=larguras, repeatRows=1)
    estilo = _estilo_tabela(len(dados_tab), colunas_direita=[4, 5, 6, 7, 8, 9],
                            total=True, respiro=1.6)
    estilo.add("FONTNAME", (9, 1), (9, -1), "Helvetica-Bold")
    for i, l in enumerate(linhas, start=1):
        if l.em_ferias:
            estilo.add("TEXTCOLOR", (0, i), (-1, i), CINZA)
    tabela.setStyle(estilo)
    return tabela


def _tabela_descontos(dados: Dados) -> list:
    if not dados.descontos_pessoa:
        return []
    linhas = [_cab("Colaborador", "Setor", "Valor (R$)", "Tipo", "Observação")]
    for nome, setor, valor, tipo, obs in dados.descontos_pessoa:
        linhas.append([Paragraph(nome, ESTILO_CEL), setor, _brl(valor), tipo,
                       Paragraph(obs, ESTILO_CEL)])
    linhas.append(["TOTAL", "", _brl(sum(d[2] for d in dados.descontos_pessoa)), "", ""])
    tabela = Table(linhas, colWidths=[150, 70, 75, 70, 240], repeatRows=1)
    tabela.setStyle(_estilo_tabela(len(linhas), colunas_direita=[2], total=True))
    return [Spacer(1, 10), _secao("Descontos por pessoa"), tabela]


def _tabela_presenca(dados: Dados) -> list:
    dias = dados.dias
    cabecalho = _cab("Colaborador", *[str(d.day) for d in dias], "Dias")
    linhas = [cabecalho]
    for l in dados.linhas:
        presentes = dados.presencas.get(l.colaborador_id or -1, set())
        linhas.append(
            [Paragraph(l.nome, ESTILO_CEL)]
            + ["1" if d in presentes else "0" for d in dias]
            + [str(len(presentes))]
        )
    linhas.append(
        ["Comissão do dia (R$)"]
        + [f"{dados.comissao_dia.get(d, ZERO):,.0f}".replace(",", ".") for d in dias]
        + [f"{sum(dados.comissao_dia.values(), ZERO):,.0f}".replace(",", ".")]
    )

    largura_nome = 132
    largura_total = 42
    largura_dia = (LARGURA_UTIL - largura_nome - largura_total) / max(len(dias), 1)
    tabela = Table(linhas, colWidths=[largura_nome] + [largura_dia] * len(dias) + [largura_total],
                   repeatRows=1)
    estilo = _estilo_tabela(len(linhas), fonte=6.2, total=True)
    estilo.add("ALIGN", (1, 0), (-1, -1), "CENTER")
    estilo.add("ALIGN", (1, -1), (-1, -1), "RIGHT")
    estilo.add("LEFTPADDING", (1, 0), (-1, -1), 1)
    estilo.add("RIGHTPADDING", (1, 0), (-1, -1), 1)
    # fim de semana em destaque (o texto é Paragraph: marca o fundo)
    for i, d in enumerate(dias, start=1):
        if d.weekday() >= 5:
            estilo.add("BACKGROUND", (i, 0), (i, 0), colors.HexColor("#8E3D19"))
    # faltas em cinza claro para a presença saltar aos olhos
    for li, l in enumerate(dados.linhas, start=1):
        presentes = dados.presencas.get(l.colaborador_id or -1, set())
        for ci, d in enumerate(dias, start=1):
            if d not in presentes:
                estilo.add("TEXTCOLOR", (ci, li), (ci, li), colors.HexColor("#C9C4BC"))
    tabela.setStyle(estilo)
    return [_secao("Presença por dia (1 = trabalhou, 0 = faltou)"), tabela]


def _tabela_resumo(dados: Dados) -> list:
    linhas = [_cab("Setor", "A distribuir (pool)", "Pago aos colaboradores",
                   "(-) Extras do setor", "(-) Vales/descontos", "Diferença", "Status")]
    for s in dados.setores:
        linhas.append([s.nome, _brl(s.pool), _brl(s.pago), _brl(s.extras), _brl(s.vales),
                       _brl(s.diferenca), s.status])
    status_geral = "OK" if abs(dados.diferenca) <= _tolerancia(len(dados.linhas)) else "DIVERGE"
    linhas.append(["TOTAL", _brl(dados.total_liquido), _brl(dados.total_pago),
                   _brl(dados.total_extras), _brl(dados.total_vales),
                   _brl(dados.diferenca), status_geral])

    tabela = Table(linhas, colWidths=[90, 110, 125, 100, 105, 90, 60])
    estilo = _estilo_tabela(len(linhas), colunas_direita=[1, 2, 3, 4, 5], total=True)
    estilo.add("ALIGN", (6, 0), (6, -1), "CENTER")
    for i, s in enumerate(dados.setores, start=1):
        estilo.add("TEXTCOLOR", (6, i), (6, i), VERDE if s.status == "OK" else VERMELHO)
    estilo.add("TEXTCOLOR", (6, -1), (6, -1), VERDE if status_geral == "OK" else VERMELHO)
    tabela.setStyle(estilo)

    fechamento = [
        ["Total geral a pagar (soma dos líquidos)", _brl(dados.total_pago)],
        ["Pool líquido da quinzena", _brl(dados.total_liquido)],
        ["Diferença (pago + extras + vales - pool)", _brl(dados.diferenca)],
    ]
    t_fechamento = Table(fechamento, colWidths=[250, 110])
    t_fechamento.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDA),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F6DCCB")),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))

    saida = [_secao("Resumo"), tabela, Spacer(1, 8), t_fechamento]
    if dados.redistribuido > 0:
        saida.append(Spacer(1, 4))
        saida.append(Paragraph(
            f"Setor sem ninguém presente em algum dia: {_brl(dados.redistribuido)} "
            "foram redistribuídos aos setores com presença.", ESTILO_NOTA))
    return saida


def _tabela_extras(dados: Dados) -> list:
    if not dados.extras:
        return []
    linhas = [_cab("Dia", "Setor", "Turno", "Pontos", "Comissão do turno",
                   "Devido ao extra pelo rateio", "Pago pelo restaurante",
                   "(-) Reposto pelo setor", "Parte do restaurante", "Excedente do setor")]
    for e in dados.extras:
        linhas.append([
            e.data.strftime("%d/%m"), e.setor, e.turno or "—", _num(e.pontos),
            _brl(e.comissao_turno), _brl(e.devido_pelo_rateio), _brl(e.pago_pelo_restaurante),
            _brl(e.reposto_pelo_setor), _brl(e.parte_restaurante), _brl(e.excedente_do_setor),
        ])
    somar = lambda campo: sum((getattr(e, campo) for e in dados.extras), ZERO)
    linhas.append([
        "TOTAL", "", "", "", "", _brl(somar("devido_pelo_rateio")),
        _brl(somar("pago_pelo_restaurante")), _brl(somar("reposto_pelo_setor")),
        _brl(somar("parte_restaurante")), _brl(somar("excedente_do_setor")),
    ])
    tabela = Table(linhas, colWidths=[38, 55, 68, 38, 78, 92, 85, 85, 80, 85], repeatRows=1)
    tabela.setStyle(_estilo_tabela(len(linhas), colunas_direita=[3, 4, 5, 6, 7, 8, 9], total=True))
    return [
        Spacer(1, 10),
        _secao("Extras (pessoa de fora contratada por diária)"),
        tabela,
        Spacer(1, 4),
        Paragraph(
            "O restaurante paga a diária do extra; o setor repõe apenas o que o extra teria "
            "recebido no rateio daquele turno, dividido por ponto entre quem estava presente no dia. "
            "A diferença fica por conta do restaurante.", ESTILO_NOTA),
    ]


def _tabela_ferias(bloco: BlocoFerias) -> list:
    resumo = [
        [f"REEMBOLSO DE FÉRIAS — rateio entre o time de {bloco.setor}", ""],
        ["Líquido do setor apurado na quinzena", _brl(bloco.liquido_do_setor)],
        [f"Nº de colaboradores ({bloco.setor})", str(bloco.pessoas)],
        ["Nº de colaboradores em férias", str(bloco.em_ferias)],
        ["(=) Alvo por pessoa (férias e presentes terminam iguais)", _brl(bloco.alvo)],
    ]
    t_resumo = Table(resumo, colWidths=[300, 110])
    estilo_resumo = _estilo_tabela(len(resumo), colunas_direita=[1], fonte=7.5)
    estilo_resumo.add("SPAN", (0, 0), (1, 0))
    t_resumo.setStyle(estilo_resumo)

    linhas = [_cab("Colaborador", "Função", "Motivo da ausência", "Líquido apurado (R$)",
                   "Paga / (Recebe) (R$)", "Fica com (R$)", "Status")]
    total_apurado = total_paga = total_fica = ZERO
    for l in bloco.linhas:
        paga = -l.reembolso_ferias
        fica = l.liquido + l.reembolso_ferias
        total_apurado += l.liquido
        total_paga += paga
        total_fica += fica
        linhas.append([
            Paragraph(l.nome, ESTILO_CEL), Paragraph(l.funcao, ESTILO_CEL),
            "Férias" if l.em_ferias else "",
            _brl(l.liquido), _brl(paga), _brl(fica),
            "Recebe" if paga < 0 else ("Paga" if paga > 0 else "—"),
        ])
    status = "OK" if abs(total_paga) <= _tolerancia(len(bloco.linhas)) else "DIVERGE"
    linhas.append(["TOTAL", "", "", _brl(total_apurado), _brl(total_paga),
                   _brl(total_fica), status])

    tabela = Table(linhas, colWidths=[140, 100, 95, 105, 105, 105, 60], repeatRows=1)
    estilo = _estilo_tabela(len(linhas), colunas_direita=[3, 4, 5], total=True)
    estilo.add("ALIGN", (6, 0), (6, -1), "CENTER")
    estilo.add("SPAN", (0, len(linhas) - 1), (2, len(linhas) - 1))
    for i, l in enumerate(bloco.linhas, start=1):
        if l.em_ferias:
            estilo.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FBF0E9"))
            estilo.add("FONTNAME", (0, i), (-1, i), "Helvetica-Bold")
            estilo.add("TEXTCOLOR", (6, i), (6, i), VERDE)
    estilo.add("TEXTCOLOR", (6, -1), (6, -1), VERDE if status == "OK" else VERMELHO)
    tabela.setStyle(estilo)

    saida = [t_resumo, Spacer(1, 10), tabela]
    if total_paga:
        saida.append(Spacer(1, 4))
        saida.append(Paragraph(
            f"A diferença de {_brl(abs(total_paga))} vem do arredondamento ao centavo de cada "
            "valor e fica por conta do restaurante.", ESTILO_NOTA))
    return saida


# ────────────────────────────── saídas ──────────────────────────────


def _gerar(dados: Dados, titulo: str, elementos: list) -> bytes:
    buffer = BytesIO()
    doc = _documento(buffer, dados, titulo)
    doc.build(_cabecalho(dados, titulo) + elementos)
    return buffer.getvalue()


def pdf_completo(periodo) -> bytes:
    """Tabelas gerais + presença por dia + resumo + extras."""
    dados = montar(periodo)
    elementos = [
        _bloco_topo(dados),
        Spacer(1, 9),
        _secao("Colaboradores"),
        _tabela_colaboradores(dados.linhas),
        PageBreak(),
        *_tabela_presenca(dados),
        PageBreak(),
        *_tabela_resumo(dados),
        *_tabela_descontos(dados),
        *_tabela_extras(dados),
    ]
    if dados.tem_ferias:
        elementos.append(Spacer(1, 10))
        elementos.append(Paragraph(
            "Há colaborador de férias nesta quinzena: o acerto do reembolso sai no "
            "relatório \"Valores a pagar — Férias\".", ESTILO_NOTA))
    return _gerar(dados, "Gorjeta completa", elementos)


def pdf_resumido(periodo) -> bytes:
    """Versão para circular: só as tabelas gerais, sem quem é pago por fora."""
    dados = montar(periodo)
    linhas = [l for l in dados.linhas if not l.por_fora]
    ocultos = len(dados.linhas) - len(linhas)
    elementos = [
        _bloco_topo(dados),
        Spacer(1, 9),
        _secao("Colaboradores"),
        _tabela_colaboradores(linhas),
        *_tabela_descontos(dados),
    ]
    if ocultos:
        elementos.append(Spacer(1, 6))
        elementos.append(Paragraph(
            f"Versão resumida: {ocultos} colaborador(es) com registro \"Por fora\" "
            "não aparecem nesta listagem.", ESTILO_NOTA))
    return _gerar(dados, "Gorjeta", elementos)


def pdf_ferias(periodo) -> bytes | None:
    """Só faz sentido quando alguém do time está de férias na quinzena."""
    dados = montar(periodo)
    blocos = dados.blocos_ferias()
    if not blocos:
        return None

    elementos = []
    for i, bloco in enumerate(blocos):
        if i:
            elementos.append(PageBreak())
        elementos.append(KeepTogether(_tabela_ferias(bloco)))
    elementos.append(Spacer(1, 10))
    elementos.append(Paragraph(
        "Política interna: quem sai de férias recebe a comissão como se tivesse trabalhado todos "
        "os dias da quinzena. Para isso, todo o time do setor termina a quinzena com o mesmo valor "
        "(o alvo acima): quem apurou mais que o alvo paga a diferença, quem apurou menos recebe. "
        "O acerto acontece dentro do setor e fecha em zero.",
        ESTILO_NOTA))
    return _gerar(dados, "Valores a pagar — Férias", elementos)


def nome_arquivo(periodo, tipo: str) -> str:
    prefixos = {
        "completo": "Gorjeta Completa",
        "resumido": "Gorjeta",
        "ferias": "Valores a pagar Ferias",
    }
    referencia = (
        periodo.referencia.replace("ª", "a").replace("/", "-").replace("  ", " ")
    )
    return f"{prefixos[tipo]} - {referencia}.pdf"
