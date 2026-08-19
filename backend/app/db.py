"""SQLite 엔진과 세션."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import PATHS
from .models import Base

_engine = None
_SessionLocal = None


def init_engine(db_path=None):
    global _engine, _SessionLocal
    PATHS.ensure()
    target = db_path or PATHS.db_path
    _engine = create_engine(
        f"sqlite:///{target}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(_engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - 드라이버 훅
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_sessionmaker():
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 의존성."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()
