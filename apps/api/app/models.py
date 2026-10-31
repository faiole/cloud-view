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
