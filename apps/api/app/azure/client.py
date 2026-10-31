"""Acesso ao Azure via REST (sem SDK): token de app (client credentials), ARM e Resource Graph.

Autenticação do app:
- certificado (recomendado): client assertion JWT RS256 assinado com a chave privada;
- client secret.

O "app hub" é um app multi-tenant da plataforma: cada cliente dá admin consent no próprio tenant
e atribui Reader / Cost Management Reader ao service principal. O token é pedido com a
autoridade do tenant do cliente, então nenhum segredo por cliente é necessário.
"""

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy.orm import Session

from app.models import Connection, PlatformSetting
from app.services.secrets import read_secret

LOGIN = "https://login.microsoftonline.com"
ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
AZURE_HUB_KEY = "azure_hub_secret_id"  # noqa: S105 - nome da configuração


class AzureError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class AzureCredential:
    client_id: str
    client_secret: str | None = None
    certificate_pem: str | None = None
    private_key_pem: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "AzureCredential":
        return cls(
            client_id=data["client_id"],
            client_secret=data.get("client_secret"),
            certificate_pem=data.get("certificate_pem"),
            private_key_pem=data.get("private_key_pem"),
        )


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def client_assertion(cred: AzureCredential, token_url: str) -> str:
    cert = x509.load_pem_x509_certificate(cred.certificate_pem.encode())
    key = serialization.load_pem_private_key(cred.private_key_pem.encode(), password=None)
    thumb = cert.fingerprint(hashes.SHA1())  # noqa: S303 - formato exigido pelo Entra ID (x5t)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "x5t": _b64url(thumb)}
    payload = {
        "aud": token_url,
        "iss": cred.client_id,
        "sub": cred.client_id,
        "jti": str(uuid.uuid4()),
        "nbf": now - 60,
        "exp": now + 600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(sig)}"


class AzureHub:
    """Obtém tokens por tenant e devolve clientes ARM. `transport` permite testes sem rede."""

    def __init__(self, credential: AzureCredential | None, transport: httpx.BaseTransport | None = None):
        self.credential = credential
        self.http = httpx.Client(transport=transport, timeout=60)
        self._tokens: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.sleep = time.sleep

    @classmethod
    def from_settings(cls, db: Session, vault, transport=None) -> "AzureHub":
        s = db.get(PlatformSetting, AZURE_HUB_KEY)
        cred = AzureCredential.from_json(json.loads(read_secret(db, vault, s.value))) if s else None
        return cls(cred, transport)

    def credential_for(self, db: Session, vault, conn: Connection) -> AzureCredential:
        if conn.secret_ref:  # app próprio do cliente
            return AzureCredential.from_json(json.loads(read_secret(db, vault, conn.secret_ref)))
        if self.credential is None:
            raise AzureError("app hub do Azure não configurado (tela Conexões)")
        return self.credential

    def token(self, tenant_id: str, cred: AzureCredential, scope: str = ARM_SCOPE) -> str:
        key = (tenant_id, cred.client_id, scope)
        with self._lock:
            hit = self._tokens.get(key)
            if hit and hit[0] > time.time() + 120:
                return hit[1]
        url = f"{LOGIN}/{tenant_id}/oauth2/v2.0/token"
        form = {"grant_type": "client_credentials", "client_id": cred.client_id, "scope": scope}
        if cred.certificate_pem and cred.private_key_pem:
            form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            form["client_assertion"] = client_assertion(cred, url)
        elif cred.client_secret:
            form["client_secret"] = cred.client_secret
        else:
            raise AzureError("credencial do app sem secret nem certificado")
        r = self.http.post(url, data=form)
        if r.status_code != 200:
            body = _json(r)
            code = (body.get("error") or "").strip()
            raise AzureError(
                f"login no tenant {tenant_id} falhou: {code} {body.get('error_description', '')[:300]}",
                r.status_code,
                code,
            )
        body = r.json()
        with self._lock:
            self._tokens[key] = (time.time() + int(body.get("expires_in", 3600)), body["access_token"])
        return body["access_token"]

    def arm(self, tenant_id: str, cred: AzureCredential) -> "Arm":
        return Arm(self, tenant_id, cred)


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


class Arm:
    MAX_TRIES = 5

    def __init__(self, hub: AzureHub, tenant_id: str, cred: AzureCredential):
        self.hub, self.tenant_id, self.cred = hub, tenant_id, cred

    def _request(self, method: str, url: str, *, scope: str = ARM_SCOPE, **kw) -> httpx.Response:
        if url.startswith("/"):
            url = ARM + url
        for attempt in range(self.MAX_TRIES):
            headers = {
                "Authorization": f"Bearer {self.hub.token(self.tenant_id, self.cred, scope)}",
                **kw.pop("headers", {}),
            }
            r = self.hub.http.request(method, url, headers=headers, **kw)
            if r.status_code in (429, 503) and attempt < self.MAX_TRIES - 1:
                # Cost Management e Resource Graph limitam bastante; respeitar o Retry-After
                wait = next(
                    (r.headers[h] for h in r.headers if h.lower().endswith("retry-after")),
                    str(2**attempt),
                )
                self.hub.sleep(min(float(wait), 60))
                continue
            if r.status_code >= 400:
                err = _json(r).get("error") or {}
                raise AzureError(
                    f"{method} {url.split('?')[0].replace(ARM, '')}: {r.status_code} {err.get('code', '')} {err.get('message', '')[:300]}".strip(),
                    r.status_code,
                    err.get("code"),
                )
            return r
        raise AzureError("limite de requisições do Azure", 429)

    def get(self, path: str, api_version: str, **params) -> dict:
        q = urlencode({"api-version": api_version, **params})
        return _json(self._request("GET", f"{path}?{q}"))

    def post(self, path: str, api_version: str, body: dict | None = None, **params) -> httpx.Response:
        q = urlencode({"api-version": api_version, **params})
        return self._request("POST", f"{path}?{q}", json=body if body is not None else {})

    def list_all(self, path: str, api_version: str, **params) -> list[dict]:
        out, page = [], self.get(path, api_version, **params)
        while True:
            out += page.get("value", [])
            nxt = page.get("nextLink")
            if not nxt:
                return out
            page = _json(self._request("GET", nxt))

    def graph(self, query: str, subscriptions: list[str]) -> list[dict]:
        """Resource Graph com paginação por $skipToken."""
        out, opts = [], {"resultFormat": "objectArray", "$top": 1000}
        while True:
            body = {"subscriptions": subscriptions, "query": query, "options": opts}
            page = _json(
                self._request(
                    "POST", "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01", json=body
                )
            )
            out += page.get("data", [])
            token = page.get("$skipToken")
            if not token:
                return out
            opts = {**opts, "$skipToken": token}

    def get_url(self, url: str, scope: str = STORAGE_SCOPE, headers: dict | None = None) -> httpx.Response:
        return self._request("GET", url, scope=scope, headers=headers or {})
