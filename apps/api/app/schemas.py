import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import TenantRole

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


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str | None
    role: TenantRole


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
