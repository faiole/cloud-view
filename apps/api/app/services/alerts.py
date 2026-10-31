"""Criação, resolução e entrega de alertas."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, NotificationChannel
from app.services.jobs import enqueue

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def raise_alert(
    db: Session,
    *,
    tenant_id: str | None,
    type: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    dedupe_key: str,
    data: dict | None = None,
) -> Alert | None:
    """Cria o alerta se ainda não existe (mesma `dedupe_key`). Um alerta resolvido que volta a
    acontecer é reaberto e notificado de novo."""
    existing = db.scalar(select(Alert).where(Alert.dedupe_key == dedupe_key))
    now = datetime.now(UTC)
    if existing is not None:
        if existing.status != "resolved":
            existing.updated_at = now
            return None
        existing.status, existing.resolved_at, existing.status_by = "open", None, None
        existing.severity, existing.title, existing.message = severity, title, message
        existing.data, existing.updated_at = data or {}, now
        alert = existing
    else:
        alert = Alert(
            tenant_id=tenant_id,
            type=type,
            category=category,
            severity=severity,
            title=title[:300],
            message=message,
            data=data or {},
            dedupe_key=dedupe_key,
        )
        db.add(alert)
    db.flush()
    enqueue(db, "notify", payload={"alert_id": alert.id}, dedupe=False)
    return alert


def resolve_alert(db: Session, dedupe_key: str, by: str | None = None) -> bool:
    a = db.scalar(select(Alert).where(Alert.dedupe_key == dedupe_key, Alert.status != "resolved"))
    if a is None:
        return False
    a.status, a.resolved_at, a.status_by = "resolved", datetime.now(UTC), by or "automático"
    a.updated_at = a.resolved_at
    return True


def resolve_missing(db: Session, prefix: str, keep: set[str]) -> int:
    """Resolve alertas abertos com o prefixo que não estão mais em `keep`."""
    n = 0
    for a in db.scalars(select(Alert).where(Alert.dedupe_key.startswith(prefix), Alert.status != "resolved")):
        if a.dedupe_key not in keep:
            a.status, a.resolved_at, a.status_by = "resolved", datetime.now(UTC), "automático"
            n += 1
    return n


def channels_for(db: Session, alert: Alert) -> list[NotificationChannel]:
    q = select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
    if alert.tenant_id:
        # canais da empresa + canais da plataforma (equipe do MSP)
        q = q.where(
            (NotificationChannel.tenant_id == alert.tenant_id) | (NotificationChannel.tenant_id.is_(None))
        )
    else:
        q = q.where(NotificationChannel.tenant_id.is_(None))
    out = []
    for ch in db.scalars(q):
        if SEVERITY_RANK[alert.severity] < SEVERITY_RANK.get(ch.min_severity, 1):
            continue
        if alert.category not in (ch.categories or []):
            continue
        out.append(ch)
    return out
