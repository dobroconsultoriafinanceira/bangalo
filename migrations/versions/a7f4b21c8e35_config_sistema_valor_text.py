"""config_sistema.valor passa a Text

Os parametros de previsao do DRE sao gravados como JSON nessa coluna e ja
ocupam mais de 400 caracteres. Em SQLite o limite de String(255) nunca foi
aplicado; no Postgres a linha era recusada (StringDataRightTruncation).

Revision ID: a7f4b21c8e35
Revises: e91c3a75d0b4
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "a7f4b21c8e35"
down_revision = "e91c3a75d0b4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("config_sistema", schema=None) as batch_op:
        batch_op.alter_column("valor",
                              existing_type=sa.String(length=255),
                              type_=sa.Text(),
                              existing_nullable=False)


def downgrade():
    with op.batch_alter_table("config_sistema", schema=None) as batch_op:
        batch_op.alter_column("valor",
                              existing_type=sa.Text(),
                              type_=sa.String(length=255),
                              existing_nullable=False)
