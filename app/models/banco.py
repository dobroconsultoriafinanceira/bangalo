# -*- coding: utf-8 -*-
"""Extrato bancário gravado (hoje: Itaú PJ), fotografias de saldo e regras de
classificação gerencial.

Separado de `Lancamento` de propósito: o fluxo de caixa vem da planilha e o
extrato é a realidade do banco — gravar o extrato como lançamento contaria o
mesmo dinheiro duas vezes. A conciliação liga os dois por `ConciliacaoItem`,
que aceita vários lançamentos para o mesmo movimento.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db

TIPOS_MOVIMENTO = ("credito", "debito")


class MovimentoBancario(db.Model):
    """Uma linha do extrato. Idempotente por (banco, conta, id_externo)."""

    __tablename__ = "movimento_bancario"
    __table_args__ = (
        UniqueConstraint("banco", "conta", "id_externo", name="uq_movimento_bancario_externo"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    banco: Mapped[str] = mapped_column(String(20), nullable=False)       # "itau"
    conta: Mapped[str] = mapped_column(String(20), nullable=False)       # agência+00+conta+DAC
    id_externo: Mapped[str] = mapped_column(String(80), nullable=False)  # id do banco / agr:<code>
    data: Mapped[date] = mapped_column(Date, nullable=False, index=True)  # data contábil
    tipo: Mapped[str] = mapped_column(
        Enum(*TIPOS_MOVIMENTO, name="tipo_movimento_bancario", native_enum=False), nullable=False
    )
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # sempre positivo
    descricao: Mapped[str | None] = mapped_column(String(255))
    origem: Mapped[str | None] = mapped_column(String(40))        # PIX_RECEPCAO, DEBITO...
    contraparte: Mapped[str | None] = mapped_column(String(160))
    contraparte_documento: Mapped[str | None] = mapped_column(String(20))    # só dígitos
    contraparte_instituicao: Mapped[str | None] = mapped_column(String(80))
    estorno: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # classificação gerencial (services/classificacao_bancaria.py)
    categoria_gerencial: Mapped[str] = mapped_column(
        String(40), nullable=False, default="a_classificar_saida"
    )
    bloco: Mapped[str] = mapped_column(String(20), nullable=False, default="a_classificar", index=True)
    revisar: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # a Manu confere o que o sistema classificou sozinho e marca como revisado
    revisado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    classificacao_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    regra_aplicada: Mapped[str | None] = mapped_column(String(120))
    # linha do caixa escolhida na tela (a mesma da planilha). Quando preenchida,
    # manda no de-para: a categoria gerencial acima e derivada dela.
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))

    # conciliação: um movimento do banco pode corresponder a VÁRIOS lançamentos
    # do fluxo (ex.: um repasse da Stone junta as vendas de vários cartões do dia).
    # Os pares ficam em ConciliacaoItem; aqui fica só o resumo.
    conciliado_em: Mapped[datetime | None] = mapped_column(DateTime)
    # sobra/falta quando a soma dos lançamentos não bate com o valor do movimento
    diferenca_conciliacao: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00"), server_default="0"
    )
    linha = relationship("Categoria")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    itens_conciliacao = relationship(
        "ConciliacaoItem", back_populates="movimento", cascade="all, delete-orphan",
        order_by="ConciliacaoItem.id",
    )

    @property
    def valor_com_sinal(self) -> Decimal:
        return self.valor if self.tipo == "credito" else -self.valor

    @property
    def conciliado(self) -> bool:
        return bool(self.itens_conciliacao)

    @property
    def lancamentos(self) -> list:
        return [i.lancamento for i in self.itens_conciliacao if i.lancamento]

    @property
    def lancamento(self):
        """Compatibilidade: o lançamento único quando a conciliação é 1 para 1."""
        lancs = self.lancamentos
        return lancs[0] if len(lancs) == 1 else None

    @property
    def lancamento_proprio(self):
        """O lançamento que ESTE movimento gerou no caixa.

        Diferente de `lancamento`, que só responde quando a conciliação é 1
        para 1: quando o movimento também está amarrado a lançamentos de outra
        fonte (planilha, Stone), é este aqui que diz a linha do próprio
        movimento.
        """
        for item in self.itens_conciliacao:
            lanc = item.lancamento
            if lanc is not None and lanc.origem_id == f"{self.id}:mov":
                return lanc
        return None

    @property
    def conciliacao_trivial(self) -> bool:
        """O único lançamento ligado é o que este movimento gerou.

        É o caso do pagamento comum do Itaú, que vira uma linha do caixa e
        pronto — não há o que conferir. Repasse da Stone (cartão por bandeira,
        PIX da maquininha) e a quebra do pró-labore amarram lançamentos de
        outra origem, e aí conciliar é trabalho de verdade.
        """
        if len(self.itens_conciliacao) != 1:
            return False
        lanc = self.itens_conciliacao[0].lancamento
        return bool(lanc and lanc.origem_id == f"{self.id}:mov")

    @property
    def total_conciliado(self) -> Decimal:
        return sum((Decimal(l.valor) for l in self.lancamentos), Decimal("0.00"))


class SaldoBancario(db.Model):
    """Fotografia do saldo a cada sincronização (histórico simples)."""

    __tablename__ = "saldo_bancario"

    id: Mapped[int] = mapped_column(primary_key=True)
    banco: Mapped[str] = mapped_column(String(20), nullable=False)
    conta: Mapped[str] = mapped_column(String(20), nullable=False)
    consultado_em: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)  # horário SP
    data: Mapped[date] = mapped_column(Date, nullable=False)
    disponivel: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    bloqueado: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    aplicacao_automatica: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))


class DreContabilConta(db.Model):
    """Uma conta de resultado do razão da contabilidade, por mês.

    O DRE contábil é reconstruído somando estas contas (ver
    services/dre_contabil.py). Serve para conferir a previsão do sistema e
    para calibrá-la.
    """

    __tablename__ = "dre_contabil_conta"
    __table_args__ = (UniqueConstraint("ano", "mes", "conta", name="uq_dre_contabil_conta"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ano: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    mes: Mapped[int] = mapped_column(Integer, nullable=False)
    linha: Mapped[str] = mapped_column(String(30), nullable=False)  # chave da ESTRUTURA do DRE
    conta: Mapped[str] = mapped_column(String(10), nullable=False)
    classificacao: Mapped[str] = mapped_column(String(30), nullable=False)
    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    lancamentos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    importado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


CAMPOS_REGRA = ("contraparte_documento", "contraparte_nome", "descricao")


class RegraClassificacaoBancaria(db.Model):
    """Regra do usuário: "sempre que <campo> casar <valor>, classificar como X".

    Tem prioridade sobre as regras padrão. `tipo` restringe a crédito/débito.
    """

    __tablename__ = "regra_classificacao_bancaria"

    id: Mapped[int] = mapped_column(primary_key=True)
    campo: Mapped[str] = mapped_column(
        Enum(*CAMPOS_REGRA, name="campo_regra_classificacao", native_enum=False), nullable=False
    )
    valor: Mapped[str] = mapped_column(String(160), nullable=False)
    tipo: Mapped[str | None] = mapped_column(String(10))  # credito | debito | None (ambos)
    categoria: Mapped[str] = mapped_column(String(40), nullable=False)
    # linha do caixa da planilha: "todo pagamento a esta contraparte e Light"
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id", ondelete="SET NULL"))
    # fornecedor do fluxo (pagamentos): "sempre que pagar a CRBS, é a Ambev"
    fornecedor_id: Mapped[int | None] = mapped_column(ForeignKey("fornecedor.id", ondelete="SET NULL"))
    revisar: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    observacao: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    fornecedor = relationship("Fornecedor")
    linha = relationship("Categoria")


class ConciliacaoItem(db.Model):
    """Par movimento do banco ↔ lançamento do fluxo.

    Vários itens no mesmo movimento = uma entrada do banco que junta vários
    lançamentos (repasse de cartão, pagamento de várias notas num PIX só).
    """

    __tablename__ = "conciliacao_item"
    __table_args__ = (
        UniqueConstraint("movimento_id", "lancamento_id", name="uq_conciliacao_item"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    movimento_id: Mapped[int] = mapped_column(
        ForeignKey("movimento_bancario.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lancamento_id: Mapped[int] = mapped_column(
        ForeignKey("lancamento.id", ondelete="CASCADE"), nullable=False, index=True
    )
    automatica: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    criado_por_id: Mapped[int | None] = mapped_column(ForeignKey("usuario.id"))

    movimento = relationship("MovimentoBancario", back_populates="itens_conciliacao")
    lancamento = relationship("Lancamento")
