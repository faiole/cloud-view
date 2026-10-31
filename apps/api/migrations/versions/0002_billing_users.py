"""Papéis com permissões, administração de usuários, contas/atribuições, custos e documentos

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "cloudview_app"
TENANT_VISIBLE = (
    "coalesce(current_setting('app.is_admin', true), 'off') = 'on' "
    "OR {col} = ANY (string_to_array(coalesce(current_setting('app.tenant_ids', true), ''), ','))"
)
ROLES = ("tenant_admin", "operator", "finance", "viewer")


def upgrade() -> None:
    # --- Papéis: enum fixo → texto validado (permite novos papéis sem ALTER TYPE) --------------
    op.execute("ALTER TABLE user_tenant_role ALTER COLUMN role TYPE varchar(32) USING role::text")
    op.execute("UPDATE user_tenant_role SET role = 'viewer' WHERE role = 'client'")
    op.execute("DROP TYPE IF EXISTS tenant_role")
    op.create_check_constraint(
        "ck_user_tenant_role_role", "user_tenant_role", "role IN (" + ",".join(f"'{r}'" for r in ROLES) + ")"
    )

    op.add_column("app_user", sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("app_user", sa.Column("invited_at", sa.DateTime(timezone=True)))

    # --- Conexões e contas ---------------------------------------------------------------------
    op.create_table(
        "connection",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("secret_ref", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "native_id"),
    )
    op.create_table(
        "cloud_secret",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("sealed", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "cloud_account",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("native_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("connection.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "native_id"),
    )
    op.create_index("ix_cloud_account_connection_id", "cloud_account", ["connection_id"])
    op.create_table(
        "account_assignment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "cloud_account_id",
            sa.String(36),
            sa.ForeignKey("cloud_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_key", sa.String(128)),
        sa.Column("tag_value", sa.String(256)),
        sa.Column("valid_from", sa.Date),
        sa.Column("valid_to", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("(tag_key IS NULL) = (tag_value IS NULL)", name="ck_assignment_tag_pair"),
    )
    op.create_index("ix_account_assignment_cloud_account_id", "account_assignment", ["cloud_account_id"])
    op.create_index("ix_account_assignment_tenant_id", "account_assignment", ["tenant_id"])

    # --- Custos --------------------------------------------------------------------------------
    op.create_table(
        "import_batch",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16)),
        sa.Column("filename", sa.String(300)),
        sa.Column("billing_account_id", sa.String(128)),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("source_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stored_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("replaced_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "cost_line",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column(
            "cloud_account_id",
            sa.String(36),
            sa.ForeignKey("cloud_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("billing_account_id", sa.String(128), nullable=False),
        sa.Column("charge_date", sa.Date, nullable=False),
        sa.Column("service", sa.String(200), nullable=False),
        sa.Column("region", sa.String(64)),
        sa.Column("charge_category", sa.String(32)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("billed_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("effective_cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("import_batch_id", sa.String(36)),
    )
    op.create_index("ix_cost_line_tenant_date", "cost_line", ["tenant_id", "charge_date"])
    op.create_index("ix_cost_line_account_date", "cost_line", ["cloud_account_id", "charge_date"])
    op.create_index(
        "ix_cost_line_billing_date", "cost_line", ["provider", "billing_account_id", "charge_date"]
    )
    op.execute("CREATE INDEX ix_cost_line_tags ON cost_line USING gin (tags jsonb_path_ops)")

    # --- Documentos ----------------------------------------------------------------------------
    op.create_table(
        "document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16)),
        sa.Column("billing_period", sa.Date, nullable=False),
        sa.Column("number", sa.String(100)),
        sa.Column("issuer", sa.String(200)),
        sa.Column("amount", sa.Numeric(18, 6)),
        sa.Column("currency", sa.String(3)),
        sa.Column("due_date", sa.Date),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("uploaded_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "sha256"),
    )
    op.create_index("ix_document_tenant_period", "document", ["tenant_id", "billing_period"])

    # --- RLS: custos e documentos só do(s) cliente(s) do usuário ----------------------------------
    for table in ("cost_line", "document"):
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
            GRANT SELECT, INSERT, UPDATE, DELETE
              ON connection, cloud_account, account_assignment, import_batch, cost_line, document TO {APP_ROLE};
            GRANT SELECT, INSERT, DELETE ON cloud_secret TO {APP_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for t in (
        "document",
        "cost_line",
        "import_batch",
        "account_assignment",
        "cloud_account",
        "cloud_secret",
        "connection",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.drop_column("app_user", "invited_at")
    op.drop_column("app_user", "disabled")
    op.drop_constraint("ck_user_tenant_role_role", "user_tenant_role")
    tenant_role = postgresql.ENUM("operator", "viewer", "client", name="tenant_role")
    tenant_role.create(op.get_bind())
    op.execute("UPDATE user_tenant_role SET role = 'operator' WHERE role = 'tenant_admin'")
    op.execute("UPDATE user_tenant_role SET role = 'viewer' WHERE role = 'finance'")
    op.execute("ALTER TABLE user_tenant_role ALTER COLUMN role TYPE tenant_role USING role::tenant_role")
