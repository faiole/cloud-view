import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.permissions import TenantRole

__all__ = ["TenantRole"]

JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(18, 6)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    # ExternalId único por cliente, usado na trust policy das roles AWS (anti confused-deputy)
    aws_external_id: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: uuid.uuid4().hex)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    members: Mapped[list["UserTenantRole"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # `sub` do token = id do usuário no Keycloak (preenchido no convite ou no primeiro login)
    keycloak_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list["UserTenantRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserTenantRole(Base):
    __tablename__ = "user_tenant_role"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32))

    user: Mapped[AppUser] = relationship(back_populates="memberships")
    tenant: Mapped[Tenant] = relationship(back_populates="members")


class AuditLog(Base):
    """Trilha de auditoria append-only (UPDATE/DELETE bloqueados por trigger no Postgres)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str | None] = mapped_column(String(300), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(20), default="success")
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- Contas de nuvem e atribuição a clientes ---------------------------------------------------


class Provider(enum.StrEnum):
    aws = "aws"
    azure = "azure"
    oci = "oci"


class Connection(Base):
    """Ponto de acesso a uma nuvem: uma Organization/conta pagadora AWS, um tenant Azure, uma tenancy OCI.

    Pertence à plataforma, não a um cliente: uma mesma Organization pode conter contas de vários
    clientes. Quem vê o quê é decidido conta a conta em `AccountAssignment`.
    """

    __tablename__ = "connection"
    __table_args__ = (UniqueConstraint("provider", "native_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(
        String(32)
    )  # aws_organization | aws_account | azure_tenant | oci_tenancy
    native_id: Mapped[str] = mapped_column(String(128))  # id da conta pagadora / tenant / tenancy
    name: Mapped[str] = mapped_column(String(200))
    # Referência ao segredo cifrado no cofre (cloud_secret.id), nunca o segredo em si
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # --- Coleta automática (Fase 1). Conexões criadas só por importação ficam com role_arn vazio.
    role_arn: Mapped[str | None] = mapped_column(String(300), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    member_role_name: Mapped[str] = mapped_column(String(64), default="CloudViewReadOnly")
    operator_role_name: Mapped[str] = mapped_column(String(64), default="CloudViewOperator")
    regions: Mapped[list | None] = mapped_column(JSON, nullable=True)  # None = todas as habilitadas
    cost_source: Mapped[str] = mapped_column(
        String(32), default="manual"
    )  # cost_explorer | focus_s3 | manual
    focus_s3_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    collect_invoices: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="manual")  # manual | pending | ok | error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Azure: app próprio do cliente (None = app hub da plataforma) e conta de cobrança para invoices
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_account_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # OCI: chave de API própria do cliente (own_key) ou identidade hub com política cross-tenancy (hub)
    oci_auth: Mapped[str | None] = mapped_column(String(16), nullable=True)
    oci_user_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    oci_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oci_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    home_region: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @property
    def collectable(self) -> bool:
        if self.provider == "aws":
            return bool(self.role_arn)
        if self.provider == "azure":
            return self.kind == "azure_tenant" and self.status != "manual"
        if self.provider == "oci":
            ready = self.oci_auth == "hub" or (self.oci_auth == "own_key" and bool(self.oci_user_id))
            return self.kind == "oci_tenancy" and self.status != "manual" and ready and bool(self.home_region)
        return False

    @property
    def is_organization(self) -> bool:
        return self.kind == "aws_organization"


class CloudSecret(Base):
    """Credencial de nuvem cifrada pelo cofre (`app/vault.py`). A API grava; só workers leem."""

    __tablename__ = "cloud_secret"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    purpose: Mapped[str] = mapped_column(String(64))  # ex.: aws_hub_access_key, oci_api_key, azure_cert
    key_id: Mapped[str] = mapped_column(String(32))  # chave do cofre usada (rotação)
    sealed: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CloudAccount(Base):
    """Conta AWS / subscription Azure / compartment OCI que aparece nos dados de custo."""

    __tablename__ = "cloud_account"
    __table_args__ = (UniqueConstraint("provider", "native_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(16))
    native_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Conexão (Organization/pagadora) atual; muda se a conta for transferida de AB para AC
    connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("connection.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ACTIVE/SUSPENDED (Organizations)
    collect: Mapped[bool] = mapped_column(Boolean, default=True)  # coletar inventário/saúde desta conta
    last_inventory_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    connection: Mapped[Connection | None] = relationship()
    assignments: Mapped[list["AccountAssignment"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AccountAssignment(Base):
    """Regra que diz a qual cliente pertencem os custos de uma conta.

    - sem tag: a conta inteira é do cliente;
    - com tag (ex.: cost-center = empresa-a): só as linhas com essa tag (contas compartilhadas).
    Regras com tag têm prioridade sobre a regra da conta inteira. `valid_from/valid_to` cobrem
    contas que trocaram de dono.
    """

    __tablename__ = "account_assignment"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cloud_account_id: Mapped[str] = mapped_column(
        ForeignKey("cloud_account.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    tag_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tag_value: Mapped[str | None] = mapped_column(String(256), nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)  # inclusivo
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[CloudAccount] = relationship(back_populates="assignments")
    tenant: Mapped[Tenant] = relationship()


# --- Custos -------------------------------------------------------------------------------------


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(32))  # focus_csv
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    billing_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_rows: Mapped[int] = mapped_column(Integer, default=0)
    stored_rows: Mapped[int] = mapped_column(Integer, default=0)
    replaced_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CostLine(Base):
    """Custo diário agregado (formato FOCUS), já atribuído a um cliente.

    `tenant_id` é calculado pela alocação (`services/allocation.py`) a partir das regras de
    `AccountAssignment`; o RLS do Postgres filtra por ele. Linhas sem cliente ficam com NULL e só
    o administrador da plataforma as vê.
    """

    __tablename__ = "cost_line"
    __table_args__ = (
        Index("ix_cost_line_tenant_date", "tenant_id", "charge_date"),
        Index("ix_cost_line_account_date", "cloud_account_id", "charge_date"),
        Index("ix_cost_line_billing_date", "provider", "billing_account_id", "charge_date"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cloud_account_id: Mapped[str] = mapped_column(ForeignKey("cloud_account.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(16))
    billing_account_id: Mapped[str] = mapped_column(String(128))
    charge_date: Mapped[date] = mapped_column(Date)
    service: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    charge_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    billed_cost: Mapped[Decimal] = mapped_column(Money)
    effective_cost: Mapped[Decimal] = mapped_column(Money)
    tags: Mapped[dict] = mapped_column(JSONType, default=dict)
    import_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


# --- Documentos (invoices e notas fiscais) ------------------------------------------------------


class DocumentKind(enum.StrEnum):
    invoice = "invoice"
    nota_fiscal = "nota_fiscal"
    boleto = "boleto"
    statement = "statement"  # demonstrativo
    other = "other"


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (Index("ix_document_tenant_period", "tenant_id", "billing_period"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    billing_period: Mapped[date] = mapped_column(Date)  # primeiro dia do mês de referência
    number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    issuer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(500))
    uploaded_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Fase 1: coleta, inventário, saúde e ações --------------------------------------------------


class PlatformSetting(Base):
    __tablename__ = "platform_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Job(Base):
    """Fila de trabalho dos workers, no próprio Postgres (SELECT ... FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "job"
    __table_args__ = (
        Index("ix_job_claim", "status", "run_after"),
        Index("ix_job_dedupe", "dedupe_key", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    cloud_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | done | error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Resource(Base):
    """Recurso coletado (instância, volume, IP, VPC, subnet, security group).

    `tenant_id` segue as mesmas regras de `AccountAssignment` dos custos (conta inteira ou tag).
    """

    __tablename__ = "resource"
    __table_args__ = (
        UniqueConstraint("cloud_account_id", "region", "type", "native_id"),
        Index("ix_resource_tenant_type", "tenant_id", "type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cloud_account_id: Mapped[str] = mapped_column(
        ForeignKey("cloud_account.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(
        String(32)
    )  # ver app/resource_types.py (ec2_instance/vm, ebs_volume/disk, elastic_ip/public_ip, vpc, subnet, security_group)
    native_id: Mapped[str] = mapped_column(String(400))
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    instance_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vcpu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mib: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    private_ips: Mapped[list] = mapped_column(JSON, default=list)
    public_ips: Mapped[list] = mapped_column(JSON, default=list)
    vpc_id: Mapped[str | None] = mapped_column(String(400), nullable=True)
    subnet_id: Mapped[str | None] = mapped_column(String(400), nullable=True)
    security_groups: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[dict] = mapped_column(JSONType, default=dict)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list] = mapped_column(JSON, default=list)  # dicas/alertas simples (ex.: porta 22 aberta)
    health: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # ok | impaired | initializing | unknown
    health_details: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[CloudAccount] = relationship()


class HealthEvent(Base):
    __tablename__ = "health_event"
    __table_args__ = (
        UniqueConstraint("source", "native_id", "cloud_account_id"),
        Index("ix_health_event_tenant", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cloud_account_id: Mapped[str] = mapped_column(ForeignKey("cloud_account.id", ondelete="CASCADE"))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[str] = mapped_column(String(32))  # aws_health | ec2_scheduled | ec2_status
    native_id: Mapped[str] = mapped_column(String(400))
    service: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # issue | scheduledChange | accountNotification
    status: Mapped[str] = mapped_column(String(16))  # open | upcoming | closed
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ActionRequest(Base):
    __tablename__ = "action_request"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    resource_id: Mapped[str] = mapped_column(String(36))
    resource_native_id: Mapped[str] = mapped_column(String(400))
    action: Mapped[str] = mapped_column(String(16))  # reboot | start | stop
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | succeeded | failed
    requested_by: Mapped[str] = mapped_column(String(320))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


# --- Fase 2: otimização, orçamentos, anomalias, alertas ------------------------------------------


class Recommendation(Base):
    __tablename__ = "recommendation"
    __table_args__ = (
        UniqueConstraint("source", "native_id"),
        Index("ix_recommendation_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cloud_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("cloud_account.id", ondelete="CASCADE"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(32))  # cost_optimization_hub | rules
    native_id: Mapped[str] = mapped_column(String(300))
    action_type: Mapped[str] = mapped_column(String(40))  # Rightsize, Delete, MigrateToGraviton, ...
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_native_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    current: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recommended: Mapped[str | None] = mapped_column(String(300), nullable=True)
    monthly_savings: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    monthly_cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    savings_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    restart_needed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    estimated: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # True = estimativa própria (preço de tabela)
    tags: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | dismissed | done | resolved
    status_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CommitmentCoverage(Base):
    """Cobertura de Savings Plans por conta e mês (Cost Explorer)."""

    __tablename__ = "commitment_coverage"
    __table_args__ = (UniqueConstraint("cloud_account_id", "month"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    cloud_account_id: Mapped[str] = mapped_column(ForeignKey("cloud_account.id", ondelete="CASCADE"))
    month: Mapped[date] = mapped_column(Date)
    covered: Mapped[Decimal] = mapped_column(Money)
    on_demand: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Budget(Base):
    __tablename__ = "budget"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Filtros opcionais: {"accounts": ["111..."], "services": ["Amazon EC2"], "tag": "chave=valor"}
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    thresholds: Mapped[list] = mapped_column(JSON, default=lambda: [50, 80, 100])
    alert_on_forecast: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Último cálculo (para a tela)
    last_actual: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_forecast: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base):
    __tablename__ = "alert"
    __table_args__ = (Index("ix_alert_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # None = alerta da plataforma
    type: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(16))  # cost | health | security | platform
    severity: Mapped[str] = mapped_column(String(16))  # info | warning | critical
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | acknowledged | resolved
    status_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationChannel(Base):
    __tablename__ = "notification_channel"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # None = plataforma
    type: Mapped[str] = mapped_column(String(16))  # email | slack | teams | webhook
    name: Mapped[str] = mapped_column(String(200))
    emails: Mapped[list] = mapped_column(JSON, default=list)
    # URL do Slack/Teams/webhook e segredo de assinatura ficam cifrados no cofre
    secret_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    min_severity: Mapped[str] = mapped_column(String(16), default="warning")
    categories: Mapped[list] = mapped_column(JSON, default=lambda: ["cost", "health", "security"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NotificationDelivery(Base):
    __tablename__ = "notification_delivery"
    __table_args__ = (UniqueConstraint("alert_id", "channel_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    alert_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # None = teste
    channel_id: Mapped[str] = mapped_column(ForeignKey("notification_channel.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16))  # sent | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
