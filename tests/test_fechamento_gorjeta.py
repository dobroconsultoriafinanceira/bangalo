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
