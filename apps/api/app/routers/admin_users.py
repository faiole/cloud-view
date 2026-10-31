"""Administração de usuários da plataforma (somente administrador da plataforma)."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import AppUser, UserTenantRole
from app.schemas import FlagBody, UserInvite, UserMembership, UserOut
from app.security.deps import get_store, require_admin, require_recent_mfa, scoped_db
from app.security.session import SessionData, SessionStore
from app.services import audit
from app.services.keycloak_admin import KeycloakAdmin, KeycloakAdminError, get_kc_admin
from app.services.users import ensure_user

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


def _out(u: AppUser, sent: bool | None = None) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        name=u.name,
        disabled=u.disabled,
        pending=u.last_login_at is None,
        last_login_at=u.last_login_at,
        memberships=sorted(
            (
                UserMembership(tenant_id=m.tenant_id, tenant_name=m.tenant.name, role=m.role)
                for m in u.memberships
            ),
            key=lambda m: m.tenant_name,
        ),
        invite_email_sent=sent,
    )


def _load(db: Session, user_id: str) -> AppUser:
    u = db.scalar(
        select(AppUser)
        .where(AppUser.id == user_id)
        .options(selectinload(AppUser.memberships).selectinload(UserTenantRole.tenant))
    )
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user_not_found")
    return u


def _kc_id(u: AppUser) -> str:
    if not u.keycloak_sub:
        raise HTTPException(status.HTTP_409_CONFLICT, "user_not_in_identity_provider")
    return u.keycloak_sub


@router.get("", response_model=list[UserOut])
def list_users(
    q: str | None = None, _: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)
):
    stmt = (
        select(AppUser)
        .options(selectinload(AppUser.memberships).selectinload(UserTenantRole.tenant))
        .order_by(AppUser.email)
        .limit(500)
    )
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(AppUser.email.like(like), AppUser.name.ilike(like)))
    return [_out(u) for u in db.scalars(stmt).all()]


@router.post("", response_model=UserOut, status_code=201)
def invite_user(
    body: UserInvite,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
):
    try:
        u, sent = ensure_user(db, kc, body.email, body.name)
        if body.platform_admin:
            kc.set_platform_admin(_kc_id(u), True)
    except KeycloakAdminError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    audit.record(
        db, request, "user.invite", user=user, target=u.email, details={"platform_admin": body.platform_admin}
    )
    return _out(_load(db, u.id), sent)


def _guard_self(user: SessionData, target: AppUser) -> None:
    if target.id == user.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_change_yourself")


@router.post("/{user_id}/disable", response_model=UserOut)
def disable_user(
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
    store: SessionStore = Depends(get_store),
):
    u = _load(db, user_id)
    _guard_self(user, u)
    u.disabled = True
    if u.keycloak_sub:
        try:
            kc.set_enabled(u.keycloak_sub, False)
            kc.logout(u.keycloak_sub)
        except KeycloakAdminError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    store.delete_user_sessions(u.id)
    audit.record(db, request, "user.disable", user=user, target=u.email)
    return _out(u)


@router.post("/{user_id}/enable", response_model=UserOut)
def enable_user(
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
):
    u = _load(db, user_id)
    u.disabled = False
    if u.keycloak_sub:
        try:
            kc.set_enabled(u.keycloak_sub, True)
        except KeycloakAdminError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    audit.record(db, request, "user.enable", user=user, target=u.email)
    return _out(u)


@router.post("/{user_id}/reset-mfa", response_model=UserOut)
def reset_mfa(
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
    store: SessionStore = Depends(get_store),
):
    """Para quem perdeu o celular: apaga o TOTP e obriga a cadastrar outro no próximo login."""
    u = _load(db, user_id)
    _guard_self(user, u)
    try:
        kc.reset_mfa(_kc_id(u))
        kc.logout(_kc_id(u))
    except KeycloakAdminError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    store.delete_user_sessions(u.id)
    audit.record(db, request, "user.reset_mfa", user=user, target=u.email)
    return _out(u)


@router.post("/{user_id}/logout", response_model=UserOut)
def force_logout(
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
    store: SessionStore = Depends(get_store),
):
    u = _load(db, user_id)
    if u.keycloak_sub:
        try:
            kc.logout(u.keycloak_sub)
        except KeycloakAdminError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    n = store.delete_user_sessions(u.id)
    audit.record(db, request, "user.logout", user=user, target=u.email, details={"sessions": n})
    return _out(u)


@router.post("/{user_id}/resend-invite", response_model=UserOut)
def resend_invite(
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
):
    u = _load(db, user_id)
    try:
        kc.send_setup_email(_kc_id(u))
    except KeycloakAdminError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "invite_email_failed") from e
    audit.record(db, request, "user.resend_invite", user=user, target=u.email)
    return _out(u, True)


@router.put("/{user_id}/platform-admin", response_model=UserOut)
def set_platform_admin(
    user_id: str,
    body: FlagBody,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
    store: SessionStore = Depends(get_store),
):
    u = _load(db, user_id)
    _guard_self(user, u)
    try:
        kc.set_platform_admin(_kc_id(u), body.value)
        kc.logout(_kc_id(u))
    except KeycloakAdminError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    # O papel vem no token: derrubar as sessões faz a mudança valer no próximo login
    store.delete_user_sessions(u.id)
    audit.record(db, request, "user.platform_admin", user=user, target=u.email, details={"value": body.value})
    return _out(u)
