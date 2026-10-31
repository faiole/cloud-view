"""Alertas de todas as empresas e da plataforma, e canais da equipe da plataforma."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, NotificationChannel, Tenant
from app.routers.finops import _channel, channel_out, create_channel
from app.schemas import AlertOut, ChannelIn, ChannelOut
from app.security.deps import require_admin, scoped_db
from app.security.session import SessionData
from app.services import audit
from app.services.jobs import enqueue
from app.vault import Vault, get_vault

router = APIRouter(prefix="/admin", tags=["admin-alerts"])


@router.get("/alerts", response_model=list[AlertOut])
def all_alerts(
    status_: str = Query("active", alias="status"),
    category: str | None = None,
    _: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    q = select(Alert)
    if status_ == "active":
        q = q.where(Alert.status != "resolved")
    elif status_ != "all":
        q = q.where(Alert.status == status_)
    if category:
        q = q.where(Alert.category == category)
    names = {t.id: t.name for t in db.scalars(select(Tenant))}
    out = []
    for a in db.scalars(q.order_by(Alert.created_at.desc()).limit(500)):
        o = AlertOut.model_validate(a)
        o.tenant_name = names.get(a.tenant_id or "")
        out.append(o)
    return out


@router.post("/alerts/{alert_id}/{action}", response_model=AlertOut)
def set_alert(
    alert_id: str,
    action: str,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    if action not in ("ack", "resolve"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert_not_found")
    a.status = "acknowledged" if action == "ack" else "resolved"
    a.status_by, a.updated_at = user.email, datetime.now(UTC)
    if a.status == "resolved":
        a.resolved_at = a.updated_at
    audit.record(db, request, f"alert.{a.status}", user=user, tenant_id=a.tenant_id, target=a.title)
    return a


@router.get("/channels", response_model=list[ChannelOut])
def platform_channels(_: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    return [
        channel_out(db, c)
        for c in db.scalars(select(NotificationChannel).where(NotificationChannel.tenant_id.is_(None)))
    ]


@router.post("/channels", response_model=ChannelOut, status_code=201)
def add_platform_channel(
    body: ChannelIn,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
    vault: Vault = Depends(get_vault),
):
    out = create_channel(db, vault, None, body, user)
    audit.record(db, request, "channel.create", user=user, target=f"{body.type}:{body.name}")
    return out


@router.delete("/channels/{channel_id}", status_code=204)
def remove_platform_channel(
    channel_id: str,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    ch = _channel(db, None, channel_id)
    audit.record(db, request, "channel.delete", user=user, target=ch.name)
    db.delete(ch)


@router.post("/channels/{channel_id}/test", status_code=202)
def test_platform_channel(
    channel_id: str, _: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)
) -> dict:
    ch = _channel(db, None, channel_id)
    job = enqueue(db, "notify", payload={"test": True, "channel_id": ch.id}, dedupe=False)
    return {"job_id": job.id if job else None}
