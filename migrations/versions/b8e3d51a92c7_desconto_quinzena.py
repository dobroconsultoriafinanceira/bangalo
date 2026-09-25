"""cria desconto_quinzena (tabela existia no modelo, mas em nenhuma migration)

A tabela nasceu num `create_all()` de desenvolvimento e nunca ganhou migration.
Bancos criados do zero pelas migrations — como o de producao — subiam sem ela,
e a tela de descontos por setor quebraria no primeiro uso.

Revision ID: b8e3d51a92c7
Revises: a7f4b21c8e35
Create Date: 2026-09-25
"""
import sqlalchemy as sa
from alembic import op

revision = "b8e3d51a92c7"
down_revision = "a7f4b21c8e35"
branch_labels = None
depends_on = None

TIPOS = ("Perda", "Avaria", "Vale", "Desconto")


def upgrade():
    # `if_not_exists` porque o banco de desenvolvimento ja tem a tabela vinda
    # do create_all(): sem isso, esta migration falharia justamente la.
    if "desconto_quinzena" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        "desconto_quinzena",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("periodo_id", sa.Integer(), nullable=False),
        sa.Column("setor_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.Enum(*TIPOS, name="tipo_desconto_setor",
                                  native_enum=False), nullable=False),
        sa.Column("valor", sa.Numeric(14, 2), nullable=False),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["periodo_id"], ["periodo_gorjeta.id"]),
        sa.ForeignKeyConstraint(["setor_id"], ["setor.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("desconto_quinzena", schema=None) as batch_op:
        batch_op.create_index("ix_desconto_quinzena_periodo_id",
                              ["periodo_id"], unique=False)


def downgrade():
    op.drop_table("desconto_quinzena")
