from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AccountAssignment, AppUser, Tenant, UserTenantRole
from app.permissions import Perm
from app.schemas import MemberOut, MemberPut, TenantCreate, TenantOut, TenantUpdate
from app.security.deps import current_session, require_admin, require_recent_mfa, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services.allocation import reallocate_account
from app.services.keycloak_admin import KeycloakAdmin, KeycloakAdminError, get_kc_admin
from app.services.users import ensure_user

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _get(db: Session, tenant_id: str) -> Tenant:
    t = db.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    return t


@router.get("", response_model=list[TenantOut])
def list_tenants(user: SessionData = Depends(current_session), db: Session = Depends(scoped_db)):
    q = select(Tenant).order_by(Tenant.name)
    if not user.is_admin:
        # Filtro explícito além do RLS (defesa em profundidade)
        q = q.where(Tenant.id.in_(list(user.tenants) or [""]))
    return db.scalars(q).all()


@router.post("", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantCreate,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    t = Tenant(name=body.name, slug=body.slug)
    db.add(t)
    try:
        db.flush()
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug_already_exists") from e
    audit.record(db, request, "tenant.create", user=user, tenant_id=t.id, target=t.slug)
    return t


@router.get("/{tenant_id}", response_model=TenantOut)
def get_tenant(tenant_id: str, _: SessionData = Depends(tenant_access()), db: Session = Depends(scoped_db)):
    return _get(db, tenant_id)


@router.patch("/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    t = _get(db, tenant_id)
    old = t.name
    t.name = body.name
    audit.record(
        db,
        request,
        "tenant.update",
        user=user,
        tenant_id=t.id,
        target=t.slug,
        details={"name": [old, body.name]},
    )
    return t


@router.delete("/{tenant_id}", status_code=204)
def delete_tenant(
    tenant_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    t = _get(db, tenant_id)
    accounts = db.scalars(
        select(AccountAssignment.cloud_account_id).where(AccountAssignment.tenant_id == tenant_id).distinct()
    ).all()
    audit.record(db, request, "tenant.delete", user=user, tenant_id=t.id, target=t.slug)
    db.execute(delete(AccountAssignment).where(AccountAssignment.tenant_id == tenant_id))
    db.delete(t)
    db.flush()
    # Custos das contas do cliente excluído voltam a ficar sem dono (visíveis só ao administrador)
    for acc in accounts:
        reallocate_account(db, acc)


# --- Membros: administrador da plataforma ou administrador da própria empresa ------------------


def _member_out(u: AppUser, role: str, sent: bool | None = None) -> MemberOut:
    return MemberOut(
        user_id=u.id,
        email=u.email,
        name=u.name,
        role=role,
        disabled=u.disabled,
        pending=u.last_login_at is None,
        invite_email_sent=sent,
    )


@router.get("/{tenant_id}/members", response_model=list[MemberOut])
def list_members(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.members_manage)),
    db: Session = Depends(scoped_db),
):
    _get(db, tenant_id)
    rows = db.execute(
        select(AppUser, UserTenantRole.role)
        .join(UserTenantRole, UserTenantRole.user_id == AppUser.id)
        .where(UserTenantRole.tenant_id == tenant_id)
        .order_by(AppUser.email)
    ).all()
    return [_member_out(u, r) for u, r in rows]


@router.put("/{tenant_id}/members", response_model=MemberOut)
def put_member(
    tenant_id: str,
    body: MemberPut,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.members_manage)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    kc: KeycloakAdmin = Depends(get_kc_admin),
):
    """Concede ou altera o acesso de alguém a este cliente. Se a pessoa ainda não existe, é
    criada no Keycloak e recebe um e-mail para definir senha e cadastrar o MFA."""
    _get(db, tenant_id)
    try:
        member, sent = ensure_user(db, kc, body.email, body.name)
    except KeycloakAdminError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "identity_provider_unavailable") from e
    link = db.scalar(
        select(UserTenantRole).where(
            UserTenantRole.user_id == member.id, UserTenantRole.tenant_id == tenant_id
        )
    )
    previous = link.role if link else None
    if link is None:
        link = UserTenantRole(user_id=member.id, tenant_id=tenant_id, role=body.role.value)
        db.add(link)
    else:
        link.role = body.role.value
    db.flush()
    audit.record(
        db,
        request,
        "tenant.member.put",
        user=user,
        tenant_id=tenant_id,
        target=member.email,
        details={"role": [previous, body.role.value], "invited": sent is not None},
    )
    return _member_out(member, body.role.value, sent)


@router.delete("/{tenant_id}/members/{user_id}", status_code=204)
def delete_member(
    tenant_id: str,
    user_id: str,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.members_manage)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    link = db.scalar(
        select(UserTenantRole).where(UserTenantRole.user_id == user_id, UserTenantRole.tenant_id == tenant_id)
    )
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member_not_found")
    if user_id == user.user_id and not user.is_admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_remove_yourself")
    target = db.get(AppUser, user_id)
    audit.record(
        db,
        request,
        "tenant.member.delete",
        user=user,
        tenant_id=tenant_id,
        target=target.email if target else user_id,
    )
    db.delete(link)
