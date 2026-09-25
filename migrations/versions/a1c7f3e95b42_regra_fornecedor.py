"""regra bancária guarda o fornecedor do fluxo

Revision ID: a1c7f3e95b42
Revises: f3b8c2a97d14
Create Date: 2026-09-24 09:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1c7f3e95b42'
down_revision = 'f3b8c2a97d14'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('regra_classificacao_bancaria', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fornecedor_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_regra_fornecedor', 'fornecedor', ['fornecedor_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('regra_classificacao_bancaria', schema=None) as batch_op:
        batch_op.drop_constraint('fk_regra_fornecedor', type_='foreignkey')
        batch_op.drop_column('fornecedor_id')
