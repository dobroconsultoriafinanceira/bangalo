# -*- coding: utf-8 -*-
"""Classificação gerencial do extrato bancário — visão de CFO de restaurante.

Separa o que é OPERAÇÃO do restaurante (vendas recebidas e despesas pagas) de:
  - SÓCIOS E FINANCIAMENTOS: retiradas/lucro e empréstimos pagos ou recebidos;
  - TESOURARIA: aplicação/resgate automático, CDB e rendimentos — dinheiro que
    só muda de lugar dentro do banco;
  - TRANSFERÊNCIAS entre contas próprias (mesmo CNPJ), exceto o repasse da
    conta Stone, que é o recebimento das vendas no cartão enquanto a
    integração Stone não traz as vendas diretamente.

Prioridade: classificação manual do movimento (tratada no serviço) > regras
cadastradas pelo usuário (contraparte/descrição) > regras padrão > "a
classificar". Funções puras: aceitam qualquer objeto com tipo, descricao,
origem, contraparte, contraparte_documento e contraparte_instituicao.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

BLOCOS = {  # ordem de exibição
    "operacional": "Operação do restaurante",
    "socios": "Sócios e financiamentos",
    "tesouraria": "Tesouraria (aplicações)",
    "transferencia": "Transferências entre contas próprias",
    "a_classificar": "A classificar",
}


@dataclass(frozen=True)
class CategoriaGerencial:
    codigo: str
    nome: str
    bloco: str


_CATEGORIAS = (
    # operação — recebimentos
    CategoriaGerencial("rec_stone", "Vendas no cartão – repasse Stone", "operacional"),
    CategoriaGerencial("rec_ifood", "iFood", "operacional"),
    CategoriaGerencial("rec_99food", "99Food", "operacional"),
    CategoriaGerencial("rec_eventos", "Eventos e reservas", "operacional"),
    CategoriaGerencial("rec_pix_clientes", "PIX de clientes", "operacional"),
    CategoriaGerencial("rec_outros", "Outros recebimentos", "operacional"),
    # operação — pagamentos
    CategoriaGerencial("pag_fornecedores", "Fornecedores", "operacional"),
    CategoriaGerencial("pag_folha", "Folha de pagamento", "operacional"),
    CategoriaGerencial("pag_encargos", "Encargos trabalhistas (FGTS)", "operacional"),
    CategoriaGerencial("pag_prolabore", "Pró-labore", "operacional"),
    CategoriaGerencial("pag_reembolso_socio", "Reembolso de despesas a sócios", "operacional"),
    CategoriaGerencial("pag_tributos", "Tributos", "operacional"),
    CategoriaGerencial("pag_utilidades", "Água, luz, gás e telefone", "operacional"),
    CategoriaGerencial("pag_cartao", "Fatura de cartão de crédito (a detalhar)", "operacional"),
    CategoriaGerencial("pag_aluguel", "Aluguel e condomínio", "operacional"),
    CategoriaGerencial("pag_manutencao", "Manutenção, obras e equipamentos", "operacional"),
    CategoriaGerencial("pag_artistas", "Músicos e produção artística", "operacional"),
    CategoriaGerencial("pag_servicos", "Serviços profissionais", "operacional"),
    CategoriaGerencial("pag_tarifas", "Tarifas bancárias e IOF", "operacional"),
    CategoriaGerencial("pag_outros", "Outros pagamentos", "operacional"),
    # sócios e financiamentos
    CategoriaGerencial("soc_retirada", "Retirada de sócios / lucro", "socios"),
    CategoriaGerencial("soc_a_detalhar", "Sócios – a detalhar", "socios"),
    CategoriaGerencial("fin_emprestimo_pagamento", "Pagamento de empréstimo", "socios"),
    CategoriaGerencial("fin_emprestimo_recebido", "Empréstimo recebido / aporte", "socios"),
    # tesouraria
    CategoriaGerencial("tes_aplicacao", "Aplicação (automática/CDB)", "tesouraria"),
    CategoriaGerencial("tes_resgate", "Resgate (automático/CDB)", "tesouraria"),
    CategoriaGerencial("tes_rendimento", "Rendimento de aplicação", "tesouraria"),
    # transferências
    CategoriaGerencial("trf_entre_contas", "Transferência entre contas próprias", "transferencia"),
    # sem regra
    CategoriaGerencial("a_classificar_entrada", "Entrada a classificar", "a_classificar"),
    CategoriaGerencial("a_classificar_saida", "Saída a classificar", "a_classificar"),
)
# códigos antigos que já foram gravados no banco continuam resolvendo (sem aparecer
# nas listas da tela): "rec_delivery" virou iFood/99Food em 24/09/2026.
LEGADO = {"rec_delivery": "rec_ifood"}

CATEGORIAS = {c.codigo: c for c in _CATEGORIAS}
CATEGORIAS.update({antigo: CATEGORIAS[novo] for antigo, novo in LEGADO.items()})

CAMPOS_REGRA = {
    "contraparte_documento": "documento",
    "contraparte_nome": "nome",
    "descricao": "descrição",
}


@dataclass(frozen=True)
class Resultado:
    categoria: str
    revisar: bool = False
    regra: str = ""
    # linha do caixa (planilha) fixada pela regra: "este CNPJ e sempre Light"
    categoria_id: int | None = None

    @property
    def bloco(self) -> str:
        return CATEGORIAS[self.categoria].bloco


def normalizar(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "").upper()).strip()


def digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def chave_descricao(descricao) -> str:
    """Parte estável da descrição: sem números/datas, até 3 palavras."""
    return " ".join(re.sub(r"[\d/.\-]+", " ", normalizar(descricao)).split()[:3])


def chave_regra(mov) -> tuple[str, str]:
    """Campo/valor usados ao criar uma regra "sempre para esta contraparte"."""
    doc = digitos(mov.contraparte_documento)
    if doc:
        return "contraparte_documento", doc
    nome = normalizar(mov.contraparte)
    if nome:
        return "contraparte_nome", nome
    return "descricao", chave_descricao(mov.descricao)


def regra_casa(regra, mov) -> bool:
    if regra.tipo and regra.tipo != mov.tipo:
        return False
    if regra.campo == "contraparte_documento":
        alvo = digitos(regra.valor)
        return bool(alvo) and digitos(mov.contraparte_documento) == alvo
    alvo = normalizar(regra.valor)
    if not alvo:
        return False
    if regra.campo == "contraparte_nome":
        return alvo in normalizar(mov.contraparte)
    if regra.campo == "descricao":
        return alvo in normalizar(mov.descricao)
    return False


def classificar(mov, regras=(), cnpj_raiz_empresa: str = "") -> Resultado:
    for regra in regras:
        if regra.categoria in CATEGORIAS and regra_casa(regra, mov):
            return Resultado(regra.categoria, bool(regra.revisar),
                             f"regra: {CAMPOS_REGRA.get(regra.campo, regra.campo)} “{regra.valor}”",
                             getattr(regra, "categoria_id", None))
    return _padrao(mov, digitos(cnpj_raiz_empresa)[:8])


_ARTISTAS = re.compile(r"PRODUC\w* ARTIST|EDICOES MUSICA|MUSICAIS|MUSICOS")
_SERVICOS = re.compile(r"ADVOGAD|CONTABIL|CONSU\w*TORIA|ASSESSORIA")


def _padrao(mov, raiz: str) -> Resultado:
    desc = normalizar(mov.descricao)
    origem = normalizar(mov.origem)
    nome = normalizar(mov.contraparte)
    instituicao = normalizar(mov.contraparte_instituicao)
    doc = digitos(mov.contraparte_documento)
    credito = mov.tipo == "credito"
    pessoa_fisica, empresa = len(doc) == 11, len(doc) == 14

    # tesouraria: dinheiro que só muda de lugar
    if desc.startswith("APL APLIC"):
        return Resultado("tes_aplicacao", regra="aplicação automática")
    if desc.startswith("RES APLIC"):
        return Resultado("tes_resgate", regra="resgate automático")
    if desc.startswith("REND PAGO"):
        return Resultado("tes_rendimento", regra="rendimento de aplicação")
    if "CDB" in desc or "CDB" in origem:
        return Resultado("tes_resgate" if credito else "tes_aplicacao", regra="CDB")

    # contas do próprio CNPJ
    if raiz and empresa and doc[:8] == raiz:
        if credito and "STONE" in instituicao:
            return Resultado("rec_stone", regra="PIX da conta Stone do próprio CNPJ")
        return Resultado("trf_entre_contas", regra="mesmo CNPJ da empresa")

    if "IFOOD" in desc or "IFOOD" in nome:
        if credito:
            return Resultado("rec_ifood", regra="repasse do iFood")
        return Resultado("pag_outros", revisar=True, regra="débito do iFood")

    if "99 FOOD" in nome or "99FOOD" in nome or "99 FOOD" in desc or "99FOOD" in desc:
        if credito:
            return Resultado("rec_99food", regra="repasse do 99Food")
        return Resultado("pag_outros", revisar=True, regra="débito do 99Food")

    if credito:
        if pessoa_fisica:
            return Resultado("rec_pix_clientes", regra="crédito de pessoa física")
        return Resultado("a_classificar_entrada", revisar=True, regra="sem regra")

    # pagamentos
    if desc.startswith("SISPAG SALARIOS"):
        return Resultado("pag_folha", regra="SISPAG salários")
    if desc.startswith(("SISPAG TRIB", "DARF", "DAS ")) or origem == "TIC":
        return Resultado("pag_tributos", regra="guia de tributos")
    if "CEF MATRIZ" in nome or "CAIXA ECONOMICA FEDERAL" in nome:
        return Resultado("pag_encargos", regra="Caixa (FGTS)")
    if re.match(r"BUSINESS \d{4}-\d{4}", desc):
        return Resultado("pag_cartao", regra="fatura do cartão Business")
    if desc.startswith("DA "):
        return Resultado("pag_utilidades", regra="débito automático")
    if desc == "IOF" or desc.startswith(("IOF ", "TAR ", "TARIFA")):
        return Resultado("pag_tarifas", regra="tarifa/IOF")
    if _ARTISTAS.search(nome):
        return Resultado("pag_artistas", regra="produção artística")
    if _SERVICOS.search(nome):
        return Resultado("pag_servicos", regra="serviço profissional")
    if desc.startswith("BOLETO PAGO") or origem in ("DEBITO", "TIT"):
        return Resultado("pag_fornecedores", regra="boleto")
    if empresa:
        return Resultado("pag_fornecedores", regra="pagamento a empresa")
    return Resultado("a_classificar_saida", revisar=True,
                     regra="pagamento a pessoa física sem regra" if pessoa_fisica else "sem regra")


# ---- Aprendizado: categoria do fluxo (planilha) -> categoria gerencial ----
# Usado quando um movimento conciliado com a planilha ainda está "a classificar":
# o sistema aprende a contraparte e passa a classificar sozinho.
_POR_NOME = {
    "Pro Labore/Lucro": "pag_prolabore",
    "Pro Labore": "pag_prolabore",
    "Distribuição de Lucros": "soc_retirada",
    "RENDIMENTO": "tes_rendimento",
    "Resgate": "tes_resgate",
    "Dinheiro": "rec_outros",
    "Pix Itau": "rec_pix_clientes",
    "Pix Stone": "rec_stone",
    "Empréstimo": "fin_emprestimo_recebido",
    "Outros/Acertos": "rec_outros",
    "DAS": "pag_tributos",
    "ICMS": "pag_tributos",
    "Compras": "pag_fornecedores",
    "Salários": "pag_folha",
    "Músicos": "pag_artistas",
    "Técnico Som": "pag_artistas",
    "Segurança": "pag_servicos",
    "Aluguel": "pag_aluguel",
    "Condomínio": "pag_aluguel",
    "IPTU": "pag_tributos",
    "Manutenção/Obras/Equip.": "pag_manutencao",
    "Empréstimos Heitor": "fin_emprestimo_pagamento",
    "Reembolso Barbara": "pag_reembolso_socio",
    "Aplicação": "tes_aplicacao",
    "Despesas Bancárias": "pag_tarifas",
    "Dívidas Receita (Simples/PERT)": "pag_tributos",
    "FGTS": "pag_encargos",
    "Medicina Trabalho": "pag_servicos",
    "Contabilidade": "pag_servicos",
    "Jurídico": "pag_servicos",
    "Assessoria Financeira": "pag_servicos",
    "NixConsultoria": "pag_servicos",
    "IFOOD": "rec_ifood",
    "99 FOOD": "rec_99food",
    "Patrocínio": "rec_eventos",
}
_UTILIDADES = ("IGUA", "Light", "Gedisa (luz)", "CEG", "Net")
_POR_GRUPO = {
    "Vendas - Repasse Stone": "rec_stone",
    "Pix Itau": "rec_pix_clientes",
    "IFOOD": "rec_ifood",
    "99 FOOD": "rec_99food",
    "Dinheiro": "rec_outros",
    "Patrocínio": "rec_eventos",
    "Empréstimo": "fin_emprestimo_recebido",
    "Resgate": "tes_resgate",
    "RENDIMENTO": "tes_rendimento",
    "Outros/Acertos": "rec_outros",
    "Compras": "pag_fornecedores",
    "Folha/Salários": "pag_folha",
    "Demais Salários": "pag_folha",
    "Impostos": "pag_tributos",
    "Despesas Fixas": "pag_outros",
    "Outras despesas": "pag_outros",
}
_POR_NOME_NORM = {normalizar(k): v for k, v in _POR_NOME.items()}
_UTILIDADES_NORM = {normalizar(u) for u in _UTILIDADES}


def categoria_do_lancamento(grupo: str, nome: str) -> str | None:
    """Categoria gerencial equivalente à categoria da planilha (ou None)."""
    chave = normalizar(nome)
    if chave in _POR_NOME_NORM:
        return _POR_NOME_NORM[chave]
    if chave.startswith("CARTAO DE CREDITO") or chave.startswith("CARTÃO DE CRÉDITO"):
        return "pag_cartao"
    if chave in _UTILIDADES_NORM:
        return "pag_utilidades"
    return _POR_GRUPO.get(grupo)


def categorias_por_bloco() -> list[tuple[str, list[CategoriaGerencial]]]:
    return [(nome, [c for c in _CATEGORIAS if c.bloco == codigo]) for codigo, nome in BLOCOS.items()]
