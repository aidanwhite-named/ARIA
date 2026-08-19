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
