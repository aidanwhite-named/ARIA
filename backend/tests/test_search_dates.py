"""선택적 검색 기준일.

무엇을 고정하는가
-----------------
1. 비어 있으면 날짜 조건이 **없다.** 실행일이 대신 들어가지 않는다.
2. 판정은 공개일로만 한다. 출원일·우선일은 보지 않는다.
3. 공개일을 확인할 수 없는 후보는 지우지 않고 '공개일 미확인'으로 남긴다.
4. 기준일·적용 여부·제외된 후보·사유가 감사 기록에 남는다.
5. 이 필드를 모르는 옛 클라이언트와 옛 실행 기록이 그대로 동작한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from app import search_dates, search_manifest, search_report
from app.db import _add_compatible_columns


# --- 입력 정규화 -----------------------------------------------------------


def test_an_empty_cutoff_means_no_date_limit() -> None:
    """비어 있음은 오늘이 아니다. 이 구분이 이 기능의 전부다."""
    for value in (None, "", "   "):
        assert search_dates.normalize_cutoff(value) == search_dates.NO_CUTOFF
    assert search_dates.describe("") == "날짜 제한 없음"


def test_both_iso_and_compact_dates_are_accepted() -> None:
    assert search_dates.normalize_cutoff("2024-12-31") == "2024-12-31"
    assert search_dates.normalize_cutoff("20241231") == "2024-12-31"
    assert search_dates.to_compact("2024-12-31") == "20241231"


def test_unreadable_dates_are_refused_not_guessed() -> None:
    """모르는 표기를 추측해 받지 않는다. 추측이 검색 범위를 바꾼다."""
    for value in ("2024/12/31", "31-12-2024", "2024-13-01", "2024-02-30", "어제"):
        with pytest.raises(search_dates.DateInputError):
            search_dates.normalize_cutoff(value)


# --- 공개일 판정 -----------------------------------------------------------


def test_publication_dates_come_in_three_shapes() -> None:
    """EPO 는 YYYYMMDD, Crossref 는 부분 날짜도 준다."""
    assert search_dates.parse_publication_date("20260115") == ("2026-01-15", "day")
    assert search_dates.parse_publication_date("2026-01-15") == ("2026-01-15", "day")
    assert search_dates.parse_publication_date("2026-01") == ("2026-01", "month")
    assert search_dates.parse_publication_date("2026") == ("2026", "year")
    assert search_dates.parse_publication_date("") == ("", "")
    assert search_dates.parse_publication_date("미상") == ("", "")


def test_without_a_cutoff_nothing_is_judged() -> None:
    verdict = search_dates.evaluate("20260115", "")
    assert verdict.status == search_dates.STATUS_NO_LIMIT
    assert verdict.excluded is False


def test_only_documents_published_after_the_cutoff_are_excluded() -> None:
    within = search_dates.evaluate("20241230", "2024-12-31")
    after = search_dates.evaluate("20260108", "2024-12-31")

    assert within.status == search_dates.STATUS_WITHIN
    assert within.excluded is False
    assert after.status == search_dates.STATUS_AFTER
    assert after.excluded is True


def test_an_unknown_publication_date_is_not_an_exclusion() -> None:
    """공개일을 모르는 것과 기준일 뒤에 공개된 것은 다른 사실이다."""
    verdict = search_dates.evaluate("", "2024-12-31")
    assert verdict.status == search_dates.STATUS_UNKNOWN
    assert verdict.excluded is False
    assert "공개일 미확인" in search_dates.STATUS_LABELS[verdict.status]


def test_a_partial_date_that_straddles_the_cutoff_is_not_excluded() -> None:
    """연도만 아는 문헌을 기준일 뒤로 단정하지 않는다."""
    verdict = search_dates.evaluate("2024", "2024-06-30")
    assert verdict.status == search_dates.STATUS_AMBIGUOUS
    assert verdict.excluded is False
    # 기간 전체가 기준일 뒤면 그때는 확정할 수 있다.
    assert search_dates.evaluate("2025", "2024-06-30").excluded is True
    # 기간 전체가 기준일 앞이면 통과다.
    assert search_dates.evaluate("2023", "2024-06-30").status == (
        search_dates.STATUS_WITHIN
    )


# --- 후보 목록에 적용 -------------------------------------------------------


def _candidate(index: int, number: str, published: str = "") -> dict:
    return {
        "index": index,
        "doc_number": number,
        "doi": "",
        "title": f"{number} 명칭",
        "publication_date": published,
    }


def test_no_cutoff_keeps_every_candidate_and_says_so() -> None:
    reported = {
        "candidates": [
            _candidate(1, "US1111111B2", "20200101"),
            _candidate(2, "US2222222B2", "20991231"),
            _candidate(3, "US3333333B2"),
        ]
    }
    record = search_dates.filter_candidates(reported, "")

    assert len(reported["candidates"]) == 3
    assert record["applied"] is False
    assert record["cutoff"] == ""
    assert record["excluded"] == []
    # 통과한 후보에도 상태가 남는다. "날짜를 봤다"와 "날짜가 없었다"는 다르다.
    assert all(
        item["publication_date_status"] == search_dates.STATUS_NO_LIMIT
        for item in reported["candidates"]
    )


def test_a_cutoff_removes_only_later_publications_and_records_why() -> None:
    reported = {
        "candidates": [
            _candidate(1, "US1111111B2", "20200101"),
            _candidate(2, "US2222222B2", "2026-01-08"),
            _candidate(3, "US3333333B2"),
        ]
    }
    record = search_dates.filter_candidates(reported, "2024-12-31")

    assert [item["doc_number"] for item in reported["candidates"]] == [
        "US1111111B2",
        "US3333333B2",
    ]
    assert record["applied"] is True
    assert record["basis"] == "publication_date"
    assert [row["doc_number"] for row in record["excluded"]] == ["US2222222B2"]
    excluded = record["excluded"][0]
    assert excluded["reason_code"] == search_dates.EXCLUDE_REASON
    assert "2026-01-08" in excluded["detail"]
    assert "2024-12-31" in excluded["detail"]
    # 공개일 미확인은 제외가 아니라 표시다.
    assert record["unknown_publication_date"] == 1
    kept = reported["candidates"][1]
    assert kept["publication_date_status"] == search_dates.STATUS_UNKNOWN


def test_the_publication_date_is_read_from_every_channel() -> None:
    """채널마다 공개일이 다른 자리에 온다. 한 곳에서만 읽으면 웹 후보가 샌다."""
    assert search_dates.candidate_publication_date({"publication_date": "20200101"})
    assert search_dates.candidate_publication_date(
        {"official_evidence": {"publication_date": "20200101"}}
    )
    assert search_dates.candidate_publication_date(
        {"epo_discovery": {"publication_date": "20200101"}}
    )
    assert search_dates.candidate_publication_date(
        {"literature_discovery": {"publication_date": "2020-01-01"}}
    )
    assert search_dates.candidate_publication_date({}) == ""


# --- 감사 기록과 보고서 -----------------------------------------------------


def _manifest(date_filter: dict) -> dict:
    return search_manifest.build(
        claim_text="청구항 1. 센서.",
        prompt_id="search_prompt.md",
        prompt_sha256="0" * 64,
        claim_boundary_neutralized=False,
        started_at=None,
        completed_at=None,
        tool_calls=[],
        tool_uses=[],
        tool_policy_name="search",
        allowed_tools=[],
        reported={"candidates": [], "rounds": [], "access_failures": []},
        notes=[],
        error=None,
        date_filter=date_filter,
    )


def test_the_manifest_keeps_the_cutoff_even_when_it_is_empty() -> None:
    manifest = _manifest(search_dates.empty_section())
    assert manifest["date_filter"]["cutoff"] == ""
    assert manifest["date_filter"]["applied"] is False


def test_the_report_says_no_date_limit_when_there_is_none() -> None:
    """아무 말도 하지 않으면 읽는 사람이 실행일까지로 잘렸다고 짐작한다."""
    report = search_report.render(_manifest(search_dates.empty_section()))
    assert "## 검색 기준일" in report
    assert "날짜 제한 없음" in report
    assert "공개일(publication date)" in report


def test_the_report_lists_the_cutoff_and_the_documents_it_removed() -> None:
    reported = {
        "candidates": [
            _candidate(1, "US1111111B2", "20200101"),
            _candidate(2, "US20260010642A1", "2026-01-08"),
        ]
    }
    record = search_dates.filter_candidates(reported, "2024-12-31")
    report = search_report.render(_manifest(record))

    assert "2024-12-31 까지 공개된 문헌" in report
    assert "기준일 뒤에 공개돼 제외한 후보" in report
    assert "US20260010642A1" in report


# --- 옛 기록·옛 DB 호환 -----------------------------------------------------


def test_a_manifest_without_a_date_filter_still_renders() -> None:
    """옛 실행 기록에는 이 키가 없다. 그 기록도 계속 열려야 한다."""
    manifest = _manifest(search_dates.empty_section())
    del manifest["date_filter"]
    report = search_report.render(manifest)
    assert "날짜 제한 없음" in report


def test_the_cutoff_column_is_added_to_an_existing_database(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'pre-cutoff.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE execution_jobs ("
            "  id VARCHAR(36) PRIMARY KEY,"
            "  claim_text TEXT NOT NULL DEFAULT ''"
            ")"
        )
        connection.exec_driver_sql("INSERT INTO execution_jobs (id) VALUES ('old')")

    _add_compatible_columns(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("execution_jobs")}
    assert "search_cutoff_date" in columns
    assert "search_depth" in columns
    with engine.connect() as connection:
        stored = connection.exec_driver_sql(
            "SELECT search_cutoff_date FROM execution_jobs WHERE id = 'old'"
        ).scalar_one()
    # NULL 이 기본값이다. 그 실행은 날짜 조건 없이 돌았고, 그것이 사실이다.
    assert stored is None
    engine.dispose()
