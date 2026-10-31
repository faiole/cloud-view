"""Contas de nuvem, atribuição a clientes e importação de custos (administrador da plataforma)."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import AccountAssignment, CloudAccount, ImportBatch, Tenant
from app.schemas import AccountOut, AccountUpdate, AssignmentIn, AssignmentOut, ImportOut
from app.security.deps import require_admin, require_recent_mfa, scoped_db
from app.security.session import SessionData
from app.services import audit
from app.services.allocation import AllocationConflict, reallocate_account, validate_rule
from app.services.focus_import import ImportErrorInvalid, import_focus

router = APIRouter(prefix="/admin", tags=["admin-accounts"])


def _account_out(a: CloudAccount) -> AccountOut:
    return AccountOut(
        id=a.id,
        provider=a.provider,
        native_id=a.native_id,
        name=a.name,
        connection_id=a.connection_id,
        connection_name=a.connection.name if a.connection else None,
        connection_native_id=a.connection.native_id if a.connection else None,
        tenants=sorted({r.tenant.name for r in a.assignments}),
    )


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(
    unassigned: bool = False,
    connection_id: str | None = None,
    _: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    q = (
        select(CloudAccount)
        .options(
            selectinload(CloudAccount.connection),
            selectinload(CloudAccount.assignments).selectinload(AccountAssignment.tenant),
        )
        .order_by(CloudAccount.provider, CloudAccount.native_id)
    )
    if unassigned:
        q = q.where(~CloudAccount.assignments.any())
    if connection_id:
        q = q.where(CloudAccount.connection_id == connection_id)
    return [_account_out(a) for a in db.scalars(q).all()]


@router.patch("/accounts/{account_id}", response_model=AccountOut)
def rename_account(
    account_id: str,
    body: AccountUpdate,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    a = db.get(CloudAccount, account_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account_not_found")
    a.name = body.name
    audit.record(db, request, "account.rename", user=user, target=a.native_id, details={"name": body.name})
    db.flush()
    db.refresh(a)
    return _account_out(a)


@router.get("/accounts/{account_id}/assignments", response_model=list[AssignmentOut])
def list_assignments(
    account_id: str, _: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)
):
    return db.scalars(
        select(AccountAssignment)
        .where(AccountAssignment.cloud_account_id == account_id)
        .order_by(AccountAssignment.tag_key.is_not(None), AccountAssignment.valid_from)
    ).all()


@router.post("/accounts/{account_id}/assignments", response_model=AssignmentOut, status_code=201)
def create_assignment(
    account_id: str,
    body: AssignmentIn,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Atribui a conta (inteira ou só as linhas com uma tag) a um cliente e realoca os custos."""
    acc = db.get(CloudAccount, account_id)
    if acc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account_not_found")
    tenant = db.get(Tenant, body.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    rule = AccountAssignment(cloud_account_id=account_id, **body.model_dump())
    try:
        validate_rule(db, rule)
    except AllocationConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    db.add(rule)
    db.flush()
    reallocate_account(db, account_id)
    audit.record(
        db,
        request,
        "account.assign",
        user=user,
        tenant_id=tenant.id,
        target=f"{acc.provider}:{acc.native_id}",
        details=body.model_dump(mode="json"),
    )
    return rule


@router.delete("/assignments/{assignment_id}", status_code=204)
def delete_assignment(
    assignment_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    rule = db.get(AccountAssignment, assignment_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "assignment_not_found")
    acc_id, tenant_id = rule.cloud_account_id, rule.tenant_id
    db.delete(rule)
    db.flush()
    reallocate_account(db, acc_id)
    audit.record(db, request, "account.unassign", user=user, tenant_id=tenant_id, target=acc_id)


@router.get("/imports", response_model=list[ImportOut])
def list_imports(_: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    rows = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)).all()
    return [ImportOut.model_validate(b, from_attributes=True) for b in rows]


@router.post("/imports/focus", response_model=ImportOut, status_code=201)
async def upload_focus(
    request: Request,
    file: UploadFile = File(...),
    provider: str | None = Form(default=None),
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    """Importa um arquivo FOCUS (.csv ou .csv.gz) exportado pela AWS, Azure ou OCI."""
    limit = get_settings().max_import_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large")
    try:
        result = import_focus(db, data, filename=file.filename, provider_hint=provider, user_email=user.email)
    except ImportErrorInvalid as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    b = result.batch
    audit.record(
        db,
        request,
        "costs.import",
        user=user,
        target=file.filename,
        details={"rows": b.source_rows, "stored": b.stored_rows, "replaced": b.replaced_rows},
    )
    out = ImportOut.model_validate(b, from_attributes=True)
    out.new_accounts = result.new_accounts
    out.unassigned_accounts = result.unassigned_accounts
    return out
