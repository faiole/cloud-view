from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AppUser, Tenant, TenantRole, UserTenantRole
from app.schemas import MemberOut, MemberPut, TenantCreate, TenantOut, TenantUpdate
from app.security.deps import current_session, require_admin, require_recent_mfa, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit

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
    audit.record(db, request, "tenant.delete", user=user, tenant_id=t.id, target=t.slug)
    db.delete(t)


# --- Membros ------------------------------------------------------------------------------------


@router.get("/{tenant_id}/members", response_model=list[MemberOut])
def list_members(tenant_id: str, _: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    _get(db, tenant_id)
    rows = db.execute(
        select(AppUser, UserTenantRole.role)
        .join(UserTenantRole, UserTenantRole.user_id == AppUser.id)
        .where(UserTenantRole.tenant_id == tenant_id)
        .order_by(AppUser.email)
    ).all()
    return [MemberOut(user_id=u.id, email=u.email, name=u.name, role=r) for u, r in rows]


@router.put("/{tenant_id}/members", response_model=MemberOut)
def put_member(
    tenant_id: str,
    body: MemberPut,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Concede/atualiza acesso. O usuário pode ainda não ter logado: é pré-cadastrado pelo e-mail."""
    _get(db, tenant_id)
    email = body.email.lower()
    member = db.scalar(select(AppUser).where(AppUser.email == email))
    if member is None:
        member = AppUser(email=email)
        db.add(member)
        db.flush()
    link = db.scalar(
        select(UserTenantRole).where(
            UserTenantRole.user_id == member.id, UserTenantRole.tenant_id == tenant_id
        )
    )
    previous = link.role.value if link else None
    if link is None:
        link = UserTenantRole(user_id=member.id, tenant_id=tenant_id, role=body.role)
        db.add(link)
    else:
        link.role = body.role
    db.flush()
    audit.record(
        db,
        request,
        "tenant.member.put",
        user=user,
        tenant_id=tenant_id,
        target=email,
        details={"role": [previous, body.role.value]},
    )
    return MemberOut(user_id=member.id, email=member.email, name=member.name, role=TenantRole(body.role))


@router.delete("/{tenant_id}/members/{user_id}", status_code=204)
def delete_member(
    tenant_id: str,
    user_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    link = db.scalar(
        select(UserTenantRole).where(UserTenantRole.user_id == user_id, UserTenantRole.tenant_id == tenant_id)
    )
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member_not_found")
    audit.record(db, request, "tenant.member.delete", user=user, tenant_id=tenant_id, target=user_id)
    db.delete(link)
