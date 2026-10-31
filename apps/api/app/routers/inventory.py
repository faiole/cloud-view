"""Recursos, saúde, rede e ações rápidas de uma empresa."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import ActionRequest, CloudAccount, HealthEvent, Resource
from app.permissions import Perm
from app.resource_types import ACTION_TAGS, INSTANCE_TYPES, NETWORK_TYPES, RUNNING, STOPPED
from app.schemas import (
    ActionIn,
    ActionOut,
    HealthEventOut,
    HealthSummary,
    NetworkSummary,
    NetworkVpc,
    ResourceOut,
)
from app.security.deps import require_recent_mfa, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services.jobs import enqueue

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["inventory"])

ALLOWED_FROM = {"reboot": RUNNING, "stop": RUNNING, "start": STOPPED}


def _out(r: Resource) -> ResourceOut:
    o = ResourceOut.model_validate(r)
    o.account_native_id = r.account.native_id
    o.account_name = r.account.name
    return o


def _base(tenant_id: str):
    return select(Resource).options(joinedload(Resource.account)).where(Resource.tenant_id == tenant_id)


@router.get("/resources", response_model=list[ResourceOut])
def list_resources(
    tenant_id: str,
    type: str | None = Query(default=None, alias="type"),
    q: str | None = None,
    state: str | None = None,
    account: str | None = None,
    with_findings: bool = False,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    stmt = _base(tenant_id).order_by(Resource.type, Resource.name, Resource.native_id).limit(2000)
    if type:
        types = [t for t in type.split(",") if t]  # ex.: "ec2_instance,vm" = instâncias de todas as nuvens
        stmt = stmt.where(Resource.type.in_(types))
    if state:
        stmt = stmt.where(Resource.state == state)
    if account:
        stmt = stmt.where(Resource.account.has(CloudAccount.native_id == account))
    rows = db.scalars(stmt).all()
    if q:
        ql = q.lower()
        rows = [
            r
            for r in rows
            if ql in (r.name or "").lower()
            or ql in r.native_id.lower()
            or any(ql in ip for ip in r.private_ips + r.public_ips)
            or ql in (r.instance_type or "")
        ]
    if with_findings:
        rows = [r for r in rows if r.findings]
    return [_out(r) for r in rows]


def _resource(db: Session, tenant_id: str, resource_id: str) -> Resource:
    r = db.scalar(_base(tenant_id).where(Resource.id == resource_id))
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "resource_not_found")
    return r


@router.get("/resources/{resource_id}", response_model=ResourceOut)
def get_resource(
    tenant_id: str,
    resource_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    return _out(_resource(db, tenant_id, resource_id))


@router.get("/health", response_model=HealthSummary)
def health(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    instances = db.scalars(_base(tenant_id).where(Resource.type.in_(INSTANCE_TYPES))).all()
    events = db.scalars(
        select(HealthEvent)
        .where(HealthEvent.tenant_id == tenant_id, HealthEvent.status.in_(("open", "upcoming")))
        .order_by(HealthEvent.start_at.desc().nulls_last())
        .limit(200)
    ).all()
    return HealthSummary(
        instances=len(instances),
        running=sum(1 for i in instances if i.state == "running"),
        impaired=[_out(i) for i in instances if i.health == "impaired"],
        events=[HealthEventOut.model_validate(e) for e in events],
        last_collected_at=max((i.collected_at for i in instances), default=None),
    )


@router.get("/network", response_model=NetworkSummary)
def network(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    rows = db.scalars(_base(tenant_id).where(Resource.type.in_((*NETWORK_TYPES, *INSTANCE_TYPES)))).all()
    vpcs = {}
    for r in rows:
        if r.type == "vpc":
            vpcs[(r.cloud_account_id, r.region, r.native_id)] = NetworkVpc(
                vpc=_out(r), subnets=[], instances=0, exposed_instances=[]
            )
    findings: dict[str, int] = {}
    open_sgs = []
    for r in rows:
        for f in r.findings or []:
            findings[f["code"]] = findings.get(f["code"], 0) + 1
        key = (r.cloud_account_id, r.region, r.vpc_id)
        if r.type == "subnet" and key in vpcs:
            vpcs[key].subnets.append(_out(r))
        elif r.type in INSTANCE_TYPES and key in vpcs:
            vpcs[key].instances += 1
            if r.public_ips and any(f["code"].startswith("sg_") for f in r.findings or []):
                vpcs[key].exposed_instances.append(_out(r))
        elif r.type == "security_group" and r.findings:
            open_sgs.append(_out(r))
    return NetworkSummary(
        vpcs=sorted(vpcs.values(), key=lambda v: (v.vpc.account_native_id, v.vpc.region)),
        open_security_groups=open_sgs,
        findings_by_code=findings,
    )


@router.get("/actions", response_model=list[ActionOut])
def list_actions(
    tenant_id: str,
    _: SessionData = Depends(tenant_access(Perm.resources_read)),
    db: Session = Depends(scoped_db),
):
    return db.scalars(
        select(ActionRequest)
        .where(ActionRequest.tenant_id == tenant_id)
        .order_by(ActionRequest.requested_at.desc())
        .limit(100)
    ).all()


@router.post("/resources/{resource_id}/actions", response_model=ActionOut, status_code=202)
def request_action(
    tenant_id: str,
    resource_id: str,
    body: ActionIn,
    request: Request,
    _: SessionData = Depends(tenant_access(Perm.actions_execute)),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Reiniciar/ligar/desligar uma instância EC2 ou VM do Azure. Exige MFA recente, confirmação pelo
    nome e a tag `cloudview-actions=enabled` no recurso. Na AWS a role CloudViewOperator também só age
    com a tag; no Azure, limite a role 'Cloud View Operator' aos grupos de recursos desejados."""
    r = _resource(db, tenant_id, resource_id)
    if r.type not in INSTANCE_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "ações disponíveis só para instâncias (EC2, VMs do Azure, Compute da OCI)",
        )
    if body.confirm.strip() not in {r.native_id, r.name or r.native_id}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "confirmação não confere com o nome do recurso"
        )
    if not any(r.tags.get(t) == "enabled" for t in ACTION_TAGS):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "ações rápidas não habilitadas para este recurso (adicione a tag cloudview-actions=enabled)",
        )
    if r.state not in ALLOWED_FROM[body.action]:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"não é possível '{body.action}' com o estado atual ({r.state})"
        )
    pending = db.scalar(
        select(func.count()).where(
            ActionRequest.resource_id == r.id, ActionRequest.status.in_(("queued", "running"))
        )
    )
    if pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "já existe uma ação em andamento para este recurso")

    req = ActionRequest(
        tenant_id=tenant_id,
        resource_id=r.id,
        resource_native_id=r.native_id,
        action=body.action,
        reason=body.reason,
        requested_by=user.email,
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
        request,
        f"action.{body.action}",
        user=user,
        tenant_id=tenant_id,
        target=f"{r.account.native_id}/{r.region}/{r.native_id}",
        details={"reason": body.reason, "request_id": req.id},
    )
    return req
