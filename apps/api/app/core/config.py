from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração via variáveis de ambiente (prefixo CV_)."""

    model_config = SettingsConfigDict(env_prefix="CV_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://cloudview_app:cloudview_app@localhost:5432/cloudview"
    redis_url: str = "redis://localhost:6379/0"

    # URL pública do painel (onde o browser acessa o front e /api)
    public_url: str = "http://localhost:3000"

    # OIDC / Keycloak
    oidc_issuer: str = "http://localhost:8080/realms/cloudview"
    # Endereço interno para chamadas servidor→Keycloak (no docker-compose difere do issuer público)
    oidc_internal_base: str | None = None
    oidc_client_id: str = "cloudview-bff"
    oidc_client_secret: str = Field(default="change-me", repr=False)
    platform_admin_role: str = "platform-admin"

    # Sessão
    session_ttl_seconds: int = 15 * 60  # inatividade
    session_absolute_seconds: int = 8 * 60 * 60  # duração máxima
    step_up_max_age_seconds: int = 5 * 60  # MFA recente exigido em ações sensíveis
    cookie_secure: bool = True

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
