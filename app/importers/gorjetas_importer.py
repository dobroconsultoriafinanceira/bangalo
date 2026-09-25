# -*- coding: utf-8 -*-
"""Importa a planilha 'Calculadora de Gorjetas'.

Importa os INSUMOS de cada quinzena (não só o resultado), para o sistema
recalcular com a própria engine e conferir contra a planilha:

  - parâmetros: comissão bruta, % de encargos e a tabela de funções/pontos;
  - comissão por dia (F-Rest) e a grade de presença;
  - colaboradores da quinzena (setor, função, pontos, registro);
  - descontos por pessoa (vale/perda/avaria);
  - extras por diária (o setor repõe o rateio do extra);
  - férias (quem sai recebe o reembolso do próprio setor).

Layout confirmado na versão de 16/09/2026 da planilha:
  B5 comissão bruta · B6 encargos · F5:H14 funções/pontos · A18:H33 colaboradores
  L18:M32 comissão/dia · A38+ descontos por pessoa · L37:AA52 presença
  E54 pontos do extra · G54 setor do extra · A56+ diárias dos extras
  A73+ reembolso de férias (coluna C marca quem está de férias)

Reimportar a mesma quinzena SUBSTITUI os insumos (a planilha é a fonte).
A aba "Histórico" segue como fallback para quinzenas sem aba própria.
"""
import calendar
import re
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl

from app.extensions import db
from app.importers.comum import para_decimal
from app.models.gorjetas import (
    Colaborador,
    ComissaoDiaria,
    DescontoQuinzena,
    ExtraQuinzena,
    FechamentoGorjeta,
    Funcao,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Presenca,
    Setor,
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}
MESES_NOME = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}
# o enum do desconto por PESSOA usa "Descontos"; o de SETOR usa "Desconto"
MOTIVOS_VALIDOS = {"perda": "Perda", "avaria": "Avaria", "vale": "Vale", "desconto": "Descontos",
                   "descontos": "Descontos"}
MOTIVOS_SETOR = {"perda": "Perda", "avaria": "Avaria", "vale": "Vale", "desconto": "Desconto",
                 "descontos": "Desconto"}
ZERO = Decimal("0")
CENTAVOS = Decimal("0.01")

# Mesma pessoa com outra grafia nas abas antigas → nome do cadastro (o da
# planilha atual). Confirmado com a cliente em 16/09/2026.
ALIASES_COLABORADOR = {
    "valderi": "JOSE VALDERI",
    "tonhao": "ANTONIO LUIZ",
    "raimundo (por dentro)": "RAIMUNDO",
    "leo cozinheiro": "LEO COZINHEIRO",
}
# Quando a pessoa mudou de vínculo NO MEIO da quinzena a aba tem duas linhas
# para ela (pontos/registro diferentes). A engine precisa das duas; ficam como
# vínculos antigos da mesma pessoa, inativos no cadastro.
VINCULOS_ANTIGOS = {
    "raimundo (por fora)": "RAIMUNDO (POR FORA)",
    "leo ajudante": "LEO COZINHEIRO (AJUDANTE)",
    "leo cozinheiro (por fora)": "LEO COZINHEIRO (POR FORA)",
}
# "Auxiliar de Cozinha" (sem nível) valia 1 ponto = hoje "Auxiliar de Cozinha 2"
ALIASES_FUNCAO = {"auxiliar de cozinha": "Auxiliar de Cozinha 2"}


def nome_canonico(nome: str) -> str:
    chave = _norm(nome)
    return ALIASES_COLABORADOR.get(chave) or VINCULOS_ANTIGOS.get(chave) or _txt(nome).upper()


def funcao_canonica(nome: str) -> str:
    return ALIASES_FUNCAO.get(_norm(nome), _txt(nome))


def _parse_referencia(texto: str) -> tuple[int, int, int] | None:
    """'1ª Quinzena Maio/26' → (ordem=1, mes=5, ano=2026)."""
    m = re.search(r"([12])[ªa]?\s*Quinzena\s+([A-Za-zçãÇÃ]+)\s*/?\s*(\d{2,4})", texto, re.I)
    if not m:
        return None
    mes = MESES.get(m.group(2).strip().lower())
    if not mes:
        return None
    ano = int(m.group(3))
    return int(m.group(1)), mes, ano + 2000 if ano < 100 else ano


def _parse_tab_name(nome: str) -> tuple[int, int, int] | None:
    """'2ª Q Julho-26' → (ordem=2, mes=7, ano=2026). None se não bater."""
    m = re.match(r"^([12])[ªa]?\s*Q\s+([A-Za-zçãõÃÕÇ]+)-(\d{2,4})\s*$", nome.strip(), re.I)
    if not m:
        return None
    mes = MESES.get(m.group(2).strip().lower())
    if not mes:
        return None
    ano = int(m.group(3))
    return int(m.group(1)), mes, ano + 2000 if ano < 100 else ano


def _make_referencia(ordem: int, mes: int, ano: int) -> str:
    return f"{ordem}ª Quinzena {MESES_NOME[mes]}/{str(ano)[-2:]}"


def _datas_quinzena(ordem: int, mes: int, ano: int) -> tuple[date, date]:
    if ordem == 1:
        return date(ano, mes, 1), date(ano, mes, 15)
    return date(ano, mes, 16), date(ano, mes, calendar.monthrange(ano, mes)[1])


def _txt(valor) -> str:
    return str(valor).strip() if valor is not None else ""


def _num(valor) -> Decimal:
    return para_decimal(valor) or ZERO if isinstance(valor, (int, float, Decimal)) else ZERO


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFKD", _txt(texto)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", base).strip()


def _achar(ws, prefixo: str, ate_linha: int = 120, ate_coluna: int = 40) -> tuple[int, int] | None:
    """Posição (linha, coluna) do primeiro rótulo que começa com `prefixo`.

    O layout da planilha muda de quinzena para quinzena (a comissão por dia já
    esteve na coluna J e hoje está na L): por isso tudo é localizado pelo rótulo.
    """
    alvo = _norm(prefixo)
    for linha in range(1, ate_linha + 1):
        for coluna in range(1, ate_coluna + 1):
            valor = ws.cell(linha, coluna).value
            if isinstance(valor, str) and _norm(valor).startswith(alvo):
                return linha, coluna
    return None


def _valor_ao_lado(ws, prefixo: str, colunas: tuple[int, ...] = (1, 2, 3)) -> Decimal:
    """Valor à direita de um rótulo (ex.: 'Comissão Bruta' → B5).

    Procura primeiro nas colunas do bloco de parâmetros: em algumas abas existe
    um "Comissão Bruta" do MÊS mais à direita, que não é o da quinzena.
    """
    alvo = _norm(prefixo)
    posicoes = []
    for linha in range(1, 40):
        for coluna in colunas:
            valor = ws.cell(linha, coluna).value
            if isinstance(valor, str) and _norm(valor).startswith(alvo):
                posicoes.append((linha, coluna))
    if not posicoes:
        pos = _achar(ws, prefixo)
        if not pos:
            return ZERO
        posicoes = [pos]
    for linha, coluna in posicoes:
        for offset in (1, 2, 3):
            valor = ws.cell(linha, coluna + offset).value
            if isinstance(valor, (int, float)):
                return _num(valor)
    return ZERO


def _tem_dados(ws) -> bool:
    """Aba de quinzena futura vem como modelo: sem comissão nenhuma."""
    pos = _achar(ws, "COMISSÃO POR DIA")
    if not pos:
        return False
    for linha in range(pos[0] + 2, pos[0] + 20):
        for coluna in (pos[1], pos[1] + 1):
            if _num(ws.cell(linha, coluna).value) and coluna != pos[1]:
                return True
    return False


def _mapa_colunas(ws, linha: int, coluna_inicial: int = 1, largura: int = 20) -> dict[str, int]:
    """Cabeçalho de UMA tabela → {rótulo normalizado: coluna}.

    A janela de colunas importa: várias seções dividem a mesma linha de
    cabeçalho (o "Dia" da comissão fica ao lado do "Dias trab." dos
    colaboradores), e sem a janela uma seção lê a coluna da outra.
    """
    mapa = {}
    for coluna in range(coluna_inicial, coluna_inicial + largura):
        rotulo = _norm(ws.cell(linha, coluna).value)
        if rotulo and rotulo not in mapa:
            mapa[rotulo] = coluna
    return mapa


def _coluna(mapa: dict[str, int], *nomes: str) -> int | None:
    for nome in nomes:
        alvo = _norm(nome)
        if alvo in mapa:
            return mapa[alvo]
    for nome in nomes:
        alvo = _norm(nome)
        for rotulo, coluna in mapa.items():
            if rotulo.startswith(alvo):
                return coluna
    return None


def importar(caminho: Path) -> list[str]:
    wb = openpyxl.load_workbook(caminho, data_only=True)
    setores = {s.nome: s for s in db.session.execute(db.select(Setor)).scalars()}
    rel: list[str] = []
    resumo = {"quinzenas": 0, "colaboradores": 0, "comissoes": 0, "presencas": 0,
              "descontos": 0, "extras": 0, "ferias": 0, "funcoes": 0}
    conferencia: list[str] = []

    for nome_tab in wb.sheetnames:
        if any(p in nome_tab for p in ("Planejado", "Meta", "TESTE", "Rescisão", "Histórico")):
            continue
        parsed = _parse_tab_name(nome_tab)
        if not parsed:
            continue
        ordem, mes, ano = parsed
        ws = wb[nome_tab]
        if not _tem_dados(ws):
            continue  # quinzena futura (modelo em branco)

        comissao_bruta = _valor_ao_lado(ws, "Comissão Bruta")
        encargos = _valor_ao_lado(ws, "Desconto Encargos") or Decimal("0.20")

        resumo["funcoes"] += _atualizar_funcoes(ws, setores)

        periodo = db.session.execute(
            db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes, ordem_quinzena=ordem)
        ).scalar_one_or_none()
        inicio, fim = _datas_quinzena(ordem, mes, ano)
        if periodo is None:
            periodo = PeriodoGorjeta(
                referencia=_make_referencia(ordem, mes, ano), ordem_quinzena=ordem, mes=mes, ano=ano,
                data_inicio=inicio, data_fim=fim,
            )
            db.session.add(periodo)
        periodo.comissao_bruta = comissao_bruta
        periodo.percentual_encargos = encargos
        periodo.status = "fechado" if comissao_bruta > ZERO else "aberto"
        db.session.flush()
        resumo["quinzenas"] += 1

        # o que uma pessoa decidiu na tela (férias, desconto, pontos da quinzena)
        # sobrevive ao reimport: a planilha manda nos insumos, não nas decisões
        manuais = {
            part.colaborador_id: {
                "em_ferias": part.em_ferias,
                "desconto": part.desconto,
                "desconto_motivo": part.desconto_motivo,
                "desconto_observacao": part.desconto_observacao,
                "dias_trabalhados_manual": part.dias_trabalhados_manual,
            }
            for part in db.session.execute(
                db.select(ParticipacaoPeriodo).filter_by(periodo_id=periodo.id)).scalars()
            if part.em_ferias or part.desconto or part.dias_trabalhados_manual is not None
        }

        # a planilha é a fonte: limpa os insumos antes de regravar
        for modelo in (ComissaoDiaria, Presenca, ExtraQuinzena, DescontoQuinzena,
                       ParticipacaoPeriodo, FechamentoGorjeta):
            db.session.query(modelo).filter_by(periodo_id=periodo.id).delete(synchronize_session=False)
        db.session.flush()  # o flush ordena inserts antes de deletes: apaga primeiro

        colaboradores, esperado = _importar_colaboradores(ws, periodo, setores)
        _restaurar_decisoes(periodo, manuais, resumo)
        resumo["colaboradores"] += len(colaboradores)
        resumo["comissoes"] += _importar_comissao_diaria(ws, periodo, inicio)
        resumo["presencas"] += _importar_presencas(ws, periodo, inicio, colaboradores)
        resumo["descontos"] += _importar_descontos(ws, periodo, colaboradores)
        resumo["descontos"] += _importar_descontos_setor(ws, periodo, colaboradores, setores)
        resumo["extras"] += _importar_extras(ws, periodo, inicio, setores)
        resumo["ferias"] += _importar_ferias(ws, periodo, colaboradores)
        db.session.flush()

        if periodo.status == "fechado":
            db.session.expire_all()  # senão o recálculo lê as coleções antigas
            conferencia.append(_fechar_e_conferir(periodo, esperado, colaboradores))

    # ── Histórico: fallback para quinzenas sem aba própria ────────────────────
    historico = 0
    if "Histórico" in wb.sheetnames:
        for row in list(wb["Histórico"].iter_rows(values_only=True))[3:]:
            if not row or not row[0] or not row[1]:
                continue
            parsed = _parse_referencia(_txt(row[0]))
            if not parsed:
                continue
            ordem, mes, ano = parsed
            if db.session.execute(
                db.select(PeriodoGorjeta).filter_by(ano=ano, mes=mes, ordem_quinzena=ordem)
            ).scalar_one_or_none():
                continue
            inicio, fim = _datas_quinzena(ordem, mes, ano)
            periodo = PeriodoGorjeta(
                referencia=_txt(row[0]), ordem_quinzena=ordem, mes=mes, ano=ano,
                data_inicio=inicio, data_fim=fim, status="fechado",
            )
            db.session.add(periodo)
            db.session.flush()
            colab = _get_colaborador(_txt(row[1]), _txt(row[2]) or "Salão", _txt(row[3]) or "Garçom", setores)
            db.session.add(FechamentoGorjeta(
                periodo_id=periodo.id, colaborador_id=colab.id if colab else None,
                nome=_txt(row[1]), setor=_txt(row[2]) or "Salão", funcao=_txt(row[3]) or "Garçom",
                registro=_txt(row[4]) or "CLT", pontos=colab.pontos if colab else Decimal("2"),
                dias_trabalhados=0, liquido_a_pagar=para_decimal(row[5]) or ZERO,
            ))
            historico += 1

    wb.close()
    db.session.flush()

    rel.append(
        f"Quinzenas importadas: {resumo['quinzenas']} · colaboradores {resumo['colaboradores']} · "
        f"comissões diárias {resumo['comissoes']} · presenças {resumo['presencas']} · "
        f"descontos {resumo['descontos']} · extras {resumo['extras']} · férias {resumo['ferias']}."
    )
    if resumo["funcoes"]:
        rel.append(f"Funções com pontos atualizados pela planilha: {resumo['funcoes']}.")
    rel.extend(conferencia)
    if historico:
        rel.append(f"Aba Histórico: {historico} fechamentos de quinzenas sem aba própria.")
    return rel


# ------------------------------- seções da aba -------------------------------

def _atualizar_funcoes(ws, setores: dict) -> int:
    """Tabela de funções e pontos — os pontos mudam com o tempo."""
    pos = _achar(ws, "TABELA DE FUNÇÕES")
    if not pos:
        return 0
    linha_cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, linha_cabecalho, pos[1])
    col_funcao = _coluna(mapa, "função") or pos[1]
    col_setor = _coluna(mapa, "setor") or col_funcao + 1
    col_pontos = _coluna(mapa, "pontos") or col_funcao + 2

    alteradas = 0
    for linha in range(linha_cabecalho + 1, linha_cabecalho + 20):
        nome = _txt(ws.cell(linha, col_funcao).value)
        setor_nome = _txt(ws.cell(linha, col_setor).value)
        pontos = _num(ws.cell(linha, col_pontos).value)
        if not nome:
            break
        if _norm(nome) in ALIASES_FUNCAO:
            continue  # função antiga: os pontos da atual vêm da própria aba atual
        if not setor_nome or not pontos:
            continue
        setor = setores.get(setor_nome)
        if not setor:
            continue
        funcao = db.session.execute(
            db.select(Funcao).filter_by(nome=nome, setor_id=setor.id)
        ).scalar_one_or_none()
        if funcao is None:
            db.session.add(Funcao(nome=nome, setor_id=setor.id, pontos_padrao=pontos))
            alteradas += 1
        elif funcao.pontos_padrao != pontos:
            funcao.pontos_padrao = pontos
            alteradas += 1
    db.session.flush()
    return alteradas


def _importar_colaboradores(ws, periodo, setores: dict) -> tuple[dict[str, Colaborador], dict[str, Decimal]]:
    """Tabela de colaboradores. Retorna (por nome, líquido esperado na planilha)."""
    pos = _achar(ws, "COLABORADORES")
    if not pos:
        return {}, {}
    cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, cabecalho, pos[1])
    col_nome = _coluna(mapa, "nome") or 1
    col_setor = _coluna(mapa, "setor") or col_nome + 1
    col_funcao = _coluna(mapa, "função") or col_nome + 2
    col_pontos = _coluna(mapa, "pontos") or col_nome + 3
    col_liquido = _coluna(mapa, "líquido a pagar") or col_nome + 6
    col_registro = _coluna(mapa, "registro") or col_nome + 7

    colaboradores: dict[str, Colaborador] = {}
    esperado: dict[str, Decimal] = {}
    for linha in range(cabecalho + 1, cabecalho + 40):
        nome = _txt(ws.cell(linha, col_nome).value)
        if not nome:
            break
        if _norm(nome).startswith(("descontos", "resumo", "extras", "reembolso", "total",
                                   "colaboradores", "parametros", "rateio")):
            break  # acabou a tabela: começou a próxima seção
        if nome.lower() == "nome":
            continue
        setor_nome = _txt(ws.cell(linha, col_setor).value)
        if not setor_nome:
            continue  # linha de apoio sem setor não é colaborador
        setor_nome = setor_nome or "Salão"
        funcao_nome = _txt(ws.cell(linha, col_funcao).value) or "Garçom"
        pontos = _num(ws.cell(linha, col_pontos).value)
        registro = _txt(ws.cell(linha, col_registro).value) or "CLT"
        colab = _get_colaborador(nome, setor_nome, funcao_nome, setores)
        if not colab:
            continue
        if pontos and colab.pontos != pontos:
            colab.pontos = pontos
        if registro and colab.registro != registro:
            colab.registro = registro
        colaboradores[nome] = colab
        esperado[nome] = _num(ws.cell(linha, col_liquido).value)
        db.session.add(ParticipacaoPeriodo(
            periodo_id=periodo.id, colaborador_id=colab.id, pontos=pontos or None,
        ))
    db.session.flush()
    return colaboradores, esperado


def _importar_comissao_diaria(ws, periodo, inicio: date) -> int:
    """Comissão por dia — vem do F-Rest."""
    pos = _achar(ws, "COMISSÃO POR DIA")
    if not pos:
        return 0
    cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, cabecalho, pos[1], largura=4)
    col_dia = _coluna(mapa, "dia") or pos[1]
    col_valor = _coluna(mapa, "comissão (r$)", "comissão") or col_dia + 1

    total = 0
    for linha in range(cabecalho + 1, cabecalho + 40):
        dia = ws.cell(linha, col_dia).value
        if _norm(dia) == "total":
            break
        valor = _num(ws.cell(linha, col_valor).value)
        if not isinstance(dia, (int, float)) or not valor:
            continue
        db.session.add(ComissaoDiaria(
            periodo_id=periodo.id, data=date(inicio.year, inicio.month, int(dia)), valor=valor,
        ))
        total += 1
    return total


def _importar_presencas(ws, periodo, inicio: date, colaboradores: dict) -> int:
    """Grade de presença: 1 = trabalhou, por dia."""
    pos = _achar(ws, "PRESENÇA POR DIA")
    if not pos:
        return 0
    cabecalho = pos[0] + 1
    col_nome = pos[1]
    dias = {}
    for coluna in range(col_nome + 1, col_nome + 20):
        dia = ws.cell(cabecalho, coluna).value
        if isinstance(dia, (int, float)) and 1 <= int(dia) <= 31:
            dias[coluna] = int(dia)

    # a grade às vezes escreve o nome diferente da tabela de colaboradores
    por_norm = {_norm(nome): colab for nome, colab in colaboradores.items()}
    total = 0
    for linha in range(cabecalho + 1, cabecalho + 40):
        nome = _txt(ws.cell(linha, col_nome).value)
        if not nome:
            break
        colab = _casar_nome(nome, por_norm)
        if not colab:
            continue
        for coluna, dia in dias.items():
            if _num(ws.cell(linha, coluna).value) == Decimal("1"):
                db.session.add(Presenca(
                    periodo_id=periodo.id, colaborador_id=colab.id,
                    data=date(inicio.year, inicio.month, dia), presente=True,
                ))
                total += 1
    return total


def _casar_nome(nome: str, por_norm: dict):
    """Casa o nome da grade com o da tabela de colaboradores (tolerante)."""
    alvo = _norm(nome)
    if alvo in por_norm:
        return por_norm[alvo]
    for chave, colab in por_norm.items():
        if chave.startswith(alvo) or alvo.startswith(chave):
            return colab
    return None


def _importar_descontos_setor(ws, periodo, colaboradores: dict, setores: dict) -> int:
    """Abas antigas usam 'DESCONTOS POR SETOR' — e às vezes põem o nome de uma
    pessoa na coluna do setor. Cada caso vai para o seu lugar."""
    pos = _achar(ws, "DESCONTOS POR SETOR")
    if not pos:
        return 0
    cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, cabecalho, pos[1])
    col_alvo = _coluna(mapa, "setor") or pos[1]
    col_tipo = _coluna(mapa, "tipo") or col_alvo + 1
    col_valor = _coluna(mapa, "valor") or col_alvo + 2
    col_obs = _coluna(mapa, "observação") or col_alvo + 3
    por_norm = {_norm(nome): colab for nome, colab in colaboradores.items()}

    total = 0
    for linha in range(cabecalho + 1, cabecalho + 12):
        alvo = _txt(ws.cell(linha, col_alvo).value)
        if not alvo or _norm(alvo).startswith(("(", "total", "(=")):
            break
        valor = _num(ws.cell(linha, col_valor).value)
        if not valor:
            continue
        tipo = MOTIVOS_SETOR.get(_norm(ws.cell(linha, col_tipo).value), "Desconto")
        observacao = _txt(ws.cell(linha, col_obs).value) or None
        setor = setores.get(alvo)
        if setor:
            db.session.add(DescontoQuinzena(
                periodo_id=periodo.id, setor_id=setor.id, tipo=tipo,
                valor=valor, observacao=observacao,
            ))
            total += 1
            continue
        colab = _casar_nome(alvo, por_norm)  # nome de pessoa na coluna do setor
        if not colab:
            continue
        participacao = db.session.execute(
            db.select(ParticipacaoPeriodo).filter_by(periodo_id=periodo.id, colaborador_id=colab.id)
        ).scalar_one_or_none()
        if participacao:
            participacao.desconto = (participacao.desconto or ZERO) + valor
            participacao.desconto_motivo = MOTIVOS_VALIDOS.get(_norm(ws.cell(linha, col_tipo).value))
            participacao.desconto_observacao = observacao
            total += 1
    return total


def _importar_descontos(ws, periodo, colaboradores: dict) -> int:
    """Descontos por pessoa: vale, perda, avaria — voltam ao restaurante."""
    pos = _achar(ws, "DESCONTOS POR PESSOA")
    if not pos:
        return 0
    cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, cabecalho, pos[1])
    col_nome = _coluna(mapa, "colaborador") or pos[1]
    col_valor = _coluna(mapa, "valor") or col_nome + 2
    col_tipo = _coluna(mapa, "tipo") or col_nome + 3
    col_obs = _coluna(mapa, "observação") or col_nome + 4

    total = 0
    for linha in range(cabecalho + 1, cabecalho + 12):
        nome = _txt(ws.cell(linha, col_nome).value)
        if not nome:
            break
        valor = _num(ws.cell(linha, col_valor).value)
        if nome not in colaboradores or not valor:
            continue
        participacao = db.session.execute(
            db.select(ParticipacaoPeriodo).filter_by(
                periodo_id=periodo.id, colaborador_id=colaboradores[nome].id)
        ).scalar_one_or_none()
        if not participacao:
            continue
        participacao.desconto = valor
        participacao.desconto_motivo = MOTIVOS_VALIDOS.get(_txt(ws.cell(linha, col_tipo).value).lower())
        participacao.desconto_observacao = _txt(ws.cell(linha, col_obs).value) or None
        total += 1
    return total


def _importar_extras(ws, periodo, inicio: date, setores: dict) -> int:
    """Extras por diária: pontos e setor ficam no cabeçalho da seção."""
    pos = _achar(ws, "EXTRAS")
    if not pos:
        return 0
    pontos_pos = _achar(ws, "Pontos atribuídos ao extra") or _achar(ws, "Pontos do extra")
    setor_pos = _achar(ws, "Setor do extra")
    pontos = _num(ws.cell(pontos_pos[0], pontos_pos[1] + 1).value) if pontos_pos else ZERO
    setor_nome = _txt(ws.cell(setor_pos[0], setor_pos[1] + 1).value) if setor_pos else ""
    setor = setores.get(setor_nome)
    if not setor:
        # abas antigas trazem o setor no título: "EXTRAS DO SALÃO", "EXTRAS DA COZINHA"
        titulo = _norm(ws.cell(*pos).value)
        setor = next((s for nome, s in setores.items() if _norm(nome) in titulo), None)
    if not setor:
        return 0

    cabecalho = pos[0] + 1
    mapa = _mapa_colunas(ws, cabecalho, pos[1])
    col_dia = _coluna(mapa, "dia") or pos[1]
    col_turno = _coluna(mapa, "turno") or col_dia + 1
    col_comissao = _coluna(mapa, "comissão do turno") or col_dia + 2
    col_pago = _coluna(mapa, "pago pelo restaurante") or col_dia + 4

    total = 0
    for linha in range(cabecalho + 1, cabecalho + 15):
        dia = ws.cell(linha, col_dia).value
        if _norm(dia) == "total":
            break
        if not isinstance(dia, (int, float)) or not dia:
            continue
        db.session.add(ExtraQuinzena(
            periodo_id=periodo.id, setor_id=setor.id,
            data=date(inicio.year, inicio.month, int(dia)),
            turno=_txt(ws.cell(linha, col_turno).value) or None,
            pontos=pontos or Decimal("1.5"),
            comissao_turno=_num(ws.cell(linha, col_comissao).value),
            valor_pago=_num(ws.cell(linha, col_pago).value),
        ))
        total += 1
    return total


def _importar_ferias(ws, periodo, colaboradores: dict) -> int:
    """Reembolso de férias: a coluna 'Motivo da ausência' marca quem saiu."""
    pos = _achar(ws, "REEMBOLSO DE FÉRIAS")
    if not pos:
        return 0
    cabecalho = None
    for linha in range(pos[0], pos[0] + 12):
        if _coluna(_mapa_colunas(ws, linha, pos[1]), "motivo da ausência"):
            cabecalho = linha
            break
    if cabecalho is None:
        return 0
    mapa = _mapa_colunas(ws, cabecalho, pos[1])
    col_nome = _coluna(mapa, "colaborador") or pos[1]
    col_motivo = _coluna(mapa, "motivo da ausência")

    total = 0
    for linha in range(cabecalho + 1, cabecalho + 30):
        nome = _txt(ws.cell(linha, col_nome).value)
        if not nome or _norm(nome).startswith("total"):
            break
        motivo = _norm(ws.cell(linha, col_motivo).value)
        if nome not in colaboradores or "feria" not in motivo:
            continue
        participacao = db.session.execute(
            db.select(ParticipacaoPeriodo).filter_by(
                periodo_id=periodo.id, colaborador_id=colaboradores[nome].id)
        ).scalar_one_or_none()
        if participacao:
            participacao.em_ferias = True
            total += 1
    return total


def _ajuste_de_ferias(resultado, esperado: dict[str, Decimal], por_nome: dict) -> dict[str, Decimal]:
    """Acerto de férias calculado sobre o apurado DA PLANILHA.

    A engine faz o mesmo cálculo, mas partindo do recálculo dela; usando o
    apurado da planilha o snapshot fica coerente com o que o time recebeu,
    mesmo nas quinzenas em que o recálculo difere alguns centavos.
    """
    setores = {c.setor for c in resultado.colaboradores if c.em_ferias}
    ajustes: dict[str, Decimal] = {}
    for setor in setores:
        time = [
            nome for nome in esperado
            if (c := por_nome.get(_norm(nome))) is not None and c.setor == setor
        ]
        if not time:
            continue
        alvo = (sum(esperado[n] for n in time) / Decimal(len(time))).quantize(CENTAVOS)
        for nome in time:
            ajustes[nome] = alvo - esperado[nome]
    return ajustes


def _fechar_e_conferir(periodo, esperado: dict[str, Decimal], colaboradores: dict) -> str:
    """Grava o fechamento com o valor PAGO (o da planilha) e confere o recálculo.

    O histórico guarda o que foi efetivamente pago; o recálculo serve de
    conferência. Quinzenas antigas podem divergir de propósito: a regra do
    desconto de extras mudou (era rateio igual, hoje é por ponto e por dia).
    """
    from app.services import gorjetas_consultas

    resultado = gorjetas_consultas.calcular(periodo)
    por_id = {c.id: c for c in resultado.colaboradores}
    # nome da planilha → resultado do recálculo (as grafias antigas já caíram
    # no colaborador certo ao importar)
    por_nome = {
        _norm(nome): por_id[colab.id]
        for nome, colab in colaboradores.items() if colab.id in por_id
    }
    ajustes = _ajuste_de_ferias(resultado, esperado, por_nome)
    divergentes = []
    for nome, alvo in esperado.items():
        calculado = por_nome.get(_norm(nome))
        if calculado is None:
            continue
        db.session.add(FechamentoGorjeta(
            periodo_id=periodo.id, colaborador_id=calculado.id, nome=calculado.nome,
            setor=calculado.setor, funcao=calculado.funcao, registro=calculado.registro,
            pontos=calculado.pontos, dias_trabalhados=calculado.dias_trabalhados,
            desconto=calculado.desconto, desconto_extra=calculado.desconto_extra,
            reembolso_ferias=ajustes.get(nome, ZERO),
            desconto_setor=calculado.desconto_setor,
            # a planilha mostra o apurado ANTES do acerto de férias; o snapshot
            # guarda o que a pessoa recebe de fato
            liquido_a_pagar=alvo + ajustes.get(nome, ZERO),
        ))
        # na planilha, quem está de férias aparece na seção de reembolso
        valor = calculado.liquido - calculado.reembolso_ferias
        if abs(valor - alvo) > Decimal("0.05"):
            divergentes.append(f"{nome} {valor} x {alvo}")
    if divergentes:
        return (f"  {periodo.referencia}: pago conforme a planilha; recálculo difere em "
                f"{len(divergentes)} de {len(esperado)} — " + "; ".join(divergentes[:3]))
    return f"  {periodo.referencia}: OK (o recálculo reproduz a planilha, {len(esperado)} colaboradores)."


def _get_colaborador(nome: str, setor_nome: str, funcao_nome: str, setores: dict) -> Colaborador | None:
    nome = nome_canonico(nome)
    funcao_nome = funcao_canonica(funcao_nome)
    colab = db.session.execute(
        db.select(Colaborador).filter(Colaborador.nome.ilike(nome))
    ).scalar_one_or_none()
    setor = setores.get(setor_nome)
    if not setor:
        return colab
    funcao = db.session.execute(
        db.select(Funcao).filter_by(nome=funcao_nome, setor_id=setor.id)
    ).scalar_one_or_none()
    if not funcao:
        funcao = db.session.execute(
            db.select(Funcao).filter_by(setor_id=setor.id)
        ).scalars().first()
    if colab:
        # a planilha manda: setor/função podem ter mudado
        if funcao and (colab.funcao_id != funcao.id or colab.setor_id != setor.id):
            colab.funcao_id, colab.setor_id = funcao.id, setor.id
        return colab
    if not funcao:
        return None
    colab = Colaborador(
        nome=nome, funcao_id=funcao.id, setor_id=setor.id,
        pontos=funcao.pontos_padrao, registro="CLT",
        ativo=nome not in VINCULOS_ANTIGOS.values(),  # vínculo antigo só vale no histórico
    )
    db.session.add(colab)
    db.session.flush()
    return colab


def _restaurar_decisoes(periodo, manuais: dict, resumo: dict) -> None:
    """Devolve à quinzena o que foi marcado na tela antes do reimport.

    A planilha reescreve presença, comissão e extras; férias, descontos e dias
    manuais são decisão de gente e não podem ser apagados por uma importação.
    """
    if not manuais:
        return
    db.session.flush()
    participacoes = {
        part.colaborador_id: part
        for part in db.session.execute(
            db.select(ParticipacaoPeriodo).filter_by(periodo_id=periodo.id)).scalars()
    }
    restaurados = 0
    for colaborador_id, valores in manuais.items():
        part = participacoes.get(colaborador_id)
        if part is None:      # saiu da planilha: recria para não perder a decisão
            part = ParticipacaoPeriodo(periodo_id=periodo.id, colaborador_id=colaborador_id)
            db.session.add(part)
        for campo, valor in valores.items():
            setattr(part, campo, valor)
        restaurados += 1
    resumo["decisoes_preservadas"] = resumo.get("decisoes_preservadas", 0) + restaurados
