import csv
import io
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountAssignment, CloudAccount, CostLine
from app.permissions import Perm
from app.schemas import CostSummary, TagKeys, TagValues, TenantAccountOut, UsageReport
from app.security.deps import scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services import reports as rep

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["reports"])
Metric = Literal["billed", "effective"]


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    today = date.today()
    end = end or today
    start = start or end.replace(day=1)
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start depois de end")
    if (end - start).days > 731:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "período máximo de 2 anos")
    return start, end


def _tag(tag: str | None) -> tuple[str, str] | None:
    if not tag:
        return None
    k, sep, v = tag.partition("=")
    if not sep or not k:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "tag deve ser chave=valor")
    return k, v


def _account_ids(db: Session, tenant_id: str, native_ids: list[str] | None) -> list[str] | None:
    if not native_ids:
        return None
    return list(
        db.scalars(
            select(CloudAccount.id)
            .join(AccountAssignment, AccountAssignment.cloud_account_id == CloudAccount.id)
            .where(AccountAssignment.tenant_id == tenant_id, CloudAccount.native_id.in_(native_ids))
        ).all()
    ) or ["-"]


@router.get("/accounts", response_model=list[TenantAccountOut])
def tenant_accounts(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.costs_read)),
    db: Session = Depends(scoped_db),
):
    """Contas cujos custos (todos ou parte, por tag) pertencem a este cliente."""
    rules = db.execute(
        select(AccountAssignment, CloudAccount)
        .join(CloudAccount, CloudAccount.id == AccountAssignment.cloud_account_id)
        .where(AccountAssignment.tenant_id == tenant_id)
        .order_by(CloudAccount.provider, CloudAccount.native_id)
    ).all()
    out: dict[str, TenantAccountOut] = {}
    for r, a in rules:
        item = out.setdefault(
            a.id,
            TenantAccountOut(
                id=a.id, provider=a.provider, native_id=a.native_id, name=a.name, shared=False, rules=[]
            ),
        )
        desc = "conta inteira" if r.tag_key is None else f"tag {r.tag_key} = {r.tag_value}"
        if r.valid_from or r.valid_to:
            desc += f" ({r.valid_from or '…'} a {r.valid_to or '…'})"
        item.rules.append(desc)
        item.shared = item.shared or r.tag_key is not None
    return list(out.values())


@router.get("/costs/summary", response_model=CostSummary)
def cost_summary(
    tenant_id: str,
    metric: Metric = "billed",
    _: SessionData = Depends(tenant_access(Perm.costs_read)),
    db: Session = Depends(scoped_db),
):
    today = date.today()
    first, prev_first, prev_last = rep.month_bounds(today)
    year_ago = (first - timedelta(days=335)).replace(day=1)
    return CostSummary(
        month_to_date=rep.totals(db, rep.base_filters(tenant_id, first, today), metric),
        previous_month=rep.totals(db, rep.base_filters(tenant_id, prev_first, prev_last), metric),
        monthly=rep.usage(db, rep.base_filters(tenant_id, year_ago, today), "month", metric),
        top_services=rep.usage(db, rep.base_filters(tenant_id, first, today), "service", metric, limit=8),
        accounts=db.scalar(
            select(func.count(func.distinct(CostLine.cloud_account_id))).where(
                CostLine.tenant_id == tenant_id
            )
        )
        or 0,
    )


@router.get("/reports/usage", response_model=UsageReport)
def usage_report(
    tenant_id: str,
    start: date | None = None,
    end: date | None = None,
    group_by: str = "service",
    metric: Metric = "billed",
    account: list[str] | None = Query(default=None, description="ids nativos das contas"),
    service: list[str] | None = Query(default=None),
    tag: str | None = Query(default=None, description="chave=valor"),
    _: SessionData = Depends(tenant_access(Perm.reports_read)),
    db: Session = Depends(scoped_db),
):
    start, end = _period(start, end)
    try:
        rep.parse_group(group_by)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    conds = rep.base_filters(tenant_id, start, end, _account_ids(db, tenant_id, account), service, _tag(tag))
    return UsageReport(
        start=start,
        end=end,
        group_by=group_by,
        metric=metric,
        totals=rep.totals(db, conds, metric),
        rows=rep.usage(db, conds, group_by, metric),
        daily=rep.daily(db, conds, metric),
    )


def _csv_response(rows, filename: str) -> StreamingResponse:
    def gen():
        buf = io.StringIO()
        writer = None
        yield "﻿"  # BOM: Excel em português abre acentos corretamente
        for row in rows:
            if writer is None:
                writer = csv.DictWriter(buf, fieldnames=list(row.keys()), delimiter=";")
                writer.writeheader()
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()

    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/usage.csv")
def usage_csv(
    request: Request,
    tenant_id: str,
    start: date | None = None,
    end: date | None = None,
    account: list[str] | None = Query(default=None),
    service: list[str] | None = Query(default=None),
    tag: str | None = None,
    user: SessionData = Depends(tenant_access(Perm.reports_read)),
    db: Session = Depends(scoped_db),
):
    """Detalhe diário por conta, serviço, região e tags (separador `;`)."""
    start, end = _period(start, end)
    conds = rep.base_filters(tenant_id, start, end, _account_ids(db, tenant_id, account), service, _tag(tag))
    rows = list(rep.detail_rows(db, conds, include_billing=user.is_admin))
    audit.record(
        db,
        request,
        "report.export",
        user=user,
        tenant_id=tenant_id,
        details={"start": str(start), "end": str(end)},
    )
    return _csv_response(rows, f"uso-{start}-{end}.csv")


@router.get("/reports/statement.csv")
def statement_csv(
    request: Request,
    tenant_id: str,
    month: str = Query(pattern=r"^\d{4}-\d{2}$", description="AAAA-MM"),
    metric: Metric = "billed",
    user: SessionData = Depends(tenant_access(Perm.costs_read)),
    db: Session = Depends(scoped_db),
):
    """Demonstrativo mensal do cliente: total por conta e serviço, só com as contas dele.

    Serve como detalhamento mesmo quando a fatura oficial é de uma conta pagadora compartilhada.
    """
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    conds = rep.base_filters(tenant_id, start, end)
    q = (
        select(
            CloudAccount.native_id,
            CloudAccount.name,
            CostLine.service,
            CostLine.currency,
            func.sum(rep._metric(metric)),
        )
        .join(CloudAccount, CloudAccount.id == CostLine.cloud_account_id)
        .where(*conds)
        .group_by(CloudAccount.native_id, CloudAccount.name, CostLine.service, CostLine.currency)
        .order_by(CloudAccount.native_id, func.sum(rep._metric(metric)).desc())
    )
    rows = [
        {
            "mes": month,
            "conta_id": nid,
            "conta_nome": name or "",
            "servico": svc,
            "moeda": cur,
            "valor": f"{c:.2f}",
        }
        for nid, name, svc, cur, c in db.execute(q)
    ]
    audit.record(db, request, "report.statement", user=user, tenant_id=tenant_id, details={"month": month})
    return _csv_response(rows, f"demonstrativo-{month}.csv")


@router.get("/tags", response_model=TagKeys)
def list_tag_keys(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.reports_read)),
    db: Session = Depends(scoped_db),
):
    return TagKeys(keys=rep.tag_keys(db, tenant_id))


@router.get("/tags/{key}", response_model=TagValues)
def list_tag_values(
    tenant_id: str,
    key: str,
    _: SessionData = Depends(tenant_access(Perm.reports_read)),
    db: Session = Depends(scoped_db),
):
    return TagValues(key=key, values=rep.tag_values(db, tenant_id, key))
