"""Conexões AWS (conta avulsa ou Organization), credencial hub e fila de coleta. Só administrador."""

import json
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.aws.session import HUB_SECRET_KEY, role_arn_for
from app.azure.client import AZURE_HUB_KEY
from app.models import CloudAccount, CloudSecret, Connection, Job, PlatformSetting
from app.schemas import (
    AzureConnectionCreate,
    AzureHubIn,
    AzureHubStatus,
    ConnectionCreate,
    ConnectionDetail,
    ConnectionUpdate,
    HubCredentialsIn,
    HubPrincipalIn,
    HubStatus,
    JobOut,
)
from app.security.deps import require_admin, require_recent_mfa, scoped_db
from app.security.session import SessionData
from app.services import audit
from app.services.jobs import enqueue
from app.services.scheduler import sync_now
from app.services.secrets import store_secret
from app.vault import Vault, VaultError, get_vault

router = APIRouter(prefix="/admin", tags=["admin-connections"])
HUB_PRINCIPAL = "aws_hub_principal_arn"
HUB_HINT = "aws_hub_key_hint"
AZURE_HUB_META = "azure_hub_meta"


def _detail(db: Session, c: Connection) -> ConnectionDetail:
    out = ConnectionDetail.model_validate(c)
    out.accounts = db.scalar(select(func.count()).where(CloudAccount.connection_id == c.id)) or 0
    return out


def _get(db: Session, connection_id: str) -> Connection:
    c = db.get(Connection, connection_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection_not_found")
    return c


@router.get("/connections", response_model=list[ConnectionDetail])
def list_connections(_: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    return [
        _detail(db, c) for c in db.scalars(select(Connection).order_by(Connection.provider, Connection.name))
    ]


@router.post("/connections", response_model=ConnectionDetail, status_code=201)
def create_connection(
    body: ConnectionCreate,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Cria (ou habilita a coleta numa conexão que veio de importação) e gera o ExternalId.

    Fluxo: criar aqui → implantar o CloudFormation com o ExternalId → "Testar conexão".
    """
    if body.cost_source == "focus_s3" and not body.focus_s3_uri:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "informe focus_s3_uri para a fonte focus_s3"
        )
    c = db.scalar(
        select(Connection).where(Connection.provider == "aws", Connection.native_id == body.account_id)
    )
    if c is None:
        c = Connection(provider="aws", native_id=body.account_id, kind=body.kind, name=body.name)
        db.add(c)
    elif c.role_arn:
        raise HTTPException(status.HTTP_409_CONFLICT, "já existe uma conexão com coleta para esta conta")
    c.kind, c.name = body.kind, body.name
    c.role_arn = role_arn_for(body.account_id, body.role_name)
    c.member_role_name = body.role_name
    c.external_id = c.external_id or secrets.token_hex(16)
    c.cost_source, c.focus_s3_uri = body.cost_source, body.focus_s3_uri
    c.regions = body.regions or None
    c.collect_invoices = body.collect_invoices
    c.status, c.last_error = "pending", None
    db.flush()
    audit.record(
        db, request, "connection.create", user=user, target=f"aws:{c.native_id}", details={"kind": c.kind}
    )
    return _detail(db, c)


def _azure_cred_json(c) -> str:
    return json.dumps({k: v for k, v in c.model_dump().items() if v is not None and k != "home_tenant_id"})


@router.post("/connections/azure", response_model=ConnectionDetail, status_code=201)
def create_azure_connection(
    body: AzureConnectionCreate,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    vault: Vault = Depends(get_vault),
):
    """Tenant Azure de um cliente. Fluxo: criar → cliente abre o link de admin consent do app hub →
    atribui as roles (script em onboarding/azure) → "Testar conexão"."""
    if body.cost_source == "focus_blob" and not body.focus_uri:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "informe o local do export FOCUS no Blob Storage"
        )
    if body.own_app and not (body.own_app.client_secret or body.own_app.certificate_pem):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "app próprio precisa de client secret ou certificado"
        )
    tid = body.tenant_id.lower()
    c = db.scalar(select(Connection).where(Connection.provider == "azure", Connection.native_id == tid))
    if c is not None and c.status != "manual":
        raise HTTPException(status.HTTP_409_CONFLICT, "já existe uma conexão com coleta para este tenant")
    if c is None:
        c = Connection(provider="azure", native_id=tid, kind="azure_tenant", name=body.name)
        db.add(c)
    c.kind, c.name = "azure_tenant", body.name
    c.cost_source, c.focus_s3_uri = body.cost_source, body.focus_uri
    c.billing_account_id, c.collect_invoices = body.billing_account_id, body.collect_invoices
    c.status, c.last_error = "pending", None
    if body.own_app:
        try:
            c.secret_ref = store_secret(
                db, vault, "azure_app", _azure_cred_json(body.own_app).encode(), user.email
            )
        except VaultError as e:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "cofre sem chave pública configurada"
            ) from e
        c.client_id = body.own_app.client_id
    db.flush()
    audit.record(
        db,
        request,
        "connection.create",
        user=user,
        target=f"azure:{tid}",
        details={"own_app": bool(body.own_app)},
    )
    return _detail(db, c)


@router.get("/azure/hub", response_model=AzureHubStatus)
def azure_hub_status(_: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    sec = _setting(db, AZURE_HUB_KEY)
    meta = json.loads(_setting(db, AZURE_HUB_META).value) if _setting(db, AZURE_HUB_META) else {}
    return AzureHubStatus(
        configured=sec is not None,
        client_id=meta.get("client_id"),
        home_tenant_id=meta.get("home_tenant_id"),
        auth=meta.get("auth"),
        updated_at=sec.updated_at if sec else None,
    )


@router.put("/azure/hub", response_model=AzureHubStatus)
def set_azure_hub(
    body: AzureHubIn,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    vault: Vault = Depends(get_vault),
):
    """App multi-tenant da plataforma. Certificado (recomendado) ou client secret, cifrado no cofre."""
    if not (body.client_secret or body.certificate_pem):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "informe certificado+chave ou client secret"
        )
    try:
        sid = store_secret(db, vault, "azure_hub_app", _azure_cred_json(body).encode(), user.email)
    except VaultError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "cofre sem chave pública configurada") from e
    old = _setting(db, AZURE_HUB_KEY)
    if old is not None and (prev := db.get(CloudSecret, old.value)) is not None:
        db.delete(prev)
    _put_setting(db, AZURE_HUB_KEY, sid)
    meta = {
        "client_id": body.client_id,
        "home_tenant_id": body.home_tenant_id.lower(),
        "auth": "certificate" if body.certificate_pem else "secret",
    }
    _put_setting(db, AZURE_HUB_META, json.dumps(meta))
    db.flush()
    audit.record(
        db, request, "azure.hub.set", user=user, target=body.client_id, details={"auth": meta["auth"]}
    )
    return azure_hub_status(user, db)


@router.patch("/connections/{connection_id}", response_model=ConnectionDetail)
def update_connection(
    connection_id: str,
    body: ConnectionUpdate,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    c = _get(db, connection_id)
    changes = body.model_dump(exclude_unset=True)
    allowed = {
        "aws": {"cost_explorer", "focus_s3", "manual"},
        "azure": {"azure_cost_management", "focus_blob", "manual"},
    }
    if "cost_source" in changes and changes["cost_source"] not in allowed.get(c.provider, {"manual"}):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "fonte de custos não disponível para esta nuvem"
        )
    uri = changes.get("focus_s3_uri")
    if uri and not uri.startswith({"aws": "s3://", "azure": "https://"}.get(c.provider, "-")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "local do export não corresponde à nuvem da conexão"
        )
    for k, v in changes.items():
        setattr(c, k, v)
    if c.cost_source in ("focus_s3", "focus_blob") and not c.focus_s3_uri:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "informe o local do export FOCUS")
    audit.record(
        db, request, "connection.update", user=user, target=f"{c.provider}:{c.native_id}", details=changes
    )
    return _detail(db, c)


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(
    connection_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Para a coleta. Contas, custos e documentos já coletados continuam."""
    c = _get(db, connection_id)
    c.role_arn, c.status, c.cost_source = None, "manual", "manual"
    audit.record(db, request, "connection.disable", user=user, target=f"{c.provider}:{c.native_id}")


@router.post("/connections/{connection_id}/test", response_model=JobOut, status_code=202)
def test_connection(
    connection_id: str,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    """O teste roda no worker (só ele tem a credencial hub). Acompanhe pelo job devolvido."""
    c = _get(db, connection_id)
    if not c.collectable:
        raise HTTPException(status.HTTP_409_CONFLICT, "conexão sem coleta configurada")
    job = enqueue(db, "discover", connection_id=c.id) or db.scalar(
        select(Job).where(Job.dedupe_key == f"discover:{c.id}:", Job.status.in_(("queued", "running")))
    )
    audit.record(db, request, "connection.test", user=user, target=f"{c.provider}:{c.native_id}")
    return job


@router.post("/connections/{connection_id}/sync")
def sync_connection(
    connection_id: str,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
) -> dict:
    c = _get(db, connection_id)
    if not c.collectable:
        raise HTTPException(status.HTTP_409_CONFLICT, "conexão sem coleta configurada")
    created = sync_now(db, c)
    audit.record(db, request, "connection.sync", user=user, target=f"{c.provider}:{c.native_id}")
    return {"queued": created}


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    connection_id: str | None = None,
    status_: str | None = Query(None, alias="status"),
    limit: int = 100,
    _: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    q = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))
    if connection_id:
        q = q.where(Job.connection_id == connection_id)
    if status_:
        q = q.where(Job.status == status_)
    return db.scalars(q).all()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, _: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job_not_found")
    return j


# --- Credencial hub ------------------------------------------------------------------------------


def _setting(db: Session, key: str) -> PlatformSetting | None:
    return db.get(PlatformSetting, key)


def _put_setting(db: Session, key: str, value: str) -> None:
    s = _setting(db, key)
    if s is None:
        db.add(PlatformSetting(key=key, value=value))
    else:
        s.value, s.updated_at = value, datetime.now(UTC)


@router.get("/aws/hub", response_model=HubStatus)
def hub_status(_: SessionData = Depends(require_admin), db: Session = Depends(scoped_db)):
    sec = _setting(db, HUB_SECRET_KEY)
    principal = _setting(db, HUB_PRINCIPAL)
    hint = _setting(db, HUB_HINT)
    return HubStatus(
        mode="vault" if sec else "default",
        principal_arn=principal.value if principal else None,
        access_key_hint=hint.value if hint else None,
        updated_at=sec.updated_at if sec else None,
    )


@router.put("/aws/hub", response_model=HubStatus)
def set_hub(
    body: HubCredentialsIn,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    vault: Vault = Depends(get_vault),
):
    """Grava a chave do usuário IAM hub no cofre. A API cifra mas não consegue ler de volta."""
    plaintext = json.dumps({"access_key_id": body.access_key_id, "secret_access_key": body.secret_access_key})
    try:
        sid = store_secret(db, vault, "aws_hub_access_key", plaintext.encode(), user.email)
    except VaultError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "cofre sem chave pública configurada") from e
    old = _setting(db, HUB_SECRET_KEY)
    if old is not None:
        prev = db.get(CloudSecret, old.value)
        if prev is not None:
            db.delete(prev)
    _put_setting(db, HUB_SECRET_KEY, sid)
    _put_setting(db, HUB_PRINCIPAL, body.principal_arn)
    _put_setting(db, HUB_HINT, f"…{body.access_key_id[-4:]}")
    db.flush()
    audit.record(db, request, "aws.hub.set", user=user, target=body.principal_arn)
    return hub_status(user, db)


@router.delete("/aws/hub", response_model=HubStatus)
def clear_hub(
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
):
    """Volta para a cadeia padrão de credenciais (ex.: instance profile quando a VM roda na AWS)."""
    for key in (HUB_SECRET_KEY, HUB_HINT):
        s = _setting(db, key)
        if s is not None:
            if key == HUB_SECRET_KEY and (prev := db.get(CloudSecret, s.value)) is not None:
                db.delete(prev)
            db.delete(s)
    db.flush()
    audit.record(db, request, "aws.hub.clear", user=user)
    return hub_status(user, db)


@router.put("/aws/hub/principal", response_model=HubStatus)
def set_hub_principal(
    body: HubPrincipalIn,
    request: Request,
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
):
    """ARN da identidade hub no modo padrão (ex.: role da instance profile), usado no onboarding."""
    arn = body.principal_arn
    _put_setting(db, HUB_PRINCIPAL, arn)
    db.flush()
    audit.record(db, request, "aws.hub.principal", user=user, target=arn)
    return hub_status(user, db)
