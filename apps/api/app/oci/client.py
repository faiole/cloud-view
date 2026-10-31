"""Acesso à OCI via REST (sem SDK), com assinatura de requisição (HTTP Signature, rsa-sha256).

Credencial = tenancy + usuário + fingerprint + chave privada (chave de API do usuário).
- own_key: usuário na tenancy do cliente; o par de chaves é gerado pelo Cloud View e só a chave
  pública é mostrada (o cliente cola no usuário). A privada vai direto para o cofre.
- hub: usuário na tenancy da plataforma, com políticas cross-tenancy (endorse/admit).
"""

import base64
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy.orm import Session

from app.models import Connection, PlatformSetting
from app.services.secrets import read_secret

OCI_HUB_KEY = "oci_hub_secret_id"  # noqa: S105 - nome da configuração
DOMAIN = "oraclecloud.com"
SERVICES = {
    "iaas": ("iaas", "20160918"),
    "identity": ("identity", "20160918"),
    "query": ("query", "20180409"),
    "usage": ("usageapi", "20200107"),
    "optimizer": ("optimizer", "20200606"),
    "announcements": ("announcements", "20180904"),
    "objectstorage": ("objectstorage", ""),
}


class OciError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class OciCredential:
    tenancy_id: str
    user_id: str
    fingerprint: str
    private_key_pem: str

    @property
    def key_id(self) -> str:
        return f"{self.tenancy_id}/{self.user_id}/{self.fingerprint}"


def generate_api_key() -> tuple[str, str, str]:
    """(chave privada PEM, chave pública PEM, fingerprint no formato da OCI: MD5 do DER, aa:bb:...)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return priv.decode(), pub.decode(), fingerprint(pub_der)


def fingerprint(public_der: bytes) -> str:
    digest = hashlib.md5(public_der).hexdigest()  # noqa: S324 - formato de fingerprint exigido pela OCI
    return ":".join(digest[i : i + 2] for i in range(0, 32, 2))


def signing_string(request: httpx.Request, headers: list[str]) -> str:
    parts = urlsplit(str(request.url))
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = []
    for h in headers:
        if h == "(request-target)":
            lines.append(f"(request-target): {request.method.lower()} {target}")
        else:
            lines.append(f"{h}: {request.headers[h]}")
    return "\n".join(lines)


class OciSigner(httpx.Auth):
    requires_request_body = True

    def __init__(self, cred: OciCredential):
        self.cred = cred
        self.key = serialization.load_pem_private_key(cred.private_key_pem.encode(), password=None)

    def auth_flow(self, request: httpx.Request):
        request.headers["date"] = formatdate(usegmt=True)
        request.headers.setdefault("host", request.url.host)
        headers = ["date", "(request-target)", "host"]
        if request.method in ("POST", "PUT", "PATCH"):
            body = request.content or b""
            request.headers["x-content-sha256"] = base64.b64encode(hashlib.sha256(body).digest()).decode()
            request.headers.setdefault("content-type", "application/json")
            request.headers["content-length"] = str(len(body))
            headers += ["x-content-sha256", "content-type", "content-length"]
        sig = self.key.sign(signing_string(request, headers).encode(), padding.PKCS1v15(), hashes.SHA256())
        request.headers["authorization"] = (
            f'Signature version="1",keyId="{self.cred.key_id}",algorithm="rsa-sha256",'
            f'headers="{" ".join(headers)}",signature="{base64.b64encode(sig).decode()}"'
        )
        yield request


class OciHub:
    def __init__(self, credential: OciCredential | None, transport: httpx.BaseTransport | None = None):
        self.credential = credential
        self.transport = transport
        self.sleep = time.sleep
        self._clients: dict[str, httpx.Client] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, db: Session, vault, transport=None) -> "OciHub":
        s = db.get(PlatformSetting, OCI_HUB_KEY)
        cred = OciCredential(**json.loads(read_secret(db, vault, s.value))) if s else None
        return cls(cred, transport)

    def credential_for(self, db: Session, vault, conn: Connection) -> OciCredential:
        if conn.oci_auth == "own_key":
            if not conn.secret_ref or not conn.oci_user_id:
                raise OciError("informe o OCID do usuário que recebeu a chave pública")
            priv = read_secret(db, vault, conn.secret_ref).decode()
            return OciCredential(conn.native_id, conn.oci_user_id, conn.oci_fingerprint, priv)
        if self.credential is None:
            raise OciError("identidade hub da OCI não configurada (tela Conexões)")
        return self.credential

    def client(self, cred: OciCredential) -> "Oci":
        with self._lock:
            http = self._clients.get(cred.key_id)
            if http is None:
                http = httpx.Client(transport=self.transport, timeout=60, auth=OciSigner(cred))
                self._clients[cred.key_id] = http
        return Oci(self, http)


def _json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return {}


class Oci:
    MAX_TRIES = 5

    def __init__(self, hub: OciHub, http: httpx.Client):
        self.hub, self.http = hub, http

    @staticmethod
    def url(service: str, region: str, path: str) -> str:
        host, version = SERVICES[service]
        prefix = f"/{version}" if version else ""
        return f"https://{host}.{region}.{DOMAIN}{prefix}{path}"

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(self.MAX_TRIES):
            r = self.http.request(method, url, **kw)
            if r.status_code in (429, 503) and attempt < self.MAX_TRIES - 1:
                self.hub.sleep(min(float(r.headers.get("retry-after", 2**attempt)), 60))
                continue
            if r.status_code >= 400:
                body = _json(r) if r.content else {}
                code = body.get("code") if isinstance(body, dict) else None
                msg = body.get("message", "") if isinstance(body, dict) else ""
                raise OciError(
                    f"{method} {urlsplit(url).path}: {r.status_code} {code or ''} {msg[:300]}".strip(),
                    r.status_code,
                    code,
                )
            return r
        raise OciError("limite de requisições da OCI", 429)

    def get(self, service: str, region: str, path: str, **params):
        q = f"?{urlencode(params)}" if params else ""
        return _json(self.request("GET", self.url(service, region, path) + q))

    def list_all(self, service: str, region: str, path: str, **params) -> list:
        """Listas da OCI paginam pelo cabeçalho opc-next-page."""
        out, page = [], None
        while True:
            q = {**params, **({"page": page} if page else {})}
            r = self.request("GET", self.url(service, region, path) + (f"?{urlencode(q)}" if q else ""))
            data = _json(r)
            out += data if isinstance(data, list) else data.get("items", [])
            page = r.headers.get("opc-next-page")
            if not page:
                return out

    def post(
        self, service: str, region: str, path: str, body: dict | None = None, **params
    ) -> httpx.Response:
        q = f"?{urlencode(params)}" if params else ""
        return self.request(
            "POST", self.url(service, region, path) + q, content=json.dumps(body or {}).encode()
        )

    def post_all(self, service: str, region: str, path: str, body: dict) -> list:
        out, page = [], None
        while True:
            r = self.post(service, region, path, body, **({"page": page} if page else {}))
            data = _json(r)
            out += data.get("items", []) if isinstance(data, dict) else data
            page = r.headers.get("opc-next-page")
            if not page:
                return out
