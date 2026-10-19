"""Database engine and session handling (SQLAlchemy 2.0 + SQLite)."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)
_engine: Engine | None = None


def init_engine(url: str) -> Engine:
    """Create the engine. "sqlite://" gives a shared in-memory DB (used in tests)."""
    global _engine
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_engine() has not been called")
    return _engine


def create_all() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())


def drop_all() -> None:
    from app import models  # noqa: F401

    Base.metadata.drop_all(get_engine())


SCHEMA_VERSION = "5"  # bump when tables change; the demo database is rebuilt automatically


def ensure_schema() -> bool:
    """Create tables; if an older demo database is found, drop and recreate it. Returns True if rebuilt."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    current = None
    if "app_meta" in tables:
        with engine.connect() as conn:
            current = conn.execute(text("SELECT value FROM app_meta WHERE key = 'schema_version'")).scalar()
    rebuilt = bool(tables) and current != SCHEMA_VERSION
    if rebuilt:
        drop_all()
    create_all()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_meta WHERE key = 'schema_version'"))
        conn.execute(text("INSERT INTO app_meta (key, value) VALUES ('schema_version', :v)"), {"v": SCHEMA_VERSION})
    return rebuilt


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed on success."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
