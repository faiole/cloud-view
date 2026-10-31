import hashlib
import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document


class DocumentRejected(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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
    raise DocumentRejected(415, "aceitos: PDF, XML (NF-e), ZIP ou CSV")


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "documento"
    return name[:150]


def store_document(
    db: Session,
    storage,
    *,
    tenant_id: str,
    data: bytes,
    filename: str,
    kind: str,
    billing_period: date,
    uploaded_by: str | None,
    provider: str | None = None,
    number: str | None = None,
    issuer: str | None = None,
    amount: Decimal | None = None,
    currency: str | None = None,
    due_date: date | None = None,
) -> Document:
    if not data:
        raise DocumentRejected(422, "arquivo vazio")
    filename = safe_filename(filename)
    content_type = sniff(data, filename)
    digest = hashlib.sha256(data).hexdigest()
    if db.scalar(select(Document.id).where(Document.tenant_id == tenant_id, Document.sha256 == digest)):
        raise DocumentRejected(409, "este arquivo já foi publicado para o cliente")
    doc_id = str(uuid.uuid4())
    key = f"tenants/{tenant_id}/{billing_period:%Y-%m}/{doc_id}/{filename}"
    storage.put(key, data, content_type)
    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        kind=kind,
        provider=provider,
        billing_period=billing_period,
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
        uploaded_by=uploaded_by,
    )
    db.add(doc)
    db.flush()
    return doc
