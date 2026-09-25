# -*- coding: utf-8 -*-
"""Saldo de arredondamento por setor: centavos guardados em vez de sumirem.

Revision ID: e91c3a75d0b4
Revises: d4b7c91f2a68
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "e91c3a75d0b4"
down_revision = "d4b7c91f2a68"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "saldo_setor_quinzena",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("periodo_id", sa.Integer(), nullable=False),
        sa.Column("setor_id", sa.Integer(), nullable=False),
        sa.Column("anterior", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("gerado", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("distribuido", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("saldo", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("pessoas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["periodo_id"], ["periodo_gorjeta.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["setor_id"], ["setor.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("periodo_id", "setor_id", name="uq_saldo_setor_quinzena"),
    )
    op.create_index("ix_saldo_setor_quinzena_periodo_id", "saldo_setor_quinzena", ["periodo_id"])
    op.create_index("ix_saldo_setor_quinzena_setor_id", "saldo_setor_quinzena", ["setor_id"])


def downgrade():
    op.drop_index("ix_saldo_setor_quinzena_setor_id", table_name="saldo_setor_quinzena")
    op.drop_index("ix_saldo_setor_quinzena_periodo_id", table_name="saldo_setor_quinzena")
    op.drop_table("saldo_setor_quinzena")
