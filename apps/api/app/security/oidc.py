"""Cliente OIDC mínimo para o Keycloak (Authorization Code + PKCE, cliente confidencial)."""

import base64
import hashlib
import secrets
import time
from functools import lru_cache
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError

from app.core.config import Settings, get_settings


class OIDCError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OIDCClient:
    def __init__(self, settings: Settings):
        self.s = settings
        issuer = settings.oidc_issuer.rstrip("/")
        internal = (settings.oidc_internal_base or issuer).rstrip("/")
        self.issuer = issuer
        # O browser usa o endereço público; chamadas servidor→servidor podem usar o interno.
        self.authorization_endpoint = f"{issuer}/protocol/openid-connect/auth"
        self.end_session_endpoint = f"{issuer}/protocol/openid-connect/logout"
        self.token_endpoint = f"{internal}/protocol/openid-connect/token"
        self.jwks_uri = f"{internal}/protocol/openid-connect/certs"
        self._jwks: tuple[float, object] | None = None

    def authorize_url(
        self, *, state: str, nonce: str, code_challenge: str, max_age: int | None = None
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self.s.oidc_client_id,
            "redirect_uri": self.s.redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if max_age is not None:
            # max_age=0 força nova autenticação completa (senha + MFA) → step-up
            params["max_age"] = str(max_age)
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    def end_session_url(self, id_token_hint: str | None) -> str:
        params = {"client_id": self.s.oidc_client_id, "post_logout_redirect_uri": self.s.public_url}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        return f"{self.end_session_endpoint}?{urlencode(params)}"

    def exchange_code(self, code: str, verifier: str) -> dict:
        resp = httpx.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.s.redirect_uri,
                "code_verifier": verifier,
            },
            auth=(self.s.oidc_client_id, self.s.oidc_client_secret),
            timeout=10,
        )
        if resp.status_code != 200:
            raise OIDCError(f"token endpoint returned {resp.status_code}")
        return resp.json()

    def _keys(self):
        if self._jwks and time.time() - self._jwks[0] < 3600:
            return self._jwks[1]
        resp = httpx.get(self.jwks_uri, timeout=10)
        resp.raise_for_status()
        keys = JsonWebKey.import_key_set(resp.json())
        self._jwks = (time.time(), keys)
        return keys

    def validate_id_token(self, id_token: str, nonce: str) -> dict:
        try:
            claims = jwt.decode(
                id_token,
                self._keys(),
                claims_options={
                    "iss": {"essential": True, "value": self.issuer},
                    "aud": {"essential": True, "value": self.s.oidc_client_id},
                    "exp": {"essential": True},
                    "nonce": {"essential": True, "value": nonce},
                },
            )
            claims.validate(leeway=30)
        except JoseError as e:
            raise OIDCError(f"invalid id_token: {e}") from e
        return dict(claims)


@lru_cache
def get_oidc() -> OIDCClient:
    return OIDCClient(get_settings())
