import os
import time

os.environ["CV_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["CV_COOKIE_SECURE"] = "false"
os.environ["CV_PUBLIC_URL"] = "http://testserver"

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import db as dbmod
from app.main import app
from app.security.deps import get_store
from app.security.oidc import get_oidc
from app.security.session import SessionStore


class FakeOIDC:
    """Simula o Keycloak: o teste define quais claims o próximo login devolve."""

    def __init__(self):
        self.next_claims: dict = {}
        self._nonces: dict[str, str] = {}

    def authorize_url(self, *, state, nonce, code_challenge, max_age=None):
        self._nonces[state] = nonce
        return f"https://idp.example/auth?state={state}&max_age={max_age}"

    def end_session_url(self, id_token_hint):
        return "https://idp.example/logout"

    def exchange_code(self, code, verifier):
        return {"id_token": f"token-{code}"}

    def validate_id_token(self, id_token, nonce):
        return {"auth_time": int(time.time()), "email_verified": True, **self.next_claims}


@pytest.fixture
def oidc():
    return FakeOIDC()


@pytest.fixture
def client(oidc):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    dbmod.Base.metadata.create_all(engine)
    store = SessionStore(fakeredis.FakeRedis(decode_responses=True))
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_oidc] = lambda: oidc
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    dbmod.Base.metadata.drop_all(engine)


def login(client: TestClient, oidc: FakeOIDC, *, sub: str, email: str, roles=(), step_up=False):
    oidc.next_claims = {"sub": sub, "email": email, "name": email.split("@")[0], "roles": list(roles)}
    path = "/api/auth/step-up" if step_up else "/api/auth/login"
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302, r.text
    state = r.headers["location"].split("state=")[1].split("&")[0]
    r = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302, r.text
    client.headers["X-CSRF-Token"] = client.cookies.get("cv_csrf")
    return r
