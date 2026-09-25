# -*- coding: utf-8 -*-
"""Retirada da sócia: separar pró-labore de distribuição de lucros."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.banco import MovimentoBancario
from app.models.fluxo import Categoria, Lancamento
from app.services import itau_fluxo


def _pagamento(dia: date, valor: str, id_externo: str) -> MovimentoBancario:
    mov = MovimentoBancario(
        banco="itau", conta="1234500123456", id_externo=id_externo, data=dia, tipo="debito",
        valor=Decimal(valor), descricao="PIX ENVIADO BARBARA MEND",
        contraparte="BARBARA MENDES GONCALVES SALLABERRY",
        categoria_gerencial="soc_a_detalhar", bloco="socios",
    )
    db.session.add(mov)
    db.session.commit()
    return mov


def _por_categoria() -> dict:
    linhas = db.session.execute(
        db.select(Categoria.nome, db.func.sum(Lancamento.valor))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id)
        .group_by(Categoria.nome)
    ).all()
    return {nome: Decimal(total) for nome, total in linhas}


def test_setembro_separa_pro_labore_e_lucros(app):
    """Set/26 real: 15.000 em 01/09 e 5.000 + 5.000 em 15/09 = 25.000."""
    _pagamento(date(2026, 9, 1), "15000.00", "a")
    _pagamento(date(2026, 9, 15), "5000.00", "b")
    _pagamento(date(2026, 9, 15), "5000.00", "c")

    rel = itau_fluxo.importar_socios(date(2026, 9, 1), date(2026, 9, 30))
    db.session.commit()

    assert rel["pro_labore"] == Decimal("8475.55")
    assert rel["lucros"] == Decimal("16524.45")      # confere com a tabela da consultoria
    assert _por_categoria() == {"Pro Labore": Decimal("8475.55"),
                                "Distribuição de Lucros": Decimal("16524.45")}

    # o primeiro pagamento vira dois lançamentos, na data em que saiu do banco
    do_dia_1 = db.session.execute(
        db.select(Lancamento).filter_by(data=date(2026, 9, 1))
    ).scalars().all()
    assert sorted(l.valor for l in do_dia_1) == [Decimal("6524.45"), Decimal("8475.55")]

    # e já nascem conciliados com o movimento que os gerou
    mov = db.session.execute(
        db.select(MovimentoBancario).filter_by(id_externo="a")
    ).scalar_one()
    assert len(mov.itens_conciliacao) == 2 and mov.diferenca_conciliacao == Decimal("0.00")


def test_pro_labore_muda_de_valor_em_marco(app):
    _pagamento(date(2026, 2, 2), "20000.00", "f1")
    _pagamento(date(2026, 2, 18), "10000.00", "f2")
    rel_fev = itau_fluxo.importar_socios(date(2026, 2, 1), date(2026, 2, 28))
    db.session.commit()
    assert rel_fev["pro_labore"] == Decimal("8157.41")      # vigência antiga
    assert rel_fev["lucros"] == Decimal("21842.59")         # igual à tabela da consultoria

    _pagamento(date(2026, 3, 2), "25000.00", "m1")
    rel_mar = itau_fluxo.importar_socios(date(2026, 3, 1), date(2026, 3, 31))
    db.session.commit()
    assert rel_mar["pro_labore"] == Decimal("8475.55")
    assert rel_mar["lucros"] == Decimal("16524.45")


def test_mes_menor_que_o_pro_labore_vira_tudo_pro_labore(app):
    """Abr/26: pagou 3.828,02, menos que o pró-labore — nada de lucro."""
    _pagamento(date(2026, 4, 1), "3828.02", "abr")
    rel = itau_fluxo.importar_socios(date(2026, 4, 1), date(2026, 4, 30))
    db.session.commit()
    assert rel["pro_labore"] == Decimal("3828.02") and rel["lucros"] == Decimal("0.00")
    assert _por_categoria() == {"Pro Labore": Decimal("3828.02")}


def test_substitui_a_linha_da_planilha_e_nao_duplica(app):
    antiga = Categoria(nome="Pro Labore/Lucro", tipo="saida", grupo="Demais Salários")
    db.session.add(antiga)
    db.session.flush()
    db.session.add(Lancamento(data=date(2026, 9, 1), categoria_id=antiga.id,
                              valor=Decimal("15000.00"), origem="manual"))
    _pagamento(date(2026, 9, 1), "15000.00", "s1")
    db.session.commit()

    rel = itau_fluxo.importar_socios(date(2026, 9, 1), date(2026, 9, 30))
    db.session.commit()
    assert rel["removidos"] == 1
    assert db.session.execute(
        db.select(Lancamento).filter_by(origem="manual")
    ).scalars().all() == []

    # rodar de novo não duplica
    rel2 = itau_fluxo.importar_socios(date(2026, 9, 1), date(2026, 9, 30))
    db.session.commit()
    assert rel2["inseridos"] == 0 and rel2["atualizados"] == 2
    assert db.session.query(Lancamento).count() == 2


# ---------------- de-para do extrato: o caixa nasce do banco ----------------

def _mov(id_externo, valor, categoria, tipo="debito", desc="PAGAMENTO", contraparte=None,
         dia=date(2026, 9, 10), documento=None):
    m = MovimentoBancario(banco="itau", conta="1234500123456", id_externo=id_externo, data=dia,
                          tipo=tipo, valor=Decimal(valor), descricao=desc, contraparte=contraparte,
                          contraparte_documento=documento,
                          categoria_gerencial=categoria, bloco="operacional")
    db.session.add(m)
    db.session.commit()
    return m


def _categoria(nome, tipo, grupo="Despesas Fixas"):
    c = Categoria(nome=nome, tipo=tipo, grupo=grupo)
    db.session.add(c)
    db.session.commit()
    return c


def test_cada_categoria_do_banco_vira_a_linha_certa(app):
    _categoria("Compras", "saida", "Compras")
    _categoria("Salários", "saida", "Folha/Salários")
    _categoria("Light", "saida")
    _categoria("Pix Itau", "entrada", "Pix Itau")

    _mov("f1", "500.00", "pag_fornecedores", contraparte="AMBEV")
    _mov("f2", "3000.00", "pag_folha", desc="SISPAG SALARIOS")
    _mov("f3", "800.00", "pag_utilidades", contraparte="LIGHT SERVICOS DE ELETRICIDADE")
    _mov("f4", "120.00", "rec_pix_clientes", tipo="credito", contraparte="CLIENTE X")

    rel = itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()

    por_categoria = {nome: total for nome, total in db.session.execute(
        db.select(Categoria.nome, db.func.sum(Lancamento.valor))
        .join(Lancamento, Lancamento.categoria_id == Categoria.id).group_by(Categoria.nome))}
    assert por_categoria == {"Compras": Decimal("500.00"), "Salários": Decimal("3000.00"),
                             "Light": Decimal("800.00"), "Pix Itau": Decimal("120.00")}
    assert rel["criados"] == 4


def test_movimento_sem_traducao_entra_em_a_classificar(app):
    """O caixa não pode ficar com buraco: entra na linha-ônibus e fica para revisão."""
    mov = _mov("x1", "250.00", "a_classificar_saida", desc="PIX ENVIADO FULANO",
               contraparte="FULANO DE TAL")
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()

    lanc = db.session.execute(db.select(Lancamento)).scalar_one()
    assert lanc.categoria.nome == "A classificar (saídas)" and lanc.valor == Decimal("250.00")
    assert db.session.get(MovimentoBancario, mov.id).revisar is True


def test_repasse_da_stone_nao_vira_lancamento(app):
    """O crédito do repasse já entrou pela Stone; lançar de novo contaria em dobro."""
    _mov("s1", "10659.72", "rec_stone", tipo="credito", desc="PIX TRANSF BANGALO17/09",
         contraparte="BANGALO")
    rel = itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    assert rel["criados"] == 0
    assert db.session.query(Lancamento).filter_by(origem="itau", origem_id="1:mov").count() == 0


def test_pix_da_stone_entra_pela_diferenca_do_repasse(app):
    """O arquivo da Stone não traz PIX; a diferença do crédito consolidado é ele."""
    _categoria("Pix Stone", "entrada", "Vendas - Repasse Stone")
    visa = _categoria("Visa Crédito", "entrada", "Vendas - Repasse Stone")
    dia = date(2026, 9, 17)
    db.session.add(Lancamento(data=dia, categoria_id=visa.id, valor=Decimal("8626.33"),
                              origem="stone", origem_id="pg:1"))
    _mov("st", "10659.72", "rec_stone", tipo="credito", desc="PIX TRANSF BANGALO17/09", dia=dia)

    rel = itau_fluxo.importar_periodo(dia, dia, substituir_planilha=False)
    db.session.commit()

    pix = db.session.execute(
        db.select(Lancamento).filter(Lancamento.origem_id.like("%:pix"))
    ).scalar_one()
    assert pix.valor == Decimal("2033.39")     # 10.659,72 - 8.626,33
    assert rel["pix_stone"]["dias"] == 1


def test_repasse_por_bandeira_nao_gera_pix(app):
    """Quando a Stone paga por bandeira, o crédito casa com o arquivo: PIX é zero."""
    _categoria("Pix Stone", "entrada", "Vendas - Repasse Stone")
    visa = _categoria("Visa Crédito", "entrada", "Vendas - Repasse Stone")
    dia = date(2026, 1, 21)
    db.session.add(Lancamento(data=dia, categoria_id=visa.id, valor=Decimal("11962.11"),
                              origem="stone", origem_id="pg:9"))
    _mov("b1", "25804.91", "rec_outros", tipo="credito", desc="STONE  VISA CD0181345124", dia=dia)

    rel = itau_fluxo.importar_periodo(dia, dia, substituir_planilha=False)
    db.session.commit()
    assert rel["pix_stone"]["dias"] == 0
    assert db.session.execute(
        db.select(Lancamento).filter(Lancamento.origem_id.like("%:pix"))
    ).scalars().all() == []


def test_classificacao_da_manu_move_o_lancamento(app):
    """Ciclo de aprendizado: classificou o movimento, o lançamento vai junto."""
    from app.services import itau_sync

    _categoria("Músicos", "saida", "Demais Salários")
    mov = _mov("q1", "700.00", "a_classificar_saida", desc="PIX ENVIADO MARVIO",
               contraparte="MARVIO FERREIRA")
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    assert db.session.execute(db.select(Lancamento)).scalar_one().categoria.nome == "A classificar (saídas)"

    itau_sync.revisar_movimento(mov, "pag_artistas")
    db.session.commit()

    lanc = db.session.execute(db.select(Lancamento)).scalar_one()
    assert lanc.categoria.nome == "Músicos" and lanc.valor == Decimal("700.00")


def test_manu_liga_o_pagamento_ao_fornecedor_e_a_regra_vale_para_os_proximos(app):
    """O caso da Ambev: pagamento a "CRBS" vira fornecedor Ambev, e os próximos também."""
    from app.models.banco import RegraClassificacaoBancaria
    from app.models.fluxo import Fornecedor
    from app.services import itau_sync

    _categoria("Compras", "saida", "Compras")
    ambev = Fornecedor(nome="Ambev")
    db.session.add(ambev)
    db.session.commit()

    mov = _mov("amb1", "500.00", "a_classificar_saida", desc="PIX ENVIADO CRBS",
               contraparte="CRBS S A", dia=date(2026, 9, 10))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    assert db.session.execute(db.select(Lancamento)).scalar_one().categoria.nome == "A classificar (saídas)"

    # a Manu revisa: é fornecedor, é a Ambev, e vale como regra
    itau_sync.revisar_movimento(mov, "pag_fornecedores", criar_regra=True, fornecedor_id=ambev.id)
    db.session.commit()

    lanc = db.session.execute(db.select(Lancamento)).scalar_one()
    assert lanc.categoria.nome == "Compras" and lanc.fornecedor_id == ambev.id
    regra = db.session.execute(db.select(RegraClassificacaoBancaria)).scalar_one()
    assert regra.fornecedor_id == ambev.id and regra.categoria == "pag_fornecedores"

    # o próximo pagamento à mesma empresa já entra na Ambev, sem ninguém tocar
    novo = _mov("amb2", "780.00", "pag_fornecedores", desc="PIX ENVIADO CRBS",
                contraparte="CRBS S A", dia=date(2026, 9, 20))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()

    lanc_novo = db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{novo.id}:mov")
    ).scalar_one()
    assert lanc_novo.fornecedor_id == ambev.id and lanc_novo.valor == Decimal("780.00")


def test_regra_de_fornecedor_alcanca_movimentos_anteriores(app):
    """A regra criada hoje resolve tambem o pagamento ja importado da semana passada."""
    from app.models.fluxo import Fornecedor
    from app.services import itau_sync

    _categoria("Compras", "saida", "Compras")
    ambev = Fornecedor(nome="Ambev")
    db.session.add(ambev)
    db.session.commit()

    # o nome no extrato nao lembra o cadastro: so a regra liga os dois
    antigo = _mov("crbs1", "252.48", "pag_fornecedores", desc="BOLETO  PAGO CRBS",
                  contraparte="CRBS S A", documento="56228356000158", dia=date(2026, 9, 18))
    novo = _mov("crbs2", "697.89", "pag_fornecedores", desc="BOLETO  PAGO CRBS",
                contraparte="CRBS S A", documento="56228356000158", dia=date(2026, 9, 21))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    lanc = lambda mov: db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{mov.id}:mov")).scalar_one()
    assert lanc(antigo).fornecedor_id is None

    # a Manu revisa so o de 21/09 e manda valer como regra
    alterados = itau_sync.revisar_movimento(novo, "pag_fornecedores", criar_regra=True,
                                            fornecedor_id=ambev.id)
    db.session.commit()

    assert alterados == 2
    assert lanc(novo).fornecedor_id == ambev.id
    assert lanc(antigo).fornecedor_id == ambev.id   # sem esperar o proximo sync
    assert lanc(antigo).categoria.nome == "Compras"


def test_revisar_escolhendo_a_linha_da_planilha(app):
    """A tela fala a lingua da planilha: escolher "Light" leva o lancamento para la."""
    from app.models.fluxo import Categoria
    from app.services import itau_sync

    _categoria("Light", "saida", "Despesas Fixas")
    _categoria("Net", "saida", "Despesas Fixas")
    mov = _mov("luz1", "930.00", "a_classificar_saida", desc="DA CONCESSIONARIA",
               contraparte="DISTRIBUIDORA DE ENERGIA", dia=date(2026, 9, 10))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    lanc = lambda: db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{mov.id}:mov")).scalar_one()
    assert lanc().categoria.nome == "A classificar (saídas)"

    light = db.session.execute(db.select(Categoria).filter_by(nome="Light", tipo="saida")).scalar_one()
    itau_sync.revisar_movimento(mov, criar_regra=False, categoria_id=light.id)
    db.session.commit()

    assert lanc().categoria.nome == "Light"
    # a categoria gerencial (DRE, blocos) vem derivada da linha escolhida
    assert mov.categoria_gerencial == "pag_utilidades"
    assert mov.categoria_id == light.id and mov.revisado and mov.classificacao_manual

    # trocar so a linha, dentro do mesmo grupo, tambem vale
    net = db.session.execute(db.select(Categoria).filter_by(nome="Net", tipo="saida")).scalar_one()
    itau_sync.revisar_movimento(mov, criar_regra=False, categoria_id=net.id)
    db.session.commit()
    assert lanc().categoria.nome == "Net"


def test_linha_da_planilha_precisa_bater_com_o_tipo(app):
    """Nao da para mandar um credito para uma linha de saida."""
    import pytest

    from app.models.fluxo import Categoria
    from app.services import itau_sync

    _categoria("Light", "saida", "Despesas Fixas")
    mov = _mov("cred1", "100.00", "a_classificar_entrada", tipo="credito",
               desc="PIX RECEBIDO", dia=date(2026, 9, 11))
    light = db.session.execute(db.select(Categoria).filter_by(nome="Light", tipo="saida")).scalar_one()
    with pytest.raises(ValueError, match="linha de saida|linha de saída"):
        itau_sync.revisar_movimento(mov, criar_regra=False, categoria_id=light.id)


def test_regra_guarda_a_linha_da_planilha(app):
    """Escolher a linha + criar regra: os proximos ja chegam naquela linha."""
    from app.models.banco import RegraClassificacaoBancaria
    from app.models.fluxo import Categoria
    from app.services import itau_sync

    _categoria("Light", "saida", "Despesas Fixas")
    mov = _mov("luz2", "800.00", "a_classificar_saida", desc="DA ENERGIA",
               contraparte="ENEL RIO", documento="33050071000158", dia=date(2026, 9, 12))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()

    light = db.session.execute(db.select(Categoria).filter_by(nome="Light", tipo="saida")).scalar_one()
    itau_sync.revisar_movimento(mov, criar_regra=True, categoria_id=light.id)
    db.session.commit()
    regra = db.session.execute(db.select(RegraClassificacaoBancaria)).scalar_one()
    assert regra.categoria_id == light.id and regra.categoria == "pag_utilidades"

    novo = _mov("luz3", "770.00", "a_classificar_saida", desc="DA ENERGIA",
                contraparte="ENEL RIO", documento="33050071000158", dia=date(2026, 9, 22))
    itau_sync.reaplicar_regras()
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    lanc_novo = db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{novo.id}:mov")).scalar_one()
    assert lanc_novo.categoria.nome == "Light"


def test_linha_manual_nao_duplica_credito_da_stone(app):
    """Escolher uma linha para o repasse da Stone nao pode lancar de novo."""
    from app.models.fluxo import Categoria

    _categoria("Visa Crédito", "entrada", "Vendas - Repasse Stone")
    visa = db.session.execute(
        db.select(Categoria).filter_by(nome="Visa Crédito", tipo="entrada")).scalar_one()
    mov = _mov("stn1", "10659.72", "rec_stone", tipo="credito",
               desc="PIX TRANSF BANGALO", contraparte="STONE INSTITUICAO DE PAGAMENTO",
               dia=date(2026, 9, 17))
    mov.categoria_id = visa.id
    db.session.commit()

    nome, motivo, fora = itau_fluxo.destino(mov)
    assert (nome, fora) == (None, True) and "Stone" in motivo
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    assert db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{mov.id}:mov")).scalar_one_or_none() is None


def test_regra_deixa_os_irmaos_classificados_e_revisados(app):
    """O caso da Solucao Ambiental: criar a regra num tira o outro da fila."""
    from app.models.fluxo import Categoria
    from app.services import itau_sync

    _categoria("Força Ambiental", "saida", "Despesas Fixas")
    forca = db.session.execute(
        db.select(Categoria).filter_by(nome="Força Ambiental", tipo="saida")).scalar_one()
    primeiro = _mov("sa1", "786.48", "a_classificar_saida", desc="BOLETO PAGO SOLUCAO AMBI",
                    contraparte="SOLUCAO AMBIENTAL E SERVICOS LTDA",
                    documento="20082362000186", dia=date(2026, 9, 21))
    segundo = _mov("sa2", "786.48", "a_classificar_saida", desc="BOLETO PAGO SOLUCAO AMBI",
                   contraparte="SOLUCAO AMBIENTAL E SERVICOS LTDA",
                   documento="20082362000186", dia=date(2026, 9, 21))
    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 30), substituir_planilha=False)
    db.session.commit()
    assert (segundo.revisado, segundo.revisar) == (False, True)

    itau_sync.revisar_movimento(primeiro, criar_regra=True, categoria_id=forca.id)
    db.session.commit()

    # o irmão sai da fila junto: a regra é decisão de gente
    assert segundo.revisado and not segundo.revisar
    assert segundo.categoria_id == forca.id
    lanc = db.session.execute(
        db.select(Lancamento).filter_by(origem_id=f"{segundo.id}:mov")).scalar_one()
    assert lanc.categoria.nome == "Força Ambiental"

    # e a tela sabe reabrir os dois com "criar ou atualizar regra" marcado
    achados = itau_sync.regras_dos_movimentos([primeiro, segundo])
    assert set(achados) == {primeiro.id, segundo.id}


def test_dia_coberto_pelo_extrato_perde_a_linha_da_planilha(app):
    """A compra prevista na planilha some quando o pagamento real chega do banco."""
    from app.models.fluxo import Categoria

    _categoria("Compras", "saida", "Compras")
    _categoria("Dinheiro", "entrada", "Dinheiro")
    compras = db.session.execute(
        db.select(Categoria).filter_by(nome="Compras", tipo="saida")).scalar_one()
    dinheiro = db.session.execute(
        db.select(Categoria).filter_by(nome="Dinheiro", tipo="entrada")).scalar_one()

    # o que veio da planilha: a compra prevista e o dinheiro lançado pela Manu
    db.session.add_all([
        Lancamento(data=date(2026, 9, 24), categoria_id=compras.id,
                   valor=Decimal("231.42"), origem="manual"),
        Lancamento(data=date(2026, 9, 24), categoria_id=dinheiro.id,
                   valor=Decimal("500.00"), origem="manual"),
        Lancamento(data=date(2026, 9, 30), categoria_id=compras.id,
                   valor=Decimal("999.00"), origem="manual"),   # dia futuro: fica
    ])
    _mov("ume9", "231.42", "pag_fornecedores", desc="BOLETO PAGO UMEHARA",
         contraparte="UMEHARA ALIMENTOS LTDA", dia=date(2026, 9, 24))
    db.session.commit()

    itau_fluxo.importar_periodo(date(2026, 9, 1), date(2026, 9, 24), substituir_planilha=True)
    db.session.commit()

    manuais = db.session.execute(
        db.select(Lancamento).filter_by(origem="manual").order_by(Lancamento.data)).scalars().all()
    # some a compra prevista do dia coberto; o dinheiro e o dia futuro permanecem
    assert [(l.data, l.valor) for l in manuais] == [
        (date(2026, 9, 24), Decimal("500.00")), (date(2026, 9, 30), Decimal("999.00"))]
    # e a compra do dia passa a vir do banco, uma vez só
    do_banco = db.session.execute(
        db.select(Lancamento).filter_by(origem="itau", data=date(2026, 9, 24))).scalars().all()
    assert [l.valor for l in do_banco] == [Decimal("231.42")]
