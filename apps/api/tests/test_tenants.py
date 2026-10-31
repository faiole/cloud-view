import time

from fastapi.testclient import TestClient

from app.core import db as dbmod
from app.main import app
from app.models import AuditLog
from app.security.session import SESSION_COOKIE
from tests.conftest import login


def _admin(client, oidc):
    login(client, oidc, sub="adm", email="adm@example.com", roles=["platform-admin"])


def _expire_mfa(client):
    """Simula que o último MFA aconteceu há mais de 5 minutos."""
    from app.security.deps import get_store

    store = app.dependency_overrides[get_store]()
    sid = client.cookies.get(SESSION_COOKIE)
    data = store.get(sid)
    data.auth_time = int(time.time()) - 3600
    store.update(sid, data)


def test_only_admin_creates_tenant(client, oidc):
    login(client, oidc, sub="u1", email="u1@example.com")
    assert client.post("/api/tenants", json={"name": "Acme", "slug": "acme"}).status_code == 403


def test_admin_creates_and_lists_tenant(client, oidc):
    _admin(client, oidc)
    r = client.post("/api/tenants", json={"name": "Acme SA", "slug": "acme"})
    assert r.status_code == 201
    assert len(r.json()["aws_external_id"]) == 32
    assert [t["slug"] for t in client.get("/api/tenants").json()] == ["acme"]
    assert client.post("/api/tenants", json={"name": "Acme 2", "slug": "acme"}).status_code == 409


def test_invalid_slug(client, oidc):
    _admin(client, oidc)
    assert client.post("/api/tenants", json={"name": "Acme", "slug": "Acme SA"}).status_code == 422


def test_member_sees_only_own_tenants(client, oidc):
    _admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    b = client.post("/api/tenants", json={"name": "B", "slug": "b"}).json()
    assert (
        client.put(
            f"/api/tenants/{a['id']}/members", json={"email": "op@example.com", "role": "viewer"}
        ).status_code
        == 200
    )

    login(client, oidc, sub="op", email="op@example.com")
    assert [t["slug"] for t in client.get("/api/tenants").json()] == ["a"]
    assert client.get(f"/api/tenants/{a['id']}").status_code == 200
    assert client.get(f"/api/tenants/{b['id']}").status_code == 404
    assert client.get(f"/api/tenants/{a['id']}/members").status_code == 403


def test_removing_member_revokes_access_immediately(client, oidc):
    _admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    body = {"email": "op@example.com", "role": "operator"}
    m = client.put(f"/api/tenants/{a['id']}/members", json=body).json()

    with TestClient(app, base_url="http://testserver") as op:
        login(op, oidc, sub="op", email="op@example.com")
        assert op.get(f"/api/tenants/{a['id']}").status_code == 200
        assert client.delete(f"/api/tenants/{a['id']}/members/{m['user_id']}").status_code == 204
        assert op.get(f"/api/tenants/{a['id']}").status_code == 404


def test_sensitive_actions_require_recent_mfa(client, oidc):
    _admin(client, oidc)
    t = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    _expire_mfa(client)
    r = client.delete(f"/api/tenants/{t['id']}")
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "step_up_required"

    login(client, oidc, sub="adm", email="adm@example.com", roles=["platform-admin"], step_up=True)
    assert client.delete(f"/api/tenants/{t['id']}").status_code == 204


def test_actions_are_audited(client, oidc):
    _admin(client, oidc)
    client.post("/api/tenants", json={"name": "A", "slug": "a"})
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert "tenant.create" in actions and "auth.login" in actions
    with dbmod.get_sessionmaker()() as s:
        entry = s.query(AuditLog).filter_by(action="tenant.create").one()
        assert entry.user_email == "adm@example.com"
