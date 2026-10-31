"""Invoices, notas fiscais e boletos por cliente.

Regra de visibilidade: um documento pertence a UM cliente. A fatura de uma conta pagadora
compartilhada (ex.: Organization AB com contas das empresas A e B) não é exposta a nenhum dos dois;
o administrador publica para cada cliente o documento dele (NF emitida para ele, demonstrativo).
"""

import hashlib
import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentKind, Tenant
from app.permissions import Perm
from app.schemas import DocumentOut
from app.security.deps import require_admin, require_recent_mfa, scoped_db, tenant_access
from app.security.session import SessionData
from app.services import audit
from app.services.storage import get_storage

router = APIRouter(prefix="/tenants/{tenant_id}/documents", tags=["documents"])


def sniff(data: bytes, filename: str) -> str:
    """Tipo pelo conteúdo, não pela extensão informada."""
    head = data[:512].lstrip()
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.startswith(b"<?xml") or head.startswith(b"<nfeProc") or head.startswith(b"<NFe"):
        return "application/xml"
    if filename.lower().endswith(".csv"):
        try:
            data[:4096].decode("utf-8")
            return "text/csv"
        except UnicodeDecodeError:
            pass
    raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "aceitos: PDF, XML (NF-e), ZIP ou CSV")


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "documento"
    return name[:150]


def _period(v: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}", v):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "billing_period deve ser AAAA-MM")
    y, m = map(int, v.split("-"))
    if not 1 <= m <= 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mês inválido")
    return date(y, m, 1)


@router.get("", response_model=list[DocumentOut])
def list_documents(
    tenant_id: str,
    year: int | None = None,
    kind: DocumentKind | None = None,
    _: SessionData = Depends(tenant_access(Perm.invoices_read)),
    db: Session = Depends(scoped_db),
):
    q = select(Document).where(Document.tenant_id == tenant_id)
    if year:
        q = q.where(Document.billing_period >= date(year, 1, 1), Document.billing_period <= date(year, 12, 1))
    if kind:
        q = q.where(Document.kind == kind.value)
    return db.scalars(q.order_by(Document.billing_period.desc(), Document.kind)).all()


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_document(
    tenant_id: str,
    request: Request,
    file: UploadFile = File(...),
    kind: DocumentKind = Form(...),
    billing_period: str = Form(..., description="AAAA-MM"),
    provider: str | None = Form(default=None),
    number: str | None = Form(default=None, max_length=100),
    issuer: str | None = Form(default=None, max_length=200),
    amount: Decimal | None = Form(default=None),
    currency: str | None = Form(default=None, max_length=3),
    due_date: date | None = Form(default=None),
    user: SessionData = Depends(require_admin),
    db: Session = Depends(scoped_db),
    storage=Depends(get_storage),
):
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant_not_found")
    limit = get_settings().max_document_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "arquivo maior que o limite")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "arquivo vazio")
    filename = safe_filename(file.filename or "documento")
    content_type = sniff(data, filename)
    period = _period(billing_period)
    digest = hashlib.sha256(data).hexdigest()

    dup = db.scalar(select(Document.id).where(Document.tenant_id == tenant_id, Document.sha256 == digest))
    if dup:
        raise HTTPException(status.HTTP_409_CONFLICT, "este arquivo já foi publicado para o cliente")

    doc_id = str(uuid.uuid4())
    key = f"tenants/{tenant_id}/{period:%Y-%m}/{doc_id}/{filename}"
    storage.put(key, data, content_type)
    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        kind=kind.value,
        provider=provider,
        billing_period=period,
        number=number,
        issuer=issuer,
        amount=amount,
        currency=currency.upper() if currency else None,
        due_date=due_date,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=digest,
        storage_key=key,
        uploaded_by=user.email,
    )
    db.add(doc)
    db.flush()
    audit.record(
        db,
        request,
        "document.upload",
        user=user,
        tenant_id=tenant_id,
        target=filename,
        details={"kind": kind.value, "period": f"{period:%Y-%m}", "sha256": digest},
    )
    return doc


def _doc(db: Session, tenant_id: str, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document_not_found")
    return doc


@router.get("/{doc_id}/download")
def download_document(
    tenant_id: str,
    doc_id: str,
    request: Request,
    user: SessionData = Depends(tenant_access(Perm.invoices_read)),
    db: Session = Depends(scoped_db),
    storage=Depends(get_storage),
):
    doc = _doc(db, tenant_id, doc_id)
    audit.record(db, request, "document.download", user=user, tenant_id=tenant_id, target=doc.filename)
    db.commit()
    return storage.download(doc.storage_key, doc.filename, doc.content_type)


@router.delete("/{doc_id}", status_code=204)
def delete_document(
    tenant_id: str,
    doc_id: str,
    request: Request,
    _: SessionData = Depends(require_admin),
    user: SessionData = Depends(require_recent_mfa),
    db: Session = Depends(scoped_db),
    storage=Depends(get_storage),
):
    doc = _doc(db, tenant_id, doc_id)
    audit.record(
        db,
        request,
        "document.delete",
        user=user,
        tenant_id=tenant_id,
        target=doc.filename,
        details={"sha256": doc.sha256},
    )
    storage.delete(doc.storage_key)
    db.delete(doc)
