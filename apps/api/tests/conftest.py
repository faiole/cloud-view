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
from app.services.keycloak_admin import KeycloakAdminError, get_kc_admin
from app.services.storage import LocalStorage, get_storage


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


class FakeKC:
    """Simula a Admin API do Keycloak e registra as chamadas."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_email = False

    def find_user_id(self, email):
        return next((uid for uid, u in self.users.items() if u["email"] == email), None)

    def create_user(self, email, first, last):
        uid = f"kc-{len(self.users) + 1}"
        self.users[uid] = {"email": email, "enabled": True, "otp": True, "admin": False}
        self.calls.append(("create", email))
        return uid

    def send_setup_email(self, uid):
        if self.fail_email:
            raise KeycloakAdminError("smtp down")
        self.calls.append(("email", uid))

    def set_enabled(self, uid, enabled):
        self.users.setdefault(uid, {})["enabled"] = enabled

    def reset_mfa(self, uid):
        self.users.setdefault(uid, {})["otp"] = False

    def logout(self, uid):
        self.calls.append(("logout", uid))

    def set_platform_admin(self, uid, value):
        self.users.setdefault(uid, {})["admin"] = value


@pytest.fixture
def oidc():
    return FakeOIDC()


@pytest.fixture
def kc():
    return FakeKC()


PG_OWNER = os.environ.get("CV_TEST_PG_OWNER_URL")
PG_APP = os.environ.get("CV_TEST_PG_APP_URL")
TABLES = (
    "notification_delivery, notification_channel, alert, budget, commitment_coverage, recommendation, "
    "action_request, health_event, resource, job, platform_setting, cloud_secret, "
    "document, cost_line, import_batch, account_assignment, cloud_account, connection, "
    "user_tenant_role, app_user, tenant, audit_log"
)


@pytest.fixture(scope="session")
def pg_migrated():
    """Com Postgres configurado, a suíte da API roda nele como usuário da aplicação (RLS ativo)."""
    if not (PG_OWNER and PG_APP):
        return False
    import subprocess
    import sys

    env = {**os.environ, "CV_MIGRATION_DATABASE_URL": PG_OWNER}
    here = os.path.dirname(os.path.dirname(__file__))
    for step in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *step], cwd=here, env=env, check=True)
    return True


def _engine(pg: bool):
    if pg:
        owner = create_engine(PG_OWNER)
        with owner.begin() as c:
            c.exec_driver_sql(f"TRUNCATE {TABLES} CASCADE")
        owner.dispose()
        return create_engine(PG_APP)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    dbmod.Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(oidc, kc, tmp_path, pg_migrated):
    engine = _engine(pg_migrated)
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    store = SessionStore(fakeredis.FakeRedis(decode_responses=True))
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_oidc] = lambda: oidc
    app.dependency_overrides[get_kc_admin] = lambda: kc
    storage = LocalStorage(str(tmp_path / "docs"))
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    if not pg_migrated:
        dbmod.Base.metadata.drop_all(engine)
    engine.dispose()


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
