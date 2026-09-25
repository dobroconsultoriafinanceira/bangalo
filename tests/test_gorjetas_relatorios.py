# -*- coding: utf-8 -*-
"""Relatórios em PDF das gorjetas e a regra de férias que os alimenta.

Os números de férias conferem com as abas reais "1ª Q Setembro-26"
(um colaborador de férias) e "2ª Q Agosto-26" (dois, um deles tendo
trabalhado parte da quinzena).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models.gorjetas import (
    Colaborador,
    ComissaoDiaria,
    ExtraQuinzena,
    Funcao,
    ParticipacaoPeriodo,
    PeriodoGorjeta,
    Presenca,
    Setor,
)
from app.services import gorjetas as engine
from app.services import gorjetas_consultas, gorjetas_relatorios

PERCENTUAIS = {"Cozinha": Decimal("0.25"), "Salão": Decimal("0.73"), "Caixa": Decimal("0.02")}


def _colab(id_, nome, setor, pontos, presencas, **kwargs):
    return engine.ColaboradorRateio(
        id=id_, nome=nome, setor=setor, funcao="Garçom", registro="CLT",
        pontos=Decimal(str(pontos)), presencas=set(presencas), **kwargs,
    )


# ───────────────────────── regra de férias (engine pura) ─────────────────────────


def test_ferias_iguala_o_apurado_de_todo_o_setor():
    """Quem sai de férias recebe como se tivesse trabalhado; o setor banca."""
    dias = [date(2026, 9, d) for d in range(1, 4)]
    comissao = {d: Decimal("1000") for d in dias}
    colaboradores = [
        _colab(1, "A", "Salão", 2, dias),
        _colab(2, "B", "Salão", 2, dias),
        _colab(3, "C", "Salão", 2, [], em_ferias=True),
    ]
    r = engine.ratear_diario(comissao, Decimal("0.20"), PERCENTUAIS, colaboradores)
    por_id = {c.id: c for c in r.colaboradores}

    # só há gente no salão: os pools vazios são redistribuídos, sobram
    # 3000 × 0,80 = 2400 para o setor → alvo = 2400 / 3 pessoas
    assert por_id[1].liquido == por_id[2].liquido == por_id[3].liquido == Decimal("800.00")
    assert por_id[3].reembolso_ferias == Decimal("800.00")   # recebe (não trabalhou)
    assert por_id[1].reembolso_ferias == Decimal("-400.00")  # paga


def test_ferias_conta_quem_trabalhou_parte_da_quinzena():
    """2ª Q Agosto-26: um dos dois de férias trabalhou dias e ainda paga."""
    dias = [date(2026, 8, d) for d in range(16, 19)]
    comissao = {d: Decimal("1000") for d in dias}
    colaboradores = [
        _colab(1, "A", "Salão", 2, dias),
        _colab(2, "B", "Salão", 2, dias),
        # de férias mas presente em 1 dos 3 dias: apura menos e recebe a diferença
        _colab(3, "C", "Salão", 2, dias[:1], em_ferias=True),
    ]
    r = engine.ratear_diario(comissao, Decimal("0.20"), PERCENTUAIS, colaboradores)
    liquidos = {c.nome: c.liquido for c in r.colaboradores}

    assert liquidos["A"] == liquidos["B"] == liquidos["C"]
    # fecha em zero a menos do arredondamento ao centavo de cada um
    assert abs(sum(c.reembolso_ferias for c in r.colaboradores)) <= Decimal("0.03")


def test_desconto_entra_na_base_do_acerto_de_ferias():
    """A planilha rateia sobre o líquido (já com vales), não sobre o bruto."""
    dias = [date(2026, 9, 1)]
    comissao = {dias[0]: Decimal("1000")}
    colaboradores = [
        _colab(1, "A", "Salão", 2, dias, desconto=Decimal("100")),
        _colab(2, "B", "Salão", 2, dias),
        _colab(3, "C", "Salão", 2, [], em_ferias=True),
    ]
    r = engine.ratear_diario(comissao, Decimal("0.20"), PERCENTUAIS, colaboradores)
    liquidos = [c.liquido for c in r.colaboradores]
    assert len(set(liquidos)) == 1  # todos terminam iguais, inclusive quem teve vale


# ───────────────────────────── relatórios em PDF ─────────────────────────────


@pytest.fixture()
def periodo(app):
    """Uma quinzena de 2 dias com cozinha, salão (um de férias), caixa e um extra."""
    setores = {}
    for nome, pct in PERCENTUAIS.items():
        s = Setor(nome=nome, percentual_rateio=pct)
        db.session.add(s)
        setores[nome] = s
    db.session.flush()

    funcoes = {
        "Cozinheiro": Funcao(nome="Cozinheiro", setor_id=setores["Cozinha"].id, pontos_padrao=Decimal("2")),
        "Garçom": Funcao(nome="Garçom", setor_id=setores["Salão"].id, pontos_padrao=Decimal("2")),
        "Caixa": Funcao(nome="Caixa", setor_id=setores["Caixa"].id, pontos_padrao=Decimal("1")),
    }
    db.session.add_all(funcoes.values())
    db.session.flush()

    pessoas = [
        ("Cozinheiro CLT", "Cozinheiro", "Cozinha", "CLT"),
        ("Cozinheiro de fora", "Cozinheiro", "Cozinha", "Por fora"),
        ("Garçom presente", "Garçom", "Salão", "CLT"),
        ("Garçom de férias", "Garçom", "Salão", "CLT"),
        ("Caixa", "Caixa", "Caixa", "CLT"),
    ]
    colaboradores = {}
    for nome, funcao, setor, registro in pessoas:
        c = Colaborador(
            nome=nome, funcao_id=funcoes[funcao].id, setor_id=setores[setor].id,
            pontos=funcoes[funcao].pontos_padrao, registro=registro,
        )
        db.session.add(c)
        colaboradores[nome] = c
    db.session.flush()

    dias = [date(2026, 9, 1), date(2026, 9, 2)]
    p = PeriodoGorjeta(
        referencia="1ª Quinzena Setembro/26", ordem_quinzena=1, mes=9, ano=2026,
        data_inicio=dias[0], data_fim=dias[-1],
        comissao_bruta=Decimal("2000"), percentual_encargos=Decimal("0.20"),
    )
    db.session.add(p)
    db.session.flush()

    for d in dias:
        db.session.add(ComissaoDiaria(periodo_id=p.id, data=d, valor=Decimal("1000")))
        for nome, c in colaboradores.items():
            if nome == "Garçom de férias":
                continue
            db.session.add(Presenca(periodo_id=p.id, colaborador_id=c.id, data=d, presente=True))

    for nome, c in colaboradores.items():
        db.session.add(ParticipacaoPeriodo(
            periodo_id=p.id, colaborador_id=c.id,
            em_ferias=(nome == "Garçom de férias"),
            desconto=Decimal("50") if nome == "Cozinheiro CLT" else Decimal("0"),
            desconto_motivo="Vale" if nome == "Cozinheiro CLT" else None,
        ))
    db.session.add(ExtraQuinzena(
        periodo_id=p.id, setor_id=setores["Cozinha"].id, data=dias[0], turno="Noite",
        pontos=Decimal("1.5"), comissao_turno=Decimal("1000"), valor_pago=Decimal("150"),
    ))
    db.session.commit()
    return p


def test_relatorio_completo_tem_as_quatro_secoes(periodo):
    pdf = gorjetas_relatorios.pdf_completo(periodo)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 3000


def test_relatorio_resumido_esconde_quem_e_pago_por_fora(periodo):
    dados = gorjetas_relatorios.montar(periodo)
    visiveis = [l.nome for l in dados.linhas if not l.por_fora]
    assert "Cozinheiro de fora" in [l.nome for l in dados.linhas]
    assert "Cozinheiro de fora" not in visiveis
    assert gorjetas_relatorios.pdf_resumido(periodo).startswith(b"%PDF")


def test_relatorio_de_ferias_so_existe_quando_ha_alguem_de_ferias(periodo):
    assert gorjetas_relatorios.pdf_ferias(periodo).startswith(b"%PDF")

    for part in periodo.participacoes:
        part.em_ferias = False
    db.session.commit()
    assert gorjetas_relatorios.pdf_ferias(periodo) is None


def test_bloco_de_ferias_deixa_o_setor_no_mesmo_valor(periodo):
    bloco = gorjetas_relatorios.montar(periodo).blocos_ferias()[0]
    assert bloco.setor == "Salão"
    assert bloco.pessoas == 2 and bloco.em_ferias == 1
    for linha in bloco.linhas:
        assert linha.liquido + linha.reembolso_ferias == bloco.alvo


def test_resumo_fecha_pool_contra_pago_extras_e_vales(periodo):
    dados = gorjetas_relatorios.montar(periodo)
    for setor in dados.setores:
        assert setor.status == "OK", f"{setor.nome} não fechou: {setor.diferenca}"


def test_quinzena_fechada_reporta_o_que_foi_pago(periodo):
    gorjetas_consultas.fechar(periodo)
    db.session.commit()

    dados = gorjetas_relatorios.montar(periodo)
    assert dados.fonte == "fechamento"
    pagos = {f.nome: f.liquido_a_pagar for f in periodo.fechamentos}
    for linha in dados.linhas:
        assert linha.liquido + linha.reembolso_ferias == pagos[linha.nome]


def test_importador_reconhece_grafias_antigas_do_mesmo_colaborador():
    from app.importers.gorjetas_importer import funcao_canonica, nome_canonico

    assert nome_canonico("Valderi") == "JOSE VALDERI"
    assert nome_canonico("Tonhão") == "ANTONIO LUIZ"
    assert nome_canonico("Raimundo (Por dentro)") == "RAIMUNDO"
    assert nome_canonico("Leo Cozinheiro") == "LEO COZINHEIRO"
    assert nome_canonico("Leo Ajudante") == "LEO COZINHEIRO (AJUDANTE)"
    assert nome_canonico("Soraya") == "SORAYA"
    assert funcao_canonica("Auxiliar de Cozinha") == "Auxiliar de Cozinha 2"
    assert funcao_canonica("Auxiliar de Cozinha 1") == "Auxiliar de Cozinha 1"


# ── Tela Clareza da quinzena: etapas não perdem dados ────────────────────────

def _login(client, role="gerencia"):
    from app.models.usuario import Usuario

    u = Usuario(nome="Manu", email="manu@x.com", role=role)
    u.definir_senha("senha1234")
    db.session.add(u)
    db.session.commit()
    client.post("/login", data={"email": "manu@x.com", "senha": "senha1234"})


def test_tela_da_quinzena_salva_todas_as_etapas_juntas(periodo, client):
    _login(client)
    html = client.get(f"/gorjetas/{periodo.id}?etapa=3").get_data(as_text=True)
    for trecho in ("Comissão por dia", "Presença por dia", "Ajustes individuais", "Rateio por colaborador",
                   'form="form-principal"', "Extras por diária"):
        assert trecho in html

    colabs = {c.nome: c for c in db.session.execute(db.select(Colaborador)).scalars()}
    garcom, cozinheiro = colabs["Garçom presente"], colabs["Cozinheiro CLT"]
    form = {
        "etapa": "3", "comissao_bruta": "2000,00", "percentual_encargos": "20",
        "comissao_2026-09-01": "1.200,00", "comissao_2026-09-02": "800,00",
        f"presenca_{garcom.id}_2026-09-01": "on",
        f"desconto_{cozinheiro.id}": "70,00", f"motivo_{cozinheiro.id}": "Vale",
        "ferias_ids": [str(garcom.id), str(cozinheiro.id)], f"ferias_{garcom.id}": "1",
    }
    resp = client.post(f"/gorjetas/{periodo.id}/salvar", data=form)
    assert resp.status_code in (302, 303) and "etapa=3" in resp.headers["Location"]

    db.session.expire_all()
    p = db.session.get(PeriodoGorjeta, periodo.id)
    assert {c.data.day: c.valor for c in p.comissoes_diarias} == {1: Decimal("1200.00"), 2: Decimal("800.00")}
    presentes = {(pr.colaborador_id, pr.data.day) for pr in p.presencas if pr.presente}
    assert presentes == {(garcom.id, 1)}
    partes = {pp.colaborador_id: pp for pp in p.participacoes}
    assert (partes[cozinheiro.id].desconto, partes[cozinheiro.id].desconto_motivo) == (Decimal("70.00"), "Vale")
    assert partes[garcom.id].em_ferias is True and partes[cozinheiro.id].em_ferias is False


def test_extra_por_diaria_pela_tela(periodo, client):
    _login(client)
    setor = db.session.execute(db.select(Setor).filter_by(nome="Cozinha")).scalar_one()
    antes = len(periodo.extras)
    resp = client.post(f"/gorjetas/{periodo.id}/extras/add", data={
        "setor_id": setor.id, "data": "2026-09-02", "turno": "Noite", "pontos": "1,5",
        "comissao_turno": "900,00", "valor_pago": "150,00"})
    assert resp.status_code in (302, 303)
    db.session.expire_all()
    p = db.session.get(PeriodoGorjeta, periodo.id)
    assert len(p.extras) == antes + 1
    fora = client.post(f"/gorjetas/{periodo.id}/extras/add", data={
        "setor_id": setor.id, "data": "2026-10-01", "pontos": "1", "comissao_turno": "1", "valor_pago": "1"})
    db.session.expire_all()
    assert len(db.session.get(PeriodoGorjeta, periodo.id).extras) == antes + 1  # fora da quinzena: recusado
    novo = db.session.get(PeriodoGorjeta, periodo.id).extras[-1]
    client.post(f"/gorjetas/{periodo.id}/extras/{novo.id}/remover")
    db.session.expire_all()
    assert len(db.session.get(PeriodoGorjeta, periodo.id).extras) == antes


def test_quinzena_em_somente_leitura(periodo, client):
    _login(client, role="leitura")
    html = client.get(f"/gorjetas/{periodo.id}?etapa=3").get_data(as_text=True)
    assert "data-somente-leitura" in html and "Somente leitura" in html
    for acao in ("Salvar quinzena", "Adicionar extra", "Fechar quinzena", "Equipe da quinzena", "Gorjeta completa"):
        assert acao not in html, acao
    assert client.post(f"/gorjetas/{periodo.id}/salvar", data={"etapa": "1"}).status_code == 403
