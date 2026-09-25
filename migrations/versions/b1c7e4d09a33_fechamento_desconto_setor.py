"""fechamento_gorjeta: guardar a parte do desconto de setor

Sem esta coluna o snapshot perde de onde veio o desconto rateado por setor,
e o relatório de uma quinzena fechada não fecha com o pool.

Revision ID: b1c7e4d09a33
Revises: 9d4e2b71ac05
Create Date: 2026-09-16 21:30:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1c7e4d09a33'
down_revision = '9d4e2b71ac05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('fechamento_gorjeta', schema=None) as batch_op:
        batch_op.add_column(sa.Column('desconto_setor', sa.Numeric(precision=14, scale=2),
                                      nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('fechamento_gorjeta', schema=None) as batch_op:
        batch_op.drop_column('desconto_setor')
