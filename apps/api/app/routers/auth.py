import secrets
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.models import AppUser
from app.security.deps import current_session, get_store
from app.security.oidc import OIDCClient, OIDCError, get_oidc, pkce_pair
from app.security.session import CSRF_COOKIE, SESSION_COOKIE, SessionData, SessionStore
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])
LOGIN_STATE_COOKIE = "cv_login"


def _start(store: SessionStore, oidc: OIDCClient, *, step_up: bool, return_to: str) -> RedirectResponse:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    store.put_login_state(
        state, {"nonce": nonce, "verifier": verifier, "step_up": step_up, "return_to": return_to}
    )
    url = oidc.authorize_url(
        state=state, nonce=nonce, code_challenge=challenge, max_age=0 if step_up else None
    )
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    # Amarra o `state` ao navegador que iniciou o login (evita login CSRF)
    resp.set_cookie(
        LOGIN_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/api/auth",
    )
    return resp


def _safe_return_to(value: str | None) -> str:
    # Só caminhos relativos do próprio painel (evita open redirect)
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/login")
def login(
    return_to: str | None = None,
    store: SessionStore = Depends(get_store),
    oidc: OIDCClient = Depends(get_oidc),
):
    return _start(store, oidc, step_up=False, return_to=_safe_return_to(return_to))


@router.get("/step-up")
def step_up(
    return_to: str | None = None,
    _: SessionData = Depends(current_session),
    store: SessionStore = Depends(get_store),
    oidc: OIDCClient = Depends(get_oidc),
):
    return _start(store, oidc, step_up=True, return_to=_safe_return_to(return_to))


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_store),
    oidc: OIDCClient = Depends(get_oidc),
):
    settings = get_settings()
    if error or not code or not state:
        audit.record(db, request, "auth.login", outcome="failure", details={"error": error or "missing_code"})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "login_failed")
    if request.cookies.get(LOGIN_STATE_COOKIE) != state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state_mismatch")
    pending = store.pop_login_state(state)
    if not pending:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state_expired")

    try:
        tokens = oidc.exchange_code(code, pending["verifier"])
        claims = oidc.validate_id_token(tokens["id_token"], pending["nonce"])
    except (OIDCError, KeyError) as e:
        audit.record(db, request, "auth.login", outcome="failure", details={"error": str(e)})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "login_failed") from e

    auth_time = int(claims.get("auth_time") or 0)
    if pending["step_up"] and time.time() - auth_time > 60:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "step_up_not_performed")

    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified", False):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "email_not_verified")

    user = db.scalar(select(AppUser).where(AppUser.keycloak_sub == claims["sub"]))
    if user is None:
        # Usuário pode ter sido pré-cadastrado por e-mail ao ser vinculado a um cliente
        user = db.scalar(select(AppUser).where(AppUser.email == email))
        if user is None:
            user = AppUser(email=email)
            db.add(user)
        user.keycloak_sub = claims["sub"]
    user.name = claims.get("name")
    user.last_login_at = datetime.now(UTC)
    db.flush()

    roles = claims.get("roles") or []
    data = SessionData(
        user_id=user.id,
        sub=claims["sub"],
        email=email,
        name=user.name,
        is_admin=settings.platform_admin_role in roles,
        auth_time=auth_time,
        id_token=tokens["id_token"],
    )

    old_sid = request.cookies.get(SESSION_COOKIE)
    if old_sid:
        store.delete(old_sid)  # rotação de sessão (evita session fixation)
    sid = store.create(data)
    audit.record(db, request, "auth.step_up" if pending["step_up"] else "auth.login", user=data)

    resp = RedirectResponse(pending["return_to"], status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_absolute_seconds,
    )
    # Token CSRF legível pelo front, reenviado no header X-CSRF-Token
    resp.set_cookie(
        CSRF_COOKIE,
        data.csrf,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        max_age=settings.session_absolute_seconds,
    )
    resp.delete_cookie(LOGIN_STATE_COOKIE, path="/api/auth")
    return resp


@router.post("/logout")
def logout(
    request: Request,
    user: SessionData = Depends(current_session),
    db: Session = Depends(get_db),
    store: SessionStore = Depends(get_store),
    oidc: OIDCClient = Depends(get_oidc),
):
    store.delete(request.cookies.get(SESSION_COOKIE, ""))
    audit.record(db, request, "auth.logout", user=user)
    resp = JSONResponse({"logout_url": oidc.end_session_url(user.id_token)})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@router.get("/me")
def me(user: SessionData = Depends(current_session)) -> dict:
    return {
        "id": user.user_id,
        "email": user.email,
        "name": user.name,
        "is_admin": user.is_admin,
        "tenants": user.tenants,
        "mfa_age_seconds": user.mfa_age(),
        "step_up_valid": user.mfa_age() <= get_settings().step_up_max_age_seconds,
    }
