# -*- coding: utf-8 -*-
"""Sincronização Itaú: gravação idempotente, classificação CFO, conciliação e telas."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.importers import itau_adapter as itau
from app.models.banco import MovimentoBancario, RegraClassificacaoBancaria, SaldoBancario
from app.models.fluxo import Categoria, Lancamento
from app.models.usuario import Usuario
from app.services import itau_sync
from app.utils.filtros import format_brl

CONTA = "123400123456"
CFG = itau.ItauConfig(contas=["1234-12345-6"])
JULHO = (date(2026, 7, 1), date(2026, 7, 31))
RAIZ = "11222333"


@pytest.fixture(autouse=True)
def _cnpj_da_empresa(app):
    app.config["EMPRESA_CNPJ_RAIZ"] = RAIZ


class _ClienteFake:
    def __init__(self, txns, saldo="1500.25"):
        self.txns = txns
        self._saldo = Decimal(saldo)
        self.chamadas = []

    def extrato(self, conta, inicio, fim=None):
        self.chamadas.append((conta, inicio, fim))
        return [t for t in self.txns if t.data >= inicio and (fim is None or t.data <= fim)]

    def saldo(self, conta):
        return itau.SaldoConta(conta=conta, data=date(2026, 7, 15), saldo=self._saldo,
                               saldo_bloqueado=Decimal("0.00"),
                               saldo_aplicacao_automatica=Decimal("300.50"))


def _tx(id_, dia, valor, tipo, descricao="PIX", **extra):
    return itau.TransacaoBancaria(id=id_, conta=CONTA, data=date(2026, 7, dia),
                                  valor=Decimal(valor), tipo=tipo, descricao=descricao, **extra)


def _seed():
    """Lançamentos como os que a equipe digita na tela: têm autor e sobrevivem
    à limpeza que o sync faz nas linhas importadas da planilha."""
    autor = Usuario(nome="Manu", email="manu@x.com", role="gerencia")
    autor.definir_senha("senha1234")
    ent = Categoria(nome="PIX", tipo="entrada", grupo="Vendas - Repasse Stone")
    sai = Categoria(nome="Aluguel", tipo="saida", grupo="Despesas Fixas")
    db.session.add_all([autor, ent, sai])
    db.session.flush()
    l_ent = Lancamento(data=date(2026, 7, 2), categoria_id=ent.id, valor=Decimal("100.00"),
                       usuario_id=autor.id)
    l_sai = Lancamento(data=date(2026, 7, 5), categoria_id=sai.id, valor=Decimal("50.00"),
                       usuario_id=autor.id)
    l_sem = Lancamento(data=date(2026, 7, 6), categoria_id=sai.id, valor=Decimal("77.00"),
                       usuario_id=autor.id)
    db.session.add_all([l_ent, l_sai, l_sem])
    db.session.commit()
    return l_ent, l_sai, l_sem


def _login(client, role="consultoria"):
    u = Usuario(nome="U", email="u@x.com", role=role)
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "u@x.com", "senha": "senha1234"})


def _movs():
    return {m.id_externo: m for m in db.session.execute(db.select(MovimentoBancario)).scalars()}


EXTRATO_JULHO = [
    _tx("a", 1, "100.00", itau.CREDITO, "PIX TRANSF CLIENTE", contraparte_documento="12345678901"),
    _tx("b", 4, "50.00", itau.DEBITO, "BOLETO PAGO ALUGUEL", origem="DEBITO"),
    _tx("c", 10, "999.00", itau.DEBITO, "PIX ENVIADO FULANO", contraparte="FULANO",
        contraparte_documento="98765432100"),
    _tx("d", 3, "5000.00", itau.CREDITO, "PIX TRANSF BANGALO03/07", contraparte="BANGALO",
        contraparte_documento=RAIZ + "000199", contraparte_instituicao="STONE IP S.A."),
    _tx("e", 5, "77.00", itau.DEBITO, "APL APLIC AUT MAIS"),   # mesmo valor do aluguel l_sem
    _tx("f", 6, "77.00", itau.CREDITO, "RES APLIC AUT MAIS"),
]


def test_sincronizar_grava_classifica_concilia_e_e_idempotente(app):
    l_ent, l_sai, _ = _seed()
    cli = _ClienteFake(EXTRATO_JULHO)
    rel = itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()

    assert cli.chamadas[0][0] == CONTA
    assert (rel["inseridos"], rel["atualizados"]) == (6, 0)
    # tesouraria não concilia (a aplicação de 77 não casa com o aluguel de 77)
    assert rel["conciliacao"] == {"conciliados": 2, "em_grupo": 0, "pendentes": 2}
    movs = _movs()
    assert movs["a"].lancamento == l_ent
    assert movs["b"].lancamento == l_sai
    assert movs["e"].lancamento is None
    assert {k: (m.categoria_gerencial, m.bloco, m.revisar) for k, m in movs.items()} == {
        "a": ("rec_pix_clientes", "operacional", False),
        "b": ("pag_fornecedores", "operacional", False),
        "c": ("a_classificar_saida", "a_classificar", True),
        "d": ("rec_stone", "operacional", False),
        "e": ("tes_aplicacao", "tesouraria", False),
        "f": ("tes_resgate", "tesouraria", False),
    }

    # reimportar não duplica nem desfaz a conciliação
    rel2 = itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()
    assert (rel2["inseridos"], rel2["atualizados"]) == (0, 0)
    assert db.session.query(MovimentoBancario).count() == 6
    assert db.session.get(MovimentoBancario, movs["a"].id).lancamento == l_ent

    assert db.session.query(SaldoBancario).count() == 2
    assert itau_sync.ultimo_saldo().disponivel == Decimal("1500.25")
    assert itau_sync.ultima_sincronizacao() is not None

    demo = itau_sync.demonstrativo(*JULHO)
    assert [b["codigo"] for b in demo["blocos"]] == ["operacional", "tesouraria", "a_classificar"]
    assert (demo["recebimentos"], demo["pagamentos"], demo["geracao_operacional"]) == (
        Decimal("5100.00"), Decimal("50.00"), Decimal("5050.00"))
    assert demo["por_bloco"]["tesouraria"]["liquido"] == Decimal("0.00")
    assert demo["variacao_conta"] == Decimal("4051.00")
    assert demo["revisar"] == 1
    assert itau_sync.totais_periodo(*JULHO) == {"recebimentos": Decimal("5100.00"),
                                                "pagamentos": Decimal("50.00")}
    # desde 23/09/2026 o extrato alimenta o caixa: o movimento com tradução vira
    # lançamento na hora da sincronização e já nasce conciliado
        # o repasse da Stone sem arquivo do dia fica fora de toda a tela
    assert itau_sync.resumo(*JULHO) == {"conciliaveis": 3, "conciliados": 3, "pendentes": 0,
                                        "revisar": 1, "novos": 5, "lancamentos_sem_banco": 1}
    assert [m.id_externo for m in itau_sync.movimentos(*JULHO, status="revisar")] == ["c"]
    assert {m.id_externo for m in itau_sync.movimentos(*JULHO, bloco="tesouraria")} == {"e", "f"}
    # "c" (sem classificação) entra no caixa na linha "A classificar" e já fica conciliado;
    # o repasse da Stone ("d") seria o único pendente, mas o arquivo daquele dia
    # ainda não chegou: ele não aparece em nenhuma aba até chegar
    assert {m.id_externo for m in itau_sync.movimentos(*JULHO, status="pendentes")} == set()
    assert "d" not in {m.id_externo for m in itau_sync.movimentos(*JULHO)}


def test_reclassificar_manual_e_regra_por_contraparte(app):
    _, l_sai, _ = _seed()
    cli = _ClienteFake([
        _tx("c1", 10, "999.00", itau.DEBITO, "PIX ENVIADO FULANO", contraparte="FULANO", contraparte_documento="98765432100"),
        _tx("c2", 12, "300.00", itau.DEBITO, "PIX ENVIADO FULANO", contraparte="FULANO", contraparte_documento="98765432100"),
        _tx("x", 11, "200.00", itau.DEBITO, "PIX ENVIADO BELTRANO", contraparte="BELTRANO", contraparte_documento="11111111111"),
        _tx("b", 4, "50.00", itau.DEBITO, "BOLETO PAGO ALUGUEL", origem="DEBITO"),
    ])
    itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()
    movs = _movs()

    # regra pela contraparte: vale para todos os PIX do mesmo CPF
    alterados = itau_sync.reclassificar(movs["c1"], "pag_artistas", para_contraparte=True)
    db.session.commit()
    assert alterados == 2
    regra = db.session.execute(db.select(RegraClassificacaoBancaria)).scalar_one()
    assert (regra.campo, regra.valor, regra.tipo, regra.categoria) == (
        "contraparte_documento", "98765432100", "debito", "pag_artistas")
    assert {movs["c1"].categoria_gerencial, movs["c2"].categoria_gerencial} == {"pag_artistas"}
    assert movs["c2"].revisar is False and movs["x"].categoria_gerencial == "a_classificar_saida"

    # manual: só o movimento, e a sincronização não sobrescreve
    itau_sync.reclassificar(movs["x"], "soc_retirada")
    db.session.commit()
    itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()
    assert db.session.get(MovimentoBancario, movs["x"].id).categoria_gerencial == "soc_retirada"
    assert db.session.get(MovimentoBancario, movs["x"].id).classificacao_manual is True
    assert db.session.get(MovimentoBancario, movs["c2"].id).categoria_gerencial == "pag_artistas"

    # mover para transferência desfaz a conciliação
    assert movs["b"].lancamento == l_sai
    itau_sync.reclassificar(movs["b"], "trf_entre_contas")
    db.session.commit()
    assert db.session.get(MovimentoBancario, movs["b"].id).lancamento is None

    with pytest.raises(ValueError):
        itau_sync.reclassificar(movs["x"], "categoria_inexistente")


def test_aprende_a_classificar_pela_planilha(app):
    """PIX para pessoa física casado com 'Músicos' na planilha vira regra da contraparte."""
    musicos = Categoria(nome="Músicos", tipo="saida", grupo="Demais Salários")
    db.session.add(musicos)
    db.session.flush()
    db.session.add(Lancamento(data=date(2026, 7, 1), categoria_id=musicos.id, valor=Decimal("1200.00")))
    db.session.commit()

    cli = _ClienteFake([
        _tx("m1", 1, "1200.00", itau.DEBITO, "PIX ENVIADO PAULO", contraparte="PAULO JOSE",
            contraparte_documento="55544433322"),
    ])
    rel = itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()
    assert rel["aprendizado"] == {"regras": 1, "movimentos": 1}
    mov = _movs()["m1"]
    assert (mov.categoria_gerencial, mov.bloco, mov.revisar) == ("pag_artistas", "operacional", False)
    assert "aprendido da planilha" in db.session.execute(
        db.select(RegraClassificacaoBancaria)).scalar_one().observacao

    # o próximo PIX para a mesma pessoa já entra classificado, sem par na planilha
    cli.txns.append(_tx("m2", 8, "980.00", itau.DEBITO, "PIX ENVIADO PAULO", contraparte="PAULO JOSE",
                        contraparte_documento="55544433322"))
    itau_sync.sincronizar(*JULHO, client=cli, config=CFG)
    db.session.commit()
    novo = _movs()["m2"]
    assert (novo.categoria_gerencial, novo.revisar) == ("pag_artistas", False)
    # e já entra no caixa na linha de músicos, conciliado com o próprio movimento
    assert novo.lancamento is not None
    assert novo.lancamento.categoria.nome == "Músicos"


def test_fila_de_revisao(app):
    _seed()
    itau_sync.sincronizar(*JULHO, client=_ClienteFake(EXTRATO_JULHO), config=CFG)
    db.session.commit()
    # 6 movimentos não revisados, mas o repasse da Stone sem o arquivo do dia
    # fica fora da tela toda até o arquivo chegar
    assert itau_sync.resumo(*JULHO)["novos"] == 5
    novos = itau_sync.movimentos(*JULHO, status="classificar")
    assert len(novos) == 5
    assert itau_sync.marcar_revisados(novos[:2]) == 2
    db.session.commit()
    assert itau_sync.resumo(*JULHO)["novos"] == 3
    assert itau_sync.marcar_revisados(novos[:2]) == 0  # idempotente


def test_valor_alterado_no_banco_volta_para_pendente(app):
    _seed()
    cli = _ClienteFake([_tx("a", 1, "100.00", itau.CREDITO, contraparte_documento="12345678901")])
    itau_sync.sincronizar(date(2026, 7, 1), client=cli, config=CFG)
    db.session.commit()
    assert db.session.execute(db.select(MovimentoBancario)).scalar_one().lancamento is not None

    cli.txns = [_tx("a", 1, "120.00", itau.CREDITO, contraparte_documento="12345678901")]
    rel = itau_sync.sincronizar(date(2026, 7, 1), client=cli, config=CFG)
    db.session.commit()
    m = db.session.execute(db.select(MovimentoBancario)).scalar_one()
    assert rel["atualizados"] == 1
    assert m.valor == Decimal("120.00")
    assert m.lancamento is None


def test_sem_contas_erra_claro(app):
    with pytest.raises(RuntimeError, match="ITAU_CONTAS"):
        itau_sync.sincronizar(date(2026, 7, 1), client=_ClienteFake([]), config=itau.ItauConfig())


def test_tela_conciliacao_lista_classifica_e_desfaz(app, client):
    _seed()
    itau_sync.sincronizar(*JULHO, client=_ClienteFake([
        _tx("a", 1, "100.00", itau.CREDITO, "PIX RECEBIDO TESTE", contraparte_documento="12345678901")]), config=CFG)
    db.session.commit()
    _login(client)

    resp = client.get("/conciliacao/itau?inicio=2026-07-01&fim=2026-07-31")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    for trecho in ("PIX RECEBIDO TESTE", "Conciliado", "Demonstrativo do caixa Itaú", "PIX de clientes",
                   format_brl(Decimal("1500.25"))):
        assert trecho in html

    mov = db.session.execute(db.select(MovimentoBancario)).scalar_one()
    form = {"inicio": "2026-07-01", "fim": "2026-07-31"}
    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/classificar", data={**form, "categoria": "rec_eventos"})
    assert resp.status_code in (302, 303)
    mov = db.session.get(MovimentoBancario, mov.id)
    assert (mov.categoria_gerencial, mov.classificacao_manual) == ("rec_eventos", True)

    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/desfazer", data=form)
    assert resp.status_code in (302, 303)
    assert db.session.get(MovimentoBancario, mov.id).lancamento is None


def test_gerencia_classifica_e_revisa_mas_nao_sincroniza(app, client):
    """A Manu (gerência) faz o trabalho do dia a dia; sincronizar é da consultoria."""
    _seed()
    itau_sync.sincronizar(*JULHO, client=_ClienteFake([
        _tx("c", 10, "999.00", itau.DEBITO, "PIX ENVIADO FULANO", contraparte="FULANO",
            contraparte_documento="98765432100")]), config=CFG)
    db.session.commit()
    mov = db.session.execute(db.select(MovimentoBancario)).scalar_one()
    _login(client, role="gerencia")
    form = {"inicio": "2026-07-01", "fim": "2026-07-31"}

    assert client.get("/conciliacao/itau").status_code == 200
    assert client.post("/conciliacao/itau/sincronizar", data=form).status_code == 403

    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/classificar",
                       data={**form, "categoria": "pag_artistas"})
    assert resp.status_code in (302, 303)
    assert db.session.get(MovimentoBancario, mov.id).categoria_gerencial == "pag_artistas"

    resp = client.post("/conciliacao/itau/revisar", data={**form, "status": "novos"})
    assert resp.status_code in (302, 303)
    assert db.session.get(MovimentoBancario, mov.id).revisado is True
    assert itau_sync.resumo(*JULHO)["novos"] == 0


def test_dashboard_mostra_saldo_itau(app, client):
    itau_sync.sincronizar(*JULHO, client=_ClienteFake([], saldo="29088.14"), config=CFG)
    db.session.commit()
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert format_brl(Decimal("29088.14")) in resp.get_data(as_text=True)


# ── Clareza: revisão atômica, lote por id e busca ────────────────────────────

def _popular():
    _seed()
    itau_sync.sincronizar(*JULHO, client=_ClienteFake(EXTRATO_JULHO), config=CFG)
    db.session.commit()
    return _movs()


def test_revisar_movimento_confirma_sem_travar_como_manual(app):
    mov = _popular()["a"]
    itau_sync.revisar_movimento(mov, mov.categoria_gerencial)
    assert (mov.revisado, mov.classificacao_manual) == (True, False)  # só confirmou a sugestão


def test_revisar_movimento_com_outra_categoria_fica_manual(app):
    mov = _popular()["c"]
    itau_sync.revisar_movimento(mov, "pag_fornecedores")
    assert (mov.revisado, mov.classificacao_manual, mov.categoria_gerencial, mov.revisar) == (
        True, True, "pag_fornecedores", False)


def test_revisar_movimento_rejeita_categoria_invalida(app):
    mov = _popular()["c"]
    with pytest.raises(ValueError):
        itau_sync.revisar_movimento(mov, "inexistente")
    assert mov.revisado is False


def test_contagens_e_busca(app):
    _popular()
    cont = itau_sync.contagens(*JULHO)
    assert cont == {"sem_classificacao": 1, "revisar": 1, "classificar": 5, "pendentes": 0,
                    "conciliados": 3, "todos": 5}
    assert [m.id_externo for m in itau_sync.movimentos(*JULHO, busca="fulano")] == ["c"]
    assert itau_sync.contagens(*JULHO, busca="aplic")["todos"] == 2


def test_tela_revisao_e_lote_atingem_so_o_selecionado(app, client):
    movs = _popular()
    _login(client, role="gerencia")
    form = {"inicio": "2026-07-01", "fim": "2026-07-31"}

    html = client.get("/conciliacao/itau?inicio=2026-07-01&fim=2026-07-31").get_data(as_text=True)
    assert "Revisar movimento" in html and "A classificar · 5" in html

    resp = client.post(f"/conciliacao/itau/movimentos/{movs['c'].id}/revisao",
                       data={**form, "categoria": "pag_artistas", "alcance": "regra"})
    assert resp.status_code in (302, 303)
    c = db.session.get(MovimentoBancario, movs["c"].id)
    assert (c.categoria_gerencial, c.revisado, c.classificacao_manual) == ("pag_artistas", True, False)

    resp = client.post("/conciliacao/itau/revisar-lote",
                       data={**form, "mov_ids": [movs["a"].id, movs["b"].id]})
    assert resp.status_code in (302, 303)
    revisados = {k for k, m in _movs().items() if db.session.get(MovimentoBancario, m.id).revisado}
    assert revisados == {"a", "b", "c"}  # d, e, f ficaram de fora do lote

    # id inexistente: nada é revisado
    client.post("/conciliacao/itau/revisar-lote", data={**form, "mov_ids": [movs["d"].id, 999999]})
    assert db.session.get(MovimentoBancario, movs["d"].id).revisado is False


def test_fornecedor_durante_revisao_volta_para_o_movimento(app, client):
    _login(client, role="gerencia")
    proximo = "/conciliacao/itau?revisar=3&categoria=pag_fornecedores"
    resp = client.post("/cadastros/fornecedores/salvar", data={"nome": "Aurora", "ativo": "1", "proximo": proximo})
    # volta para o movimento levando o fornecedor recém-cadastrado já escolhido
    assert proximo in resp.headers["Location"] and resp.headers["Location"].endswith("&fornecedor=1")
    # duplicado não cria outro e volta para o mesmo lugar
    resp = client.post("/cadastros/fornecedores/salvar", data={"nome": "aurora", "proximo": proximo})
    assert resp.headers["Location"].endswith(proximo)
    from app.models.fluxo import Fornecedor
    assert db.session.query(Fornecedor).filter(Fornecedor.nome.ilike("aurora")).count() == 1
    # destino externo é ignorado
    resp = client.post("/cadastros/fornecedores/salvar", data={"nome": "Outro", "proximo": "https://evil.example"})
    assert resp.headers["Location"].endswith("/cadastros/fornecedores")


def test_regras_bancarias_previa_salva_e_exclui(app, client):
    from app.models.banco import RegraClassificacaoBancaria

    _popular()
    _login(client)
    dados = {"campo": "contraparte_nome", "valor": "fulano", "tipo": "debito", "categoria": "pag_artistas", "ativo": "1"}

    html = client.post("/admin/regras/previa", data=dados).get_data(as_text=True)
    assert "Confirmar regra" in html and "mudam de categoria" in html
    assert db.session.query(RegraClassificacaoBancaria).count() == 0  # prévia não grava

    assert client.post("/admin/regras/salvar", data=dados).status_code in (302, 303)
    regra = db.session.execute(db.select(RegraClassificacaoBancaria)).scalar_one()
    assert _movs()["c"].categoria_gerencial == "pag_artistas"
    assert "fulano" in client.get("/admin/regras").get_data(as_text=True)

    # duplicada é recusada
    client.post("/admin/regras/salvar", data=dados)
    assert db.session.query(RegraClassificacaoBancaria).count() == 1

    client.post(f"/admin/regras/{regra.id}/excluir")
    assert db.session.query(RegraClassificacaoBancaria).count() == 0
    assert _movs()["c"].categoria_gerencial == "a_classificar_saida"


def test_integracoes_mostra_status_sem_credenciais(app, client):
    itau_sync.sincronizar(*JULHO, client=_ClienteFake([], saldo="10.00"), config=CFG)
    db.session.commit()
    _login(client)
    html = client.get("/admin/integracoes").get_data(as_text=True)
    assert "Itaú" in html and "Google Sheets" in html and "Stone" in html
    assert "CLIENT_SECRET" not in html


def test_gerencia_nao_acessa_administracao(app, client):
    _login(client, role="gerencia")
    for url in ("/admin/integracoes", "/admin/regras", "/admin/usuarios"):
        assert client.get(url).status_code == 403


# ---------------- conciliação 1 → N (um crédito do banco, vários lançamentos) ----------------

def _fluxo_cartoes(dia, valores):
    """Vendas de cartão do dia, como a planilha lança (uma linha por bandeira)."""
    cat = db.session.execute(db.select(Categoria).filter_by(nome="Visa Crédito")).scalar_one_or_none()
    if not cat:
        cat = Categoria(nome="Visa Crédito", tipo="entrada", grupo="Vendas - Repasse Stone")
        db.session.add(cat)
        db.session.flush()
    criados = []
    for valor in valores:
        l = Lancamento(data=dia, categoria_id=cat.id, valor=Decimal(valor))
        db.session.add(l)
        criados.append(l)
    db.session.commit()
    return criados


def test_repasse_de_cartao_concilia_somando_varios_lancamentos(app):
    """17/09: o PIX da Stone (10.659,72) é a soma das vendas do dia."""
    dia = date(2026, 9, 17)
    partes = _fluxo_cartoes(dia, ["4007.08", "3191.99", "232.02", "800.66", "394.58", "2033.39"])
    mov = MovimentoBancario(banco="itau", conta="1234500123456", id_externo="stone-17", data=dia,
                            tipo="credito", valor=Decimal("10659.72"), descricao="PIX TRANSF BANGALO",
                            contraparte="BANGALO", categoria_gerencial="rec_cartao", bloco="entradas")
    db.session.add(mov)
    db.session.commit()

    rel = itau_sync.conciliar_periodo(dia, dia)
    assert rel["em_grupo"] == 1 and rel["pendentes"] == 0
    db.session.commit()

    mov = db.session.get(MovimentoBancario, mov.id)
    assert len(mov.itens_conciliacao) == 6
    assert mov.total_conciliado == Decimal("10659.72")
    assert mov.diferenca_conciliacao == Decimal("0.00")
    assert {i.lancamento_id for i in mov.itens_conciliacao} == {l.id for l in partes}
    assert mov.lancamento is None  # em grupo não existe "o" lançamento


def test_conciliacao_manual_com_varios_e_diferenca(app, client):
    dia = date(2026, 9, 17)
    partes = _fluxo_cartoes(dia, ["100.00", "50.00", "25.00"])
    mov = MovimentoBancario(banco="itau", conta="1234500123456", id_externo="manual-1", data=dia,
                            tipo="credito", valor=Decimal("180.00"), descricao="PIX",
                            categoria_gerencial="rec_cartao", bloco="entradas")
    db.session.add(mov)
    db.session.commit()
    _login(client)

    candidatos = client.get(f"/conciliacao/itau/movimentos/{mov.id}/conciliacao").get_json()
    assert len(candidatos["candidatos"]) == 3 and candidatos["sugestao"] == []  # nenhuma soma dá 180

    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/conciliacao",
                       data={"lancamento_ids": [partes[0].id, partes[1].id]})
    assert resp.status_code in (302, 303)
    mov = db.session.get(MovimentoBancario, mov.id)
    assert len(mov.itens_conciliacao) == 2
    assert mov.diferenca_conciliacao == Decimal("30.00")  # 180 - 150, permitida e registrada

    # o mesmo lançamento não pode ir para dois movimentos
    outro = MovimentoBancario(banco="itau", conta="1234500123456", id_externo="manual-2", data=dia,
                              tipo="credito", valor=Decimal("100.00"), categoria_gerencial="rec_cartao",
                              bloco="entradas")
    db.session.add(outro)
    db.session.commit()
    with pytest.raises(ValueError, match="já está conciliado"):
        itau_sync.conciliar_manual(outro, [partes[0].id])
    db.session.rollback()

    # desfazer pela mesma rota, sem marcar nada
    client.post(f"/conciliacao/itau/movimentos/{mov.id}/conciliacao", data={})
    assert db.session.get(MovimentoBancario, mov.id).itens_conciliacao == []


def test_sugestao_marca_a_combinacao_exata(app, client):
    dia = date(2026, 9, 16)
    partes = _fluxo_cartoes(dia, ["774.87", "1272.93", "192.00", "562.56", "159.84"])
    mov = MovimentoBancario(banco="itau", conta="1234500123456", id_externo="sug-1", data=dia,
                            tipo="credito", valor=Decimal("2962.20"), categoria_gerencial="rec_cartao",
                            bloco="entradas")
    db.session.add(mov)
    db.session.commit()
    _login(client)
    dados = client.get(f"/conciliacao/itau/movimentos/{mov.id}/conciliacao").get_json()
    assert sorted(dados["sugestao"]) == sorted(l.id for l in partes)


def test_revisado_nao_volta_para_a_fila_na_proxima_sincronizacao(app):
    """Sócios "a detalhar" pede revisão pela regra — mas depois de revisado, não volta.

    Era o caso dos PIX para a Bárbara: a Manu revisava e no sync seguinte eles
    reapareciam em "Para revisar".
    """
    # em produção existe a regra da contraparte, que classifica como "Sócios – a detalhar"
    db.session.add(RegraClassificacaoBancaria(
        campo="contraparte_nome", valor="BARBARA MENDES GONCALVES SALLABERRY", tipo="debito",
        categoria="soc_a_detalhar", revisar=True, ativo=True))
    mov = MovimentoBancario(banco="itau", conta="1234500123456", id_externo="soc1",
                            data=date(2026, 9, 15), tipo="debito", valor=Decimal("5000.00"),
                            descricao="PIX ENVIADO BARBARA MEND",
                            contraparte="BARBARA MENDES GONCALVES SALLABERRY",
                            categoria_gerencial="soc_a_detalhar", bloco="socios", revisar=True)
    db.session.add(mov)
    db.session.commit()

    itau_sync.marcar_revisados([mov])
    db.session.commit()
    assert (mov.revisado, mov.revisar) == (True, False)

    # a regra continua dizendo "revisar", mas a pessoa já olhou
    itau_sync.reaplicar_regras()
    db.session.commit()
    assert db.session.get(MovimentoBancario, mov.id).revisar is False
    assert [m.id for m in itau_sync.movimentos(date(2026, 9, 1), date(2026, 9, 30),
                                               status="revisar")] == []

    # mas se a classificação mudar, aí sim volta para a fila
    itau_sync._definir_categoria(mov, "pag_fornecedores", True, "regra nova")
    db.session.commit()
    assert db.session.get(MovimentoBancario, mov.id).revisar is True


def test_tela_revisa_pela_linha_da_planilha(app, client):
    """A gaveta manda a linha do caixa; a categoria gerencial vem derivada dela."""
    from app.models.fluxo import Categoria

    _seed()
    itau_sync.sincronizar(*JULHO, client=_ClienteFake([
        _tx("z", 3, "430.00", itau.DEBITO, "DA CONCESSIONARIA",
            contraparte="DISTRIBUIDORA DE ENERGIA")]), config=CFG)
    db.session.commit()
    _login(client)

    light = Categoria(nome="Light", tipo="saida", grupo="Despesas Fixas")
    db.session.add(light)
    db.session.commit()
    mov = db.session.execute(db.select(MovimentoBancario)).scalar_one()
    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/revisao", data={
        "inicio": "2026-07-01", "fim": "2026-07-31",
        "categoria_id": str(light.id), "alcance": "movimento"})
    assert resp.status_code in (302, 303)

    mov = db.session.get(MovimentoBancario, mov.id)
    assert (mov.categoria_id, mov.categoria_gerencial, mov.revisado) == (light.id, "pag_utilidades", True)
    assert mov.lancamento.categoria.nome == "Light"


def test_reconciliar_mantendo_um_lancamento_ja_ligado(app):
    """Regravar a conciliação incluindo quem já estava ligado não pode estourar."""
    from app.models.banco import ConciliacaoItem

    movs = _popular()
    mov = movs["a"]
    tipo = "entrada" if mov.tipo == "credito" else "saida"
    cats = db.session.execute(db.select(Categoria).filter_by(tipo=tipo)).scalars().all()
    a, b = (Lancamento(data=mov.data, categoria_id=cats[0].id, valor=Decimal("60.00")),
            Lancamento(data=mov.data, categoria_id=cats[0].id, valor=Decimal("40.00")))
    db.session.add_all([a, b])
    db.session.commit()

    itau_sync.conciliar_manual(mov, [a.id])
    db.session.commit()
    assert {i.lancamento_id for i in mov.itens_conciliacao} == {a.id}

    # o caso do erro: o mesmo lançamento entra de novo, agora com companhia
    itau_sync.conciliar_manual(mov, [a.id, b.id])
    db.session.commit()
    assert {i.lancamento_id for i in mov.itens_conciliacao} == {a.id, b.id}
    assert db.session.execute(db.select(db.func.count(ConciliacaoItem.id))
                              .filter_by(movimento_id=mov.id)).scalar_one() == 2


def test_repasse_da_stone_sem_arquivo_fica_fora_da_fila(app):
    """Sem o arquivo do dia não há o que revisar: o repasse não aparece na fila."""
    cat = Categoria(nome="Visa Crédito", tipo="entrada", grupo="Vendas - Repasse Stone")
    db.session.add(cat)
    db.session.flush()
    com = MovimentoBancario(banco="itau", conta="1", id_externo="s1", data=date(2026, 7, 10),
                            tipo="credito", valor=Decimal("500.00"), descricao="PIX TRANSF BANGALO",
                            categoria_gerencial="rec_stone", bloco="operacional")
    sem = MovimentoBancario(banco="itau", conta="1", id_externo="s2", data=date(2026, 7, 11),
                            tipo="credito", valor=Decimal("300.00"), descricao="PIX TRANSF BANGALO",
                            categoria_gerencial="rec_stone", bloco="operacional")
    outro = MovimentoBancario(banco="itau", conta="1", id_externo="s3", data=date(2026, 7, 11),
                              tipo="credito", valor=Decimal("80.00"), descricao="PIX CLIENTE",
                              categoria_gerencial="rec_pix_clientes", bloco="operacional")
    db.session.add_all([com, sem, outro])
    # só o dia 10 teve arquivo importado
    db.session.add(Lancamento(data=date(2026, 7, 10), categoria_id=cat.id,
                              valor=Decimal("500.00"), origem="stone", origem_id="pg:1"))
    db.session.commit()

    fila = itau_sync.movimentos(*JULHO, status="classificar")
    ids = {m.id for m in fila}
    assert com.id in ids and outro.id in ids     # têm o que revisar
    assert sem.id not in ids                     # espera o arquivo da Stone
    assert itau_sync.contagens(*JULHO)["classificar"] == len(fila)
    # some da tela inteira, inclusive de "Todos", até o arquivo chegar
    assert sem.id not in {m.id for m in itau_sync.movimentos(*JULHO)}
    assert itau_sync.contagens(*JULHO)["todos"] == len(itau_sync.movimentos(*JULHO))


def test_revisar_repasse_da_stone_edita_as_linhas_do_credito(app, client):
    """Na gaveta do repasse, confirmar revisão também grava a conciliação escolhida."""
    from app.models.banco import ConciliacaoItem

    movs = _popular()
    _login(client)
    mov = movs["d"]          # repasse da Stone
    tipo = "entrada" if mov.tipo == "credito" else "saida"
    cat = db.session.execute(db.select(Categoria).filter_by(tipo=tipo)).scalars().first()
    a = Lancamento(data=mov.data, categoria_id=cat.id, valor=Decimal("3000.00"))
    b = Lancamento(data=mov.data, categoria_id=cat.id, valor=Decimal("2000.00"))
    db.session.add_all([a, b])
    db.session.commit()

    form = {"inicio": "2026-07-01", "fim": "2026-07-31", "editar_linhas": "1"}
    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/revisao",
                       data={**form, "lancamento_ids": [a.id, b.id]})
    assert resp.status_code in (302, 303)
    mov = db.session.get(MovimentoBancario, mov.id)
    assert mov.revisado
    assert {i.lancamento_id for i in mov.itens_conciliacao} == {a.id, b.id}

    # e desmarcar tudo desfaz a conciliação, em vez de ser ignorado
    resp = client.post(f"/conciliacao/itau/movimentos/{mov.id}/revisao", data=form)
    assert resp.status_code in (302, 303)
    assert db.session.execute(db.select(db.func.count(ConciliacaoItem.id))
                              .filter_by(movimento_id=mov.id)).scalar_one() == 0
