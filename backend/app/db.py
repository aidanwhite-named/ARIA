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


# 후속 분석 계보 컬럼. relation_type 만 NULL 을 허용한다 (독립 실행).
_LINEAGE_COLUMNS = (
    ("source_job_id", "source_job_id VARCHAR(36)"),
    ("source_job_label", "source_job_label TEXT NOT NULL DEFAULT ''"),
    ("relation_type", "relation_type VARCHAR(20)"),
    ("followup_instruction", "followup_instruction TEXT NOT NULL DEFAULT ''"),
    ("prior_claim_text", "prior_claim_text TEXT NOT NULL DEFAULT ''"),
    ("prior_report", "prior_report TEXT NOT NULL DEFAULT ''"),
    ("citation_mapping", "citation_mapping JSON"),
    ("citation_mapping_error", "citation_mapping_error TEXT"),
    ("prior_citation_mapping", "prior_citation_mapping JSON"),
    ("prompt_capabilities", "prompt_capabilities JSON NOT NULL DEFAULT '[]'"),
    ("analysis_manifest", "analysis_manifest JSON"),
    ("analysis_manifest_error", "analysis_manifest_error TEXT"),
)

# 유사 문헌 검색 컬럼. 기존 실행은 전부 PDF 구성대비 분석이므로 job_kind 의
# 기본값이 patent_analysis 다. 검색 기록은 NULL 이 맞는 기본값이다.
_SEARCH_COLUMNS = (
    (
        "job_kind",
        "job_kind VARCHAR(30) NOT NULL DEFAULT 'patent_analysis'",
    ),
    ("search_manifest", "search_manifest JSON"),
    ("search_manifest_error", "search_manifest_error TEXT"),
    ("search_focus", "search_focus JSON"),
)

# 로컬 검색(retrieval) 컬럼. 이 기능이 없던 시절의 실행은 전부 전체 인라인
# 전달이므로 delivery_plan 기본값이 full_inline 이고, 감사 기록은 NULL 이 맞는
# 기본값이다. 과거 실행의 의미가 바뀌지 않는다.
_RETRIEVAL_COLUMNS = (
    (
        "delivery_plan",
        "delivery_plan VARCHAR(30) NOT NULL DEFAULT 'full_inline'",
    ),
    ("retrieval_manifest", "retrieval_manifest JSON"),
    ("retrieval_manifest_error", "retrieval_manifest_error TEXT"),
    # 전달 판정 기록. 없던 시절의 실행은 NULL 이고 화면은 delivery_plan 만으로
    # 예전처럼 표시한다 — 없는 사유를 지어내지 않는다.
    ("delivery_manifest", "delivery_manifest JSON"),
)


def _add_compatible_columns(engine) -> None:
    """기존 v0.1 SQLite 파일을 현재 모델에 맞춘다.

    create_all 은 이미 존재하는 테이블을 변경하지 않으므로, 실행 스냅샷과
    첨부 역할 컬럼은 작은 호환 마이그레이션으로 추가한다.

    v0.1 의 user_input 은 claim_text 로 대체됐다. 그런데 그 컬럼은 NOT NULL
    이고 기본값이 없어서, 남겨두면 현재 모델이 만드는 INSERT 에 값이 빠져
    작업 생성이 전부 IntegrityError 로 실패한다. 값을 claim_text 로 옮긴 뒤
    제거한다.

    warnings 도 같다. 성공한 실행에 덧붙이던 경고를 없앴으므로 모델에서
    빠졌는데, NOT NULL 컬럼이 그대로 남아 있으면 같은 이유로 INSERT 가
    깨진다. 지난 실행에 적혀 있던 경고 문구는 이 시점에 사라진다 — 보고서
    본문과 첨부 기록은 건드리지 않는다.

    프롬프트 버전 이력 기능을 제거했으므로 기존 prompt_version 컬럼도 함께
    정리한다. 실행 시점의 실제 본문은 prompt_snapshot에 계속 보존된다.

    후속 분석 계보 컬럼도 같은 방식으로 붙인다. 기존 실행은 독립 실행이므로
    relation_type 이 NULL, 나머지는 빈 문자열이 맞는 기본값이다.

    작업 종류(job_kind)와 검색 감사 기록도 같다. 기존 실행은 전부 PDF 구성대비
    분석이므로 job_kind 기본값이 patent_analysis 이고, 그 실행들의 도구 정책은
    예전과 똑같이 '도구 없음'으로 남는다.
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
        if "user_input" in job_columns:
            # 과거 실행 기록의 입력을 잃지 않도록 먼저 옮긴다.
            connection.exec_driver_sql(
                "UPDATE execution_jobs SET claim_text = user_input "
                "WHERE claim_text = '' AND user_input IS NOT NULL "
                "AND user_input != ''"
            )
            connection.exec_driver_sql(
                "ALTER TABLE execution_jobs DROP COLUMN user_input"
            )
        if "warnings" in job_columns:
            connection.exec_driver_sql(
                "ALTER TABLE execution_jobs DROP COLUMN warnings"
            )
        if "prompt_version" in job_columns:
            connection.exec_driver_sql(
                "ALTER TABLE execution_jobs DROP COLUMN prompt_version"
            )
        if "role" not in attachment_columns and attachment_columns:
            connection.exec_driver_sql(
                "ALTER TABLE attachments "
                "ADD COLUMN role VARCHAR(30) NOT NULL DEFAULT 'SUPPLEMENTAL'"
            )
        if "included" not in attachment_columns and attachment_columns:
            # 「분석에 포함」이 없던 시절의 첨부는 전부 프롬프트에 들어갔다.
            # 기본값 1 이 그 실행들의 기록을 그대로 유지한다.
            connection.exec_driver_sql(
                "ALTER TABLE attachments "
                "ADD COLUMN included BOOLEAN NOT NULL DEFAULT 1"
            )
        if job_columns:
            for name, ddl in (
                *_LINEAGE_COLUMNS,
                *_SEARCH_COLUMNS,
                *_RETRIEVAL_COLUMNS,
            ):
                if name not in job_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE execution_jobs ADD COLUMN {ddl}"
                    )
            if "source_job_id" not in job_columns:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_execution_jobs_source_job_id "
                    "ON execution_jobs (source_job_id)"
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
