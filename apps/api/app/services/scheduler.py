"""Decide o que coletar e quando. Roda a cada minuto dentro do worker."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CloudAccount, Connection, Job
from app.services.jobs import enqueue, now

INTERVALS = {
    "discover": timedelta(hours=6),
    "inventory": timedelta(minutes=15),
    "health": timedelta(minutes=5),
    "aws_health": timedelta(minutes=15),
    "costs": timedelta(hours=12),
    "invoices": timedelta(hours=24),
    "optimization": timedelta(hours=24),
    "evaluate": timedelta(minutes=30),
}
CONNECTION_KINDS = ("discover", "costs", "invoices", "aws_health", "optimization")
ACCOUNT_KINDS = ("inventory", "health")


def _last_runs(db: Session) -> dict[str, datetime]:
    rows = db.execute(select(Job.dedupe_key, func.max(Job.created_at)).group_by(Job.dedupe_key)).all()
    out = {}
    for k, ts in rows:
        if k and ts is not None:
            out[k] = ts if ts.tzinfo else ts.replace(tzinfo=now().tzinfo)
    return out


def _due(last: dict[str, datetime], key: str, kind: str, at: datetime) -> bool:
    ts = last.get(key)
    return ts is None or ts <= at - INTERVALS[kind]


def tick(db: Session, at: datetime | None = None) -> list[str]:
    at = at or now()
    last = _last_runs(db)
    created = []
    if _due(last, "evaluate::", "evaluate", at) and enqueue(db, "evaluate"):
        created.append("evaluate")
    conns = db.scalars(
        select(Connection).where(Connection.provider == "aws", Connection.role_arn.is_not(None))
    ).all()
    for conn in conns:
        if conn.status == "error" and _due(last, f"discover:{conn.id}:", "discover", at) is False:
            continue  # conexão quebrada: só tenta de novo no próximo ciclo de descoberta
        for kind in CONNECTION_KINDS:
            if kind == "costs" and conn.cost_source == "manual":
                continue
            if kind == "invoices" and not conn.collect_invoices:
                continue
            if _due(last, f"{kind}:{conn.id}:", kind, at) and enqueue(db, kind, connection_id=conn.id):
                created.append(f"{kind}:{conn.name}")
        if conn.status != "ok":
            continue  # inventário só depois da descoberta funcionar
        accounts = db.scalars(
            select(CloudAccount).where(
                CloudAccount.connection_id == conn.id,
                CloudAccount.collect.is_(True),
                (CloudAccount.status.is_(None)) | (CloudAccount.status == "ACTIVE"),
            )
        ).all()
        for acc in accounts:
            for kind in ACCOUNT_KINDS:
                key = f"{kind}:{conn.id}:{acc.id}"
                if _due(last, key, kind, at) and enqueue(
                    db, kind, connection_id=conn.id, cloud_account_id=acc.id
                ):
                    created.append(f"{kind}:{acc.native_id}")
    return created


def sync_now(db: Session, conn: Connection) -> list[str]:
    """Botão "sincronizar agora": enfileira tudo da conexão, respeitando o que já está na fila."""
    created = []
    for kind in CONNECTION_KINDS:
        if (kind == "costs" and conn.cost_source == "manual") or (
            kind == "invoices" and not conn.collect_invoices
        ):
            continue
        if enqueue(db, kind, connection_id=conn.id):
            created.append(kind)
    for acc in db.scalars(
        select(CloudAccount).where(CloudAccount.connection_id == conn.id, CloudAccount.collect.is_(True))
    ):
        for kind in ACCOUNT_KINDS:
            if enqueue(db, kind, connection_id=conn.id, cloud_account_id=acc.id):
                created.append(f"{kind}:{acc.native_id}")
    return created
