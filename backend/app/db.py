"""SQLite 엔진과 세션."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect
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
    _add_compatible_columns(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def _add_compatible_columns(engine) -> None:
    """기존 v0.1 SQLite 파일에 안전한 추가 컬럼만 보강한다.

    create_all 은 이미 존재하는 테이블을 변경하지 않으므로, 실행 스냅샷과
    첨부 역할 컬럼은 작은 호환 마이그레이션으로 추가한다.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    job_columns = (
        {c["name"] for c in inspector.get_columns("execution_jobs")}
        if "execution_jobs" in tables
        else set()
    )
    attachment_columns = (
        {c["name"] for c in inspector.get_columns("attachments")}
        if "attachments" in tables
        else set()
    )
    with engine.begin() as connection:
        if "claim_text" not in job_columns and job_columns:
            connection.exec_driver_sql(
                "ALTER TABLE execution_jobs "
                "ADD COLUMN claim_text TEXT NOT NULL DEFAULT ''"
            )
        if "role" not in attachment_columns and attachment_columns:
            connection.exec_driver_sql(
                "ALTER TABLE attachments "
                "ADD COLUMN role VARCHAR(30) NOT NULL DEFAULT 'SUPPLEMENTAL'"
            )


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
