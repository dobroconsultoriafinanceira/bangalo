# -*- coding: utf-8 -*-
"""Despesas previstas: o que já foi combinado e ainda não saiu da conta.

O extrato do Itaú só mostra o que já aconteceu — pagamento agendado no
internet banking não aparece em lugar nenhum da API (testado em 24/09/2026).
Então a projeção de saídas vem daqui: a Manu cadastra o que está combinado
(fornecedor, valor, data) e o sistema gera o lançamento previsto no caixa.

Quando o pagamento de verdade cai no extrato, o sistema casa os dois, dá baixa
e tira a previsão do caixa — sem contar o mesmo dinheiro duas vezes.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

SITUACOES = ("prevista", "baixada", "cancelada")


class DespesaPrevista(db.Model):
    __tablename__ = "despesa_prevista"

    id: Mapped[int] = mapped_column(primary_key=True)
    data_prevista: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    categoria_id: Mapped[int] = mapped_column(ForeignKey("categoria.id"), nullable=False)
    fornecedor_id: Mapped[int | None] = mapped_column(ForeignKey("fornecedor.id", ondelete="SET NULL"))
    descricao: Mapped[str | None] = mapped_column(String(255))
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    situacao: Mapped[str] = mapped_column(
        Enum(*SITUACOES, name="situacao_despesa_prevista", native_enum=False),
        nullable=False, default="prevista", index=True,
    )
    # baixa: qual movimento do banco pagou isto
    movimento_id: Mapped[int | None] = mapped_column(
        ForeignKey("movimento_bancario.id", ondelete="SET NULL"))
    baixada_em: Mapped[datetime | None] = mapped_column(DateTime)
    baixa_automatica: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # a Manu ainda não viu que o sistema deu baixa sozinho
    aviso_pendente: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    criado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    categoria = relationship("Categoria")
    fornecedor = relationship("Fornecedor")
    movimento = relationship("MovimentoBancario")

    @property
    def em_aberto(self) -> bool:
        return self.situacao == "prevista"

    @property
    def atrasada(self) -> bool:
        from app.utils.datas import hoje_sp

        return self.em_aberto and self.data_prevista < hoje_sp()
