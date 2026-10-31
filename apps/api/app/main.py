from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.routers import (
    admin_accounts,
    admin_users,
    audit,
    auth,
    connections,
    documents,
    health,
    inventory,
    reports,
    tenants,
)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Cache-Control", "no-store")
        if get_settings().cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return resp


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Cloud View API",
        version="0.1.0",
        docs_url="/api/docs" if settings.env == "dev" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.env == "dev" else None,
    )
    app.add_middleware(SecurityHeaders)
    # Tudo sob /api: CloudFront/ALB roteiam /api/* para este serviço sem reescrever o caminho
    for r in (
        health.router,
        auth.router,
        tenants.router,
        reports.router,
        inventory.router,
        documents.router,
        admin_users.router,
        admin_accounts.router,
        connections.router,
        audit.router,
    ):
        app.include_router(r, prefix="/api")
    return app


app = create_app()
