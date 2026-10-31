"""Consultas de custo por cliente. Sempre filtram por `tenant_id` (além do RLS)."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CloudAccount, CostLine

GROUPS = {"account", "service", "region", "month", "day", "charge_category", "provider"}
NO_TAG = "(sem tag)"


def _month_expr(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        return func.to_char(CostLine.charge_date, "YYYY-MM")
    return func.strftime("%Y-%m", CostLine.charge_date)


def _metric(metric: str):
    return CostLine.effective_cost if metric == "effective" else CostLine.billed_cost


def parse_group(group_by: str) -> tuple[str, str | None]:
    if group_by.startswith("tag:") and len(group_by) > 4:
        return "tag", group_by[4:]
    if group_by in GROUPS:
        return group_by, None
    raise ValueError("group_by inválido")


def base_filters(
    tenant_id: str,
    start: date,
    end: date,
    account_ids: list[str] | None = None,
    services: list[str] | None = None,
    tag: tuple[str, str] | None = None,
):
    conds = [CostLine.tenant_id == tenant_id, CostLine.charge_date >= start, CostLine.charge_date <= end]
    if account_ids:
        conds.append(CostLine.cloud_account_id.in_(account_ids))
    if services:
        conds.append(CostLine.service.in_(services))
    if tag:
        k, v = tag
        if v == NO_TAG:
            conds.append(CostLine.tags[k].as_string().is_(None))
        else:
            conds.append(CostLine.tags[k].as_string() == v)
    return conds


def usage(db: Session, conds: list, group_by: str, metric: str, limit: int = 200) -> list[dict]:
    kind, tag_key = parse_group(group_by)
    m = func.sum(_metric(metric)).label("cost")

    if kind == "account":
        q = (
            select(CloudAccount.native_id, CloudAccount.name, CloudAccount.provider, CostLine.currency, m)
            .join(CloudAccount, CloudAccount.id == CostLine.cloud_account_id)
            .where(*conds)
            .group_by(CloudAccount.native_id, CloudAccount.name, CloudAccount.provider, CostLine.currency)
        )
        rows = [
            {"key": nid, "label": f"{name or nid} ({prov.upper()} {nid})", "currency": cur, "cost": c}
            for nid, name, prov, cur, c in db.execute(q)
        ]
    else:
        expr = {
            "service": CostLine.service,
            "region": CostLine.region,
            "charge_category": CostLine.charge_category,
            "provider": CostLine.provider,
            "day": CostLine.charge_date,
            "month": _month_expr(db),
        }.get(kind)
        if kind == "tag":
            expr = CostLine.tags[tag_key].as_string()
        q = select(expr.label("k"), CostLine.currency, m).where(*conds).group_by(expr, CostLine.currency)
        rows = []
        for k, cur, c in db.execute(q):
            key = str(k) if k is not None else (NO_TAG if kind == "tag" else "—")
            rows.append({"key": key, "label": key, "currency": cur, "cost": c})

    if kind in ("day", "month"):
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: r["cost"] or Decimal(0), reverse=True)
    return rows[:limit]


def totals(db: Session, conds: list, metric: str) -> list[dict]:
    q = select(CostLine.currency, func.sum(_metric(metric))).where(*conds).group_by(CostLine.currency)
    return [{"currency": cur, "cost": c or Decimal(0)} for cur, c in db.execute(q)]


def daily(db: Session, conds: list, metric: str) -> list[dict]:
    q = (
        select(CostLine.charge_date, CostLine.currency, func.sum(_metric(metric)))
        .where(*conds)
        .group_by(CostLine.charge_date, CostLine.currency)
        .order_by(CostLine.charge_date)
    )
    return [{"date": d, "currency": cur, "cost": c} for d, cur, c in db.execute(q)]


def month_bounds(today: date) -> tuple[date, date, date]:
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    return first, prev_last.replace(day=1), prev_last


def tag_keys(db: Session, tenant_id: str) -> list[str]:
    if db.get_bind().dialect.name == "postgresql":
        q = (
            select(func.jsonb_object_keys(CostLine.tags).label("k"))
            .where(CostLine.tenant_id == tenant_id)
            .distinct()
        )
        return sorted(db.scalars(q).all())
    keys: set[str] = set()
    for (tags,) in db.execute(select(CostLine.tags).where(CostLine.tenant_id == tenant_id).distinct()):
        keys.update((tags or {}).keys())
    return sorted(keys)


def tag_values(db: Session, tenant_id: str, key: str) -> list[str]:
    expr = CostLine.tags[key].as_string()
    q = select(expr).where(CostLine.tenant_id == tenant_id).distinct()
    return sorted({v if v is not None else NO_TAG for v in db.scalars(q).all()})


def detail_rows(db: Session, conds: list, include_billing: bool):
    q = (
        select(CostLine, CloudAccount.native_id, CloudAccount.name)
        .join(CloudAccount, CloudAccount.id == CostLine.cloud_account_id)
        .where(*conds)
        .order_by(CostLine.charge_date, CloudAccount.native_id, CostLine.service)
        .execution_options(yield_per=2000)
    )
    for line, nid, name in db.execute(q):
        row = {
            "data": line.charge_date.isoformat(),
            "nuvem": line.provider,
            "conta_id": nid,
            "conta_nome": name or "",
            "servico": line.service,
            "regiao": line.region or "",
            "categoria": line.charge_category or "",
            "moeda": line.currency,
            "custo_faturado": f"{line.billed_cost:.6f}",
            "custo_efetivo": f"{line.effective_cost:.6f}",
            "tags": "; ".join(f"{k}={v}" for k, v in sorted((line.tags or {}).items())),
        }
        if include_billing:
            row["conta_pagadora"] = line.billing_account_id
        yield row
