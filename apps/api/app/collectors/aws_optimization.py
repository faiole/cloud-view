"""Recomendações do AWS Cost Optimization Hub, cobertura de Savings Plans e anomalias da AWS."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.aws_billing import months_to_collect
from app.collectors.common import Ctx, client, error_text, now
from app.models import CloudAccount, CommitmentCoverage, Connection, Recommendation
from app.services.alerts import raise_alert
from app.services.allocation import active_rules, owner_for_tags, whole_account_owner

ACTION_PT = {
    "Rightsize": "Redimensionar",
    "Stop": "Desligar recurso ocioso",
    "Upgrade": "Atualizar geração",
    "PurchaseSavingsPlans": "Comprar Savings Plans",
    "PurchaseReservedInstances": "Comprar instâncias reservadas",
    "MigrateToGraviton": "Migrar para Graviton",
    "Delete": "Remover recurso sem uso",
    "ScaleIn": "Reduzir capacidade",
}
COH_SOURCE = "cost_optimization_hub"


def _accounts(db: Session, conn: Connection) -> dict[str, CloudAccount]:
    return {
        a.native_id: a for a in db.scalars(select(CloudAccount).where(CloudAccount.connection_id == conn.id))
    }


def collect_recommendations(db: Session, ctx: Ctx, conn: Connection) -> dict:
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-optimization")
    coh = client(sess, "cost-optimization-hub", "us-east-1")
    accounts = _accounts(db, conn)
    rules = {a.id: active_rules(db, a.id, now().date()) for a in accounts.values()}
    items = []
    params: dict = {"includeAllRecommendations": False, "maxResults": 1000}
    try:
        while True:
            page = coh.list_recommendations(**params)
            items += page.get("items", [])
            if not page.get("nextToken"):
                break
            params["nextToken"] = page["nextToken"]
    except Exception as e:  # noqa: BLE001
        msg = error_text(e)
        if "OptIn" in msg or "AccessDenied" in msg or "not enrolled" in msg.lower():
            return {
                "skipped": "Cost Optimization Hub não habilitado na conta (Billing → Cost Optimization Hub)"
            }
        raise

    seen = set()
    for it in items:
        acc = accounts.get(it.get("accountId"))
        tags = {t["key"]: t.get("value", "") for t in it.get("tags", []) if "key" in t}
        rid = it["recommendationId"]
        rec = db.scalar(
            select(Recommendation).where(Recommendation.source == COH_SOURCE, Recommendation.native_id == rid)
        )
        if rec is None:
            rec = Recommendation(source=COH_SOURCE, native_id=rid)
            db.add(rec)
        action = it.get("actionType", "Other")
        resource = it.get("resourceId") or it.get("resourceArn")
        rec.cloud_account_id = acc.id if acc else None
        rec.tenant_id = owner_for_tags(rules.get(acc.id, []), tags) if acc else None
        rec.action_type = action
        rec.resource_type = it.get("currentResourceType")
        rec.resource_native_id = resource
        rec.region = it.get("region")
        rec.title = f"{ACTION_PT.get(action, action)}: {resource or it.get('currentResourceType')}"[:300]
        rec.current = (it.get("currentResourceSummary") or "")[:300] or None
        rec.recommended = (it.get("recommendedResourceSummary") or "")[:300] or None
        rec.monthly_savings = Decimal(str(it.get("estimatedMonthlySavings") or 0))
        rec.monthly_cost = (
            Decimal(str(it["estimatedMonthlyCost"])) if it.get("estimatedMonthlyCost") is not None else None
        )
        rec.currency = (it.get("currencyCode") or "USD")[:3]
        rec.savings_pct = (
            Decimal(str(it["estimatedSavingsPercentage"]))
            if it.get("estimatedSavingsPercentage") is not None
            else None
        )
        rec.effort = it.get("implementationEffort")
        rec.restart_needed = it.get("restartNeeded")
        rec.tags = tags
        rec.last_seen = now()
        if rec.status == "resolved":
            rec.status = "open"  # voltou a aparecer
        seen.add(rid)

    acc_ids = [a.id for a in accounts.values()]
    resolved = 0
    if acc_ids:
        for old in db.scalars(
            select(Recommendation).where(
                Recommendation.source == COH_SOURCE,
                Recommendation.cloud_account_id.in_(acc_ids),
                Recommendation.status.in_(("open", "done")),
            )
        ):
            if old.native_id not in seen:
                old.status, old.status_by = "resolved", "automático"
                resolved += 1
    db.flush()
    return {"recommendations": len(items), "resolved": resolved}


def collect_sp_coverage(db: Session, ctx: Ctx, conn: Connection, today: date | None = None) -> dict:
    today = today or now().date()
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-optimization")
    ce = client(sess, "ce", "us-east-1")
    accounts = _accounts(db, conn)
    out = 0
    for start, end in months_to_collect(today):
        params = {
            "TimePeriod": {"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()},
            "Granularity": "MONTHLY",
            "GroupBy": [{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
        }
        while True:
            page = ce.get_savings_plans_coverage(**params)
            for cov in page.get("SavingsPlansCoverages", []):
                acc = accounts.get(
                    (cov.get("Attributes") or {}).get("LINKED_ACCOUNT")
                    or (cov.get("Attributes") or {}).get("linkedAccount")
                )
                if acc is None:
                    continue
                c = cov.get("Coverage", {})
                row = db.scalar(
                    select(CommitmentCoverage).where(
                        CommitmentCoverage.cloud_account_id == acc.id, CommitmentCoverage.month == start
                    )
                )
                if row is None:
                    row = CommitmentCoverage(cloud_account_id=acc.id, month=start)
                    db.add(row)
                row.covered = Decimal(c.get("SpendCoveredBySavingsPlans") or 0)
                row.on_demand = Decimal(c.get("OnDemandCost") or 0)
                row.total = Decimal(c.get("TotalCost") or 0)
                row.tenant_id = whole_account_owner(db, acc.id)
                row.updated_at = now()
                out += 1
            if not page.get("NextToken"):
                break
            params["NextToken"] = page["NextToken"]
    db.flush()
    return {"coverage_rows": out}


def collect_aws_anomalies(db: Session, ctx: Ctx, conn: Connection, today: date | None = None) -> dict:
    """Anomalias do AWS Cost Anomaly Detection (precisa de um monitor criado na pagadora).
    Vira alerta para a empresa dona da conta afetada, se a conta for inteira dela."""
    today = today or now().date()
    sess = ctx.hub.assume(conn.role_arn, conn.external_id, session_name="cloudview-optimization")
    ce = client(sess, "ce", "us-east-1")
    accounts = _accounts(db, conn)
    params = {
        "DateInterval": {"StartDate": (today - timedelta(days=10)).isoformat(), "EndDate": today.isoformat()},
        "TotalImpact": {"NumericOperator": "GREATER_THAN_OR_EQUAL", "StartValue": 10.0},
    }
    raised = 0
    while True:
        page = ce.get_anomalies(**params)
        for an in page.get("Anomalies", []):
            impact = an.get("Impact", {})
            for rc in an.get("RootCauses", []) or [{}]:
                acc = accounts.get(rc.get("LinkedAccount") or "")
                tenant = whole_account_owner(db, acc.id) if acc else None
                if tenant is None:
                    continue
                a = raise_alert(
                    db,
                    tenant_id=tenant,
                    type="cost_anomaly_aws",
                    category="cost",
                    severity="warning",
                    title=f"AWS detectou custo anômalo em {rc.get('Service') or an.get('DimensionValue') or 'serviço'}",
                    message=(
                        f"Impacto de USD {impact.get('TotalImpact', 0):,.2f} desde {an.get('AnomalyStartDate', '')[:10]} "
                        f"na conta {rc.get('LinkedAccountName') or rc.get('LinkedAccount')} "
                        f"({rc.get('Region') or 'global'}, {rc.get('UsageType') or '—'})."
                    ),
                    dedupe_key=f"aws_anomaly:{an['AnomalyId']}:{tenant}",
                    data={"anomaly_id": an["AnomalyId"], "root_cause": rc, "impact": impact},
                )
                raised += a is not None
        if not page.get("NextPageToken"):
            break
        params["NextPageToken"] = page["NextPageToken"]
    return {"alerts": raised}


def collect_optimization(db: Session, ctx: Ctx, conn: Connection) -> dict:
    """Job diário: cada parte falha de forma independente."""
    out = {}
    for name, fn in (
        ("recommendations", collect_recommendations),
        ("savings_plans_coverage", collect_sp_coverage),
        ("aws_anomalies", collect_aws_anomalies),
    ):
        try:
            with db.begin_nested():
                out[name] = fn(db, ctx, conn)
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": error_text(e)}
    return out
