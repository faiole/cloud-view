"""Fase 1: conexões com coleta, fila de jobs, inventário, saúde e ações

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "cloudview_app"
TENANT_VISIBLE = (
    "coalesce(current_setting('app.is_admin', true), 'off') = 'on' "
    "OR {col} = ANY (string_to_array(coalesce(current_setting('app.tenant_ids', true), ''), ','))"
)


def _ts(name: str, **kw):
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    for col in (
        sa.Column("role_arn", sa.String(300)),
        sa.Column("external_id", sa.String(64)),
        sa.Column("member_role_name", sa.String(64), nullable=False, server_default="CloudViewReadOnly"),
        sa.Column("operator_role_name", sa.String(64), nullable=False, server_default="CloudViewOperator"),
        sa.Column("regions", sa.JSON),
        sa.Column("cost_source", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("focus_s3_uri", sa.String(500)),
        sa.Column("collect_invoices", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("last_error", sa.Text),
        _ts("last_sync_at"),
    ):
        op.add_column("connection", col)
    for col in (
        sa.Column("status", sa.String(16)),
        sa.Column("collect", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("last_inventory_at"),
        sa.Column("last_error", sa.Text),
    ):
        op.add_column("cloud_account", col)

    op.create_table(
        "platform_setting",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("connection_id", sa.String(36)),
        sa.Column("cloud_account_id", sa.String(36)),
        sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("dedupe_key", sa.String(200)),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _ts("run_after", nullable=False, server_default=sa.func.now()),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("error", sa.Text),
        sa.Column("result", sa.JSON, nullable=False, server_default="{}"),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_job_claim", "job", ["status", "run_after"])
    op.create_index("ix_job_dedupe", "job", ["dedupe_key", "status"])
    op.create_index("ix_job_connection_id", "job", ["connection_id"])
    # Garante no banco que o mesmo trabalho não fica duas vezes na fila
    op.execute(
        "CREATE UNIQUE INDEX ux_job_active_dedupe ON job (dedupe_key) "
        "WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')"
    )

    op.create_table(
        "resource",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column(
            "cloud_account_id",
            sa.String(36),
            sa.ForeignKey("cloud_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(300)),
        sa.Column("state", sa.String(32)),
        sa.Column("instance_type", sa.String(64)),
        sa.Column("vcpu", sa.Integer),
        sa.Column("memory_mib", sa.Integer),
        sa.Column("platform", sa.String(64)),
        sa.Column("private_ips", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("public_ips", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("vpc_id", sa.String(64)),
        sa.Column("subnet_id", sa.String(64)),
        sa.Column("security_groups", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("findings", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("health", sa.String(16)),
        sa.Column("health_details", sa.JSON, nullable=False, server_default="{}"),
        _ts("first_seen", nullable=False, server_default=sa.func.now()),
        _ts("collected_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("cloud_account_id", "region", "type", "native_id"),
    )
    op.create_index("ix_resource_tenant_type", "resource", ["tenant_id", "type"])
    op.create_index("ix_resource_cloud_account_id", "resource", ["cloud_account_id"])

    op.create_table(
        "health_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column(
            "cloud_account_id",
            sa.String(36),
            sa.ForeignKey("cloud_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_id", sa.String(36)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(400), nullable=False),
        sa.Column("service", sa.String(64)),
        sa.Column("region", sa.String(32)),
        sa.Column("category", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text),
        _ts("start_at"),
        _ts("end_at"),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "native_id", "cloud_account_id"),
    )
    op.create_index("ix_health_event_tenant", "health_event", ["tenant_id", "status"])

    op.create_table(
        "action_request",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("resource_native_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.String(320), nullable=False),
        _ts("requested_at", nullable=False, server_default=sa.func.now()),
        _ts("finished_at"),
        sa.Column("result", sa.JSON, nullable=False, server_default="{}"),
        sa.CheckConstraint("action IN ('reboot', 'start', 'stop')", name="ck_action_request_action"),
    )
    op.create_index("ix_action_request_tenant_id", "action_request", ["tenant_id"])

    for table in ("resource", "health_event", "action_request"):
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
            GRANT SELECT, INSERT, UPDATE, DELETE ON platform_setting, job, resource, health_event, action_request
              TO {APP_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for t in ("action_request", "health_event", "resource", "job", "platform_setting"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    for c in ("last_error", "last_inventory_at", "collect", "status"):
        op.drop_column("cloud_account", c)
    for c in (
        "last_sync_at",
        "last_error",
        "status",
        "collect_invoices",
        "focus_s3_uri",
        "cost_source",
        "regions",
        "operator_role_name",
        "member_role_name",
        "external_id",
        "role_arn",
    ):
        op.drop_column("connection", c)
