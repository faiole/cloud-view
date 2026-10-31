"""Otimização, orçamentos, alertas e canais de notificação de uma empresa."""

import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    Budget,
    CloudAccount,
    CommitmentCoverage,
    NotificationChannel,
    NotificationDelivery,
    Recommendation,
    Tenant,
)
from app.permissions import Perm
from app.schemas import (
    AlertOut,
    BudgetIn,
    BudgetOut,
    ChannelIn,
    ChannelOut,
    CoverageMonth,
    RecommendationOut,
    RecommendationStatusIn,
    RecommendationSummary,
    Total,
)
from app.security.deps import has_perm, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services.budgets import compute
from app.services.jobs import enqueue
from app.services.notify import InvalidTarget, validate_url
from app.services.secrets import store_secret
from app.vault import Vault, VaultError, get_vault

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["finops"])

CATEGORY_PERM = {"cost": Perm.costs_read, "health": Perm.resources_read, "security": Perm.resources_read}


def _any_perm(user: SessionData, tenant_id: str, *perms: Perm) -> None:
    if not any(has_perm(user, tenant_id, p) for p in perms):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing_permission")


# --- Recomendações --------------------------------------------------------------------------------


def _rec_out(r: Recommendation, accounts: dict[str, str]) -> RecommendationOut:
    o = RecommendationOut.model_validate(r)
    o.account_native_id = accounts.get(r.cloud_account_id or "")
    return o


@router.get("/recommendations", response_model=RecommendationSummary)
def recommendations(
    tenant_id: str,
    status_: str = Query("open", alias="status"),
    action_type: str | None = None,
    _: SessionData = Depends(tenant_access(Perm.costs_read)),
    db: Session = Depends(scoped_db),
):
    q = select(Recommendation).where(Recommendation.tenant_id == tenant_id)
    if status_ != "all":
        q = q.where(Recommendation.status == status_)
    if action_type:
        q = q.where(Recommendation.action_type == action_type)
    rows = db.scalars(q.order_by(Recommendation.monthly_savings.desc()).limit(1000)).all()
    accounts = {a.id: a.native_id for a in db.scalars(select(CloudAccount))}
    open_rows = db.scalars(
        select(Recommendation).where(Recommendation.tenant_id == tenant_id, Recommendation.status == "open")
    ).all()
    savings: dict[str, Decimal] = {}
    by_action: dict[str, int] = {}
    for r in open_rows:
        savings[r.currency] = savings.get(r.currency, Decimal(0)) + Decimal(r.monthly_savings or 0)
        by_action[r.action_type] = by_action.get(r.action_type, 0) + 1
    return RecommendationSummary(
        open=len(open_rows),
        savings=[Total(currency=c, cost=v) for c, v in savings.items()],
        by_action=by_action,
        items=[_rec_out(r, accounts) for r in rows],
    )


@router.patch("/recommendations/{rec_id}", response_model=RecommendationOut)
def set_recommendation_status(
    tenant_id: str,
    rec_id: str,
    body: RecommendationStatusIn,
    request: Request,
    user: SessionData = Depends(tenant_access()),
    db: Session = Depends(scoped_db),
):
    _any_perm(user, tenant_id, Perm.finops_manage, Perm.actions_execute)
    r = db.get(Recommendation, rec_id)
    if r is None or r.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recommendation_not_found")
    r.status, r.status_by, r.status_note = body.status, user.email, body.note
    audit.record(
        db,
        request,
        "recommendation.status",
        user=user,
        tenant_id=tenant_id,
        target=r.title,
        details=body.model_dump(),
    )
    return _rec_out(r, {})


@router.get("/commitments", response_model=list[CoverageMonth])
def commitments(
    tenant_id: str, _: SessionData = Depends(tenant_access(Perm.costs_read)), db: Session = Depends(scoped_db)
):
    rows = db.execute(
        select(
            CommitmentCoverage.month,
            func.sum(CommitmentCoverage.covered),
            func.sum(CommitmentCoverage.on_demand),
            func.sum(CommitmentCoverage.total),
        )
        .where(CommitmentCoverage.tenant_id == tenant_id)
        .group_by(CommitmentCoverage.month)
        .order_by(CommitmentCoverage.month)
    ).all()
    return [
        CoverageMonth(
            month=m,
            covered=c,
            on_demand=o,
            total=t,
            coverage_pct=(Decimal(c) / Decimal(t) * 100).quantize(Decimal("0.1")) if t else None,
        )
        for m, c, o, t in rows
    ]


# --- Orçamentos -----------------------------------------------------------------------------------


def _budget_out(b: Budget) -> BudgetOut:
    sc = b.scope or {}
    pct = (
        (Decimal(b.last_actual) / Decimal(b.amount) * 100).quantize(Decimal("0.1"))
        if b.last_actual is not None
        else None
    )
    return BudgetOut(
        id=b.id,
        name=b.name,
        amount=b.amount,
        currency=b.currency,
        accounts=sc.get("accounts", []),
        services=sc.get("services", []),
        tag=sc.get("tag"),
        thresholds=b.thresholds,
        alert_on_forecast=b.alert_on_forecast,
        actual=b.last_actual,
        forecast=b.last_forecast,
        pct=pct,
        last_evaluated_at=b.last_evaluated_at,
    )


def _budget(db: Session, tenant_id: str, budget_id: str) -> Budget:
    b = db.get(Budget, budget_id)
    if b is None or b.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "budget_not_found")
    return b


def _apply(b: Budget, body: BudgetIn) -> None:
    b.name, b.amount, b.currency = body.name, body.amount, body.currency
    b.scope = {
        k: v for k, v in {"accounts": body.accounts, "services": body.services, "tag": body.tag}.items() if v
    }
    b.thresholds, b.alert_on_forecast = body.thresholds, body.alert_on_forecast


def _refresh(db: Session, b: Budget) -> None:
    actual, forecast, _ = compute(db, b, datetime.now(UTC).date())
    b.last_actual, b.last_forecast, b.last_evaluated_at = actual, forecast, datetime.now(UTC)


@router.get("/budgets", response_model=list[BudgetOut])
def list_budgets(
    tenant_id: str, _: SessionData = Depends(tenant_access(Perm.costs_read)), db: Session = Depends(scoped_db)
):
    return [
        _budget_out(b)
        for b in db.scalars(select(Budget).where(Budget.tenant_id == tenant_id).order_by(Budget.name))
    ]


@router.post("/budgets", response_model=BudgetOut, status_code=201)
def create_budget(
    tenant_id: str,
    body: BudgetIn,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.finops_manage)),
    db: Session = Depends(scoped_db),
):
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    b = Budget(tenant_id=tenant_id, created_by=user.email)
    _apply(b, body)
    db.add(b)
    db.flush()
    _refresh(db, b)
    audit.record(
        db,
        request,
        "budget.create",
        user=user,
        tenant_id=tenant_id,
        target=b.name,
        details=body.model_dump(mode="json"),
    )
    return _budget_out(b)


@router.put("/budgets/{budget_id}", response_model=BudgetOut)
def update_budget(
    tenant_id: str,
    budget_id: str,
    body: BudgetIn,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.finops_manage)),
    db: Session = Depends(scoped_db),
):
    b = _budget(db, tenant_id, budget_id)
    _apply(b, body)
    _refresh(db, b)
    audit.record(
        db,
        request,
        "budget.update",
        user=user,
        tenant_id=tenant_id,
        target=b.name,
        details=body.model_dump(mode="json"),
    )
    return _budget_out(b)


@router.delete("/budgets/{budget_id}", status_code=204)
def delete_budget(
    tenant_id: str,
    budget_id: str,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.finops_manage)),
    db: Session = Depends(scoped_db),
):
    b = _budget(db, tenant_id, budget_id)
    audit.record(db, request, "budget.delete", user=user, tenant_id=tenant_id, target=b.name)
    db.delete(b)


# --- Alertas --------------------------------------------------------------------------------------


def _visible_categories(user: SessionData, tenant_id: str) -> list[str]:
    return [c for c, p in CATEGORY_PERM.items() if has_perm(user, tenant_id, p)]


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    tenant_id: str,
    status_: str = Query("active", alias="status"),
    category: str | None = None,
    user: SessionData = Depends(tenant_access()),
    db: Session = Depends(scoped_db),
):
    cats = _visible_categories(user, tenant_id)
    if category:
        cats = [c for c in cats if c == category]
    q = select(Alert).where(Alert.tenant_id == tenant_id, Alert.category.in_(cats or ["-"]))
    if status_ == "active":
        q = q.where(Alert.status != "resolved")
    elif status_ != "all":
        q = q.where(Alert.status == status_)
    return db.scalars(q.order_by(Alert.created_at.desc()).limit(300)).all()


def _alert_status(
    db: Session, tenant_id: str, alert_id: str, user: SessionData, new: str, request: Request
) -> Alert:
    a = db.get(Alert, alert_id)
    if a is None or a.tenant_id != tenant_id or a.category not in _visible_categories(user, tenant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert_not_found")
    _any_perm(user, tenant_id, Perm.finops_manage, Perm.actions_execute, Perm.members_manage)
    a.status, a.status_by, a.updated_at = new, user.email, datetime.now(UTC)
    if new == "resolved":
        a.resolved_at = a.updated_at
    audit.record(db, request, f"alert.{new}", user=user, tenant_id=tenant_id, target=a.title)
    return a


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    tenant_id: str,
    alert_id: str,
    request: Request,
    user: SessionData = Depends(tenant_access()),
    db: Session = Depends(scoped_db),
):
    return _alert_status(db, tenant_id, alert_id, user, "acknowledged", request)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(
    tenant_id: str,
    alert_id: str,
    request: Request,
    user: SessionData = Depends(tenant_access()),
    db: Session = Depends(scoped_db),
):
    return _alert_status(db, tenant_id, alert_id, user, "resolved", request)


# --- Canais de notificação (empresa ou plataforma) ---------------------------------------------------


def channel_out(db: Session, ch: NotificationChannel, signing_secret: str | None = None) -> ChannelOut:
    o = ChannelOut.model_validate(ch)
    o.signing_secret = signing_secret
    last = db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.channel_id == ch.id)
        .order_by(NotificationDelivery.sent_at.desc())
        .limit(1)
    )
    if last:
        o.last_delivery = "ok" if last.status == "sent" else f"falhou: {last.error}"
    return o


def create_channel(
    db: Session, vault: Vault, tenant_id: str | None, body: ChannelIn, user: SessionData
) -> ChannelOut:
    ch = NotificationChannel(
        tenant_id=tenant_id,
        type=body.type,
        name=body.name,
        min_severity=body.min_severity,
        categories=body.categories,
        created_by=user.email,
    )
    signing = None
    if body.type == "email":
        if not body.emails:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "informe pelo menos um e-mail")
        ch.emails = [str(e).lower() for e in body.emails][:20]
        ch.target_hint = ", ".join(ch.emails)[:200]
    else:
        try:
            validate_url(body.type, body.url or "")
        except InvalidTarget as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
        secret = {"url": body.url}
        if body.type == "webhook":
            signing = secrets.token_urlsafe(32)
            secret["signing_secret"] = signing
        try:
            ch.secret_ref = store_secret(
                db, vault, f"notification_{body.type}", json.dumps(secret).encode(), user.email
            )
        except VaultError as e:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "cofre sem chave pública configurada"
            ) from e
        ch.target_hint = urlparse(body.url).hostname
    db.add(ch)
    db.flush()
    return channel_out(db, ch, signing)


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.members_manage)),
    db: Session = Depends(scoped_db),
):
    return [
        channel_out(db, c)
        for c in db.scalars(select(NotificationChannel).where(NotificationChannel.tenant_id == tenant_id))
    ]


@router.post("/channels", response_model=ChannelOut, status_code=201)
def add_channel(
    tenant_id: str,
    body: ChannelIn,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.members_manage)),
    db: Session = Depends(scoped_db),
    vault: Vault = Depends(get_vault),
):
    if "platform" in body.categories and not user.is_admin:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "categoria 'platform' é só da plataforma")
    out = create_channel(db, vault, tenant_id, body, user)
    audit.record(
        db, request, "channel.create", user=user, tenant_id=tenant_id, target=f"{body.type}:{body.name}"
    )
    return out


def _channel(db: Session, tenant_id: str | None, channel_id: str) -> NotificationChannel:
    ch = db.get(NotificationChannel, channel_id)
    if ch is None or ch.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel_not_found")
    return ch


@router.delete("/channels/{channel_id}", status_code=204)
def remove_channel(
    tenant_id: str,
    channel_id: str,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.members_manage)),
    db: Session = Depends(scoped_db),
):
    ch = _channel(db, tenant_id, channel_id)
    audit.record(db, request, "channel.delete", user=user, tenant_id=tenant_id, target=ch.name)
    db.delete(ch)


@router.post("/channels/{channel_id}/test", status_code=202)
def test_channel(
    tenant_id: str,
    channel_id: str,
    user: SessionData = Depends(tenant_access(Perm.members_manage)),
    db: Session = Depends(scoped_db),
) -> dict:
    ch = _channel(db, tenant_id, channel_id)
    job = enqueue(db, "notify", payload={"test": True, "channel_id": ch.id}, dedupe=False)
    return {"job_id": job.id if job else None}
