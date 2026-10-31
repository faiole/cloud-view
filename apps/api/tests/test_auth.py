import time

from tests.conftest import login


def test_me_requires_login(client):
    assert client.get("/api/auth/me").status_code == 401


def test_login_flow_creates_session(client, oidc):
    login(client, oidc, sub="u1", email="Ana@Example.com")
    me = client.get("/api/auth/me").json()
    assert me["email"] == "ana@example.com"
    assert me["is_admin"] is False
    assert me["step_up_valid"] is True


def test_admin_role_from_idp(client, oidc):
    login(client, oidc, sub="adm", email="adm@example.com", roles=["platform-admin"])
    assert client.get("/api/auth/me").json()["is_admin"] is True


def test_callback_rejects_state_from_other_browser(client, oidc):
    oidc.next_claims = {"sub": "u1", "email": "a@example.com"}
    r = client.get("/api/auth/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    client.cookies.delete("cv_login", path="/api/auth")
    client.cookies.set("cv_login", "other", path="/api/auth")
    r = client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False)
    assert r.status_code == 400


def test_unverified_email_is_rejected(client, oidc):
    oidc.next_claims = {"sub": "u1", "email": "a@example.com", "email_verified": False}
    r = client.get("/api/auth/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    assert client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False).status_code == 403


def test_return_to_blocks_open_redirect(client, oidc):
    r = client.get("/api/auth/login?return_to=//evil.example", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    oidc.next_claims = {"sub": "u1", "email": "a@example.com"}
    r = client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_state_cannot_be_replayed(client, oidc):
    oidc.next_claims = {"sub": "u1", "email": "a@example.com"}
    r = client.get("/api/auth/login", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    assert client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False).status_code == 302
    client.cookies.set("cv_login", state, path="/api/auth")
    assert client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False).status_code == 400


def test_step_up_requests_reauthentication(client, oidc):
    login(client, oidc, sub="u1", email="a@example.com")
    r = client.get("/api/auth/step-up", follow_redirects=False)
    assert "max_age=0" in r.headers["location"]


def test_step_up_rejected_if_idp_did_not_reauthenticate(client, oidc):
    login(client, oidc, sub="u1", email="a@example.com")
    r = client.get("/api/auth/step-up", follow_redirects=False)
    state = r.headers["location"].split("state=")[1].split("&")[0]
    oidc.next_claims = {"sub": "u1", "email": "a@example.com", "auth_time": int(time.time()) - 3600}
    assert client.get(f"/api/auth/callback?code=x&state={state}", follow_redirects=False).status_code == 400


def test_logout_destroys_session(client, oidc):
    login(client, oidc, sub="u1", email="a@example.com")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200 and "logout_url" in r.json()
    assert client.get("/api/auth/me").status_code == 401


def test_csrf_required_on_mutations(client, oidc):
    login(client, oidc, sub="u1", email="a@example.com")
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/auth/logout").status_code == 403
