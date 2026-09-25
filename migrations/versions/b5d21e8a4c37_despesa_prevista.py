"""despesas previstas (projeção de saídas)

Revision ID: b5d21e8a4c37
Revises: a1c7f3e95b42
Create Date: 2026-09-24 11:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = 'b5d21e8a4c37'
down_revision = 'a1c7f3e95b42'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'despesa_prevista',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('data_prevista', sa.Date(), nullable=False),
        sa.Column('categoria_id', sa.Integer(), nullable=False),
        sa.Column('fornecedor_id', sa.Integer(), nullable=True),
        sa.Column('descricao', sa.String(length=255), nullable=True),
        sa.Column('valor', sa.Numeric(14, 2), nullable=False),
        sa.Column('situacao', sa.Enum('prevista', 'baixada', 'cancelada',
                                      name='situacao_despesa_prevista', native_enum=False),
                  nullable=False, server_default='prevista'),
        sa.Column('movimento_id', sa.Integer(), nullable=True),
        sa.Column('baixada_em', sa.DateTime(), nullable=True),
        sa.Column('baixa_automatica', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('aviso_pendente', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('criado_por_id', sa.Integer(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['categoria_id'], ['categoria.id']),
        sa.ForeignKeyConstraint(['fornecedor_id'], ['fornecedor.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['movimento_id'], ['movimento_bancario.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['criado_por_id'], ['usuario.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('despesa_prevista', schema=None) as batch_op:
        batch_op.create_index('ix_despesa_prevista_data', ['data_prevista'], unique=False)
        batch_op.create_index('ix_despesa_prevista_situacao', ['situacao'], unique=False)
        batch_op.create_index('ix_despesa_prevista_aviso', ['aviso_pendente'], unique=False)


def downgrade():
    op.drop_table('despesa_prevista')
