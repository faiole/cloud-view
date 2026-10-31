"""Recomendações próprias a partir do inventário, com economia estimada por preço de tabela
(us-east-1, sob demanda). Servem para todas as contas, mesmo sem Cost Optimization Hub."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.common import now
from app.models import CloudAccount, Recommendation, Resource

SOURCE = "rules"
# USD por GB-mês
EBS_PRICE = {
    "gp2": Decimal("0.10"),
    "gp3": Decimal("0.08"),
    "io1": Decimal("0.125"),
    "io2": Decimal("0.125"),
    "st1": Decimal("0.045"),
    "sc1": Decimal("0.015"),
    "standard": Decimal("0.05"),
}
EIP_MONTH = Decimal("3.65")  # US$ 0,005/h

RULES = {
    "volume_unattached": ("Delete", "Remover volume sem uso"),
    "gp2_to_gp3": ("Upgrade", "Migrar volume gp2 para gp3"),
    "eip_unassociated": ("Delete", "Liberar IP elástico sem uso"),
    "previous_generation": ("Upgrade", "Trocar para família de instância atual"),
}


def _estimate(res: Resource, code: str) -> tuple[Decimal, str | None, str | None]:
    a = res.attributes or {}
    if code == "volume_unattached":
        size = Decimal(a.get("size_gib") or 0)
        return (
            size * EBS_PRICE.get(a.get("volume_type"), Decimal("0.08")),
            f"{size} GiB {a.get('volume_type')}",
            "remover (faça snapshot antes)",
        )
    if code == "gp2_to_gp3":
        size = Decimal(a.get("size_gib") or 0)
        return size * (EBS_PRICE["gp2"] - EBS_PRICE["gp3"]), f"{size} GiB gp2", f"{size} GiB gp3"
    if code == "eip_unassociated":
        return EIP_MONTH, "IP elástico sem associação", "liberar o IP"
    if code == "previous_generation":
        return Decimal(0), res.instance_type, "família atual (ex.: t3/m6i/m7g)"
    return Decimal(0), None, None


def sync_rule_recommendations(db: Session, acc: CloudAccount) -> dict:
    resources = db.scalars(select(Resource).where(Resource.cloud_account_id == acc.id)).all()
    seen = set()
    for res in resources:
        for f in res.findings or []:
            if f["code"] not in RULES:
                continue
            nid = f"{acc.id}:{res.region}:{res.native_id}:{f['code']}"
            seen.add(nid)
            rec = db.scalar(
                select(Recommendation).where(Recommendation.source == SOURCE, Recommendation.native_id == nid)
            )
            if rec is None:
                rec = Recommendation(source=SOURCE, native_id=nid)
                db.add(rec)
            action, label = RULES[f["code"]]
            savings, current, recommended = _estimate(res, f["code"])
            rec.cloud_account_id = acc.id
            rec.tenant_id = res.tenant_id
            rec.action_type = action
            rec.resource_type = res.type
            rec.resource_native_id = res.native_id
            rec.region = res.region
            rec.title = f"{label}: {res.name or res.native_id}"[:300]
            rec.current, rec.recommended = current, recommended
            rec.monthly_savings = savings.quantize(Decimal("0.01"))
            rec.currency = "USD"
            rec.effort = "VeryLow" if f["code"] in ("eip_unassociated", "gp2_to_gp3") else "Low"
            rec.restart_needed = f["code"] == "previous_generation"
            rec.estimated = True
            rec.tags = res.tags
            rec.last_seen = now()
            if rec.status == "resolved":
                rec.status = "open"
    resolved = 0
    for old in db.scalars(
        select(Recommendation).where(
            Recommendation.source == SOURCE,
            Recommendation.cloud_account_id == acc.id,
            Recommendation.status.in_(("open", "done")),
        )
    ):
        if old.native_id not in seen:
            old.status, old.status_by = "resolved", "automático"
            resolved += 1
    db.flush()
    return {"rule_recommendations": len(seen), "resolved": resolved}
