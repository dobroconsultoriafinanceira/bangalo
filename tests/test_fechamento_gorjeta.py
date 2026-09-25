# -*- coding: utf-8 -*-
"""Fechamento de quinzena: gera snapshot imutável e não recalcula depois."""
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models.gorjetas import (
    Colaborador,
    ComissaoDiaria,
    FechamentoGorjeta,
    Funcao,
    PeriodoGorjeta,
    Presenca,
    Setor,
)
from app.services import gorjetas_consultas


def _cenario():
    cozinha = Setor(nome="Cozinha", percentual_rateio=Decimal("0.25"))
    salao = Setor(nome="Salão", percentual_rateio=Decimal("0.73"))
    caixa = Setor(nome="Caixa", percentual_rateio=Decimal("0.02"))
    db.session.add_all([cozinha, salao, caixa])
    db.session.flush()
    fc = Funcao(nome="Cozinheiro", setor_id=cozinha.id, pontos_padrao=Decimal("2"))
    fg = Funcao(nome="Garçom", setor_id=salao.id, pontos_padrao=Decimal("2"))
    fx = Funcao(nome="Caixa", setor_id=caixa.id, pontos_padrao=Decimal("1"))
    db.session.add_all([fc, fg, fx])
    db.session.flush()
    c1 = Colaborador(nome="Cozinheiro A", funcao_id=fc.id, setor_id=cozinha.id, pontos=Decimal("2"))
    c2 = Colaborador(nome="Garçom B", funcao_id=fg.id, setor_id=salao.id, pontos=Decimal("2"))
    c3 = Colaborador(nome="Caixa C", funcao_id=fx.id, setor_id=caixa.id, pontos=Decimal("1"))
    db.session.add_all([c1, c2, c3])
    db.session.flush()
    p = PeriodoGorjeta(
        referencia="Teste", ordem_quinzena=1, mes=5, ano=2026,
        data_inicio=date(2026, 5, 1), data_fim=date(2026, 5, 1),
        comissao_bruta=Decimal("1000"), percentual_encargos=Decimal("0.20"),
    )
    db.session.add(p)
    db.session.flush()
    db.session.add(ComissaoDiaria(periodo_id=p.id, data=date(2026, 5, 1), valor=Decimal("1000")))
    for c in (c1, c2, c3):
        db.session.add(Presenca(periodo_id=p.id, colaborador_id=c.id, data=date(2026, 5, 1), presente=True))
    db.session.commit()
    return p


def test_fechamento_gera_snapshot_e_bate_total(app):
    p = _cenario()
    resultado = gorjetas_consultas.fechar(p)
    db.session.commit()

    assert p.status == "fechado"
    snapshots = db.session.query(FechamentoGorjeta).filter_by(periodo_id=p.id).all()
    assert len(snapshots) == 3
    total = sum((s.liquido_a_pagar for s in snapshots), Decimal("0"))
    # total líquido = 1000 × 0,80 = 800 (nada retido: soma bate)
    assert total == Decimal("800.00")
    assert resultado.total_a_pagar == Decimal("800.00")


def test_quinzena_fechada_nao_altera_via_rota(app, client):
    from app.models.usuario import Usuario

    p = _cenario()
    gorjetas_consultas.fechar(p)
    db.session.commit()

    admin = Usuario(nome="C", email="c@x.com", role="consultoria")
    admin.definir_senha("senha1234")
    db.session.add(admin)
    db.session.commit()
    client.post("/login", data={"email": "c@x.com", "senha": "senha1234"})

    # salvar numa quinzena fechada deve ser bloqueado (redirect sem alterar)
    resp = client.post(f"/gorjetas/{p.id}/salvar", data={"comissao_bruta": "9999"}, follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(p)
    assert p.comissao_bruta == Decimal("1000")  # inalterada


def test_reabrir_apaga_o_snapshot_e_devolve_para_edicao(app):
    """Erro descoberto depois do fechamento: reabrir volta a calcular."""
    import pytest

    p = _cenario()
    gorjetas_consultas.fechar(p)
    db.session.commit()
    assert p.fechado and db.session.execute(
        db.select(db.func.count(FechamentoGorjeta.id))).scalar_one() > 0

    apagados = gorjetas_consultas.reabrir(p)
    db.session.commit()

    assert not p.fechado and p.status == "aberto"
    assert apagados > 0
    assert db.session.execute(db.select(db.func.count(FechamentoGorjeta.id))).scalar_one() == 0
    # e dá para fechar de novo depois da correção
    gorjetas_consultas.fechar(p)
    db.session.commit()
    assert p.fechado

    # reabrir duas vezes seguidas é erro claro, não silêncio
    gorjetas_consultas.reabrir(p)
    with pytest.raises(ValueError, match="já está aberta"):
        gorjetas_consultas.reabrir(p)


def test_excluir_leva_junto_presencas_e_fechamento(app):
    """Apagar a quinzena não pode deixar restos apontando para ela."""
    from app.models.gorjetas import ParticipacaoPeriodo, Presenca

    p = _cenario()
    gorjetas_consultas.fechar(p)
    db.session.commit()
    periodo_id = p.id
    assert db.session.execute(db.select(db.func.count(Presenca.id))
                              .filter_by(periodo_id=periodo_id)).scalar_one() > 0

    gorjetas_consultas.excluir(p)
    db.session.commit()

    assert db.session.get(PeriodoGorjeta, periodo_id) is None
    for modelo in (FechamentoGorjeta, Presenca, ParticipacaoPeriodo, ComissaoDiaria):
        sobrou = db.session.execute(
            db.select(db.func.count(modelo.id)).filter_by(periodo_id=periodo_id)).scalar_one()
        assert sobrou == 0, modelo.__name__


def test_funcao_e_setor_sao_da_quinzena_nao_do_cadastro(app):
    """O caso do Roberto: mudar de função hoje não reescreve a quinzena passada."""
    from app.models.gorjetas import Funcao, ParticipacaoPeriodo, Setor

    p = _cenario()
    gorjetas_consultas.garantir_participacoes(p)
    db.session.commit()
    cozinha = db.session.execute(db.select(Setor).filter_by(nome="Cozinha")).scalar_one()
    salao = db.session.execute(db.select(Setor).filter_by(nome="Salão")).scalar_one()
    garcom = db.session.execute(db.select(Funcao).filter_by(nome="Garçom")).scalar_one()
    cozinheiro = db.session.execute(db.select(Funcao).filter_by(nome="Cozinheiro")).scalar_one()

    colaborador = db.session.execute(
        db.select(Colaborador).filter_by(nome="Cozinheiro A")).scalar_one()
    part = db.session.execute(db.select(ParticipacaoPeriodo).filter_by(
        periodo_id=p.id, colaborador_id=colaborador.id)).scalar_one()
    # na quinzena ele era da cozinha
    part.funcao_id, part.setor_id, part.pontos = cozinheiro.id, cozinha.id, Decimal("2")
    db.session.commit()

    entrada, _ = gorjetas_consultas.montar_entrada(p)
    dele = next(c for c in entrada if c.id == colaborador.id)
    assert (dele.setor, dele.funcao, dele.pontos) == ("Cozinha", "Cozinheiro", Decimal("2"))

    # hoje ele virou garçom no cadastro: a quinzena antiga não pode mudar
    colaborador.funcao_id, colaborador.setor_id, colaborador.pontos = garcom.id, salao.id, Decimal("2")
    db.session.commit()
    entrada, _ = gorjetas_consultas.montar_entrada(p)
    dele = next(c for c in entrada if c.id == colaborador.id)
    assert (dele.setor, dele.funcao) == ("Cozinha", "Cozinheiro")


def test_aplicar_equipe_tira_quem_nao_participa(app):
    """Desmarcar alguém na etapa Equipe leva junto as presenças daquela quinzena."""
    from app.models.gorjetas import ParticipacaoPeriodo, Presenca

    p = _cenario()
    gorjetas_consultas.garantir_participacoes(p)
    db.session.commit()
    ids = [c.id for c in db.session.execute(db.select(Colaborador)).scalars()]
    fora = ids[0]
    assert db.session.execute(db.select(db.func.count(Presenca.id)).filter_by(
        periodo_id=p.id, colaborador_id=fora)).scalar_one() > 0

    escolhidos = {i: {"funcao_id": None, "setor_id": None, "pontos": None} for i in ids if i != fora}
    rel = gorjetas_consultas.aplicar_equipe(p, escolhidos)
    db.session.commit()

    assert rel["sairam"] == 1
    assert db.session.execute(db.select(db.func.count(ParticipacaoPeriodo.id)).filter_by(
        periodo_id=p.id, colaborador_id=fora)).scalar_one() == 0
    assert db.session.execute(db.select(db.func.count(Presenca.id)).filter_by(
        periodo_id=p.id, colaborador_id=fora)).scalar_one() == 0
    # e ele some do rateio
    entrada, _ = gorjetas_consultas.montar_entrada(p)
    assert fora not in {c.id for c in entrada}


def test_equipe_nova_herda_a_realidade_da_quinzena_anterior(app):
    """Quinzena nova começa com a equipe da anterior, com a função de então."""
    from app.models.gorjetas import Funcao, ParticipacaoPeriodo, Setor

    anterior = _cenario()
    gorjetas_consultas.garantir_participacoes(anterior)
    db.session.commit()
    cozinha = db.session.execute(db.select(Setor).filter_by(nome="Cozinha")).scalar_one()
    cozinheiro = db.session.execute(db.select(Funcao).filter_by(nome="Cozinheiro")).scalar_one()
    colaborador = db.session.execute(
        db.select(Colaborador).filter_by(nome="Garçom B")).scalar_one()
    part = db.session.execute(db.select(ParticipacaoPeriodo).filter_by(
        periodo_id=anterior.id, colaborador_id=colaborador.id)).scalar_one()
    part.funcao_id, part.setor_id, part.pontos = cozinheiro.id, cozinha.id, Decimal("1.5")
    gorjetas_consultas.fechar(anterior)
    db.session.commit()

    nova = PeriodoGorjeta(referencia="Nova", ordem_quinzena=2, mes=5, ano=2026,
                          data_inicio=date(2026, 5, 16), data_fim=date(2026, 5, 31),
                          comissao_bruta=Decimal("1000"), percentual_encargos=Decimal("0.20"))
    db.session.add(nova)
    db.session.flush()
    gorjetas_consultas.garantir_participacoes(nova)
    db.session.commit()

    herdada = db.session.execute(db.select(ParticipacaoPeriodo).filter_by(
        periodo_id=nova.id, colaborador_id=colaborador.id)).scalar_one()
    assert (herdada.funcao_id, herdada.setor_id, herdada.pontos) == (
        cozinheiro.id, cozinha.id, Decimal("1.5"))


def test_saldo_do_setor_acumula_e_volta_em_partes_iguais(app):
    """Centavo que não divide fica guardado para o setor, nunca com o restaurante."""
    from app.models.gorjetas import SaldoSetorQuinzena, Setor

    p = _cenario()
    gorjetas_consultas.garantir_participacoes(p)
    db.session.commit()

    resultado = gorjetas_consultas.calcular(p)
    assert set(resultado.saldo_setor) >= {"Cozinha", "Salão"}
    for setor, m in resultado.saldo_setor.items():
        # nada some: o que entrou no setor é o que saiu mais o que ficou guardado
        assert m["anterior"] + m["gerado"] == m["distribuido"] + m["saldo"]

    # um saldo já acumulado que dá para dividir igual volta inteiro para o setor
    cozinha = db.session.execute(db.select(Setor).filter_by(nome="Cozinha")).scalar_one()
    pessoas = len([c for c in resultado.colaboradores if c.setor == "Cozinha"])
    db.session.add(SaldoSetorQuinzena(
        periodo_id=p.id, setor_id=cozinha.id, anterior=Decimal("0"),
        gerado=Decimal("0"), distribuido=Decimal("0"),
        saldo=Decimal("0.01") * pessoas + Decimal("0.02"), pessoas=pessoas))
    db.session.commit()

    novo = gorjetas_consultas.calcular(p)
    m = novo.saldo_setor["Cozinha"]
    creditos = {c.credito_saldo for c in novo.colaboradores if c.setor == "Cozinha"}
    assert len(creditos) == 1 and creditos != {Decimal("0")}   # todos recebem o mesmo
    por_pessoa = creditos.pop()
    assert m["distribuido"] == por_pessoa * pessoas
    assert m["saldo"] == m["anterior"] + m["gerado"] - m["distribuido"]
    assert Decimal("0") <= m["saldo"] < Decimal("0.01") * pessoas   # sobra menor que um centavo por pessoa


def test_fechar_grava_o_saldo_do_setor(app):
    from app.models.gorjetas import SaldoSetorQuinzena

    p = _cenario()
    gorjetas_consultas.garantir_participacoes(p)
    db.session.commit()
    gorjetas_consultas.fechar(p)
    db.session.commit()

    linhas = db.session.execute(
        db.select(SaldoSetorQuinzena).filter_by(periodo_id=p.id)).scalars().all()
    assert linhas, "o fechamento tem de deixar o saldo registrado"
    for linha in linhas:
        assert linha.anterior + linha.gerado == linha.distribuido + linha.saldo
