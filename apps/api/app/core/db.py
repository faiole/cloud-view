from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_tenant_scope(db: Session, *, tenant_ids: list[str], is_admin: bool) -> None:
    """Define as variáveis usadas pelas políticas de Row-Level Security do Postgres.

    `set_config(..., true)` vale só para a transação corrente, então não vaza entre requests
    que reutilizam a mesma conexão do pool.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT set_config('app.tenant_ids', :ids, true), set_config('app.is_admin', :adm, true)"),
        {"ids": ",".join(tenant_ids), "adm": "on" if is_admin else "off"},
    )
