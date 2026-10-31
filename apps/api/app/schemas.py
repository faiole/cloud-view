import re
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.permissions import TenantRole

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("use letras minúsculas, números e hífen")
        return v


class TenantUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    aws_external_id: str
    created_at: datetime


class MemberPut(BaseModel):
    email: EmailStr
    role: TenantRole
    name: str | None = Field(default=None, max_length=200)


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str | None
    role: TenantRole
    disabled: bool = False
    pending: bool = False  # convidado e ainda não fez o primeiro login
    invite_email_sent: bool | None = None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    ts: datetime
    user_email: str | None
    tenant_id: str | None
    ip: str | None
    action: str
    target: str | None
    details: dict
    outcome: str


# --- Usuários (administração da plataforma) ----------------------------------------------------


class UserInvite(BaseModel):
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)
    platform_admin: bool = False


class UserMembership(BaseModel):
    tenant_id: str
    tenant_name: str
    role: TenantRole


class UserOut(BaseModel):
    id: str
    email: str
    name: str | None
    disabled: bool
    pending: bool
    last_login_at: datetime | None
    memberships: list[UserMembership]
    invite_email_sent: bool | None = None


class FlagBody(BaseModel):
    value: bool


# --- Contas e atribuição ------------------------------------------------------------------------


class AccountOut(BaseModel):
    id: str
    provider: str
    native_id: str
    name: str | None
    connection_id: str | None
    connection_name: str | None
    connection_native_id: str | None
    tenants: list[str]  # nomes dos clientes com alguma regra


class AccountUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AssignmentIn(BaseModel):
    tenant_id: str
    tag_key: str | None = Field(default=None, max_length=128)
    tag_value: str | None = Field(default=None, max_length=256)
    valid_from: date | None = None
    valid_to: date | None = None


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cloud_account_id: str
    tenant_id: str
    tag_key: str | None
    tag_value: str | None
    valid_from: date | None
    valid_to: date | None


class TenantAccountOut(BaseModel):
    id: str
    provider: str
    native_id: str
    name: str | None
    shared: bool  # conta dividida por tag com outros clientes
    rules: list[str]


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    kind: str
    native_id: str
    name: str


class ImportOut(BaseModel):
    id: str
    filename: str | None
    provider: str | None
    period_start: date | None
    period_end: date | None
    source_rows: int
    stored_rows: int
    replaced_rows: int
    created_by: str | None
    created_at: datetime
    new_accounts: list[str] = []
    unassigned_accounts: list[str] = []


# --- Relatórios ---------------------------------------------------------------------------------


class UsageRow(BaseModel):
    key: str
    label: str
    currency: str
    cost: Decimal


class SeriesPoint(BaseModel):
    date: date
    currency: str
    cost: Decimal


class Total(BaseModel):
    currency: str
    cost: Decimal


class UsageReport(BaseModel):
    start: date
    end: date
    group_by: str
    metric: str
    totals: list[Total]
    rows: list[UsageRow]
    daily: list[SeriesPoint]


class CostSummary(BaseModel):
    month_to_date: list[Total]
    previous_month: list[Total]
    monthly: list[UsageRow]  # últimos 12 meses (key = AAAA-MM)
    top_services: list[UsageRow]
    accounts: int


class TagKeys(BaseModel):
    keys: list[str]


class TagValues(BaseModel):
    key: str
    values: list[str]


# --- Documentos ---------------------------------------------------------------------------------


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    kind: str
    provider: str | None
    billing_period: date
    number: str | None
    issuer: str | None
    amount: Decimal | None
    currency: str | None
    due_date: date | None
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


# --- Fase 1: conexões, coleta, inventário, saúde, ações -------------------------------------------


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^(aws_organization|aws_account)$")
    account_id: str = Field(
        pattern=r"^\d{12}$", description="conta management (Organization) ou a conta avulsa"
    )
    role_name: str = Field(default="CloudViewReadOnly", pattern=r"^[\w+=,.@-]{1,64}$")
    cost_source: str = Field(default="cost_explorer", pattern=r"^(cost_explorer|focus_s3|manual)$")
    focus_s3_uri: str | None = Field(default=None, pattern=r"^s3://[a-z0-9.\-]{3,63}(/.*)?$")
    regions: list[str] | None = None
    collect_invoices: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    cost_source: str | None = Field(default=None, pattern=r"^(cost_explorer|focus_s3|manual)$")
    focus_s3_uri: str | None = Field(default=None, pattern=r"^s3://[a-z0-9.\-]{3,63}(/.*)?$")
    regions: list[str] | None = None
    collect_invoices: bool | None = None


class ConnectionDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    kind: str
    native_id: str
    name: str
    role_arn: str | None
    external_id: str | None
    member_role_name: str
    operator_role_name: str
    regions: list[str] | None
    cost_source: str
    focus_s3_uri: str | None
    collect_invoices: bool
    status: str
    last_error: str | None
    last_sync_at: datetime | None
    accounts: int = 0


class HubCredentialsIn(BaseModel):
    access_key_id: str = Field(pattern=r"^(AKIA|ASIA)[A-Z0-9]{16}$")
    secret_access_key: str = Field(min_length=40, max_length=40)
    principal_arn: str = Field(pattern=r"^arn:aws(-[a-z]+)*:iam::\d{12}:(user|role)/[\w+=,.@/-]+$")


class HubStatus(BaseModel):
    mode: str  # default | vault
    principal_arn: str | None
    access_key_hint: str | None
    updated_at: datetime | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    connection_id: str | None
    cloud_account_id: str | None
    status: str
    attempts: int
    error: str | None
    result: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ResourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    account_native_id: str = ""
    account_name: str | None = None
    region: str
    type: str
    native_id: str
    name: str | None
    state: str | None
    instance_type: str | None
    vcpu: int | None
    memory_mib: int | None
    platform: str | None
    private_ips: list
    public_ips: list
    vpc_id: str | None
    subnet_id: str | None
    security_groups: list
    tags: dict
    attributes: dict
    findings: list
    health: str | None
    health_details: dict
    collected_at: datetime


class HealthEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    service: str | None
    region: str | None
    category: str | None
    status: str
    title: str
    description: str | None
    start_at: datetime | None
    end_at: datetime | None
    resource_id: str | None


class HealthSummary(BaseModel):
    instances: int
    running: int
    impaired: list[ResourceOut]
    events: list[HealthEventOut]
    last_collected_at: datetime | None


class NetworkVpc(BaseModel):
    vpc: ResourceOut
    subnets: list[ResourceOut]
    instances: int
    exposed_instances: list[ResourceOut]


class NetworkSummary(BaseModel):
    vpcs: list[NetworkVpc]
    open_security_groups: list[ResourceOut]
    findings_by_code: dict[str, int]


class ActionIn(BaseModel):
    action: str = Field(pattern=r"^(reboot|start|stop)$")
    confirm: str = Field(min_length=1, max_length=300, description="nome ou id do recurso, digitado")
    reason: str | None = Field(default=None, max_length=500)


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    resource_id: str
    resource_native_id: str
    action: str
    reason: str | None
    status: str
    requested_by: str
    requested_at: datetime
    finished_at: datetime | None
    result: dict


class HubPrincipalIn(BaseModel):
    principal_arn: str = Field(pattern=r"^arn:aws(-[a-z]+)*:iam::\d{12}:(user|role)/[\w+=,.@/-]+$")
