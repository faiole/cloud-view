"""Dependências FastAPI de autenticação/autorização."""

import hmac
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import apply_tenant_scope, get_db
from app.core.redis import get_redis
from app.models import TenantRole, UserTenantRole
from app.security.session import CSRF_HEADER, SESSION_COOKIE, SessionData, SessionStore

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Hierarquia de papéis por cliente. `client` é o portal do próprio cliente (somente leitura).
ROLE_RANK = {TenantRole.client.value: 1, TenantRole.viewer.value: 1, TenantRole.operator.value: 2}


def get_store() -> SessionStore:
    return SessionStore(get_redis())


def current_session(
    request: Request, store: SessionStore = Depends(get_store), db: Session = Depends(get_db)
) -> SessionData:
    data = store.get(request.cookies.get(SESSION_COOKIE))
    if data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    # Vínculos com clientes são relidos a cada request: remover um acesso vale na hora,
    # sem esperar a sessão expirar.
    rows = db.execute(
        select(UserTenantRole.tenant_id, UserTenantRole.role).where(UserTenantRole.user_id == data.user_id)
    ).all()
    data.tenants = {t: r.value for t, r in rows}
    # Proteção CSRF (double submit): métodos que alteram estado exigem o header com o token da sessão
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, data.csrf):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf_failed")
    return data


def scoped_db(user: SessionData = Depends(current_session), db: Session = Depends(get_db)) -> Session:
    """Sessão de banco já com o escopo de RLS do usuário aplicado."""
    apply_tenant_scope(db, tenant_ids=list(user.tenants), is_admin=user.is_admin)
    return db


def require_admin(user: SessionData = Depends(current_session)) -> SessionData:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_required")
    return user


def require_recent_mfa(user: SessionData = Depends(current_session)) -> SessionData:
    """Step-up: ações sensíveis exigem autenticação (senha + MFA) há poucos minutos."""
    if user.mfa_age() > get_settings().step_up_max_age_seconds:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "step_up_required", "step_up_url": "/api/auth/step-up"},
        )
    return user


def tenant_access(min_role: TenantRole = TenantRole.viewer) -> Callable[..., SessionData]:
    def dep(tenant_id: str, user: SessionData = Depends(current_session)) -> SessionData:
        if user.is_admin:
            return user
        role = user.tenants.get(tenant_id)
        if role is None or ROLE_RANK.get(role, 0) < ROLE_RANK[min_role.value]:
            # 404 em vez de 403 para não revelar a existência de clientes de terceiros
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
        return user

    return dep
