# -*- coding: utf-8 -*-
"""Função e setor da quinzena na participação (o cadastro atual não reescreve o passado).

Revision ID: d4b7c91f2a68
Revises: c8a4f16b3e90
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "d4b7c91f2a68"
down_revision = "c8a4f16b3e90"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("participacao_periodo") as batch:
        batch.add_column(sa.Column("funcao_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("setor_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_participacao_funcao", "funcao", ["funcao_id"], ["id"],
                                 ondelete="SET NULL")
        batch.create_foreign_key("fk_participacao_setor", "setor", ["setor_id"], ["id"],
                                 ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("participacao_periodo") as batch:
        batch.drop_constraint("fk_participacao_setor", type_="foreignkey")
        batch.drop_constraint("fk_participacao_funcao", type_="foreignkey")
        batch.drop_column("setor_id")
        batch.drop_column("funcao_id")
