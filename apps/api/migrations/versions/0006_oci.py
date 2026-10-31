"""Fase 4: conexões OCI (usuário/chave de API, região home)

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connection", sa.Column("oci_auth", sa.String(16)))  # own_key | hub
    op.add_column("connection", sa.Column("oci_user_id", sa.String(200)))
    op.add_column("connection", sa.Column("oci_fingerprint", sa.String(64)))
    op.add_column("connection", sa.Column("oci_public_key", sa.Text))
    op.add_column("connection", sa.Column("home_region", sa.String(32)))


def downgrade() -> None:
    for c in ("home_region", "oci_public_key", "oci_fingerprint", "oci_user_id", "oci_auth"):
        op.drop_column("connection", c)
