"""Detecção de anomalias de custo por empresa, independente de nuvem.

Para cada dia recente com dado, compara o custo diário com a média e o desvio-padrão dos 14 dias
anteriores. É anomalia quando passa de média + max(3σ, 25% da média) e o aumento absoluto é de
pelo menos `MIN_DELTA` na moeda. A mensagem traz os serviços que mais subiram.
"""

import statistics
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CostLine, Tenant
from app.services.alerts import raise_alert

BASELINE_DAYS = 14
MIN_BASELINE = 7
MIN_DELTA = Decimal(20)
CHECK_DAYS = 3


def _daily(db: Session, tenant_id: str, start: date, end: date):
    rows = db.execute(
        select(CostLine.charge_date, CostLine.currency, CostLine.service, func.sum(CostLine.billed_cost))
        .where(CostLine.tenant_id == tenant_id, CostLine.charge_date >= start, CostLine.charge_date <= end)
        .group_by(CostLine.charge_date, CostLine.currency, CostLine.service)
    ).all()
    total: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    by_service: dict[str, dict[date, dict[str, Decimal]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(Decimal))
    )
    for d, cur, svc, c in rows:
        total[cur][d] += Decimal(c)
        by_service[cur][d][svc] += Decimal(c)
    return total, by_service


def detect_for_tenant(db: Session, tenant: Tenant, today: date) -> list[dict]:
    last = db.scalar(select(func.max(CostLine.charge_date)).where(CostLine.tenant_id == tenant.id))
    if last is None:
        return []
    start = last - timedelta(days=BASELINE_DAYS + CHECK_DAYS)
    total, by_service = _daily(db, tenant.id, start, last)
    found = []
    for cur, days in total.items():
        for offset in range(CHECK_DAYS):
            day = last - timedelta(days=offset)
            if day not in days:
                continue
            base = [
                days[day - timedelta(days=i)]
                for i in range(1, BASELINE_DAYS + 1)
                if day - timedelta(days=i) in days
            ]
            if len(base) < MIN_BASELINE:
                continue
            mean = sum(base) / len(base)
            std = Decimal(statistics.pstdev([float(x) for x in base]))
            value = days[day]
            limit = mean + max(3 * std, mean * Decimal("0.25"))
            if value <= limit or value - mean < MIN_DELTA:
                continue
            # Serviços que mais contribuíram para o aumento
            svc_mean: dict[str, Decimal] = defaultdict(Decimal)
            for i in range(1, BASELINE_DAYS + 1):
                for svc, c in by_service[cur].get(day - timedelta(days=i), {}).items():
                    svc_mean[svc] += c / len(base)
            deltas = sorted(
                ((svc, c - svc_mean.get(svc, Decimal(0))) for svc, c in by_service[cur][day].items()),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            found.append(
                {
                    "currency": cur,
                    "day": day,
                    "value": value,
                    "mean": mean,
                    "top": [(s, d) for s, d in deltas if d > 0],
                }
            )
    return found


def detect_anomalies(db: Session, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    raised = 0
    for t in db.scalars(select(Tenant)):
        for a in detect_for_tenant(db, t, today):
            increase = a["value"] - a["mean"]
            pct = increase / a["mean"] * 100 if a["mean"] else Decimal(100)
            top = ", ".join(f"{s} (+{a['currency']} {d:,.2f})" for s, d in a["top"]) or "—"
            alert = raise_alert(
                db,
                tenant_id=t.id,
                type="cost_anomaly",
                category="cost",
                severity="critical" if pct >= 100 else "warning",
                title=f"Custo fora do padrão em {a['day']:%d/%m}: +{pct:.0f}%",
                message=(
                    f"{t.name}: {a['currency']} {a['value']:,.2f} no dia, contra média de "
                    f"{a['currency']} {a['mean']:,.2f} nos 14 dias anteriores. Maiores aumentos: {top}."
                ),
                dedupe_key=f"anomaly:{t.id}:{a['currency']}:{a['day']}",
                data={
                    "day": a["day"].isoformat(),
                    "value": str(a["value"]),
                    "baseline": str(a["mean"]),
                    "top_services": [[s, str(d)] for s, d in a["top"]],
                },
            )
            raised += alert is not None
    return {"alerts": raised}
