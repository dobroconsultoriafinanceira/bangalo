# -*- coding: utf-8 -*-
"""Linha do caixa (planilha) escolhida no movimento e guardada na regra.

Revision ID: c8a4f16b3e90
Revises: b5d21e8a4c37
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op

revision = "c8a4f16b3e90"
down_revision = "b5d21e8a4c37"
branch_labels = None
depends_on = None


def upgrade():
    for tabela in ("movimento_bancario", "regra_classificacao_bancaria"):
        with op.batch_alter_table(tabela) as batch:
            batch.add_column(sa.Column("categoria_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                f"fk_{tabela}_categoria", "categoria", ["categoria_id"], ["id"], ondelete="SET NULL"
            )


def downgrade():
    for tabela in ("movimento_bancario", "regra_classificacao_bancaria"):
        with op.batch_alter_table(tabela) as batch:
            batch.drop_constraint(f"fk_{tabela}_categoria", type_="foreignkey")
            batch.drop_column("categoria_id")
