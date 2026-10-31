"""Fase 3: conexões Azure (app próprio do cliente e conta de cobrança)

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connection", sa.Column("client_id", sa.String(64)))
    op.add_column("connection", sa.Column("billing_account_id", sa.String(300)))
    # IDs de recurso do Azure são longos (/subscriptions/.../resourceGroups/.../providers/...)
    op.alter_column("resource", "native_id", type_=sa.String(400))
    op.alter_column("resource", "vpc_id", type_=sa.String(400))
    op.alter_column("resource", "subnet_id", type_=sa.String(400))
    op.alter_column("resource", "instance_type", type_=sa.String(100))
    op.alter_column("health_event", "region", type_=sa.String(64))
    op.alter_column("action_request", "resource_native_id", type_=sa.String(400))
    op.alter_column("recommendation", "resource_native_id", type_=sa.String(500))


def downgrade() -> None:
    op.drop_column("connection", "billing_account_id")
    op.drop_column("connection", "client_id")
