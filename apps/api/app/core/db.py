from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
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


def _set_scope(conn, scope: tuple[str, str]) -> None:
    conn.execute(
        text("SELECT set_config('app.tenant_ids', :ids, true), set_config('app.is_admin', :adm, true)"),
        {"ids": scope[0], "adm": scope[1]},
    )


@event.listens_for(Session, "after_begin")
def _reapply_scope(session: Session, transaction, connection) -> None:
    """`set_config(..., true)` vale só até o fim da transação. Toda transação nova da mesma sessão
    (por exemplo, depois de um commit) recebe o escopo de novo; sem isso o RLS esconderia tudo."""
    scope = session.info.get("tenant_scope")
    if scope and connection.dialect.name == "postgresql":
        _set_scope(connection, scope)


def apply_tenant_scope(db: Session, *, tenant_ids: list[str], is_admin: bool) -> None:
    """Define as variáveis usadas pelas políticas de Row-Level Security do Postgres.

    O escopo fica guardado na sessão e é reaplicado a cada transação (ver `_reapply_scope`), sem
    vazar para outras requisições que reutilizam a mesma conexão do pool.
    """
    scope = (",".join(tenant_ids), "on" if is_admin else "off")
    db.info["tenant_scope"] = scope
    if db.get_bind().dialect.name != "postgresql":
        return
    if db.in_transaction():
        _set_scope(db.connection(), scope)
    else:
        db.connection()  # inicia a transação; o listener aplica o escopo
