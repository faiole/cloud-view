import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.helpers import admin


def _as(oidc, sub, email, roles=()):
    c = TestClient(app, base_url="http://testserver")
    login(c, oidc, sub=sub, email=email, roles=roles)
    return c


@pytest.fixture
def two(client, oidc):
    admin(client, oidc)
    a = client.post("/api/tenants", json={"name": "A", "slug": "a"}).json()
    b = client.post("/api/tenants", json={"name": "B", "slug": "b"}).json()
    return a, b


def test_invite_creates_identity_and_sends_setup_email(client, kc, two):
    a, _ = two
    r = client.put(
        f"/api/tenants/{a['id']}/members", json={"email": "Nova@A.com", "role": "finance", "name": "Ana Lima"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] is True and body["invite_email_sent"] is True
    assert ("create", "nova@a.com") in kc.calls and any(c[0] == "email" for c in kc.calls)


def test_invite_survives_email_failure(client, kc, two):
    a, _ = two
    kc.fail_email = True
    r = client.put(f"/api/tenants/{a['id']}/members", json={"email": "x@a.com", "role": "viewer"})
    assert r.status_code == 200 and r.json()["invite_email_sent"] is False


def test_role_permissions(client, oidc, two):
    a, _ = two
    for email, role in (("fin@a.com", "finance"), ("view@a.com", "viewer"), ("adm@a.com", "tenant_admin")):
        client.put(f"/api/tenants/{a['id']}/members", json={"email": email, "role": role})
    fin = _as(oidc, "fin", "fin@a.com")
    view = _as(oidc, "view", "view@a.com")
    tadm = _as(oidc, "tadm", "adm@a.com")
    assert fin.get(f"/api/tenants/{a['id']}/members").status_code == 403
    assert fin.get(f"/api/tenants/{a['id']}/documents").status_code == 200
    assert view.get(f"/api/tenants/{a['id']}/documents").json() == []  # sem faturas; só relatórios mensais
    assert view.get(f"/api/tenants/{a['id']}/costs/summary").status_code == 200
    assert tadm.get(f"/api/tenants/{a['id']}/members").status_code == 200


def test_company_admin_manages_only_own_company(client, oidc, two):
    a, b = two
    client.put(f"/api/tenants/{a['id']}/members", json={"email": "boss@a.com", "role": "tenant_admin"})
    boss = _as(oidc, "boss", "boss@a.com")
    r = boss.put(f"/api/tenants/{a['id']}/members", json={"email": "colega@a.com", "role": "finance"})
    assert r.status_code == 200
    assert (
        boss.put(f"/api/tenants/{b['id']}/members", json={"email": "x@a.com", "role": "finance"}).status_code
        == 404
    )
    assert boss.get("/api/admin/users").status_code == 403
    me = next(m for m in boss.get(f"/api/tenants/{a['id']}/members").json() if m["email"] == "boss@a.com")
    assert boss.delete(f"/api/tenants/{a['id']}/members/{me['user_id']}").status_code == 400


def test_disable_user_kills_sessions_and_blocks_login(client, oidc, kc, two):
    a, _ = two
    client.put(f"/api/tenants/{a['id']}/members", json={"email": "u@a.com", "role": "viewer"})
    u = _as(oidc, "kc-1", "u@a.com")
    assert u.get("/api/auth/me").status_code == 200

    uid = next(x["id"] for x in client.get("/api/admin/users").json() if x["email"] == "u@a.com")
    r = client.post(f"/api/admin/users/{uid}/disable")
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert kc.users["kc-1"]["enabled"] is False
    assert u.get("/api/auth/me").status_code == 401

    oidc.next_claims = {"sub": "kc-1", "email": "u@a.com"}
    r = u.get("/api/auth/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    assert u.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False).status_code == 403

    assert client.post(f"/api/admin/users/{uid}/enable").json()["disabled"] is False


def test_reset_mfa_and_platform_admin(client, oidc, kc, two):
    a, _ = two
    client.put(f"/api/tenants/{a['id']}/members", json={"email": "u@a.com", "role": "viewer"})
    uid = next(x["id"] for x in client.get("/api/admin/users").json() if x["email"] == "u@a.com")
    assert client.post(f"/api/admin/users/{uid}/reset-mfa").status_code == 200
    assert kc.users["kc-1"]["otp"] is False
    assert client.put(f"/api/admin/users/{uid}/platform-admin", json={"value": True}).status_code == 200
    assert kc.users["kc-1"]["admin"] is True


def test_admin_cannot_disable_self(client, oidc, two):
    me = client.get("/api/auth/me").json()["id"]
    assert client.post(f"/api/admin/users/{me}/disable").status_code == 400


def test_user_admin_actions_need_recent_mfa(client, oidc, two):
    from tests.test_tenants import _expire_mfa

    _expire_mfa(client)
    r = client.post("/api/admin/users", json={"email": "z@z.com"})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "step_up_required"


def test_invite_platform_admin(client, kc, two):
    r = client.post("/api/admin/users", json={"email": "ops@msp.com", "name": "Ops", "platform_admin": True})
    assert r.status_code == 201
    assert kc.users["kc-1"]["admin"] is True
