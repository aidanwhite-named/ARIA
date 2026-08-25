"""증거 보존 생애주기.

증거는 작업에 딸린 것이다. 작업이 사라지면 증거도 사라져야 한다 — 안 그러면
사용자가 "모든 이력 삭제"를 눌러도 특허 원문이 디스크에 남는다.

동시에, 내용 주소 저장소라 여러 작업이 같은 응답을 공유할 수 있다. 그래서
작업 하나를 지웠다고 바로 지우면 안 되고 참조가 0일 때만 지운다.
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Base, EvidenceReference, ExecutionJob
from app.patent_search import retention
from app.patent_search.artifacts import ArtifactStore


@pytest.fixture()
def db_session(tmp_path):
    """이 테스트 전용 SQLite. FK CASCADE 를 확인해야 하므로 PRAGMA 를 켠다."""
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def store(tmp_path):
    return ArtifactStore(tmp_path / "evidence")


def _job(session, job_id: str) -> ExecutionJob:
    job = ExecutionJob(
        id=job_id,
        provider="claude",
        status="succeeded",
        claim_text="청구항",
    )
    session.add(job)
    session.flush()
    return job


def test_reference_is_idempotent(db_session):
    _job(db_session, "job-1")
    aid = "a" * 64
    retention.reference(db_session, "job-1", aid)
    retention.reference(db_session, "job-1", aid)
    db_session.flush()
    rows = db_session.query(EvidenceReference).all()
    assert len(rows) == 1


def test_unreferenced_artifact_is_collected(db_session, store):
    aid = store.put(b"orphan response")
    assert store.exists(aid)
    removed = retention.collect_unreferenced(db_session, store)
    assert removed == 1
    assert not store.exists(aid)


def test_referenced_artifact_survives(db_session, store):
    _job(db_session, "job-1")
    aid = store.put(b"referenced response")
    retention.reference(db_session, "job-1", aid)
    db_session.flush()

    removed = retention.collect_unreferenced(db_session, store)
    assert removed == 0
    assert store.exists(aid)


def test_shared_artifact_survives_until_last_reference_goes(db_session, store):
    """두 작업이 같은 응답을 공유하면, 하나를 지워도 남아야 한다."""
    _job(db_session, "job-1")
    _job(db_session, "job-2")
    aid = store.put(b"shared response")
    retention.reference(db_session, "job-1", aid)
    retention.reference(db_session, "job-2", aid)
    db_session.flush()

    # job-1 삭제 → 아직 job-2 가 참조 중
    db_session.delete(db_session.get(ExecutionJob, "job-1"))
    db_session.flush()
    assert retention.collect_unreferenced(db_session, store) == 0
    assert store.exists(aid)

    # job-2 까지 삭제 → 이제 아무도 안 봄
    db_session.delete(db_session.get(ExecutionJob, "job-2"))
    db_session.flush()
    assert retention.collect_unreferenced(db_session, store) == 1
    assert not store.exists(aid)


def test_reference_rows_cascade_with_job(db_session, store):
    _job(db_session, "job-1")
    aid = store.put(b"cascade response")
    retention.reference(db_session, "job-1", aid)
    db_session.flush()
    assert db_session.query(EvidenceReference).count() == 1

    db_session.delete(db_session.get(ExecutionJob, "job-1"))
    db_session.flush()
    assert db_session.query(EvidenceReference).count() == 0


def test_foreign_files_are_left_alone(db_session, store):
    """저장소 규칙에 맞지 않는 파일은 우리가 만든 게 아니므로 건드리지 않는다."""
    store.root.mkdir(parents=True, exist_ok=True)
    shard = store.root / "zz"
    shard.mkdir()
    stray = shard / "not-an-artifact.txt"
    stray.write_text("사람이 넣어 둔 파일", encoding="utf-8")

    retention.collect_unreferenced(db_session, store)
    assert stray.is_file()


def test_collect_on_empty_store_is_safe(db_session, store):
    assert retention.collect_unreferenced(db_session, store) == 0
