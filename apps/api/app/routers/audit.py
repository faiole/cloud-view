from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.schemas import AuditOut
from app.security.deps import require_admin, scoped_db
from app.security.session import SessionData

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditOut])
def list_audit(
    tenant_id: str | None = None,
    action: str | None = None,
    before: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    _: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if tenant_id:
        q = q.where(AuditLog.tenant_id == tenant_id)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    if before:
        q = q.where(AuditLog.ts < before)
    return db.scalars(q).all()
