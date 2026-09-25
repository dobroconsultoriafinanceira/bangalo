"""conciliação 1→N: itens de conciliação no lugar de movimento.lancamento_id

Revision ID: f3b8c2a97d14
Revises: d9f2b6a4c1e8
Create Date: 2026-09-17 20:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3b8c2a97d14'
down_revision = 'd9f2b6a4c1e8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'conciliacao_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('movimento_id', sa.Integer(), nullable=False),
        sa.Column('lancamento_id', sa.Integer(), nullable=False),
        sa.Column('automatica', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('criado_por_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['movimento_id'], ['movimento_bancario.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lancamento_id'], ['lancamento.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['criado_por_id'], ['usuario.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('movimento_id', 'lancamento_id', name='uq_conciliacao_item'),
    )
    with op.batch_alter_table('conciliacao_item', schema=None) as batch_op:
        batch_op.create_index('ix_conciliacao_item_movimento_id', ['movimento_id'], unique=False)
        batch_op.create_index('ix_conciliacao_item_lancamento_id', ['lancamento_id'], unique=False)

    # as conciliações 1 para 1 já existentes viram itens
    op.execute("""
        INSERT INTO conciliacao_item (movimento_id, lancamento_id, automatica, criado_em)
        SELECT id, lancamento_id, TRUE, COALESCE(conciliado_em, CURRENT_TIMESTAMP)
        FROM movimento_bancario WHERE lancamento_id IS NOT NULL
    """)

    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('diferenca_conciliacao', sa.Numeric(14, 2),
                                      nullable=False, server_default='0'))
        batch_op.drop_index('ix_movimento_bancario_lancamento_id')
        batch_op.drop_column('lancamento_id')


def downgrade():
    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lancamento_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_movimento_bancario_lancamento_id', ['lancamento_id'], unique=False)
        batch_op.drop_column('diferenca_conciliacao')

    # volta só o que era 1 para 1 (o resto não cabe no modelo antigo)
    op.execute("""
        UPDATE movimento_bancario SET lancamento_id = (
            SELECT MIN(lancamento_id) FROM conciliacao_item i WHERE i.movimento_id = movimento_bancario.id
            GROUP BY i.movimento_id HAVING COUNT(*) = 1
        )
    """)
    op.drop_table('conciliacao_item')
