"""usuários: permissões por usuário, versão de sessão e tentativas de login

Revision ID: d9f2b6a4c1e8
Revises: c4e8a1f2b7d6
Create Date: 2026-09-17 10:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9f2b6a4c1e8'
down_revision = 'c4e8a1f2b7d6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('usuario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('permissoes', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('sessao_versao', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('senha_alterada_em', sa.DateTime(), nullable=True))

    op.create_table(
        'tentativa_login',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tentativa_login', schema=None) as batch_op:
        batch_op.create_index('ix_tentativa_login_email', ['email'], unique=False)
        batch_op.create_index('ix_tentativa_login_ip', ['ip'], unique=False)
        batch_op.create_index('ix_tentativa_login_criado_em', ['criado_em'], unique=False)


def downgrade():
    op.drop_table('tentativa_login')
    with op.batch_alter_table('usuario', schema=None) as batch_op:
        batch_op.drop_column('senha_alterada_em')
        batch_op.drop_column('sessao_versao')
        batch_op.drop_column('permissoes')
