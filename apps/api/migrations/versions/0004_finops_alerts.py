"""Fase 2: recomendações, cobertura de Savings Plans, orçamentos, alertas e notificações

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "cloudview_app"
TENANT_VISIBLE = (
    "coalesce(current_setting('app.is_admin', true), 'off') = 'on' "
    "OR {col} = ANY (string_to_array(coalesce(current_setting('app.tenant_ids', true), ''), ','))"
)
MONEY = sa.Numeric(18, 6)
TABLES = (
    "recommendation",
    "commitment_coverage",
    "budget",
    "alert",
    "notification_channel",
    "notification_delivery",
)


def _ts(name, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "recommendation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column("cloud_account_id", sa.String(36), sa.ForeignKey("cloud_account.id", ondelete="CASCADE")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(300), nullable=False),
        sa.Column("action_type", sa.String(40), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_native_id", sa.String(300)),
        sa.Column("region", sa.String(32)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("current", sa.String(300)),
        sa.Column("recommended", sa.String(300)),
        sa.Column("monthly_savings", MONEY, nullable=False, server_default="0"),
        sa.Column("monthly_cost", MONEY),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("savings_pct", sa.Numeric(7, 2)),
        sa.Column("effort", sa.String(16)),
        sa.Column("restart_needed", sa.Boolean),
        sa.Column("estimated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("status_by", sa.String(320)),
        sa.Column("status_note", sa.String(500)),
        _ts("first_seen", nullable=False, server_default=sa.func.now()),
        _ts("last_seen", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "native_id"),
    )
    op.create_index("ix_recommendation_tenant_status", "recommendation", ["tenant_id", "status"])
    op.create_index("ix_recommendation_cloud_account_id", "recommendation", ["cloud_account_id"])

    op.create_table(
        "commitment_coverage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column(
            "cloud_account_id",
            sa.String(36),
            sa.ForeignKey("cloud_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("covered", MONEY, nullable=False),
        sa.Column("on_demand", MONEY, nullable=False),
        sa.Column("total", MONEY, nullable=False),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("cloud_account_id", "month"),
    )
    op.create_index("ix_commitment_coverage_tenant_id", "commitment_coverage", ["tenant_id"])

    op.create_table(
        "budget",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("scope", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("thresholds", sa.JSON, nullable=False, server_default="[50, 80, 100]"),
        sa.Column("alert_on_forecast", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(320)),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.Column("last_actual", MONEY),
        sa.Column("last_forecast", MONEY),
        _ts("last_evaluated_at"),
    )
    op.create_index("ix_budget_tenant_id", "budget", ["tenant_id"])

    op.create_table(
        "alert",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("dedupe_key", sa.String(300), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("status_by", sa.String(320)),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        _ts("resolved_at"),
    )
    op.create_index("ix_alert_tenant_status", "alert", ["tenant_id", "status"])

    op.create_table(
        "notification_channel",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("emails", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("secret_ref", sa.String(36)),
        sa.Column("target_hint", sa.String(200)),
        sa.Column("min_severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("categories", sa.JSON, nullable=False, server_default='["cost", "health", "security"]'),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(320)),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_notification_channel_tenant_id", "notification_channel", ["tenant_id"])

    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alert_id", sa.String(36)),
        sa.Column(
            "channel_id",
            sa.String(36),
            sa.ForeignKey("notification_channel.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text),
        _ts("sent_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("alert_id", "channel_id"),
    )
    op.create_index("ix_notification_delivery_alert_id", "notification_delivery", ["alert_id"])

    for table in ("recommendation", "commitment_coverage", "budget", "alert", "notification_channel"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_VISIBLE.format(col='tenant_id')})"
        )

    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON {", ".join(TABLES)} TO {APP_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
