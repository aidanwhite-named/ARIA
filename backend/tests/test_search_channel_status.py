"""웹 채널의 실행 상태.

이 파일이 지키는 경계는 하나다 — **검색 호출이 어떻게 끝났는가**와 **그 결과로
나온 후보가 얼마나 좋은가**는 다른 사실이고, 채널 상태 칸은 앞의 것만 말한다.

둘을 한 칸에 섞으면 두 종류의 오독이 생긴다. 검색이 다 성공했는데 이 청구항에
유사문헌이 없어 후보가 0건인 실행이 '검색 실패'로 읽히고, 반대로 검색 호출이
절반 실패했는데 남은 후보가 그럴듯하면 '검색 성공'으로 읽힌다. 후보의 품질은
보고서의 분류 절이 이미 따로 말한다.
"""

from __future__ import annotations

import json

from app import search_channels, search_manifest

FETCHED_URL = "https://patents.example.com/patent/AB1234"


def _call(name: str, payload: dict, ok=True, error: str | None = None) -> dict:
    return {
        "id": f"call-{name}-{json.dumps(payload, ensure_ascii=False)}",
        "name": name,
        "ts": "2026-09-03T00:00:00+00:00",
        "input": payload,
        "ok": ok,
        "error": error,
    }


def _block(entries: list) -> str:
    return (
        "[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps({"candidates": entries}, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]"
    )


def _candidate(**overrides) -> dict:
    entry = {
        "doc_type": "patent",
        "doc_number": "AB1234",
        "title": "테스트 문헌",
        "url": FETCHED_URL,
        "channel": "web",
        "provenance": search_manifest.PROV_WEBFETCH,
        "evidence_status": search_manifest.EVIDENCE_REVIEWED,
        "group": "A",
    }
    entry.update(overrides)
    return entry


def _manifest(
    calls: list,
    entries: list | None = None,
    *,
    channel_policy: dict | None = None,
):
    """호출 목록과 모델 보고로 매니페스트를 만든다. 실제 정규화 경로를 탄다."""
    observed = search_manifest.observed(calls, [call["name"] for call in calls])
    notes: list[str] = []
    reported = None
    if entries is not None:
        reported, notes = search_manifest.parse(_block(entries), observed)
    return search_manifest.build(
        claim_text="청구항 1. 테스트",
        prompt_id="search_prompt.md",
        prompt_sha256="a" * 64,
        claim_boundary_neutralized=False,
        started_at="2026-09-03T00:00:00+00:00",
        completed_at="2026-09-03T00:05:00+00:00",
        tool_calls=calls,
        tool_uses=[call["name"] for call in calls],
        tool_policy_name="web_search",
        allowed_tools=("WebSearch", "WebFetch"),
        reported=reported,
        notes=notes,
        error=None,
        channel_policy=channel_policy,
    )


def _rows(manifest: dict) -> dict:
    return {row["id"]: row for row in search_channels.status_rows(manifest)}


# ------------------------------------------------------------ 검색 호출 결과


def test_a_partly_failed_web_search_is_partial() -> None:
    """호출 둘 중 하나가 실패했다. 성공도 실패도 아니다."""
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}),
            _call("WebSearch", {"query": "검색식 B"}, ok=False, error="rate limited"),
            _call("WebFetch", {"url": FETCHED_URL}),
        ],
        [_candidate()],
    )
    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_PARTIAL
    assert "실패 1건" in row["detail"]
    # 살아남은 후보는 그대로 있다. 상태가 후보를 지우지 않는다.
    assert manifest["reported"]["candidates"]


def test_a_wholly_failed_web_search_is_failed() -> None:
    """호출이 전부 실패했으면 부분 성공이 아니다."""
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}, ok=False, error="rate limited"),
            _call("WebSearch", {"query": "검색식 B"}, ok=False, error="rate limited"),
        ],
        [],
    )
    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_FAILED
    assert "실패 2건" in row["detail"]


def test_a_clean_search_that_found_nothing_is_still_a_success() -> None:
    """찾을 것이 없었던 실행과 검색이 실패한 실행은 다르다.

    후보 0건을 실패로 적으면, 이 청구항에 유사문헌이 없다는 **결과**가
    ARIA 가 검색을 못 했다는 **사고**로 읽힌다.
    """
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}),
            _call("WebSearch", {"query": "검색식 B"}),
        ],
        [],
    )
    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_SUCCEEDED
    assert "후보 0건" in row["detail"]


def test_a_disabled_web_channel_without_calls_is_skipped() -> None:
    """정책이 끈 채널은 실행 실패가 아니라 명시적인 미실행이다."""
    policy = search_channels.resolve(
        {"literature_integration_enabled": True}, channels=["literature"]
    )
    manifest = _manifest([], [], channel_policy=policy.as_dict())

    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_SKIPPED
    assert row["detail"] == policy.reason(search_channels.CHANNEL_WEB)


def test_an_enabled_web_channel_without_calls_is_failed() -> None:
    """실행 대상으로 정했는데 호출이 없으면 검색 성공이라고 쓸 수 없다."""
    policy = search_channels.resolve({})
    manifest = _manifest([], [], channel_policy=policy.as_dict())

    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_FAILED
    assert "검색 호출이 실행되지 않았습니다" in row["detail"]


def test_unobservable_search_calls_are_not_promoted_to_success() -> None:
    """``ok`` 가 None 인 호출은 성공이 아니라 관측 불가다.

    Codex 의 web_search 가 그렇다 — 구조화된 성공 신호를 주지 않아 호출이
    끝났다는 사실만 남는다. 그것을 성공으로 적으면 ARIA 가 보지 않은 것을
    봤다고 쓰는 셈이다.
    """
    manifest = _manifest(
        [
            _call("web_search", {"query": "검색식 A", "input_kind": "query"}, ok=None),
            _call("web_search", {"query": "검색식 B", "input_kind": "query"}, ok=None),
        ],
        [],
    )
    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_PARTIAL
    assert "결과 관측 불가 2건" in row["detail"]
    assert "실패" not in row["detail"]


def test_page_fetch_failures_do_not_fail_the_search_row() -> None:
    """검색과 열람은 다른 일이다. 403 으로 한 장도 못 열어도 검색은 성공이다."""
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}),
            _call("WebFetch", {"url": FETCHED_URL}, ok=False, error="HTTP 403"),
        ],
        [],
    )
    rows = _rows(manifest)
    assert rows["web_search"]["status"] == search_channels.STATUS_SUCCEEDED
    assert rows["web_page_fetch"]["status"] == search_channels.STATUS_FAILED


# -------------------------------------------------------- 후보 품질과의 분리


def test_downgraded_candidates_do_not_change_the_search_status() -> None:
    """후보의 증거 등급이 내려간 것은 검색 호출의 실패가 아니다."""
    manifest = _manifest(
        # 페이지를 연 기록이 없다. 모델이 주장한 '본문 확인'은 강등된다.
        [_call("WebSearch", {"query": "검색식 A"})],
        [_candidate()],
    )
    candidate = manifest["reported"]["candidates"][0]
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_CANDIDATE
    assert candidate["provenance"] == search_manifest.PROV_SNIPPET
    assert any("내렸습니다" in note for note in manifest["normalization_notes"])

    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_SUCCEEDED
    assert "정규화 탈락" not in row["detail"]


def test_malformed_candidates_are_dropped_with_a_count_and_the_rest_survive() -> None:
    """읽지 못한 후보 하나가 채널 전체를 죽이지 않는다.

    버린 수는 감사에 남기되 상태는 바꾸지 않는다. 모델이 후보 하나를 이상한
    모양으로 적은 것은 검색 실패가 아니다.
    """
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}),
            _call("WebFetch", {"url": FETCHED_URL}),
        ],
        ["문자열", _candidate(), None, 42],
    )
    reported = manifest["reported"]
    stats = reported["candidate_normalization"]
    assert stats["raw"] == 4
    assert stats["accepted"] == 1
    assert stats["dropped"] == 3
    assert stats["dropped_malformed"] == 3
    assert stats["dropped_over_limit"] == 0

    # 정상 후보는 살아 있다.
    assert [item["doc_number"] for item in reported["candidates"]] == ["AB1234"]
    assert any("버렸습니다" in note for note in manifest["normalization_notes"])

    row = _rows(manifest)["web_search"]
    assert row["status"] == search_channels.STATUS_SUCCEEDED
    assert "정규화 탈락 3건" in row["detail"]


def test_an_empty_report_skeleton_carries_the_same_statistics_shape() -> None:
    """빈 골격도 같은 키 모양을 갖는다. 다운스트림이 모양으로 분기하지 않게."""
    empty = search_manifest.empty_reported(web_report_error="감사 블록 없음")
    assert empty["candidate_normalization"] == (
        search_manifest.empty_candidate_normalization()
    )


# ------------------------------------------------------------- 저장된 스냅샷


def test_the_stored_channel_status_is_a_snapshot_of_the_same_calculation() -> None:
    """저장된 상태는 파생 스냅샷이다. 만드는 시점에는 재계산과 반드시 같다.

    읽는 쪽은 이 값을 믿지 않고 같은 함수를 다시 부른다(저장된 값이 낡을 수
    있기 때문이다). 그렇다고 저장 시점부터 어긋나 있으면 스냅샷이라는 말도
    성립하지 않으므로, 만들어질 때 일치하는지는 여기서 못 박는다.
    """
    manifest = _manifest(
        [
            _call("WebSearch", {"query": "검색식 A"}),
            _call("WebSearch", {"query": "검색식 B"}, ok=False, error="rate limited"),
            _call("WebFetch", {"url": FETCHED_URL}),
        ],
        [_candidate()],
    )
    assert manifest["channel_status"] == search_channels.status_rows(manifest)
    assert manifest["channel_status_overall"] == search_channels.overall_status(
        search_channels.status_rows(manifest)
    )


def test_a_legacy_manifest_without_the_new_counts_is_read_the_same_way() -> None:
    """옛 기록에는 호출 결과 수가 없다. 원본 호출 목록에서 같은 규칙으로 센다."""
    calls = [
        _call("WebSearch", {"query": "검색식 A"}),
        _call("WebSearch", {"query": "검색식 B"}, ok=False, error="rate limited"),
    ]
    fresh = search_manifest.observed(calls, ["WebSearch", "WebSearch"])
    legacy = {
        key: value
        for key, value in fresh.items()
        if key
        not in {
            "search_call_succeeded",
            "search_call_failed",
            "search_call_unknown",
        }
    }
    assert "search_call_failed" not in legacy
    assert search_manifest.search_call_outcomes(
        legacy
    ) == search_manifest.search_call_outcomes(fresh)
