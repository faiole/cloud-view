"""Sessões server-side (padrão BFF).

O browser nunca recebe tokens OIDC: só um cookie opaco e HttpOnly que aponta para a sessão no
Redis. Inatividade expira a sessão (TTL deslizante) e há um limite absoluto de duração.
"""

import json
import secrets
import time
from dataclasses import asdict, dataclass, field

import redis

from app.core.config import get_settings

SESSION_COOKIE = "cv_session"
CSRF_COOKIE = "cv_csrf"
CSRF_HEADER = "x-csrf-token"


@dataclass
class SessionData:
    user_id: str
    sub: str
    email: str
    name: str | None
    is_admin: bool
    # tenant_id -> role
    tenants: dict[str, str] = field(default_factory=dict)
    auth_time: int = 0  # epoch da última autenticação completa (senha + MFA)
    created_at: int = field(default_factory=lambda: int(time.time()))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    id_token: str | None = None

    def mfa_age(self) -> int:
        return int(time.time()) - self.auth_time


class SessionStore:
    def __init__(self, r: redis.Redis):
        self.r = r
        self.settings = get_settings()

    @staticmethod
    def _key(sid: str) -> str:
        return f"cv:session:{sid}"

    def create(self, data: SessionData) -> str:
        sid = secrets.token_urlsafe(48)
        self.r.set(self._key(sid), json.dumps(asdict(data)), ex=self.settings.session_ttl_seconds)
        return sid

    def get(self, sid: str | None) -> SessionData | None:
        if not sid:
            return None
        raw = self.r.get(self._key(sid))
        if not raw:
            return None
        data = SessionData(**json.loads(raw))
        if int(time.time()) - data.created_at > self.settings.session_absolute_seconds:
            self.delete(sid)
            return None
        # TTL deslizante
        self.r.expire(self._key(sid), self.settings.session_ttl_seconds)
        return data

    def update(self, sid: str, data: SessionData) -> None:
        self.r.set(self._key(sid), json.dumps(asdict(data)), ex=self.settings.session_ttl_seconds)

    def delete(self, sid: str) -> None:
        self.r.delete(self._key(sid))

    # Estado transitório do fluxo OIDC (state, nonce, PKCE verifier)
    def put_login_state(self, state: str, payload: dict, ttl: int = 600) -> None:
        self.r.set(f"cv:oidc:{state}", json.dumps(payload), ex=ttl)

    def pop_login_state(self, state: str) -> dict | None:
        key = f"cv:oidc:{state}"
        pipe = self.r.pipeline()
        pipe.get(key)
        pipe.delete(key)
        raw, _ = pipe.execute()
        return json.loads(raw) if raw else None
