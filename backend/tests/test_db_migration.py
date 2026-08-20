"""기존 사용자 SQLite 파일의 무중단 호환 마이그레이션."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from app.db import _add_compatible_columns


def test_adds_claim_text_and_attachment_role_to_existing_database(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE execution_jobs (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE attachments (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO execution_jobs (id) VALUES ('job-1')")
        connection.exec_driver_sql("INSERT INTO attachments (id) VALUES ('file-1')")

    _add_compatible_columns(engine)

    inspector = inspect(engine)
    assert "claim_text" in {
        column["name"] for column in inspector.get_columns("execution_jobs")
    }
    assert "role" in {
        column["name"] for column in inspector.get_columns("attachments")
    }
    with engine.connect() as connection:
        claim_text = connection.exec_driver_sql(
            "SELECT claim_text FROM execution_jobs WHERE id = 'job-1'"
        ).scalar_one()
        role = connection.exec_driver_sql(
            "SELECT role FROM attachments WHERE id = 'file-1'"
        ).scalar_one()
    assert claim_text == ""
    assert role == "SUPPLEMENTAL"
    engine.dispose()


def test_removes_legacy_user_input_column(tmp_path) -> None:
    """v0.1 의 user_input 은 NOT NULL 이라 남아 있으면 INSERT 가 전부 실패한다."""
    engine = create_engine(f"sqlite:///{tmp_path / 'v01.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE execution_jobs ("
            "  id VARCHAR(36) NOT NULL PRIMARY KEY,"
            "  user_input TEXT NOT NULL,"
            "  provider VARCHAR(30) NOT NULL"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO execution_jobs (id, user_input, provider) "
            "VALUES ('job-1', '청구항 1. 프로세서를 포함하는 장치.', 'agy')"
        )

    _add_compatible_columns(engine)

    inspector = inspect(engine)
    assert "user_input" not in {
        column["name"] for column in inspector.get_columns("execution_jobs")
    }
    with engine.connect() as connection:
        # 남아 있던 입력은 버리지 않고 claim_text 로 옮긴다.
        claim_text = connection.exec_driver_sql(
            "SELECT claim_text FROM execution_jobs WHERE id = 'job-1'"
        ).scalar_one()
        # 현재 모델처럼 user_input 없이 INSERT 해도 통과해야 한다.
        connection.exec_driver_sql(
            "INSERT INTO execution_jobs (id, provider) VALUES ('job-2', 'agy')"
        )
    assert claim_text == "청구항 1. 프로세서를 포함하는 장치."
    engine.dispose()


def test_adds_lineage_columns_to_existing_database(tmp_path) -> None:
    """기존 실행은 독립 실행이므로 relation_type 이 NULL 로 남아야 한다."""
    engine = create_engine(f"sqlite:///{tmp_path / 'pre-lineage.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE execution_jobs ("
            "  id VARCHAR(36) NOT NULL PRIMARY KEY,"
            "  claim_text TEXT NOT NULL DEFAULT '',"
            "  provider VARCHAR(30) NOT NULL"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO execution_jobs (id, provider) VALUES ('job-1', 'agy')"
        )

    _add_compatible_columns(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("execution_jobs")}
    assert {
        "source_job_id",
        "source_job_label",
        "relation_type",
        "followup_instruction",
        "prior_claim_text",
        "prior_report",
    } <= columns

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT source_job_id, relation_type, source_job_label, prior_report "
            "FROM execution_jobs WHERE id = 'job-1'"
        ).one()
    assert row == (None, None, "", "")

    # 두 번 돌려도 안전해야 한다. 앱은 시작할 때마다 이 함수를 호출한다.
    _add_compatible_columns(engine)
    engine.dispose()
