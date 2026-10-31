from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.security.session import SessionData


def client_ip(request: Request) -> str | None:
    # Atrás de CloudFront/ALB o IP real vem em X-Forwarded-For (primeiro elemento)
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def record(
    db: Session,
    request: Request | None,
    action: str,
    *,
    user: SessionData | None = None,
    tenant_id: str | None = None,
    target: str | None = None,
    details: dict | None = None,
    outcome: str = "success",
) -> AuditLog:
    entry = AuditLog(
        action=action,
        user_id=user.user_id if user else None,
        user_email=user.email if user else None,
        tenant_id=tenant_id,
        target=target,
        details=details or {},
        outcome=outcome,
        ip=client_ip(request) if request else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(entry)
    db.flush()
    return entry
