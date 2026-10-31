"""Avaliação periódica (a cada 30 min e depois de cada coleta de custos): transforma o estado
coletado em alertas e resolve os que deixaram de valer."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connection, HealthEvent, Resource
from app.services.alerts import raise_alert, resolve_missing
from app.services.anomalies import detect_anomalies
from app.services.budgets import evaluate_budgets


def health_alerts(db: Session) -> int:
    keep, n = set(), 0
    for ev in db.scalars(
        select(HealthEvent).where(
            HealthEvent.status.in_(("open", "upcoming")), HealthEvent.tenant_id.is_not(None)
        )
    ):
        key = f"health:{ev.id}"
        keep.add(key)
        if ev.source == "ec2_status":
            sev = "critical"
        elif ev.category == "issue":
            sev = "warning"
        else:
            sev = "info"
        when = f" Início: {ev.start_at:%d/%m/%Y %H:%M} UTC." if ev.start_at else ""
        n += (
            raise_alert(
                db,
                tenant_id=ev.tenant_id,
                type=f"health_{ev.source}",
                category="health",
                severity=sev,
                title=ev.title,
                message=f"{ev.service or ''} {ev.region or ''}. {(ev.description or '')[:600]}{when}".strip(),
                dedupe_key=key,
                data={"event_id": ev.id, "resource_id": ev.resource_id},
            )
            is not None
        )
    resolve_missing(db, "health:", keep)
    return n


def security_alerts(db: Session) -> int:
    keep, n = set(), 0
    for r in db.scalars(
        select(Resource).where(Resource.tenant_id.is_not(None), Resource.type == "ec2_instance")
    ):
        codes = {f["code"] for f in r.findings or []}
        name = r.name or r.native_id
        exposed = [f for f in r.findings or [] if f["code"].startswith("sg_") and f["severity"] == "critical"]
        if exposed and r.public_ips:
            key = f"exposed:{r.id}"
            keep.add(key)
            n += (
                raise_alert(
                    db,
                    tenant_id=r.tenant_id,
                    type="exposed_instance",
                    category="security",
                    severity="critical",
                    title=f"{name} exposta à internet",
                    message=f"IP público {', '.join(r.public_ips)}. "
                    + "; ".join(f["message"] for f in exposed),
                    dedupe_key=key,
                    data={"resource_id": r.id, "region": r.region},
                )
                is not None
            )
        if "patches_missing_critical" in codes:
            key = f"patches:{r.id}"
            keep.add(key)
            p = (r.attributes or {}).get("patch", {})
            n += (
                raise_alert(
                    db,
                    tenant_id=r.tenant_id,
                    type="patches_missing",
                    category="security",
                    severity="warning",
                    title=f"Atualizações críticas pendentes em {name}",
                    message=f"{p.get('critical', 0)} críticas e {p.get('security', 0)} de segurança pendentes (SSM Patch Manager).",
                    dedupe_key=key,
                    data={"resource_id": r.id},
                )
                is not None
            )
    resolve_missing(db, "exposed:", {k for k in keep if k.startswith("exposed:")})
    resolve_missing(db, "patches:", {k for k in keep if k.startswith("patches:")})
    return n


def platform_alerts(db: Session) -> int:
    keep, n = set(), 0
    for c in db.scalars(select(Connection).where(Connection.status == "error")):
        key = f"conn_error:{c.id}"
        keep.add(key)
        n += (
            raise_alert(
                db,
                tenant_id=None,
                type="connection_error",
                category="platform",
                severity="critical",
                title=f"Coleta falhando na conexão {c.name}",
                message=c.last_error
                or "Erro ao acessar a conta. Verifique a stack CloudFormation e o ExternalId.",
                dedupe_key=key,
                data={"connection_id": c.id},
            )
            is not None
        )
    resolve_missing(db, "conn_error:", keep)
    return n


def evaluate_all(db: Session, today: date | None = None) -> dict:
    out = {
        "budgets": evaluate_budgets(db, today),
        "anomalies": detect_anomalies(db, today),
        "health": health_alerts(db),
        "security": security_alerts(db),
        "platform": platform_alerts(db),
    }
    db.flush()
    return out
