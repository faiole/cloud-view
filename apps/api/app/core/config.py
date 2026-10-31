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
    public_url: str = "http://localhost:8080"

    # OIDC / Keycloak
    oidc_issuer: str = "http://localhost:8080/realms/cloudview"
    # Endereço interno para chamadas servidor→Keycloak (no docker-compose difere do issuer público)
    oidc_internal_base: str | None = None
    oidc_client_id: str = "cloudview-bff"
    oidc_client_secret: str = Field(default="change-me", repr=False)
    platform_admin_role: str = "platform-admin"

    # Cliente de serviço do Keycloak usado para administrar usuários (convite, desativar, reset MFA)
    kc_admin_client_id: str = "cloudview-admin-api"
    kc_admin_client_secret: str = Field(default="change-me", repr=False)

    # Cofre de credenciais das nuvens (ver app/vault.py). A API recebe só a chave pública.
    vault_public_key: str | None = "/run/vault-public/vault-public.pem"
    vault_private_keys: str | None = None  # caminhos separados por vírgula (somente workers)

    # Documentos (invoices / notas fiscais)
    storage_backend: str = "local"  # local | s3 (qualquer S3 compatível: AWS, MinIO, OCI, R2...)
    storage_local_dir: str = "/data/documents"
    # Chave AES-256 (base64) para cifrar os documentos no disco local; vazio = sem cifra própria
    documents_key: str | None = Field(default=None, repr=False)
    documents_bucket: str | None = None
    s3_endpoint_url: str | None = None
    documents_kms_key_id: str | None = None
    max_document_bytes: int = 20 * 1024 * 1024
    max_import_bytes: int = 500 * 1024 * 1024

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
