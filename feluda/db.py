"""Database engine and session handling (SQLite for local runs, Postgres in production)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import app_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    # Naive UTC: SQLite drops tzinfo, so keep every stored timestamp naive.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        url = app_settings().database_url
        if url.startswith("sqlite"):
            app_settings().data_dir.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(conn, _record):  # pragma: no cover - driver hook
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            _engine = create_engine(url, pool_pre_ping=True)
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from . import records  # noqa: F401  (register tables)

    Base.metadata.create_all(engine())


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session() -> Iterator[Session]:
    engine()
    assert _factory is not None
    s = _factory()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def is_postgres() -> bool:
    return engine().dialect.name == "postgresql"
