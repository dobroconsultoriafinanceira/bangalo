"""clareza: documentos contábeis, lançamentos do razão e exportações

Revision ID: c4e8a1f2b7d6
Revises: b1c7e4d09a33
Create Date: 2026-09-17 09:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4e8a1f2b7d6'
down_revision = 'b1c7e4d09a33'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'documento_contabil',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ano', sa.Integer(), nullable=False),
        sa.Column('mes', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.Enum('razao', 'dre_pdf', 'outro', name='tipo_documento_contabil', native_enum=False), nullable=False),
        sa.Column('nome_arquivo', sa.String(length=200), nullable=False),
        sa.Column('caminho', sa.String(length=300), nullable=False),
        sa.Column('tamanho', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('situacao', sa.Enum('recebido', 'conferido', 'aprovado', name='situacao_documento_contabil', native_enum=False), nullable=False, server_default='recebido'),
        sa.Column('observacao', sa.Text(), nullable=True),
        sa.Column('recebido_em', sa.DateTime(), nullable=False),
        sa.Column('enviado_por_id', sa.Integer(), nullable=True),
        sa.Column('conferido_por_id', sa.Integer(), nullable=True),
        sa.Column('conferido_em', sa.DateTime(), nullable=True),
        sa.Column('aprovado_por_id', sa.Integer(), nullable=True),
        sa.Column('aprovado_em', sa.DateTime(), nullable=True),
        sa.Column('substituido_em', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['enviado_por_id'], ['usuario.id'], name='fk_doc_enviado_por'),
        sa.ForeignKeyConstraint(['conferido_por_id'], ['usuario.id'], name='fk_doc_conferido_por'),
        sa.ForeignKeyConstraint(['aprovado_por_id'], ['usuario.id'], name='fk_doc_aprovado_por'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('documento_contabil', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documento_contabil_ano'), ['ano'], unique=False)

    op.create_table(
        'razao_lancamento',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ano', sa.Integer(), nullable=False),
        sa.Column('mes', sa.Integer(), nullable=False),
        sa.Column('conta', sa.String(length=10), nullable=False),
        sa.Column('classificacao', sa.String(length=30), nullable=False),
        sa.Column('nome', sa.String(length=120), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('historico', sa.String(length=300), nullable=True),
        sa.Column('contrapartida', sa.String(length=20), nullable=True),
        sa.Column('debito', sa.Numeric(precision=14, scale=2), nullable=False, server_default='0'),
        sa.Column('credito', sa.Numeric(precision=14, scale=2), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('razao_lancamento', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_razao_lancamento_ano'), ['ano'], unique=False)
        batch_op.create_index(batch_op.f('ix_razao_lancamento_conta'), ['conta'], unique=False)

    op.create_table(
        'exportacao_relatorio',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=40), nullable=False),
        sa.Column('titulo', sa.String(length=200), nullable=False),
        sa.Column('parametros', sa.JSON(), nullable=True),
        sa.Column('nome_arquivo', sa.String(length=200), nullable=True),
        sa.Column('caminho', sa.String(length=300), nullable=True),
        sa.Column('tamanho', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.Enum('gerado', 'falhou', name='status_exportacao', native_enum=False), nullable=False),
        sa.Column('erro', sa.String(length=300), nullable=True),
        sa.Column('gerado_por_id', sa.Integer(), nullable=True),
        sa.Column('gerado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['gerado_por_id'], ['usuario.id'], name='fk_exportacao_usuario'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('exportacao_relatorio', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exportacao_relatorio_tipo'), ['tipo'], unique=False)
        batch_op.create_index(batch_op.f('ix_exportacao_relatorio_gerado_em'), ['gerado_em'], unique=False)


def downgrade():
    with op.batch_alter_table('exportacao_relatorio', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exportacao_relatorio_gerado_em'))
        batch_op.drop_index(batch_op.f('ix_exportacao_relatorio_tipo'))
    op.drop_table('exportacao_relatorio')
    with op.batch_alter_table('razao_lancamento', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_razao_lancamento_conta'))
        batch_op.drop_index(batch_op.f('ix_razao_lancamento_ano'))
    op.drop_table('razao_lancamento')
    with op.batch_alter_table('documento_contabil', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documento_contabil_ano'))
    op.drop_table('documento_contabil')
