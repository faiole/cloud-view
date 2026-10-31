"""Fase 5: topologia de rede, agenda liga/desliga e relatório mensal em PDF."""

import re
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import PowerSchedule, Resource, Tenant
from app.permissions import Perm
from app.resource_types import INSTANCE_TYPES
from app.schemas import (
    ScheduleIn,
    ScheduleOut,
    ScheduleTarget,
    TopoInstance,
    Topology,
    TopoNetwork,
    TopoSubnet,
)
from app.security.deps import require_recent_mfa, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services.monthly_report import build_monthly_pdf
from app.services.power_schedules import targets, valid_timezone, weekly_hours_off

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["evolution"])


# --- Topologia ------------------------------------------------------------------------------------


def _inst(r: Resource) -> TopoInstance:
    exposure = [f["message"] for f in r.findings or [] if f["code"].startswith("sg_")]
    return TopoInstance(
        id=r.id,
        name=r.name,
        provider=r.provider,
        state=r.state,
        instance_type=r.instance_type,
        private_ips=r.private_ips or [],
        public_ips=r.public_ips or [],
        exposed=bool(r.public_ips and exposure),
        exposure=exposure,
        security_groups=[g.get("name") or g.get("id") for g in r.security_groups or []],
    )


@router.get("/topology", response_model=Topology)
def topology(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    rows = db.scalars(
        select(Resource)
        .options(joinedload(Resource.account))
        .where(Resource.tenant_id == tenant_id, Resource.type.in_(("vpc", "subnet", *INSTANCE_TYPES)))
    ).all()
    nets: dict[tuple, TopoNetwork] = {}
    subnets: dict[tuple, TopoSubnet] = {}
    for r in rows:
        if r.type == "vpc":
            nets[(r.cloud_account_id, r.native_id)] = TopoNetwork(
                id=r.id,
                name=r.name or r.native_id.rsplit("/", 1)[-1],
                provider=r.provider,
                region=r.region,
                account=r.account.name or r.account.native_id,
                cidr=(r.attributes or {}).get("cidr"),
                subnets=[],
                loose_instances=[],
            )
    for r in rows:
        if r.type == "subnet":
            sn = TopoSubnet(
                id=r.id,
                name=r.name or r.native_id.rsplit("/", 1)[-1],
                cidr=(r.attributes or {}).get("cidr"),
                public=bool((r.attributes or {}).get("public_ip_on_launch")),
                instances=[],
            )
            subnets[(r.cloud_account_id, r.native_id)] = sn
            net = nets.get((r.cloud_account_id, r.vpc_id))
            if net:
                net.subnets.append(sn)
    unplaced = []
    for r in rows:
        if r.type not in INSTANCE_TYPES or r.state in ("terminated",):
            continue
        inst = _inst(r)
        sn = subnets.get((r.cloud_account_id, r.subnet_id))
        if sn:
            sn.instances.append(inst)
            sn.public = sn.public or bool(inst.public_ips)
        elif (net := nets.get((r.cloud_account_id, r.vpc_id))) is not None:
            net.loose_instances.append(inst)
        else:
            unplaced.append(inst)
    ordered = sorted(
        nets.values(),
        key=lambda n: (-sum(len(s.instances) for s in n.subnets), n.provider, n.region, n.name or ""),
    )
    for n in ordered:
        n.subnets.sort(key=lambda s: (not s.public, s.name or ""))
    exposed = sum(1 for r in rows if r.type in INSTANCE_TYPES and _inst(r).exposed)
    return Topology(networks=ordered, unplaced=unplaced, exposed=exposed)


# --- Agenda liga/desliga ------------------------------------------------------------------------------


def _schedule_out(db: Session, s: PowerSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        name=s.name,
        timezone=s.timezone,
        days=s.days,
        start_time=s.start_time,
        stop_time=s.stop_time,
        selector_tag=s.selector_tag,
        resource_ids=s.resource_ids or [],
        enabled=s.enabled,
        created_by=s.created_by,
        last_start_on=s.last_start_on,
        last_stop_on=s.last_stop_on,
        weekly_hours_off=weekly_hours_off(s),
        targets=[
            ScheduleTarget(id=r.id, name=r.name, provider=r.provider, state=r.state) for r in targets(db, s)
        ],
    )


def _validate(body: ScheduleIn) -> None:
    if not body.start_time and not body.stop_time:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "informe o horário de ligar, de desligar ou ambos"
        )
    if body.start_time and body.stop_time and body.start_time >= body.stop_time:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "o horário de ligar deve ser antes do de desligar"
        )
    if not body.selector_tag and not body.resource_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "escolha as instâncias por tag ou pela lista"
        )
    if not valid_timezone(body.timezone):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "fuso horário inválido (ex.: America/Sao_Paulo)"
        )


def _get(db: Session, tenant_id: str, sid: str) -> PowerSchedule:
    s = db.get(PowerSchedule, sid)
    if s is None or s.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule_not_found")
    return s


def _apply(db: Session, tenant_id: str, s: PowerSchedule, body: ScheduleIn) -> None:
    own = set(
        db.scalars(
            select(Resource.id).where(
                Resource.tenant_id == tenant_id, Resource.id.in_(body.resource_ids or ["-"])
            )
        )
    )
    if set(body.resource_ids) - own:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "há instâncias que não são desta empresa")
    for k, v in body.model_dump().items():
        setattr(s, k, v)


@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    return [
        _schedule_out(db, s)
        for s in db.scalars(
            select(PowerSchedule).where(PowerSchedule.tenant_id == tenant_id).order_by(PowerSchedule.name)
        )
    ]


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(
    tenant_id: str,
    body: ScheduleIn,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.actions_execute)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    _validate(body)
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    s = PowerSchedule(tenant_id=tenant_id, created_by=user.email)
    _apply(db, tenant_id, s, body)
    db.add(s)
    db.flush()
    audit.record(
        db,
        request,
        "schedule.create",
        user=user,
        tenant_id=tenant_id,
        target=s.name,
        details=body.model_dump(),
    )
    return _schedule_out(db, s)


@router.put("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    tenant_id: str,
    schedule_id: str,
    body: ScheduleIn,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.actions_execute)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    _validate(body)
    s = _get(db, tenant_id, schedule_id)
    _apply(db, tenant_id, s, body)
    audit.record(
        db,
        request,
        "schedule.update",
        user=user,
        tenant_id=tenant_id,
        target=s.name,
        details=body.model_dump(),
    )
    return _schedule_out(db, s)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    tenant_id: str,
    schedule_id: str,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.actions_execute)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    s = _get(db, tenant_id, schedule_id)
    audit.record(db, request, "schedule.delete", user=user, tenant_id=tenant_id, target=s.name)
    db.delete(s)


# --- Relatório mensal ---------------------------------------------------------------------------------


@router.get("/reports/monthly.pdf")
def monthly_pdf(
    tenant_id: str,
    request: Request,
    month: str | None = Query(
        default=None, pattern=r"^\d{4}-\d{2}$", description="AAAA-MM; padrão: mês anterior"
    ),
    user: SessionData = Depends(tenant_access(Perm.costs_read)),
    db: Session = Depends(scoped_db),
):
    t = db.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    if month:
        y, m = map(int, month.split("-"))
        if not 1 <= m <= 12:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mês inválido")
        ref = date(y, m, 1)
    else:
        ref = (datetime.now(UTC).date().replace(day=1) - timedelta(days=1)).replace(day=1)
    pdf, _ = build_monthly_pdf(db, t, ref)
    audit.record(
        db, request, "report.monthly_pdf", user=user, tenant_id=tenant_id, details={"month": f"{ref:%Y-%m}"}
    )
    slug = re.sub(r"[^a-z0-9-]", "", t.slug)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="relatorio-{slug}-{ref:%Y-%m}.pdf"'},
    )
