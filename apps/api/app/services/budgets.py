"""Orçamentos mensais por empresa: realizado, previsão e alertas por limite."""

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountAssignment, Budget, CloudAccount, CostLine, Tenant
from app.services.alerts import raise_alert
from app.services.reports import base_filters


def _scope_conds(db: Session, b: Budget, start: date, end: date) -> list:
    sc = b.scope or {}
    account_ids = None
    if sc.get("accounts"):
        account_ids = list(
            db.scalars(
                select(CloudAccount.id)
                .join(AccountAssignment, AccountAssignment.cloud_account_id == CloudAccount.id)
                .where(AccountAssignment.tenant_id == b.tenant_id, CloudAccount.native_id.in_(sc["accounts"]))
            ).all()
        ) or ["-"]
    tag = None
    if sc.get("tag") and "=" in sc["tag"]:
        k, v = sc["tag"].split("=", 1)
        tag = (k, v)
    conds = base_filters(b.tenant_id, start, end, account_ids, sc.get("services") or None, tag)
    conds.append(CostLine.currency == b.currency)
    return conds


def compute(db: Session, b: Budget, today: date) -> tuple[Decimal, Decimal, date | None]:
    """Realizado no mês e previsão linear (média dos últimos 7 dias com dado × dias restantes)."""
    first = today.replace(day=1)
    last_day = first.replace(day=monthrange(first.year, first.month)[1])
    conds = _scope_conds(db, b, first, last_day)
    actual = db.scalar(select(func.coalesce(func.sum(CostLine.billed_cost), 0)).where(*conds)) or Decimal(0)
    last_data = db.scalar(select(func.max(CostLine.charge_date)).where(*conds))
    if last_data is None:
        return Decimal(actual), Decimal(actual), None
    window_start = max(first, last_data - timedelta(days=6))
    window = db.scalar(
        select(func.coalesce(func.sum(CostLine.billed_cost), 0)).where(
            *conds, CostLine.charge_date >= window_start, CostLine.charge_date <= last_data
        )
    ) or Decimal(0)
    days = (last_data - window_start).days + 1
    daily = Decimal(window) / days
    remaining = (last_day - last_data).days
    return Decimal(actual), Decimal(actual) + daily * remaining, last_data


def evaluate_budgets(db: Session, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    ym = today.strftime("%Y-%m")
    raised = 0
    names = {t.id: t.name for t in db.scalars(select(Tenant))}
    for b in db.scalars(select(Budget)):
        actual, forecast, _ = compute(db, b, today)
        b.last_actual, b.last_forecast, b.last_evaluated_at = actual, forecast, datetime.now(UTC)
        amount = Decimal(b.amount)
        if amount <= 0:
            continue
        pct = actual / amount * 100
        for t in sorted(int(x) for x in b.thresholds or []):
            if pct >= t:
                sev = "critical" if t >= 100 else "warning" if t >= 80 else "info"
                a = raise_alert(
                    db,
                    tenant_id=b.tenant_id,
                    type="budget_actual",
                    category="cost",
                    severity=sev,
                    title=f"Orçamento '{b.name}' atingiu {t}%",
                    message=(
                        f"{names.get(b.tenant_id, '')}: gasto de {b.currency} {actual:,.2f} em {ym}, "
                        f"{pct:.0f}% do orçamento de {b.currency} {amount:,.2f}."
                    ),
                    dedupe_key=f"budget:{b.id}:{ym}:actual:{t}",
                    data={"budget_id": b.id, "month": ym, "actual": str(actual), "amount": str(amount)},
                )
                raised += a is not None
        if b.alert_on_forecast and forecast >= amount > actual:
            a = raise_alert(
                db,
                tenant_id=b.tenant_id,
                type="budget_forecast",
                category="cost",
                severity="warning",
                title=f"Previsão acima do orçamento '{b.name}'",
                message=(
                    f"No ritmo atual, {ym} deve fechar em {b.currency} {forecast:,.2f}, "
                    f"acima do orçamento de {b.currency} {amount:,.2f} (realizado: {actual:,.2f})."
                ),
                dedupe_key=f"budget:{b.id}:{ym}:forecast",
                data={"budget_id": b.id, "month": ym, "forecast": str(forecast), "amount": str(amount)},
            )
            raised += a is not None
    db.flush()
    return {"alerts": raised}
