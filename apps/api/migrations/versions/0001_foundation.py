"""Fundação: clientes, usuários, papéis, auditoria + RLS

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "cloudview_app"

# Expressão reutilizada nas políticas de RLS. current_setting(..., true) retorna NULL se não definido,
# então uma conexão sem escopo não enxerga nada.
TENANT_VISIBLE = (
    "coalesce(current_setting('app.is_admin', true), 'off') = 'on' "
    "OR {col} = ANY (string_to_array(coalesce(current_setting('app.tenant_ids', true), ''), ','))"
)


def upgrade() -> None:
    tenant_role = postgresql.ENUM("operator", "viewer", "client", name="tenant_role")
    tenant_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenant",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("aws_external_id", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_slug", "tenant", ["slug"])

    op.create_table(
        "app_user",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("keycloak_sub", sa.String(64), unique=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_app_user_email", "app_user", ["email"])

    op.create_table(
        "user_tenant_role",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", postgresql.ENUM(name="tenant_role", create_type=False), nullable=False),
        sa.UniqueConstraint("user_id", "tenant_id"),
    )
    op.create_index("ix_user_tenant_role_user_id", "user_tenant_role", ["user_id"])
    op.create_index("ix_user_tenant_role_tenant_id", "user_tenant_role", ["tenant_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("user_id", sa.String(36)),
        sa.Column("user_email", sa.String(320)),
        sa.Column("tenant_id", sa.String(36)),
        sa.Column("ip", sa.String(64)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target", sa.String(300)),
        sa.Column("details", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="success"),
        sa.Column("user_agent", sa.Text),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    # --- Auditoria imutável ---------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();"
    )

    # --- Row-Level Security nas tabelas de dados por cliente ----------------------------------------
    op.execute("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON tenant USING ({TENANT_VISIBLE.format(col='id')})")
    # Novas tabelas por cliente (conexões, recursos, custos...) devem repetir este padrão com tenant_id.

    # --- Usuário da aplicação: só DML, nunca DDL ---------------------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON tenant, app_user, user_tenant_role TO {APP_ROLE};
            GRANT SELECT, INSERT ON audit_log TO {APP_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable()")
    op.drop_table("user_tenant_role")
    op.drop_table("app_user")
    op.drop_table("tenant")
    postgresql.ENUM(name="tenant_role").drop(op.get_bind(), checkfirst=True)
