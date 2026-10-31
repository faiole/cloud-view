"""Fila de jobs no Postgres.

`enqueue` ignora pedidos repetidos enquanto o mesmo trabalho (dedupe_key) estiver na fila ou
rodando. `claim` pega o próximo job com FOR UPDATE SKIP LOCKED, então vários workers podem rodar
em paralelo sem pegar o mesmo trabalho.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job

MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=30)


def now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    db: Session,
    kind: str,
    *,
    connection_id: str | None = None,
    cloud_account_id: str | None = None,
    payload: dict | None = None,
    dedupe: bool = True,
    run_after: datetime | None = None,
) -> Job | None:
    key = f"{kind}:{connection_id or ''}:{cloud_account_id or ''}" if dedupe else None
    if key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == key, Job.status.in_(("queued", "running"))))
        if existing:
            return None
    job = Job(
        kind=kind,
        connection_id=connection_id,
        cloud_account_id=cloud_account_id,
        payload=payload or {},
        dedupe_key=key,
        run_after=run_after or now(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return None  # outro processo enfileirou ao mesmo tempo
    return job


def claim(db: Session) -> Job | None:
    q = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= now())
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    job = db.scalar(q)
    if job is None:
        return None
    job.status = "running"
    job.started_at = now()
    job.attempts += 1
    db.flush()
    return job


def finish(db: Session, job: Job, result: dict | None = None) -> None:
    job.status = "done"
    job.finished_at = now()
    job.result = result or {}
    job.error = None
    db.flush()


def fail(db: Session, job: Job, error: str) -> None:
    job.error = error[:4000]
    job.finished_at = now()
    if job.attempts < MAX_ATTEMPTS:
        job.status = "queued"
        job.run_after = now() + timedelta(minutes=2**job.attempts)
    else:
        job.status = "error"
    db.flush()


def requeue_stale(db: Session) -> int:
    """Jobs 'running' de um worker que morreu voltam para a fila."""
    return db.execute(
        update(Job)
        .where(Job.status == "running", Job.started_at < now() - STALE_AFTER)
        .values(status="queued", run_after=now())
        .execution_options(synchronize_session=False)
    ).rowcount
