import json
import uuid

from sqlalchemy.orm import Session

from app.models import CloudSecret
from app.vault import Vault


def store_secret(db: Session, vault: Vault, purpose: str, plaintext: bytes, created_by: str | None) -> str:
    """Cifra e grava uma credencial; devolve a referência para `Connection.secret_ref`."""
    sid = str(uuid.uuid4())
    sealed = vault.seal(plaintext, aad=f"cloud_secret:{sid}")

    db.add(
        CloudSecret(
            id=sid, purpose=purpose, key_id=json.loads(sealed)["kid"], sealed=sealed, created_by=created_by
        )
    )
    db.flush()
    return sid


def read_secret(db: Session, vault: Vault, secret_id: str) -> bytes:
    """Somente workers (que têm a chave privada) conseguem executar."""
    row = db.get(CloudSecret, secret_id)
    if row is None:
        raise KeyError(secret_id)
    return vault.open(row.sealed, aad=f"cloud_secret:{secret_id}")
