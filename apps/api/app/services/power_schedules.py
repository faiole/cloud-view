"""Agenda liga/desliga: no horário local da agenda, pede ações de ligar/desligar para as instâncias-alvo.

- Só age em instâncias com ações rápidas habilitadas (mesma tag exigida nas ações manuais).
- Cada horário dispara uma vez por dia (janela de 15 minutos, para tolerar atraso do worker).
- Usa o mesmo caminho das ações manuais: fila `action`, worker `worker-actions`, auditoria.
"""

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActionRequest, PowerSchedule, Resource
from app.resource_types import ACTION_TAGS, INSTANCE_TYPES, RUNNING, STOPPED
from app.services import audit
from app.services.jobs import enqueue

WINDOW = timedelta(minutes=15)


def parse_time(v: str | None) -> dtime | None:
    if not v:
        return None
    h, m = v.split(":")
    return dtime(int(h), int(m))


def valid_timezone(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def targets(db: Session, s: PowerSchedule) -> list[Resource]:
    rows = db.scalars(
        select(Resource).where(Resource.tenant_id == s.tenant_id, Resource.type.in_(INSTANCE_TYPES))
    ).all()
    key, _, value = (s.selector_tag or "").partition("=")
    out = []
    for r in rows:
        if not any((r.tags or {}).get(t) == "enabled" for t in ACTION_TAGS):
            continue
        if r.id in (s.resource_ids or []) or (key and (r.tags or {}).get(key) == value):
            out.append(r)
    return out


def due_actions(s: PowerSchedule, now: datetime) -> list[tuple[str, date]]:
    """Ações a disparar agora: ("start"|"stop", data local)."""
    local = now.astimezone(ZoneInfo(s.timezone))
    if local.weekday() not in (s.days or []):
        return []
    out = []
    for action, t, last in (("start", s.start_time, s.last_start_on), ("stop", s.stop_time, s.last_stop_on)):
        at = parse_time(t)
        if at is None or last == local.date():
            continue
        moment = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if moment <= local < moment + WINDOW:
            out.append((action, local.date()))
    return out


def weekly_hours_off(s: PowerSchedule) -> float | None:
    """Horas por semana em que as instâncias ficam desligadas (para estimar a economia de computação)."""
    start, stop = parse_time(s.start_time), parse_time(s.stop_time)
    if start is None or stop is None:
        return None
    on = (datetime.combine(date.min, stop) - datetime.combine(date.min, start)).seconds / 3600
    return round(168 - on * len(s.days or []), 1)


def run_schedules(db: Session, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    created = []
    for s in db.scalars(select(PowerSchedule).where(PowerSchedule.enabled.is_(True))):
        for action, local_day in due_actions(s, now):
            wanted = STOPPED if action == "start" else RUNNING
            for r in targets(db, s):
                if r.state not in wanted:
                    continue
                busy = db.scalar(
                    select(func.count()).where(
                        ActionRequest.resource_id == r.id, ActionRequest.status.in_(("queued", "running"))
                    )
                )
                if busy:
                    continue
                req = ActionRequest(
                    tenant_id=s.tenant_id,
                    resource_id=r.id,
                    resource_native_id=r.native_id,
                    action=action,
                    reason=f"Agenda '{s.name}'",
                    requested_by=f"agenda: {s.name}"[:320],
                )
                db.add(req)
                db.flush()
                enqueue(
                    db,
                    "action",
                    connection_id=r.account.connection_id,
                    payload={"action_request_id": req.id},
                    dedupe=False,
                )
                audit.record(
                    db,
                    None,
                    f"schedule.{action}",
                    tenant_id=s.tenant_id,
                    target=r.native_id,
                    details={"schedule": s.name},
                )
                created.append(f"{action}:{r.name or r.native_id}")
            if action == "start":
                s.last_start_on = local_day
            else:
                s.last_stop_on = local_day
    db.flush()
    return created
