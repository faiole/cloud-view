"""Cliente mínimo da Admin REST API do Keycloak (client credentials do `cloudview-admin-api`).

O service account desse cliente só tem `manage-users`, `view-users` e `query-users` no realm
`cloudview` — não administra clientes, fluxos nem outros realms.
"""

import time
from functools import lru_cache

import httpx

from app.core.config import Settings, get_settings


class KeycloakAdminError(Exception):
    pass


class KeycloakAdmin:
    def __init__(self, settings: Settings):
        self.s = settings
        realm_base = (settings.oidc_internal_base or settings.oidc_issuer).rstrip("/")
        self.token_url = f"{realm_base}/protocol/openid-connect/token"
        # .../realms/cloudview  →  .../admin/realms/cloudview
        root, realm = realm_base.rsplit("/realms/", 1)
        self.base = f"{root}/admin/realms/{realm}"
        self._token: tuple[float, str] | None = None

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token[0]:
            r = httpx.post(
                self.token_url,
                data={"grant_type": "client_credentials"},
                auth=(self.s.kc_admin_client_id, self.s.kc_admin_client_secret),
                timeout=10,
            )
            if r.status_code != 200:
                raise KeycloakAdminError(f"admin token: {r.status_code}")
            body = r.json()
            self._token = (time.time() + body.get("expires_in", 60) - 15, body["access_token"])
        return {"Authorization": f"Bearer {self._token[1]}"}

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        r = httpx.request(method, f"{self.base}{path}", headers=self._auth(), timeout=15, **kw)
        if r.status_code >= 400:
            raise KeycloakAdminError(f"{method} {path}: {r.status_code}")
        return r

    def find_user_id(self, email: str) -> str | None:
        users = self._req("GET", "/users", params={"email": email, "exact": "true"}).json()
        return users[0]["id"] if users else None

    def create_user(self, email: str, first_name: str | None, last_name: str | None) -> str:
        self._req(
            "POST",
            "/users",
            json={
                "username": email,
                "email": email,
                "firstName": first_name or "",
                "lastName": last_name or "",
                "enabled": True,
                "emailVerified": False,
                "requiredActions": ["VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP"],
            },
        )
        uid = self.find_user_id(email)
        if not uid:
            raise KeycloakAdminError("user created but not found")
        return uid

    def send_setup_email(self, user_id: str) -> None:
        """E-mail com link para definir senha, confirmar o e-mail e cadastrar o TOTP."""
        self._req(
            "PUT",
            f"/users/{user_id}/execute-actions-email",
            params={
                "client_id": self.s.oidc_client_id,
                "redirect_uri": self.s.public_url,
                "lifespan": 72 * 3600,
            },
            json=["VERIFY_EMAIL", "UPDATE_PASSWORD", "CONFIGURE_TOTP"],
        )

    def set_enabled(self, user_id: str, enabled: bool) -> None:
        self._req("PUT", f"/users/{user_id}", json={"enabled": enabled})

    def reset_mfa(self, user_id: str) -> None:
        """Remove o TOTP atual; no próximo login o usuário cadastra um novo."""
        for cred in self._req("GET", f"/users/{user_id}/credentials").json():
            if cred.get("type") == "otp":
                self._req("DELETE", f"/users/{user_id}/credentials/{cred['id']}")
        user = self._req("GET", f"/users/{user_id}").json()
        actions = set(user.get("requiredActions") or []) | {"CONFIGURE_TOTP"}
        self._req("PUT", f"/users/{user_id}", json={"requiredActions": sorted(actions)})

    def logout(self, user_id: str) -> None:
        self._req("POST", f"/users/{user_id}/logout")

    def set_platform_admin(self, user_id: str, value: bool) -> None:
        role = self._req("GET", f"/roles/{self.s.platform_admin_role}").json()
        method = "POST" if value else "DELETE"
        self._req(method, f"/users/{user_id}/role-mappings/realm", json=[role])


@lru_cache
def get_kc_admin() -> KeycloakAdmin:
    return KeycloakAdmin(get_settings())
