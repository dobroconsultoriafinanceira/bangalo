# -*- coding: utf-8 -*-
"""Classificação gerencial do extrato (funções puras)."""
from dataclasses import dataclass

from app.services import classificacao_bancaria as cls

RAIZ = "07754838"


@dataclass
class M:
    tipo: str
    descricao: str = ""
    origem: str | None = None
    contraparte: str | None = None
    contraparte_documento: str | None = None
    contraparte_instituicao: str | None = None


@dataclass
class Regra:
    campo: str
    valor: str
    categoria: str
    tipo: str | None = None
    revisar: bool = False


def _cat(mov, regras=()):
    return cls.classificar(mov, regras, RAIZ).categoria


def test_todas_as_categorias_tem_bloco_valido():
    assert all(c.bloco in cls.BLOCOS for c in cls.CATEGORIAS.values())
    # os códigos legados (ex.: rec_delivery) resolvem, mas não aparecem nas listas
    assert sum(len(cats) for _, cats in cls.categorias_por_bloco()) == len(cls.CATEGORIAS) - len(cls.LEGADO)


def test_tesouraria_fica_fora_da_operacao():
    assert _cat(M("debito", "APL APLIC AUT MAIS")) == "tes_aplicacao"
    assert _cat(M("debito", "APL APLIC AUT MAIS AP")) == "tes_aplicacao"
    assert _cat(M("credito", "RES APLIC AUT MAIS")) == "tes_resgate"
    assert _cat(M("credito", "REND PAGO APLIC AUT MAIS")) == "tes_rendimento"
    assert _cat(M("credito", "RESGATE   CDB DI", origem="ION_CDB_RESGATE")) == "tes_resgate"
    assert cls.classificar(M("debito", "APL APLIC AUT MAIS")).bloco == "tesouraria"


def test_conta_propria_stone_e_transferencias():
    stone = M("credito", "PIX TRANSF BANGALO15/09", "PIX_RECEPCAO", "BANGALO", "07754838000190", "STONE IP S.A.")
    assert _cat(stone) == "rec_stone"
    assert cls.classificar(stone, (), RAIZ).bloco == "operacional"
    outra = M("credito", "PIX TRANSF BANGALO", "PIX_RECEPCAO", "BANGALO", "07754838000271", "BCO BRADESCO S.A.")
    assert _cat(outra) == "trf_entre_contas"
    assert _cat(M("debito", "PIX ENVIADO BANGALO", "PIX_EMISSAO", "BANGALO", "07754838000190")) == "trf_entre_contas"
    # sem CNPJ da empresa configurado, não presume conta própria
    assert cls.classificar(stone, (), "").categoria == "a_classificar_entrada"


def test_regras_padrao_da_operacao():
    empresa = "12345678000199"
    casos = [
        (M("credito", "IFOOD MAST CD15/09", "TEF_SISPAG_SI", "IFOOD.COM AGENCIA", "14380200000121"), "rec_ifood"),
        (M("credito", "PIX TRANSF 99", "PIX_RECEPCAO", "99 FOOD LTDA", "60112920000100"), "rec_99food"),
        (M("credito", "PIX TRANSF JOAO", "PIX_RECEPCAO", "JOAO", "12345678901"), "rec_pix_clientes"),
        (M("debito", "SISPAG SALARIOS"), "pag_folha"),
        (M("debito", "SISPAG TRIB COD BARRAS", "TIC"), "pag_tributos"),
        (M("debito", "SISPAG TRIB MUNICIPAL", "TIC"), "pag_tributos"),
        (M("debito", "PIX ENVIADO CEF", "PIX_EMISSAO", "CEF MATRIZ", "00360305000104"), "pag_encargos"),
        (M("debito", "BUSINESS      4004-9080"), "pag_cartao"),
        (M("debito", "DA  IGUA RJ CAPITA 41987"), "pag_utilidades"),
        (M("debito", "IOF"), "pag_tarifas"),
        (M("debito", "BOLETO PAGO UMEHARA ALIM", "DEBITO"), "pag_fornecedores"),
        (M("debito", "PIX ENVIADO MMD", "PIX_EMISSAO", "MMD PRODUCOES ARTISTICAS", empresa), "pag_artistas"),
        (M("debito", "PIX ENVIADO INCAND", "PIX_EMISSAO", "INCANDESCENTE EDICOES MUSICAIS", empresa), "pag_artistas"),
        (M("debito", "PIX ENVIADO ANTUNES", "PIX_EMISSAO", "ANTUNES E CIRAUDO SOCIEDADE DE ADVOGADOS", empresa), "pag_servicos"),
        (M("debito", "PIX ENVIADO SMART", "PIX_EMISSAO", "SMART CMV CONSUTORIA E GESTAO LTDA", empresa), "pag_servicos"),
        (M("debito", "PIX ENVIADO C.C.M.", "PIX_EMISSAO", "C.C.M. COMERCIO E DISTRIBUIDORA LTDA", empresa), "pag_fornecedores"),
    ]
    for mov, esperado in casos:
        assert _cat(mov) == esperado, mov


def test_sem_regra_vai_para_revisao():
    r = cls.classificar(M("debito", "PIX ENVIADO MARVIO", "PIX_EMISSAO", "MARVIO FERREIRA", "12345678901"), (), RAIZ)
    assert (r.categoria, r.revisar, r.bloco) == ("a_classificar_saida", True, "a_classificar")
    r = cls.classificar(M("credito", "SISPAG DEFENDER 09", "PIX_RECEPCAO", "DEFENDER", "13637628000100"), (), RAIZ)
    assert (r.categoria, r.revisar) == ("a_classificar_entrada", True)


def test_regra_do_usuario_tem_prioridade_e_respeita_tipo():
    barbara = M("debito", "PIX ENVIADO BARBARA", "PIX_EMISSAO", "BARBARA MENDES GONCALVES SALLABERRY", "12345678901")
    regras = [Regra("contraparte_nome", "Barbara Mendes Goncalves", "soc_a_detalhar", revisar=True)]
    r = cls.classificar(barbara, regras, RAIZ)
    assert (r.categoria, r.revisar) == ("soc_a_detalhar", True)
    assert "nome" in r.regra

    por_documento = [Regra("contraparte_documento", "123.456.789-01", "pag_prolabore", tipo="credito")]
    assert _cat(M("debito", "X", contraparte_documento="12345678901"), por_documento) == "a_classificar_saida"
    assert _cat(M("credito", "X", contraparte_documento="12345678901"), por_documento) == "pag_prolabore"

    por_descricao = [Regra("descricao", "BUSINESS 4004", "pag_outros")]
    assert _cat(M("debito", "BUSINESS      4004-9080"), por_descricao) == "pag_outros"
    # categoria desconhecida na regra é ignorada
    assert _cat(M("debito", "BUSINESS      4004-9080"), [Regra("descricao", "BUSINESS", "xpto")]) == "pag_cartao"


def test_chave_para_criar_regra():
    assert cls.chave_regra(M("debito", "PIX ENVIADO X", contraparte="Fulano", contraparte_documento="123.456.789-01")) \
        == ("contraparte_documento", "12345678901")
    assert cls.chave_regra(M("debito", "PIX ENVIADO X", contraparte="Fulano de Tal")) == ("contraparte_nome", "FULANO DE TAL")
    assert cls.chave_regra(M("debito", "BUSINESS      4004-9080")) == ("descricao", "BUSINESS")
    assert cls.chave_descricao("PIX TRANSF BANGALO15/09") == "PIX TRANSF BANGALO"
