"""dre_contabil_conta (razão da contabilidade importado)

Revision ID: 7a41c8e55b20
Revises: 5f2ab1c7d3e9
Create Date: 2026-09-16 11:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7a41c8e55b20'
down_revision = '5f2ab1c7d3e9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dre_contabil_conta',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ano', sa.Integer(), nullable=False),
        sa.Column('mes', sa.Integer(), nullable=False),
        sa.Column('linha', sa.String(length=30), nullable=False),
        sa.Column('conta', sa.String(length=10), nullable=False),
        sa.Column('classificacao', sa.String(length=30), nullable=False),
        sa.Column('nome', sa.String(length=120), nullable=False),
        sa.Column('valor', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('lancamentos', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('importado_em', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ano', 'mes', 'conta', name='uq_dre_contabil_conta'),
    )
    with op.batch_alter_table('dre_contabil_conta', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dre_contabil_conta_ano'), ['ano'], unique=False)


def downgrade():
    with op.batch_alter_table('dre_contabil_conta', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_dre_contabil_conta_ano'))
    op.drop_table('dre_contabil_conta')
