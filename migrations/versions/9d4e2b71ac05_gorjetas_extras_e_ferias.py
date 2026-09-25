"""gorjetas: extras por diária, férias e novos campos do fechamento

Revision ID: 9d4e2b71ac05
Revises: 7a41c8e55b20
Create Date: 2026-09-16 17:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9d4e2b71ac05'
down_revision = '7a41c8e55b20'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'extra_quinzena',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('periodo_id', sa.Integer(), nullable=False),
        sa.Column('setor_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('turno', sa.String(length=30), nullable=True),
        sa.Column('pontos', sa.Numeric(precision=4, scale=2), nullable=False, server_default='1.5'),
        sa.Column('comissao_turno', sa.Numeric(precision=14, scale=2), nullable=False, server_default='0'),
        sa.Column('valor_pago', sa.Numeric(precision=14, scale=2), nullable=False, server_default='0'),
        sa.Column('observacao', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['periodo_id'], ['periodo_gorjeta.id'], name='fk_extra_periodo'),
        sa.ForeignKeyConstraint(['setor_id'], ['setor.id'], name='fk_extra_setor'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('extra_quinzena', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_extra_quinzena_periodo_id'), ['periodo_id'], unique=False)

    with op.batch_alter_table('participacao_periodo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('em_ferias', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('pontos', sa.Numeric(precision=4, scale=2), nullable=True))

    with op.batch_alter_table('fechamento_gorjeta', schema=None) as batch_op:
        batch_op.add_column(sa.Column('desconto_extra', sa.Numeric(precision=14, scale=2),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('reembolso_ferias', sa.Numeric(precision=14, scale=2),
                                      nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('fechamento_gorjeta', schema=None) as batch_op:
        batch_op.drop_column('reembolso_ferias')
        batch_op.drop_column('desconto_extra')

    with op.batch_alter_table('participacao_periodo', schema=None) as batch_op:
        batch_op.drop_column('pontos')
        batch_op.drop_column('em_ferias')

    with op.batch_alter_table('extra_quinzena', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_extra_quinzena_periodo_id'))
    op.drop_table('extra_quinzena')
