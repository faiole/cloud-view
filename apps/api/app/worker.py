"""Worker de coleta e de ações.

    python -m app.worker                 # coleta (tudo menos ações) + agendador
    python -m app.worker --only action   # só ações rápidas (serviço separado, responde rápido)

Vários workers podem rodar em paralelo: a fila usa FOR UPDATE SKIP LOCKED.
"""

import argparse
import logging
import signal
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.aws.session import AwsHub
from app.collectors import aws_actions, aws_billing, aws_inventory
from app.collectors.common import Ctx, error_text
from app.core.db import apply_tenant_scope, get_sessionmaker
from app.models import ActionRequest, CloudAccount, Connection, Job
from app.services import jobs, scheduler
from app.services.allocation import reallocate_resources
from app.services.storage import get_storage
from app.vault import get_vault

log = logging.getLogger("cloudview.worker")
HUB_REFRESH = 300


def handle(db: Session, ctx: Ctx, job: Job) -> dict:
    conn = db.get(Connection, job.connection_id) if job.connection_id else None
    acc = db.get(CloudAccount, job.cloud_account_id) if job.cloud_account_id else None
    if job.kind == "action":
        req = db.get(ActionRequest, job.payload["action_request_id"])
        return aws_actions.execute_action(db, ctx, req)
    if conn is None:
        raise ValueError("conexão removida")
    if job.kind == "discover":
        try:
            return aws_inventory.discover_accounts(db, ctx, conn)
        except Exception as e:
            conn.status = "error"
            conn.last_error = error_text(e)[:2000]
            raise
    if job.kind == "costs":
        return aws_billing.collect_costs(db, ctx, conn)
    if job.kind == "invoices":
        return aws_billing.collect_invoices(db, ctx, conn)
    if job.kind == "aws_health":
        return aws_inventory.collect_aws_health(db, ctx, conn)
    if acc is None:
        raise ValueError("conta removida")
    if job.kind == "inventory":
        out = aws_inventory.collect_inventory(db, ctx, conn, acc)
        reallocate_resources(db, acc.id)
        return out
    if job.kind == "health":
        return aws_inventory.collect_health(db, ctx, conn, acc)
    raise ValueError(f"tipo de job desconhecido: {job.kind}")


class Worker:
    def __init__(self, only: set[str] | None = None, exclude: set[str] | None = None, schedule: bool = True):
        self.only, self.exclude, self.schedule = only, exclude or set(), schedule
        self.SessionLocal = get_sessionmaker()
        self._ctx: tuple[float, Ctx] | None = None
        self._last_tick = 0.0
        self.stopping = False

    def session(self) -> Session:
        db = self.SessionLocal()
        apply_tenant_scope(db, tenant_ids=[], is_admin=True)  # o worker atende todas as empresas
        return db

    def ctx(self, db: Session) -> Ctx:
        if self._ctx is None or time.time() - self._ctx[0] > HUB_REFRESH:
            self._ctx = (time.time(), Ctx(hub=AwsHub.from_settings(db, get_vault()), storage=get_storage()))
        return self._ctx[1]

    def run_once(self) -> bool:
        """Processa no máximo um job. Retorna True se havia trabalho."""
        with self.session() as db:
            if self.schedule and time.time() - self._last_tick > 60:
                jobs.requeue_stale(db)
                created = scheduler.tick(db)
                if created:
                    log.info("agendados: %s", ", ".join(created))
                self._last_tick = time.time()
            db.commit()
        with self.session() as db:
            job = self._claim_matching(db)
            db.commit()
        if job is None:
            return False
        job_id = job.id
        with self.session() as db:
            job = db.get(Job, job_id)
            try:
                result = handle(db, self.ctx(db), job)
                jobs.finish(db, job, _jsonable(result))
                db.commit()
                log.info("ok %s %s", job.kind, job_id)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                with self.session() as db2:
                    j = db2.get(Job, job_id)
                    if j.kind == "action":
                        j.attempts = jobs.MAX_ATTEMPTS  # ação nunca é repetida automaticamente
                        req = db2.get(ActionRequest, j.payload.get("action_request_id"))
                        if req:
                            req.status, req.result = "failed", {"error": error_text(e)}
                    if j.kind == "discover" and j.connection_id:
                        c = db2.get(Connection, j.connection_id)
                        if c:
                            c.status, c.last_error = "error", error_text(e)[:2000]
                    jobs.fail(db2, j, error_text(e))
                    db2.commit()
                log.warning("erro %s %s: %s", job.kind, job_id, error_text(e))
        return True

    def _claim_matching(self, db: Session) -> Job | None:
        # Ações têm prioridade e, num worker filtrado, só pegamos o que é nosso
        q = select(Job).where(Job.status == "queued", Job.run_after <= jobs.now())
        if self.only:
            q = q.where(Job.kind.in_(self.only))
        if self.exclude:
            q = q.where(Job.kind.not_in(self.exclude))
        q = q.order_by((Job.kind != "action"), Job.run_after, Job.created_at).limit(1)
        if db.get_bind().dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        job = db.scalar(q)
        if job is None:
            return None
        job.status, job.started_at, job.attempts = "running", jobs.now(), job.attempts + 1
        db.flush()
        return job

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        log.info("worker iniciado (only=%s exclude=%s)", self.only, self.exclude)
        while not self.stopping:
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 - banco fora do ar etc.: espera e tenta de novo
                log.exception("falha no loop do worker")
                busy = False
            if not busy:
                time.sleep(2)


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple | set):
        return [_jsonable(x) for x in v]
    if isinstance(v, str | int | float | bool) or v is None:
        return v
    return str(v)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", action="append", help="tipos de job atendidos (ex.: action)")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--no-schedule", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    Worker(set(a.only) if a.only else None, set(a.exclude), schedule=not a.no_schedule).run_forever()


if __name__ == "__main__":
    main()
