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
    cost_source: str | None = Field(
        default=None, pattern=r"^(cost_explorer|focus_s3|azure_cost_management|focus_blob|manual)$"
    )
    focus_s3_uri: str | None = Field(
        default=None,
        pattern=r"^(s3://[a-z0-9.\-]{3,63}(/.*)?|https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/.+)$",
    )
    regions: list[str] | None = None
    collect_invoices: bool | None = None
    billing_account_id: str | None = Field(default=None, max_length=300)


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
    client_id: str | None = None
    billing_account_id: str | None = None
    accounts: int = 0


GUID = r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$"


class AzureAppCredential(BaseModel):
    client_id: str = Field(pattern=GUID)
    client_secret: str | None = Field(default=None, min_length=10, max_length=200)
    certificate_pem: str | None = Field(default=None, max_length=20000)
    private_key_pem: str | None = Field(default=None, max_length=20000)

    @field_validator("private_key_pem")
    @classmethod
    def _pair(cls, v, info):
        if bool(v) != bool(info.data.get("certificate_pem")):
            raise ValueError("certificado e chave privada vêm juntos")
        return v


class AzureConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(pattern=GUID, description="ID do tenant (diretório) do cliente no Entra ID")
    cost_source: str = Field(
        default="azure_cost_management", pattern=r"^(azure_cost_management|focus_blob|manual)$"
    )
    focus_uri: str | None = Field(
        default=None, pattern=r"^https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/.+$"
    )
    billing_account_id: str | None = Field(default=None, max_length=300)
    collect_invoices: bool = True
    own_app: AzureAppCredential | None = None


class AzureHubIn(AzureAppCredential):
    home_tenant_id: str = Field(pattern=GUID, description="tenant onde o app multi-tenant foi registrado")


class AzureHubStatus(BaseModel):
    configured: bool
    client_id: str | None
    home_tenant_id: str | None
    auth: str | None  # certificate | secret
    updated_at: datetime | None


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


# --- Fase 2 -----------------------------------------------------------------------------------------


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    action_type: str
    resource_type: str | None
    resource_native_id: str | None
    region: str | None
    title: str
    current: str | None
    recommended: str | None
    monthly_savings: Decimal
    monthly_cost: Decimal | None
    currency: str
    savings_pct: Decimal | None
    effort: str | None
    restart_needed: bool | None
    estimated: bool
    status: str
    status_by: str | None
    status_note: str | None
    first_seen: datetime
    last_seen: datetime
    account_native_id: str | None = None


class RecommendationSummary(BaseModel):
    open: int
    savings: list[Total]
    by_action: dict[str, int]
    items: list[RecommendationOut]


class RecommendationStatusIn(BaseModel):
    status: str = Field(pattern=r"^(open|dismissed|done)$")
    note: str | None = Field(default=None, max_length=500)


class CoverageMonth(BaseModel):
    month: date
    covered: Decimal
    on_demand: Decimal
    total: Decimal
    coverage_pct: Decimal | None


class BudgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    accounts: list[str] = []
    services: list[str] = []
    tag: str | None = Field(default=None, pattern=r"^[^=]+=.*$")
    thresholds: list[int] = Field(default=[50, 80, 100], min_length=1, max_length=6)
    alert_on_forecast: bool = True

    @field_validator("thresholds")
    @classmethod
    def _thr(cls, v: list[int]) -> list[int]:
        if any(x < 1 or x > 500 for x in v):
            raise ValueError("limites entre 1% e 500%")
        return sorted(set(v))


class BudgetOut(BaseModel):
    id: str
    name: str
    amount: Decimal
    currency: str
    accounts: list[str]
    services: list[str]
    tag: str | None
    thresholds: list[int]
    alert_on_forecast: bool
    actual: Decimal | None
    forecast: Decimal | None
    pct: Decimal | None
    last_evaluated_at: datetime | None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    type: str
    category: str
    severity: str
    title: str
    message: str
    data: dict
    status: str
    status_by: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    tenant_name: str | None = None


class ChannelIn(BaseModel):
    type: str = Field(pattern=r"^(email|slack|teams|webhook)$")
    name: str = Field(min_length=1, max_length=200)
    emails: list[EmailStr] = []
    url: str | None = Field(default=None, max_length=2000)
    min_severity: str = Field(default="warning", pattern=r"^(info|warning|critical)$")
    categories: list[str] = ["cost", "health", "security"]

    @field_validator("categories")
    @classmethod
    def _cats(cls, v: list[str]) -> list[str]:
        allowed = {"cost", "health", "security", "platform"}
        if not v or set(v) - allowed:
            raise ValueError(f"categorias válidas: {', '.join(sorted(allowed))}")
        return v


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str | None
    type: str
    name: str
    emails: list[str]
    target_hint: str | None
    min_severity: str
    categories: list[str]
    enabled: bool
    created_at: datetime
    signing_secret: str | None = None  # só na criação de webhook
    last_delivery: str | None = None
