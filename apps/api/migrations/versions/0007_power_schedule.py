"""Fase 5: agenda liga/desliga

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "cloudview_app"
TENANT_VISIBLE = (
    "coalesce(current_setting('app.is_admin', true), 'off') = 'on' "
    "OR {col} = ANY (string_to_array(coalesce(current_setting('app.tenant_ids', true), ''), ','))"
)


def upgrade() -> None:
    op.create_table(
        "power_schedule",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Sao_Paulo"),
        sa.Column("days", sa.JSON, nullable=False, server_default="[0, 1, 2, 3, 4]"),
        sa.Column("start_time", sa.String(5)),
        sa.Column("stop_time", sa.String(5)),
        sa.Column("selector_tag", sa.String(300)),
        sa.Column("resource_ids", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_start_on", sa.Date),
        sa.Column("last_stop_on", sa.Date),
    )
    op.create_index("ix_power_schedule_tenant_id", "power_schedule", ["tenant_id"])
    op.execute("ALTER TABLE power_schedule ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE power_schedule FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON power_schedule USING ({TENANT_VISIBLE.format(col='tenant_id')})"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON power_schedule TO {APP_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS power_schedule CASCADE")
