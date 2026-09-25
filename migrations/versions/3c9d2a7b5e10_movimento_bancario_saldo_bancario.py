"""movimento_bancario, saldo_bancario e regras de classificação (extrato Itaú)

Revision ID: 3c9d2a7b5e10
Revises: e57a93d7abf7
Create Date: 2026-09-15 22:30:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3c9d2a7b5e10'
down_revision = 'e57a93d7abf7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'movimento_bancario',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('banco', sa.String(length=20), nullable=False),
        sa.Column('conta', sa.String(length=20), nullable=False),
        sa.Column('id_externo', sa.String(length=80), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('tipo', sa.Enum('credito', 'debito', name='tipo_movimento_bancario',
                                  native_enum=False), nullable=False),
        sa.Column('valor', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('descricao', sa.String(length=255), nullable=True),
        sa.Column('origem', sa.String(length=40), nullable=True),
        sa.Column('contraparte', sa.String(length=160), nullable=True),
        sa.Column('contraparte_documento', sa.String(length=20), nullable=True),
        sa.Column('contraparte_instituicao', sa.String(length=80), nullable=True),
        sa.Column('estorno', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('categoria_gerencial', sa.String(length=40), nullable=False,
                  server_default='a_classificar_saida'),
        sa.Column('bloco', sa.String(length=20), nullable=False, server_default='a_classificar'),
        sa.Column('revisar', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('classificacao_manual', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('regra_aplicada', sa.String(length=120), nullable=True),
        sa.Column('lancamento_id', sa.Integer(), nullable=True),
        sa.Column('conciliado_em', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['lancamento_id'], ['lancamento.id'],
                                name='fk_movimento_bancario_lancamento', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('banco', 'conta', 'id_externo', name='uq_movimento_bancario_externo'),
    )
    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_movimento_bancario_data'), ['data'], unique=False)
        batch_op.create_index(batch_op.f('ix_movimento_bancario_bloco'), ['bloco'], unique=False)
        batch_op.create_index(batch_op.f('ix_movimento_bancario_lancamento_id'), ['lancamento_id'], unique=False)

    op.create_table(
        'saldo_bancario',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('banco', sa.String(length=20), nullable=False),
        sa.Column('conta', sa.String(length=20), nullable=False),
        sa.Column('consultado_em', sa.DateTime(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('disponivel', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('bloqueado', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column('aplicacao_automatica', sa.Numeric(precision=14, scale=2), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('saldo_bancario', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_saldo_bancario_consultado_em'), ['consultado_em'], unique=False)

    op.create_table(
        'regra_classificacao_bancaria',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campo', sa.Enum('contraparte_documento', 'contraparte_nome', 'descricao',
                                   name='campo_regra_classificacao', native_enum=False), nullable=False),
        sa.Column('valor', sa.String(length=160), nullable=False),
        sa.Column('tipo', sa.String(length=10), nullable=True),
        sa.Column('categoria', sa.String(length=40), nullable=False),
        sa.Column('revisar', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ativo', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('observacao', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('regra_classificacao_bancaria')

    with op.batch_alter_table('saldo_bancario', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_saldo_bancario_consultado_em'))
    op.drop_table('saldo_bancario')

    with op.batch_alter_table('movimento_bancario', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_movimento_bancario_lancamento_id'))
        batch_op.drop_index(batch_op.f('ix_movimento_bancario_bloco'))
        batch_op.drop_index(batch_op.f('ix_movimento_bancario_data'))
    op.drop_table('movimento_bancario')
