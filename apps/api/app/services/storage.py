"""Armazenamento de documentos (invoices, notas fiscais).

Padrão: volume local (Docker), com cada arquivo cifrado em AES-256-GCM pela própria API
(`CV_DOCUMENTS_KEY`). Opcional: qualquer storage compatível com S3 (AWS S3, MinIO, OCI Object
Storage, Cloudflare R2...), com download por URL pré-assinada de 60 s, gerada só depois da checagem
de permissão.
"""

import base64
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, RedirectResponse, Response

from app.core.config import Settings, get_settings


class LocalStorage:
    def __init__(self, base: str):
        self.base = Path(base).resolve()

    def _path(self, key: str) -> Path:
        p = (self.base / key).resolve()
        if self.base not in p.parents:
            raise ValueError("invalid key")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, 0o600)

    def download(self, key: str, filename: str, content_type: str) -> Response:
        return FileResponse(self._path(key), media_type=content_type, filename=filename)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class EncryptedLocalStorage(LocalStorage):
    """Arquivo no disco = nonce (12 bytes) + AES-GCM(conteúdo), com a chave do objeto como AAD."""

    def __init__(self, base: str, key_b64: str):
        super().__init__(base)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise RuntimeError("CV_DOCUMENTS_KEY deve ter 32 bytes em base64")
        self.aes = AESGCM(key)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        nonce = os.urandom(12)
        super().put(key, nonce + self.aes.encrypt(nonce, data, key.encode()), content_type)

    def read(self, key: str) -> bytes:
        raw = self._path(key).read_bytes()
        return self.aes.decrypt(raw[:12], raw[12:], key.encode())

    def download(self, key: str, filename: str, content_type: str) -> Response:
        return Response(
            self.read(key),
            media_type=content_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )


class S3Storage:
    def __init__(self, bucket: str, kms_key_id: str | None, endpoint_url: str | None = None):
        import boto3

        self.s3 = boto3.client("s3", endpoint_url=endpoint_url)
        self.bucket = bucket
        self.kms = kms_key_id

    def put(self, key: str, data: bytes, content_type: str) -> None:
        extra = {}
        if self.kms:  # SSE-KMS só existe na AWS; nos demais, a criptografia é a do provedor
            extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms}
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra)

    def download(self, key: str, filename: str, content_type: str) -> Response:
        url = self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
            ExpiresIn=60,
        )
        return RedirectResponse(url, status_code=303)

    def delete(self, key: str) -> None:
        # Com Object Lock a versão continua retida; isto só cria o delete marker
        self.s3.delete_object(Bucket=self.bucket, Key=key)


def build_storage(s: Settings):
    if s.storage_backend == "s3":
        if not s.documents_bucket:
            raise RuntimeError("CV_DOCUMENTS_BUCKET não configurado")
        return S3Storage(s.documents_bucket, s.documents_kms_key_id, s.s3_endpoint_url)
    if s.documents_key:
        return EncryptedLocalStorage(s.storage_local_dir, s.documents_key)
    return LocalStorage(s.storage_local_dir)


@lru_cache
def get_storage():
    return build_storage(get_settings())
