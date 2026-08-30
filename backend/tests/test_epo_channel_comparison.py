"""웹/EPO 네 레인의 후보를 섞지 않고 대조하는 manifest v6 계약."""

from __future__ import annotations

from app import search_manifest, search_report


def _reported(*candidates: dict) -> dict:
    return {
        "rounds": [],
        "term_expansions": [],
        "candidates": list(candidates),
        "access_failures": [],
    }


def _web(doc_number: str, title: str = "웹 후보", **extra) -> dict:
    return {
        "doc_number": doc_number,
        "title": title,
        "channel": search_manifest.CHANNEL_WEB,
        "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
        "quarantined": False,
        **extra,
    }


def _lane(lane_id: str, *candidates: dict) -> dict:
    return {
        "id": lane_id,
        "channel": "epo",
        "origin": lane_id.split(":", 1)[-1],
        "status": "ok",
        "termination_reason": "llm_finished",
        "search_calls": 1,
        "candidates": list(candidates),
    }


def _epo(*lanes: dict, enabled: bool = True, reason: str = "") -> dict:
    return {
        "enabled": enabled,
        "backend_id": "epo",
        "reason": reason,
        "channel_budget": {},
        "lanes": list(lanes),
        "usage": {},
        "error": "",
    }


def _epo_candidate(doc_number: str, title: str = "EPO 후보") -> dict:
    return {
        "doc_number": doc_number,
        "title": title,
        "artifact_ids": ["sha256:test"],
        "evidence": {},
    }


def _manifest(reported: dict, epo: dict) -> dict:
    return search_manifest.build(
        claim_text="청구항 1. 테스트",
        prompt_id="search_prompt.md",
        prompt_version=1,
        prompt_sha256="a" * 64,
        claim_boundary_neutralized=False,
        started_at="2026-08-28T00:00:00+00:00",
        completed_at="2026-08-28T00:01:00+00:00",
        tool_calls=[],
        tool_uses=[],
        tool_policy_name="web_search",
        allowed_tools=("WebSearch", "WebFetch"),
        reported=reported,
        notes=[],
        error=None,
        epo=epo,
    )


def test_kind_code_difference_is_the_same_publication() -> None:
    comparison = search_manifest.compare_channels(
        _reported(_web("EP 1 000 000")),
        _epo(_lane("epo:claim_only", _epo_candidate("EP1000000A1"))),
    )

    assert comparison["compared"] is True
    assert comparison["counts"] == {"both": 1, "epo_only": 0, "web_only": 0}
    assert comparison["both"][0]["web"][0]["doc_number"] == "EP 1 000 000"
    assert comparison["both"][0]["epo"][0]["doc_number"] == "EP1000000A1"


def test_same_digits_in_different_countries_are_not_joined() -> None:
    """URL 대조용 숫자 변형을 재사용하면 EP와 US가 잘못 합쳐진다."""
    comparison = search_manifest.compare_channels(
        _reported(_web("EP1000000")),
        _epo(_lane("epo:claim_only", _epo_candidate("US1000000A1"))),
    )

    assert comparison["counts"] == {"both": 0, "epo_only": 1, "web_only": 1}


def test_same_epo_candidate_in_two_lanes_is_one_unique_publication() -> None:
    comparison = search_manifest.compare_channels(
        _reported(),
        _epo(
            _lane("epo:claim_only", _epo_candidate("EP1000000A1")),
            _lane("epo:spec_assisted", _epo_candidate("EP 1000000 A1")),
        ),
    )

    assert comparison["epo"] == {
        "total": 2,
        "identified": 2,
        "unique_identified": 1,
        "unidentified": 0,
        "excluded_lanes": [],
    }
    assert comparison["counts"]["epo_only"] == 1
    lanes = {
        lane
        for row in comparison["epo_only"][0]["epo"]
        for lane in row["lanes"]
    }
    assert lanes == {"epo:claim_only", "epo:spec_assisted"}


def test_missing_document_numbers_are_counted_but_not_compared() -> None:
    comparison = search_manifest.compare_channels(
        _reported(_web("문헌번호 확인 필요")),
        _epo(_lane("epo:claim_only", _epo_candidate(""))),
    )

    assert comparison["web"]["unidentified"] == 1
    assert comparison["epo"]["unidentified"] == 1
    assert comparison["counts"] == {"both": 0, "epo_only": 0, "web_only": 0}


def test_missing_web_report_is_not_misreported_as_epo_only() -> None:
    comparison = search_manifest.compare_channels(
        None,
        _epo(_lane("epo:claim_only", _epo_candidate("EP1000000A1"))),
    )

    assert comparison["compared"] is False
    assert comparison["counts"]["epo_only"] == 0
    assert "웹 채널" in comparison["reason"]


def test_failed_epo_search_is_not_misreported_as_web_only() -> None:
    failed_lane = _lane("epo:claim_only")
    failed_lane.update(
        status="failed",
        termination_reason="provider_error",
        search_calls=1,
    )
    comparison = search_manifest.compare_channels(
        _reported(_web("EP1000000")),
        _epo(failed_lane),
    )

    assert comparison["compared"] is False
    assert comparison["counts"]["web_only"] == 0
    assert "완료된 OPS 검색" in comparison["reason"]


def test_failed_second_lane_is_disclosed_as_a_partial_comparison() -> None:
    failed_lane = _lane(
        "epo:spec_assisted", _epo_candidate("EP9999999A1")
    )
    failed_lane.update(
        status="failed",
        termination_reason="provider_error",
    )
    comparison = search_manifest.compare_channels(
        _reported(),
        _epo(
            _lane("epo:claim_only", _epo_candidate("EP1000000A1")),
            failed_lane,
        ),
    )

    assert comparison["compared"] is True
    assert comparison["complete"] is False
    assert comparison["epo"]["excluded_lanes"] == ["epo:spec_assisted"]
    assert comparison["counts"]["epo_only"] == 1
    assert comparison["epo_only"][0]["doc_number"] == "EP1000000A1"


def test_disabled_epo_channel_is_not_compared() -> None:
    comparison = search_manifest.compare_channels(
        _reported(_web("EP1000000")),
        _epo(enabled=False, reason="EPO OPS 연동이 꺼져 있습니다."),
    )

    assert comparison["compared"] is False
    assert "꺼져" in comparison["reason"]


def test_manifest_v6_records_the_derived_comparison_and_used_channel() -> None:
    manifest = _manifest(
        _reported(_web("EP1000000")),
        _epo(_lane("epo:claim_only", _epo_candidate("EP1000000A1"))),
    )

    assert manifest["version"] == 7
    assert manifest["channel_comparison"]["counts"]["both"] == 1
    assert search_manifest.CHANNEL_PATENT_DB in manifest["channels_used"]
    # 원 채널 기록은 파생 비교를 만들면서 바뀌지 않는다.
    assert manifest["reported"]["candidates"][0]["doc_number"] == "EP1000000"
    assert manifest["epo"]["lanes"][0]["candidates"][0]["doc_number"] == "EP1000000A1"


def test_report_surfaces_epo_only_as_discovery_not_classification() -> None:
    manifest = _manifest(
        _reported(),
        _epo(
            _lane(
                "epo:claim_only",
                _epo_candidate("EP2222222B1", title="EPO | 단독 후보"),
            )
        ),
    )
    report = search_report.render(manifest)

    assert "## 웹/EPO 채널 교차 발견" in report
    assert "EPO에서만 발견: 1건" in report
    assert "EP2222222B1" in report
    assert "EPO \\| 단독 후보" in report
    assert "특허 패밀리 판정이나 A/B/C 유사도 분류가 아니" in report
    assert "이번 웹 검색 기록에 같은 공개번호가 없었다는 뜻" in report
