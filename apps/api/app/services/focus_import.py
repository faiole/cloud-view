"""Importação de custos no formato FOCUS (FinOps Open Cost & Usage Specification).

AWS (Data Exports → FOCUS 1.0), Azure (Cost Management → exportação FOCUS) e OCI (FOCUS reports)
geram o mesmo layout, então um único importador atende as três nuvens. As linhas são agregadas
por dia/conta/serviço/região/tags antes de gravar.

Reimportar o mesmo período da mesma conta pagadora substitui os dados (não duplica).
"""

import csv
import gzip
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CloudAccount, Connection, CostLine, ImportBatch
from app.services.allocation import reallocate_account

REQUIRED = {"BilledCost", "BillingAccountId", "ChargePeriodStart", "BillingCurrency"}
PROVIDER_ALIASES = {
    "aws": "aws",
    "amazon web services": "aws",
    "microsoft": "azure",
    "azure": "azure",
    "microsoft azure": "azure",
    "oracle": "oci",
    "oci": "oci",
    "oracle cloud infrastructure": "oci",
}


class ImportErrorInvalid(ValueError):
    pass


@dataclass
class ImportResult:
    batch: ImportBatch
    new_accounts: list[str] = field(default_factory=list)
    unassigned_accounts: list[str] = field(default_factory=list)


def _open_text(data: bytes) -> io.TextIOBase:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return io.StringIO(data.decode("utf-8-sig"))


def _dec(v: str | None) -> Decimal:
    if v is None or v == "":
        return Decimal(0)
    try:
        return Decimal(v)
    except InvalidOperation as e:
        raise ImportErrorInvalid(f"valor inválido: {v!r}") from e


def _date(v: str) -> date:
    v = v.strip()
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(v[:10])


def _tags(v: str | None) -> dict:
    if not v:
        return {}
    try:
        raw = json.loads(v)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, val in raw.items():
        # AWS exporta tags de alocação como "user:Chave"; normalizamos para "Chave"
        key = k[5:] if k.startswith("user:") else k
        out[key] = "" if val is None else str(val)
    return out


def _provider(row: dict, default: str | None) -> str:
    raw = (row.get("ProviderName") or row.get("Provider") or default or "").strip().lower()
    p = PROVIDER_ALIASES.get(raw)
    if not p:
        raise ImportErrorInvalid("não foi possível identificar a nuvem (ProviderName); informe o provider")
    return p


def read_focus_csv(data: bytes) -> Iterator[dict]:
    reader = csv.DictReader(_open_text(data))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        raise ImportErrorInvalid(f"colunas FOCUS ausentes: {', '.join(sorted(missing))}")
    yield from reader


def import_focus(
    db: Session, data: bytes, *, filename: str | None, provider_hint: str | None, user_email: str | None
) -> ImportResult:
    return ingest_rows(
        db, read_focus_csv(data), filename=filename, provider_hint=provider_hint, user_email=user_email
    )


def ingest_rows(
    db: Session,
    rows: Iterable[dict],
    *,
    filename: str | None,
    provider_hint: str | None,
    user_email: str | None,
    source: str = "focus_csv",
    replace_range: tuple[date, date] | None = None,
) -> ImportResult:
    """Agrega linhas no layout FOCUS e grava, substituindo o período já existente das mesmas pagadoras.

    `replace_range` força o intervalo substituído (ex.: mês inteiro consultado no Cost Explorer,
    mesmo que alguns dias ainda não tenham custo).
    """
    agg: dict[tuple, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    accounts: dict[tuple[str, str], tuple[str | None, str, str | None]] = {}
    billing: dict[tuple[str, str], str | None] = {}
    source_rows = 0
    dmin: date | None = None
    dmax: date | None = None

    for row in rows:
        source_rows += 1
        prov = _provider(row, provider_hint)
        billing_id = row["BillingAccountId"].strip()
        sub_id = (row.get("SubAccountId") or "").strip() or billing_id
        d = _date(row["ChargePeriodStart"])
        dmin = d if dmin is None or d < dmin else dmin
        dmax = d if dmax is None or d > dmax else dmax
        tags = _tags(row.get("Tags"))
        key = (
            prov,
            billing_id,
            sub_id,
            d,
            (row.get("ServiceName") or "Other").strip()[:200],
            (row.get("RegionId") or row.get("Region") or "").strip()[:64] or None,
            (row.get("ChargeCategory") or "").strip()[:32] or None,
            row["BillingCurrency"].strip().upper()[:3],
            json.dumps(tags, sort_keys=True),
        )
        a = agg[key]
        a[0] += _dec(row.get("BilledCost"))
        a[1] += _dec(row.get("EffectiveCost") or row.get("BilledCost"))
        accounts[(prov, sub_id)] = (row.get("SubAccountName") or None, billing_id, None)
        billing[(prov, billing_id)] = row.get("BillingAccountName") or None

    if source_rows == 0:
        raise ImportErrorInvalid("arquivo sem linhas")

    if replace_range:
        dmin, dmax = min(dmin, replace_range[0]), max(dmax, replace_range[1])
    batch = ImportBatch(
        source=source,
        filename=filename,
        provider=next(iter(billing))[0] if len(billing) == 1 else None,
        billing_account_id=next(iter(billing))[1] if len(billing) == 1 else None,
        period_start=dmin,
        period_end=dmax,
        source_rows=source_rows,
        created_by=user_email,
    )
    db.add(batch)
    db.flush()

    # Conexões (pagadoras / Organizations) e contas descobertas
    conn_ids: dict[tuple[str, str], str] = {}
    for (prov, bid), bname in billing.items():
        conn = db.scalar(select(Connection).where(Connection.provider == prov, Connection.native_id == bid))
        if conn is None:
            kind = {"aws": "aws_organization", "azure": "azure_billing_account", "oci": "oci_tenancy"}[prov]
            conn = Connection(provider=prov, kind=kind, native_id=bid, name=bname or bid)
            db.add(conn)
            db.flush()
        conn_ids[(prov, bid)] = conn.id

    result = ImportResult(batch=batch)
    acc_ids: dict[tuple[str, str], str] = {}
    for (prov, sub_id), (sub_name, bid, _) in accounts.items():
        acc = db.scalar(
            select(CloudAccount).where(CloudAccount.provider == prov, CloudAccount.native_id == sub_id)
        )
        if acc is None:
            acc = CloudAccount(provider=prov, native_id=sub_id, name=sub_name)
            db.add(acc)
            result.new_accounts.append(sub_id)
        elif sub_name and not acc.name:
            acc.name = sub_name
        # Conta transferida entre Organizations: passa a apontar para a pagadora atual
        acc.connection_id = conn_ids[(prov, bid)]
        db.flush()
        acc_ids[(prov, sub_id)] = acc.id

    # Substitui o período já importado das mesmas pagadoras
    replaced = 0
    for prov, bid in billing:
        replaced += db.execute(
            delete(CostLine)
            .where(
                CostLine.provider == prov,
                CostLine.billing_account_id == bid,
                CostLine.charge_date >= dmin,
                CostLine.charge_date <= dmax,
            )
            .execution_options(synchronize_session=False)
        ).rowcount

    lines = [
        {
            "cloud_account_id": acc_ids[(prov, sub_id)],
            "provider": prov,
            "billing_account_id": bid,
            "charge_date": d,
            "service": service,
            "region": region,
            "charge_category": cat,
            "currency": cur,
            "billed_cost": billed,
            "effective_cost": effective,
            "tags": json.loads(tags),
            "import_batch_id": batch.id,
        }
        for (prov, bid, sub_id, d, service, region, cat, cur, tags), (billed, effective) in agg.items()
    ]
    for i in range(0, len(lines), 5000):
        db.execute(CostLine.__table__.insert(), lines[i : i + 5000])

    for acc_id in set(acc_ids.values()):
        reallocate_account(db, acc_id)

    batch.stored_rows = len(lines)
    batch.replaced_rows = replaced
    unassigned = db.scalars(
        select(CloudAccount.native_id)
        .where(CloudAccount.id.in_(list(acc_ids.values())))
        .where(~CloudAccount.assignments.any())
    ).all()
    result.unassigned_accounts = sorted(unassigned)
    db.flush()
    return result
