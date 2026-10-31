import os

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (registra as tabelas no metadata)
from app.core.db import Base

# Migrações rodam com o usuário dono do schema (CV_MIGRATION_DATABASE_URL);
# a aplicação usa um usuário sem privilégio de DDL e sujeito ao RLS.
url = os.environ.get("CV_MIGRATION_DATABASE_URL") or os.environ.get("CV_DATABASE_URL")
target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
