"""Integração com Postgres real: migrações, RLS e auditoria imutável.

Roda quando CV_TEST_PG_OWNER_URL e CV_TEST_PG_APP_URL estão definidos (CI usa um container Postgres).
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

OWNER = os.environ.get("CV_TEST_PG_OWNER_URL")
APP = os.environ.get("CV_TEST_PG_APP_URL")

pytestmark = pytest.mark.skipif(not (OWNER and APP), reason="Postgres de teste não configurado")


@pytest.fixture(scope="module")
def engines():
    env = {**os.environ, "CV_MIGRATION_DATABASE_URL": OWNER}
    here = os.path.dirname(os.path.dirname(__file__))
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], cwd=here, env=env, check=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=here, env=env, check=True)
    owner, app = create_engine(OWNER), create_engine(APP)
    yield owner, app
    owner.dispose()
    app.dispose()


def _scoped(conn, tenants: str, admin: bool):
    conn.execute(
        text("SELECT set_config('app.tenant_ids', :t, true), set_config('app.is_admin', :a, true)"),
        {"t": tenants, "a": "on" if admin else "off"},
    )


def test_rls_isolates_tenants(engines):
    _, app = engines
    with app.begin() as c:
        _scoped(c, "", True)
        c.execute(text("DELETE FROM tenant"))
        c.execute(
            text(
                "INSERT INTO tenant (id, name, slug, aws_external_id) VALUES "
                "('a', 'A', 'a', 'x1'), ('b', 'B', 'b', 'x2')"
            )
        )

    with app.begin() as c:
        _scoped(c, "a", False)
        assert c.execute(text("SELECT slug FROM tenant")).scalars().all() == ["a"]

    with app.begin() as c:
        # Sem escopo definido, nada aparece
        assert c.execute(text("SELECT count(*) FROM tenant")).scalar() == 0

    with app.begin() as c:
        _scoped(c, "a", False)
        # Não consegue alterar nem criar dados de outro cliente
        assert c.execute(text("UPDATE tenant SET name = 'x' WHERE id = 'b'")).rowcount == 0
        with pytest.raises(DBAPIError):
            c.execute(text("INSERT INTO tenant (id, name, slug, aws_external_id) VALUES ('c','C','c','x3')"))

    with app.begin() as c:
        _scoped(c, "", True)
        assert c.execute(text("SELECT count(*) FROM tenant")).scalar() == 2


def test_scope_does_not_leak_between_transactions(engines):
    _, app = engines
    with app.connect() as c:
        with c.begin():
            _scoped(c, "", True)
            assert c.execute(text("SELECT count(*) FROM tenant")).scalar() == 2
        with c.begin():
            assert c.execute(text("SELECT count(*) FROM tenant")).scalar() == 0


def test_audit_log_is_append_only(engines):
    _, app = engines
    with app.begin() as c:
        c.execute(text("INSERT INTO audit_log (id, action) VALUES ('e1', 'test')"))
    with pytest.raises(DBAPIError), app.begin() as c:
        c.execute(text("UPDATE audit_log SET action = 'x' WHERE id = 'e1'"))
    with pytest.raises(DBAPIError), app.begin() as c:
        c.execute(text("DELETE FROM audit_log WHERE id = 'e1'"))


def test_app_user_cannot_change_schema(engines):
    _, app = engines
    with pytest.raises(DBAPIError), app.begin() as c:
        c.execute(text("ALTER TABLE tenant DISABLE ROW LEVEL SECURITY"))


def test_rls_on_costs_and_documents(engines):
    _, app = engines
    with app.begin() as c:
        _scoped(c, "", True)
        c.execute(text("DELETE FROM cost_line"))
        c.execute(text("DELETE FROM document"))
        c.execute(
            text(
                "INSERT INTO connection (id, provider, kind, native_id, name) VALUES ('c1','aws','aws_organization','AB','AB')"
            )
        )
        c.execute(
            text(
                "INSERT INTO cloud_account (id, provider, native_id, connection_id) VALUES ('x1','aws','111','c1')"
            )
        )
        c.execute(
            text(
                "INSERT INTO cost_line (tenant_id, cloud_account_id, provider, billing_account_id, charge_date, "
                "service, currency, billed_cost, effective_cost, tags) VALUES "
                "('a','x1','aws','AB','2026-09-01','EC2','USD',10,10,'{}'), "
                "('b','x1','aws','AB','2026-09-01','EC2','USD',20,20,'{}'), "
                "(NULL,'x1','aws','AB','2026-09-01','EC2','USD',40,40,'{}')"
            )
        )
        c.execute(
            text(
                "INSERT INTO document (id, tenant_id, kind, billing_period, filename, content_type, size_bytes, "
                "sha256, storage_key) VALUES ('d1','a','invoice','2026-09-01','f.pdf','application/pdf',1,'h','k')"
            )
        )
    with app.begin() as c:
        _scoped(c, "a", False)
        assert c.execute(text("SELECT sum(billed_cost) FROM cost_line")).scalar() == 10
        assert c.execute(text("SELECT count(*) FROM document")).scalar() == 1
    with app.begin() as c:
        _scoped(c, "b", False)
        assert c.execute(text("SELECT sum(billed_cost) FROM cost_line")).scalar() == 20
        assert c.execute(text("SELECT count(*) FROM document")).scalar() == 0
        # Não consegue "puxar" custos sem dono ou de outro cliente para si
        assert c.execute(text("UPDATE cost_line SET tenant_id = 'b' WHERE tenant_id IS NULL")).rowcount == 0
