"""Custos (Cost Explorer ou export FOCUS no S3) e invoices da conta pagadora."""

import gzip
import itertools
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors.common import Ctx, client, now
from app.models import CloudAccount, Connection, CostLine, Document
from app.services.documents import DocumentRejected, store_document
from app.services.focus_import import ImportErrorInvalid, ingest_rows, read_focus_csv

MAX_PDF = 20 * 1024 * 1024


def months_to_collect(today: date) -> list[tuple[date, date]]:
    """Mês atual sempre; o anterior só na primeira semana (quando a AWS ainda fecha os valores).

    Cada chamada do Cost Explorer custa US$ 0,01, então não reconsultamos meses fechados.
    """
    first = today.replace(day=1)
    out = [(first, today)]
    if today.day <= 7:
        prev_last = first - timedelta(days=1)
        out.insert(0, (prev_last.replace(day=1), prev_last))
    return out


# --- Cost Explorer ------------------------------------------------------------------------------


def fetch_ce_pages(ce, start: date, end_inclusive: date):
    token = None
    while True:
        params = {
            "TimePeriod": {
                "Start": start.isoformat(),
                "End": (end_inclusive + timedelta(days=1)).isoformat(),
            },
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "AmortizedCost"],
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
        }
        if token:
            params["NextPageToken"] = token
        page = ce.get_cost_and_usage(**params)
        yield page
        token = page.get("NextPageToken")
        if not token:
            return


def ce_rows(conn: Connection, pages, names: dict[str, str]):
    for page in pages:
        for day in page.get("ResultsByTime", []):
            d = day["TimePeriod"]["Start"]
            for g in day.get("Groups", []):
                acct, service = g["Keys"]
                billed = g["Metrics"]["UnblendedCost"]
                eff = g["Metrics"].get("AmortizedCost", billed)
                if Decimal(billed["Amount"]) == 0 and Decimal(eff["Amount"]) == 0:
                    continue
                yield {
                    "ProviderName": "AWS",
                    "BillingAccountId": conn.native_id,
                    "BillingAccountName": conn.name,
                    "SubAccountId": acct,
                    "SubAccountName": names.get(acct) or "",
                    "ChargePeriodStart": d,
                    "ServiceName": service,
                    "ChargeCategory": "Usage",
                    "BillingCurrency": billed.get("Unit", "USD"),
                    "BilledCost": billed["Amount"],
                    "EffectiveCost": eff["Amount"],
                    "Tags": "",
                }


def collect_costs(db: Session, ctx: Ctx, conn: Connection, today: date | None = None) -> dict:
    today = today or now().date()
    if conn.cost_source == "focus_s3":
        return collect_focus_s3(db, ctx, conn, today)
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-costs")
    ce = client(sess, "ce", "us-east-1")
    names = {
        a.native_id: a.name or ""
        for a in db.scalars(select(CloudAccount).where(CloudAccount.connection_id == conn.id))
    }
    out = {}
    for start, end in months_to_collect(today):
        rows = list(ce_rows(conn, fetch_ce_pages(ce, start, end), names))
        if not rows:
            out[f"{start:%Y-%m}"] = 0
            continue
        r = ingest_rows(
            db,
            rows,
            filename=f"Cost Explorer {start:%Y-%m}",
            provider_hint="aws",
            user_email=None,
            source="cost_explorer",
            replace_range=(start, end),
        )
        out[f"{start:%Y-%m}"] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "cost_explorer", "rows": out}


# --- FOCUS no S3 (AWS Data Exports) --------------------------------------------------------------


def _s3_location(uri: str) -> tuple[str, str]:
    u = urlparse(uri or "")
    if u.scheme != "s3" or not u.netloc:
        raise ValueError("focus_s3_uri deve ser s3://bucket/prefixo")
    return u.netloc, u.path.lstrip("/")


def collect_focus_s3(db: Session, ctx: Ctx, conn: Connection, today: date) -> dict:
    """Lê os arquivos CSV(.gz) do Data Export FOCUS: <prefixo>/.../BILLING_PERIOD=AAAA-MM/*.csv.gz"""
    bucket, prefix = _s3_location(conn.focus_s3_uri or "")
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-costs")
    s3 = client(sess, "s3", "us-east-1")
    out = {}
    for start, _ in months_to_collect(today):
        marker = f"BILLING_PERIOD={start:%Y-%m}"
        keys = [
            o["Key"]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])
            if marker in o["Key"] and o["Key"].endswith((".csv.gz", ".csv"))
        ]
        if not keys:
            out[start.strftime("%Y-%m")] = "sem arquivos (use o formato CSV no Data Export)"
            continue

        def rows(keys=keys):
            for k in sorted(keys):
                body = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
                yield from read_focus_csv(gzip.decompress(body) if k.endswith(".gz") else body)

        _, last_day = monthrange(start.year, start.month)
        try:
            r = ingest_rows(
                db,
                rows(),
                filename=f"s3://{bucket}/{prefix} {start:%Y-%m}",
                provider_hint="aws",
                user_email=None,
                source="focus_s3",
                replace_range=(start, start.replace(day=last_day)),
            )
        except ImportErrorInvalid as e:
            out[start.strftime("%Y-%m")] = f"arquivo inválido: {e}"
            continue
        out[start.strftime("%Y-%m")] = r.batch.stored_rows
    conn.last_sync_at = now()
    return {"source": "focus_s3", "files": out}


# --- Invoices -------------------------------------------------------------------------------------


def exclusive_owner(db: Session, conn: Connection, period: date) -> str | None:
    """Empresa dona de TODOS os custos da pagadora no mês, ou None se a pagadora é compartilhada.

    Só nesse caso a invoice oficial pode ser publicada para a empresa: ela não revela custos de
    terceiros.
    """
    _, last = monthrange(period.year, period.month)
    # Todas as contas da conexão (Organization/pagadora ou tenant Azure) no mês
    owners = db.execute(
        select(CostLine.tenant_id, func.count())
        .join(CloudAccount, CloudAccount.id == CostLine.cloud_account_id)
        .where(
            CostLine.provider == conn.provider,
            CloudAccount.connection_id == conn.id,
            CostLine.charge_date >= period,
            CostLine.charge_date <= period.replace(day=last),
        )
        .group_by(CostLine.tenant_id)
    ).all()
    if len(owners) == 1 and owners[0][0] is not None:
        return owners[0][0]
    return None


def download_pdf(url: str) -> bytes:
    u = urlparse(url)
    # URL pré-assinada da própria AWS; qualquer outro destino é recusado (evita SSRF)
    if u.scheme != "https" or not (u.hostname or "").endswith(".amazonaws.com"):
        raise ValueError("URL de download inesperada")
    with httpx.stream("GET", url, timeout=60, follow_redirects=False) as r:
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > MAX_PDF:
                raise ValueError("PDF maior que o limite")
            chunks.append(chunk)
    return b"".join(chunks)


def _amount(summary: dict) -> tuple[Decimal | None, str | None]:
    for k in ("PaymentCurrencyAmount", "BaseCurrencyAmount"):
        a = summary.get(k) or {}
        if a.get("TotalAmount"):
            return Decimal(a["TotalAmount"]), a.get("CurrencyCode")
    return None, None


def collect_invoices(
    db: Session, ctx: Ctx, conn: Connection, today: date | None = None, months: int = 3
) -> dict:
    today = today or now().date()
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-invoices")
    inv = client(sess, "invoicing", "us-east-1")
    published, shared, existing = [], [], 0
    first = today.replace(day=1)
    periods = []
    for _ in range(months):
        first = (first - timedelta(days=1)).replace(day=1)
        periods.append(first)

    for period in periods:
        owner = exclusive_owner(db, conn, period)
        params = {
            "Selector": {"ResourceType": "ACCOUNT_ID", "Value": conn.native_id},
            "Filter": {"BillingPeriod": {"Month": period.month, "Year": period.year}},
        }
        summaries = []
        for _ in itertools.count():
            page = inv.list_invoice_summaries(**params)
            summaries += page.get("InvoiceSummaries", [])
            if not page.get("NextToken"):
                break
            params["NextToken"] = page["NextToken"]
        for s in summaries:
            number = s.get("CommercialInvoiceId") or s["InvoiceId"]
            if owner is None:
                shared.append(number)
                continue
            if db.scalar(
                select(Document.id).where(
                    Document.tenant_id == owner, Document.provider == "aws", Document.number == number
                )
            ):
                existing += 1
                continue
            url = inv.get_invoice_pdf(InvoiceId=s["InvoiceId"])["InvoicePDF"]["DocumentUrl"]
            amount, currency = _amount(s)
            due = s.get("DueDate")
            try:
                store_document(
                    db,
                    ctx.storage,
                    tenant_id=owner,
                    data=download_pdf(url),
                    filename=f"aws-invoice-{number}.pdf",
                    kind="invoice",
                    billing_period=period,
                    uploaded_by="coleta automática",
                    provider="aws",
                    number=number,
                    issuer=(s.get("Entity") or {}).get("InvoicingEntity"),
                    amount=amount,
                    currency=currency,
                    due_date=due.date() if hasattr(due, "date") else None,
                )
            except DocumentRejected:
                existing += 1
                continue
            published.append(number)
    return {
        "published": published,
        "already_published": existing,
        # Pagadora compartilhada: a invoice não vai para nenhuma empresa (use o demonstrativo)
        "not_published_shared_payer": shared,
    }
