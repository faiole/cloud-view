"""Prepara o banco em um ambiente novo e aplica as migrações.

Executado pela task ECS `migrate` (e pode ser usado localmente). Idempotente:
1. cria/atualiza os usuários `cloudview_app` (DML + RLS) e `keycloak`;
2. cria o banco do Keycloak;
3. roda `alembic upgrade head` como dono do schema.
"""

import os
from urllib.parse import quote

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql


def _ensure_role(cur, name: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone():
        cur.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    else:
        cur.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )


def main() -> None:
    host = os.environ["DB_HOST"]
    port = int(os.environ.get("DB_PORT", "5432"))
    db = os.environ.get("DB_NAME", "cloudview")
    owner, owner_pw = os.environ["DB_OWNER_USER"], os.environ["DB_OWNER_PASSWORD"]
    sslmode = os.environ.get("DB_SSLMODE", "require")
    conninfo = dict(host=host, port=port, user=owner, password=owner_pw, sslmode=sslmode)

    with psycopg.connect(dbname=db, autocommit=True, **conninfo) as conn, conn.cursor() as cur:
        _ensure_role(cur, "cloudview_app", os.environ["APP_DB_PASSWORD"])
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO cloudview_app"))
        if kc_pw := os.environ.get("KEYCLOAK_DB_PASSWORD"):
            _ensure_role(cur, "keycloak", kc_pw)
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'keycloak'")
            if not cur.fetchone():
                # No RDS o master não é superuser: precisa ser membro da role para torná-la dona
                cur.execute("GRANT keycloak TO CURRENT_USER")
                cur.execute("CREATE DATABASE keycloak OWNER keycloak")

    os.environ["CV_MIGRATION_DATABASE_URL"] = (
        f"postgresql+psycopg://{quote(owner)}:{quote(owner_pw, safe='')}@{host}:{port}/{db}?sslmode={sslmode}"
    )
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.upgrade(cfg, "head")
    print("bootstrap ok")


if __name__ == "__main__":
    main()
