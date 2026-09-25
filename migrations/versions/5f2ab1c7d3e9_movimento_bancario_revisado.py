"""movimento_bancario.revisado (fila de revisão da classificação automática)

Revision ID: 5f2ab1c7d3e9
Revises: 3c9d2a7b5e10
Create Date: 2026-09-16 09:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5f2ab1c7d3e9'
down_revision = '3c9d2a7b5e10'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('revisado', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.create_index(batch_op.f('ix_movimento_bancario_revisado'), ['revisado'], unique=False)


def downgrade():
    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_movimento_bancario_revisado'))
        batch_op.drop_column('revisado')
