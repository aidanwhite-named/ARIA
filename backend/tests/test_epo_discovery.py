"""EPO 독립 검색을 최종 대응표에 연결하는 경로.

여기서 검사하는 것은 네 가지다.

  1. 청구항 분석(claim_analysis)이 **첫 응답에서** 오고, 별도 모델 턴이 생기지
     않는다.
  2. shortlist 가 검색 결과와 대조되고 상한까지만 넘어간다. 잘린 것은 조용히
     사라지지 않고 사유와 함께 남는다.
  3. EPO 후보가 주 후보 목록에 들어가되, 웹 후보와 중복이면 후보 하나로 합쳐
     지고 두 발견 경로가 모두 보존된다.
  4. 이미 받은 EPO 응답을 공식 검증이 재사용해 같은 자료를 다시 부르지 않는다.
  5. 거절된 응답의 shortlist 는 되살아나지 않고, 결과 payload 를 줄일 때
     마지막 라운드의 후보가 먼저 잘리지 않는다.

네트워크를 열지 않는다.
"""

from __future__ import annotations

import json

import pytest

from app import search_manifest, search_verification
from app.patent_search import artifacts, epo_actions, epo_agent

from . import epo_fixtures as fx
from .test_epo_agent import (
    DEFAULT_SELECTION,
    FINISH,
    FakeOutcome,
    FakeProvider,
    make_agent,
    run,
    say,
    search_action,
)
from .test_epo_search import FakeTransport, ok, token_response


ANALYSIS = {
    "elements": [
        {
            "id": "E1",
            "text": "로봇 팔",
            "essential": True,
            "synonyms": ["robot arm", "manipulator"],
        },
        {"id": "E2", "text": "힘 센서", "essential": True, "synonyms": ["force sensor"]},
        # 필수 여부를 적지 않은 구성. 이것이 '필수 아님'으로 기록되면 안 된다.
        {"id": "E3", "text": "제어부"},
    ],
    "relations": [
        {
            "source": "E2",
            "target": "E1",
            "kind": "배치",
            "description": "힘 센서가 로봇 팔 끝단에 배치된다",
        }
    ],
    "concept_combinations": [
        {
            "elements": ["E1", "E2"],
            "terms": ["robot arm", "force sensor"],
            "reason": "두 구성의 결합이 핵심",
        }
    ],
    "search_conditions": [
        {"kind": "ipc", "value": "B25J 9/16", "reason": "로봇 제어 분류"}
    ],
}


@pytest.fixture()
def store(tmp_path) -> artifacts.ArtifactStore:
    return artifacts.ArtifactStore(tmp_path / "evidence")


def _first_round(**extra) -> str:
    payload = {
        "strategy": "테스트",
        "claim_analysis": ANALYSIS,
        "actions": [search_action(), FINISH],
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------- 청구항 분석


def test_claim_analysis_arrives_with_the_first_response(store, tmp_path) -> None:
    """추가 모델 턴 없이 첫 응답에서 검색 전략을 받는다."""
    provider = FakeProvider(_first_round())
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    # 모델 호출은 한 번뿐이다. 분석을 위해 턴이 하나 더 생기지 않았다.
    assert len(provider.requests) == 1
    analysis = result.claim_analysis
    assert analysis["round"] == 1
    assert [item["id"] for item in analysis["elements"]] == ["E1", "E2", "E3"]
    assert analysis["elements"][0]["synonyms"] == ["robot arm", "manipulator"]
    # 세 상태다. 적지 않은 것을 False 로 만들지 않는다.
    assert analysis["elements"][2]["essential"] is None
    assert analysis["relations"][0]["source"] == "E2"
    assert analysis["concept_combinations"][0]["terms"] == [
        "robot arm",
        "force sensor",
    ]
    assert analysis["search_conditions"][0]["value"] == "B25J 9/16"


def test_the_first_analysis_wins_over_a_later_one(store, tmp_path) -> None:
    """검색 결과를 본 뒤 고쳐 쓴 분석은 검색 전략이 아니다."""
    second = json.dumps(
        {
            "claim_analysis": {"elements": [{"id": "X1", "text": "나중에 고친 것"}]},
            "actions": [FINISH],
        },
        ensure_ascii=False,
    )
    provider = FakeProvider(
        json.dumps(
            {"claim_analysis": ANALYSIS, "actions": [search_action()]},
            ensure_ascii=False,
        ),
        second,
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert [item["id"] for item in result.claim_analysis["elements"]] == [
        "E1",
        "E2",
        "E3",
    ]
    assert any("먼저 받은 분석을 유지" in note for note in result.notes)


def test_the_lane_record_carries_the_analysis(store, tmp_path) -> None:
    provider = FakeProvider(_first_round())
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    record = search_manifest.epo_lane_record(
        origin=search_manifest.ORIGIN_CLAIM_ONLY, run=result, status="ok"
    )
    assert record["claim_analysis"]["elements"]
    assert record["shortlist"] == []
    assert record["tool_violations"] == []


# ----------------------------------------------------------------- shortlist


def test_shortlist_keeps_only_documents_the_lane_actually_saw(store, tmp_path) -> None:
    """검색 결과에 없는 번호는 최종 후보가 되지 못하고 사유가 남는다."""
    provider = FakeProvider(
        _first_round(
            shortlist=[
                {"doc_number": "EP1000000A1", "reason": "힘 센서가 그대로 개시됨"},
                {"doc_number": "EP9999999A1", "reason": "기억으로 적은 번호"},
            ]
        )
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert [item["doc_number"] for item in result.shortlist] == ["EP1000000A1"]
    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_UNKNOWN_DOC_NUMBER
    ]
    assert len(dropped) == 1
    assert dropped[0]["value"] == "EP9999999A1"


def test_shortlist_limit_records_what_it_dropped(store, tmp_path) -> None:
    """상한 때문에 빠진 후보를 조용히 누락하지 않는다."""
    provider = FakeProvider(
        _first_round(
            shortlist=[
                {"doc_number": "EP1000000A1", "reason": "첫째"},
                {"doc_number": "US9876543B2", "reason": "둘째"},
            ]
        )
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    agent = make_agent(
        provider,
        transport,
        store,
        tmp_path,
        budget=epo_agent.EpoAgentBudget(shortlist_limit=1),
    )
    result = run(agent)

    assert [item["doc_number"] for item in result.shortlist] == ["EP1000000A1"]
    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_SHORTLIST_LIMIT
    ]
    assert len(dropped) == 1
    assert "US9876543B2" in dropped[0]["value"]
    assert "상한" in dropped[0]["detail"]


def test_a_rejected_response_never_lends_its_shortlist(store, tmp_path) -> None:
    """claim_analysis 없이 온 응답은 실행되지 않는다. 그 shortlist 도 마찬가지다.

    되살아나는 경로가 실제로 있었다. shortlist 를 계약 검사 **전에** 모아 두면,
    이 응답이 고른 번호를 뒤 라운드의 검색이 데려오는 순간 대조를 통과해 최종
    후보가 된다. 거절한 응답의 판단이 한 라운드 늦게 채택되는 셈이다.
    """
    provider = FakeProvider(
        # 1차: 분석 없이 검색 + shortlist. 검색은 실행되지 않는다.
        json.dumps(
            {
                "actions": [search_action()],
                "shortlist": [
                    {"doc_number": "EP1000000A1", "reason": "거절된 응답의 선택"}
                ],
            },
            ensure_ascii=False,
        ),
        # 2차: 계약을 지킨 응답. 이 검색이 바로 그 번호를 데려온다.
        say(search_action(), FINISH, analysis=ANALYSIS),
    )
    # 검색 응답은 하나만 준비한다. 1차에서 OPS 가 나가면 여기서 터진다.
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.search_calls == 1
    assert "EP1000000A1" in result.candidates
    assert result.shortlist == [], "거절된 응답의 shortlist 가 되살아났습니다."
    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_REJECTED_RESPONSE
    ]
    assert [row["value"] for row in dropped] == ["EP1000000A1"]
    assert "claim_analysis" in dropped[0]["detail"]


def test_an_empty_action_response_never_lends_its_shortlist(store, tmp_path) -> None:
    """빈 action 으로 거절된 응답도 같다. 사유만 다르고 결과는 같아야 한다."""
    provider = FakeProvider(
        json.dumps(
            {
                "claim_analysis": ANALYSIS,
                "actions": [],
                "shortlist": [
                    {"doc_number": "EP1000000A1", "reason": "빈 응답의 선택"}
                ],
            },
            ensure_ascii=False,
        ),
        say(search_action(), FINISH, analysis=None),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert "EP1000000A1" in result.candidates
    assert result.shortlist == []
    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_REJECTED_RESPONSE
    ]
    assert [row["value"] for row in dropped] == ["EP1000000A1"]
    assert "action" in dropped[0]["detail"]


def test_result_limit_is_applied_and_recorded(store, tmp_path) -> None:
    """모델이 더 큰 결과 수를 요청해도 설정 상한까지만 받고 그 사실을 남긴다."""
    provider = FakeProvider(say(search_action(max_results=20), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    agent = make_agent(
        provider,
        transport,
        store,
        tmp_path,
        budget=epo_agent.EpoAgentBudget(max_results_per_query=2),
    )
    result = run(agent)

    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_RESULT_LIMIT
    ]
    assert len(dropped) == 1
    # OPS 로 나간 범위가 실제로 좁혀졌다.
    search_url = next(
        item["url"] for item in transport.requests if "/search" in item["url"]
    )
    assert "Range=1-2" in search_url or "1-2" in search_url


# ------------------------------------------------------- 주 대응표로 연결


def _epo_section(*lanes: dict) -> dict:
    return {
        "enabled": True,
        "backend_id": search_manifest.EPO_BACKEND_ID,
        "reason": "",
        "lanes": list(lanes),
    }


def _lane(lane_id: str, *, candidates, shortlist) -> dict:
    return {
        "id": lane_id,
        "channel": "epo",
        "origin": lane_id.split(":", 1)[1],
        "status": "ok",
        "termination_reason": "llm_finished",
        "search_calls": 1,
        "candidates": list(candidates),
        "shortlist": list(shortlist),
    }


def _epo_lane_candidate(number: str, **extra) -> dict:
    row = {
        "doc_number": number,
        "title": f"{number} 제목",
        "source_url": f"https://ops.example.test/{number}",
        "first_seen_round": 1,
        "artifact_ids": ["a" * 64],
        "evidence": {"abstract:en": {"artifact_id": "a" * 64}},
    }
    row.update(extra)
    return row


def _web_candidate(number: str, index: int = 1) -> dict:
    return {
        "index": index,
        "doc_number": number,
        "doi": "",
        "group": None,
        "provisional_group": "B",
        "classification_basis": search_manifest.CLASSIFICATION_SEARCH,
        "channel": search_manifest.CHANNEL_WEB,
        "discovery_origins": [search_manifest.DISCOVERY_WEB],
        "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
        "title": "웹이 적은 제목",
        "mapping": [],
    }


def test_an_epo_only_shortlist_entry_becomes_a_candidate() -> None:
    reported = {"candidates": []}
    merged, notes = search_manifest.merge_epo_discoveries(
        reported,
        _epo_section(
            _lane(
                "epo:claim_only",
                candidates=[_epo_lane_candidate("EP1000000A1")],
                shortlist=[{"doc_number": "EP1000000A1", "reason": "힘 센서 개시"}],
            )
        ),
    )

    candidate = merged["candidates"][0]
    assert candidate["doc_number"] == "EP1000000A1"
    assert candidate["channel"] == search_manifest.CHANNEL_PATENT_DB
    assert candidate["discovery_origins"] == [search_manifest.DISCOVERY_EPO]
    assert candidate["provenance"] == search_manifest.PROV_OFFICIAL_RESPONSE
    # 이 시점에는 어떤 등급도 붙지 않는다.
    assert candidate["group"] is None
    assert candidate["provisional_group"] is None
    # 존재하지 않는 페이지 관측을 만들지 않는다.
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["identifier_url_matched"] is False
    assert candidate["page_supported_rows"] == 0
    assert search_verification.PAGE_CLASSIFICATION_FIELD not in candidate
    # 재사용할 아티팩트 참조가 함께 넘어온다.
    assert candidate["epo_discovery"]["artifact_ids"] == ["a" * 64]
    assert candidate["epo_discovery"]["shortlist"][0]["reason"] == "힘 센서 개시"
    assert notes


def test_a_document_found_by_both_channels_stays_one_candidate() -> None:
    """중복은 하나로 합치되 두 출처를 모두 보존한다."""
    reported = {"candidates": [_web_candidate("EP 1 000 000")]}
    merged, _notes = search_manifest.merge_epo_discoveries(
        reported,
        _epo_section(
            _lane(
                "epo:claim_only",
                candidates=[_epo_lane_candidate("EP1000000A1")],
                shortlist=[{"doc_number": "EP1000000A1", "reason": "같은 문헌"}],
            )
        ),
    )

    assert len(merged["candidates"]) == 1
    candidate = merged["candidates"][0]
    assert candidate["discovery_origins"] == ["web", "epo"]
    # 웹 후보의 값은 EPO 발견 사실로 덮이지 않는다.
    assert candidate["doc_number"] == "EP 1 000 000"
    assert candidate["provisional_group"] == "B"
    # OPS 표기와 아티팩트는 별도 칸에 남는다.
    assert candidate["epo_discovery"]["doc_number"] == "EP1000000A1"
    assert candidate["epo_discovery"]["lanes"] == ["epo:claim_only"]


def test_the_lane_record_is_not_mutated_by_the_merge() -> None:
    """epo.lanes 원본은 검색 감사용으로 그대로 남는다."""
    lane = _lane(
        "epo:claim_only",
        candidates=[_epo_lane_candidate("EP1000000A1")],
        shortlist=[{"doc_number": "EP1000000A1", "reason": "r"}],
    )
    section = _epo_section(lane)
    before = json.dumps(section, ensure_ascii=False, sort_keys=True)

    search_manifest.merge_epo_discoveries({"candidates": []}, section)

    assert json.dumps(section, ensure_ascii=False, sort_keys=True) == before


def test_incomplete_lanes_do_not_contribute_candidates() -> None:
    """중간에 끊긴 레인의 목록을 최종 대응표에 올리지 않는다."""
    lane = _lane(
        "epo:claim_only",
        candidates=[_epo_lane_candidate("EP1000000A1")],
        shortlist=[{"doc_number": "EP1000000A1", "reason": "r"}],
    )
    lane["termination_reason"] = epo_agent.TERM_UNAUTHORIZED_TOOL_USE
    merged, notes = search_manifest.merge_epo_discoveries(
        {"candidates": []}, _epo_section(lane)
    )

    assert merged["candidates"] == []
    assert notes == []


def test_the_cross_channel_comparison_still_separates_the_two_channels() -> None:
    """합친 뒤에도 'EPO 에서만 찾았다'가 사라지지 않아야 한다."""
    section = _epo_section(
        _lane(
            "epo:claim_only",
            candidates=[_epo_lane_candidate("EP1000000A1")],
            shortlist=[{"doc_number": "EP1000000A1", "reason": "r"}],
        )
    )
    reported, _notes = search_manifest.merge_epo_discoveries(
        {"candidates": [_web_candidate("US9876543")]}, section
    )

    comparison = search_manifest.compare_channels(reported, section)
    assert comparison["counts"]["epo_only"] == 1
    assert comparison["counts"]["web_only"] == 1
    assert comparison["counts"]["both"] == 0


def test_legacy_candidates_read_as_web_discoveries() -> None:
    """v8 이전 후보에는 발견 경로 칸이 없다. 그때 후보를 만든 것은 웹뿐이었다."""
    assert search_manifest.discovery_origins({"doc_number": "EP1000000A1"}) == ["web"]
    assert search_manifest.discovery_origins({"discovery_origins": []}) == ["web"]
    assert search_manifest.discovery_origins(
        {"discovery_origins": ["epo", "web", "made_up"]}
    ) == ["web", "epo"]


def test_merging_two_web_lanes_keeps_the_discovery_origins() -> None:
    """청구항 단독 + 명세서 확장 병합이 발견 경로를 잃지 않는다."""
    base = _web_candidate("EP1000000A1")
    assisted = {
        **_web_candidate("EP1000000A1"),
        "search_origins": [search_manifest.ORIGIN_SPEC_ASSISTED],
        "discovery_origins": ["web", "epo"],
        "epo_discovery": {"lanes": ["epo:spec_assisted"]},
    }

    merged = search_manifest.merge_reported(
        {"candidates": [base]}, {"candidates": [assisted]}
    )

    candidate = merged["candidates"][0]
    assert candidate["discovery_origins"] == ["web", "epo"]
    assert candidate["epo_discovery"]["lanes"] == ["epo:spec_assisted"]


# --- 경계 사례 1: 웹 보고가 없어도 EPO 결과는 살린다 ------------------------


def test_epo_shortlist_builds_a_reported_skeleton_when_the_web_report_is_missing() -> None:
    """웹 출력을 읽지 못해도 완료된 EPO 검색이 있으면 후보 목록을 만든다."""
    merged, notes = search_manifest.merge_epo_discoveries(
        None,
        _epo_section(
            _lane(
                "epo:claim_only",
                candidates=[_epo_lane_candidate("EP1000000A1")],
                shortlist=[{"doc_number": "EP1000000A1", "reason": "힘 센서 개시"}],
            )
        ),
        web_report_error="감사 블록을 읽지 못했습니다",
    )

    assert merged is not None
    # 키 모양은 parse()/merge_reported() 가 만드는 것과 같아야 한다.
    assert set(merged) >= {
        "rounds",
        "term_expansions",
        "candidates",
        "access_failures",
    }
    assert [item["doc_number"] for item in merged["candidates"]] == ["EP1000000A1"]
    # 웹의 실패 상태는 지워지지 않는다.
    assert merged["web_report_error"] == "감사 블록을 읽지 못했습니다"
    assert any("웹 채널의 구조화 결과를 읽지 못했지만" in note for note in notes)


def test_a_missing_web_report_without_epo_results_stays_none() -> None:
    """살릴 EPO 결과가 없으면 골격을 지어내지 않는다."""
    merged, notes = search_manifest.merge_epo_discoveries(
        None,
        _epo_section(
            _lane("epo:claim_only", candidates=[], shortlist=[]),
        ),
        web_report_error="감사 블록을 읽지 못했습니다",
    )

    assert merged is None
    assert notes == []


def test_a_successful_web_report_keeps_its_own_shape() -> None:
    """웹이 성공한 실행에는 실패 표시가 붙지 않는다."""
    merged, _notes = search_manifest.merge_epo_discoveries(
        {"candidates": [_web_candidate("US9876543")]},
        _epo_section(
            _lane(
                "epo:claim_only",
                candidates=[_epo_lane_candidate("EP1000000A1")],
                shortlist=[{"doc_number": "EP1000000A1", "reason": "r"}],
            )
        ),
        web_report_error="",
    )

    assert merged.get("web_report_error", "") == ""
    assert len(merged["candidates"]) == 2


# --- 경계 사례 3: 청구항 분석은 계약이다 ------------------------------------


def test_a_search_without_claim_analysis_never_reaches_ops(store, tmp_path) -> None:
    """분석이 없으면 검색 action 을 실행하지 않고 다시 물어본다."""
    provider = FakeProvider(
        # 1차: 분석 없이 검색만. 실행되면 안 된다.
        say(search_action(), analysis=None),
        # 2차: 분석을 붙여 다시 보낸다. 이제 실행된다.
        say(search_action(), FINISH),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    # 1차 응답으로는 OPS 가 한 번도 불리지 않았다. 토큰 발급 + 검색 1회가 전부다.
    searches = [item["url"] for item in transport.requests if "/search" in item["url"]]
    assert len(searches) == 1
    assert result.search_calls == 1

    rejected = result.rounds[0]
    assert rejected.status == "missing_claim_analysis"
    assert rejected.counts_as_round is False, "거절된 응답이 라운드를 소모했습니다."
    assert result.claim_analysis["elements"]
    # 되물은 내용이 다음 라운드 입력에 실려야 한다.
    assert "claim_analysis" in provider.requests[1].user_message


def test_repeated_missing_analysis_ends_the_loop_without_any_ops_call(
    store, tmp_path
) -> None:
    """분석 없이 검색하려는 응답이 반복되면 끝낸다. OPS 는 부르지 않는다."""
    provider = FakeProvider(*[say(search_action(), analysis=None)] * 3)
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_INVALID_RESPONSE_LIMIT
    assert result.search_calls == 0
    assert transport.requests == [], "분석 없는 응답으로 OPS 를 불렀습니다."


def test_finish_without_analysis_is_not_blocked(store, tmp_path) -> None:
    """실행할 검색이 없는 응답까지 막지는 않는다."""
    provider = FakeProvider(say(FINISH, analysis=None))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    assert result.claim_analysis == {}


def test_analysis_arriving_after_a_search_is_not_stored_as_strategy(
    store, tmp_path
) -> None:
    """검색이 나간 뒤에 온 분석은 검색 전략이 아니다.

    1차는 짧은 형식(단일 action)으로 분석 없이 finish 를 보내 검색을 소모하지
    않게 하고, 그 다음에 검색을 실행한 뒤 분석을 보낸다.
    """
    provider = FakeProvider(
        say(search_action(), analysis=ANALYSIS),
        json.dumps(
            {
                "claim_analysis": {"elements": [{"id": "LATE", "text": "나중"}]},
                "actions": [FINISH],
            },
            ensure_ascii=False,
        ),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert [item["id"] for item in result.claim_analysis["elements"]] == [
        "E1",
        "E2",
        "E3",
    ]
    assert result.claim_analysis["round"] == 1


def test_a_rejected_round_still_accepts_the_analysis_as_pre_search(
    store, tmp_path
) -> None:
    """형식 오류로 거절된 뒤 2차 시도에서 온 분석은 여전히 검색 전의 판단이다."""
    provider = FakeProvider(
        "JSON 이 아닙니다",
        say(search_action(), FINISH, analysis=ANALYSIS),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    # 2라운드에 왔지만 그 전에 검색이 한 번도 나가지 않았다.
    assert result.claim_analysis["round"] == 2
    assert not any("검색이 이미 나간 뒤" in note for note in result.notes)


# --- 경계 사례 4: 검색하지 않는 최종 선택 턴 --------------------------------


def test_the_selection_turn_sees_the_last_search_results(store, tmp_path) -> None:
    """[검색, finish] 한 응답으로 끝나도 그 결과가 shortlist 평가를 받는다."""
    provider = FakeProvider(
        # 이 응답에는 shortlist 가 없다. 검색 루프만 있었다면 후보는 0건이다.
        say(search_action(), FINISH),
        selection=json.dumps(
            {
                "shortlist": [
                    {"doc_number": "EP1000000A1", "reason": "마지막 결과에서 골랐다"}
                ],
                "actions": [FINISH],
            },
            ensure_ascii=False,
        ),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert len(provider.selection_requests) == 1
    assert [item["doc_number"] for item in result.shortlist] == ["EP1000000A1"]
    assert result.shortlist[0]["turn"] == epo_agent.TURN_SELECTION
    # 마지막 검색 결과가 실제로 실려 갔다.
    assert "EP1000000A1" in provider.selection_requests[0].user_message


def test_the_selection_turn_cannot_search(store, tmp_path) -> None:
    """이 턴의 검색·조회 action 은 실행되지 않고 사유만 남는다."""
    provider = FakeProvider(
        say(search_action(), FINISH),
        selection=json.dumps(
            {
                "actions": [
                    search_action("한 번 더 검색"),
                    {
                        "action": "epo_fetch_document",
                        "doc_number": "EP1000000A1",
                        "constituent": "claims",
                    },
                    FINISH,
                ]
            },
            ensure_ascii=False,
        ),
    )
    # 검색 응답을 하나만 준비한다. 선택 턴이 검색을 실행하면 여기서 터진다.
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.search_calls == 1
    assert result.detail_fetches == 0
    rejected = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_SEARCH_IN_SELECTION
    ]
    assert len(rejected) == 2
    assert result.selection["rejected_actions"] == 2


def test_the_selection_turn_is_recorded_as_separate_usage(store, tmp_path) -> None:
    """검색 라운드 사용량과 섞지 않는다."""
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    usage = result.usage
    # 검색 라운드는 하나뿐이다. 선택 턴이 라운드 수를 늘리지 않았다.
    assert usage["rounds_used"] == 1
    assert usage["model_calls"] == 1
    assert usage["selection_turn"]["attempted"] is True
    assert usage["selection_turn"]["model_calls"] == 1
    assert usage["selection_turn"]["search_calls"] == 0
    assert result.selection["candidates_reviewed"] == 2


def test_no_selection_turn_after_an_abnormal_end(store, tmp_path) -> None:
    """취소·도구 위반 뒤에 모델을 한 번 더 부르지 않는다."""
    provider = FakeProvider(
        FakeOutcome(say(search_action(), FINISH), cancelled=True)
    )
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_CANCELLED
    assert provider.selection_requests == []
    assert result.selection == {}
    assert result.usage["selection_turn"]["attempted"] is False


def test_no_selection_turn_without_candidates(store, tmp_path) -> None:
    """고를 것이 없으면 턴을 만들지 않는다."""
    provider = FakeProvider(say(FINISH, analysis=None))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.candidates == {}
    assert provider.selection_requests == []


def test_a_tool_call_in_the_selection_turn_is_caught(store, tmp_path) -> None:
    """선택 턴도 NO_TOOLS 다. 도구를 부르면 그 shortlist 를 쓰지 않는다."""
    provider = FakeProvider(
        say(search_action(), FINISH),
        selection=FakeOutcome(
            json.dumps(
                {
                    "shortlist": [
                        {"doc_number": "EP1000000A1", "reason": "도구로 확인했다"}
                    ],
                    "actions": [FINISH],
                },
                ensure_ascii=False,
            ),
            tool_uses=["WebSearch"],
        ),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.shortlist == [], "도구를 부른 턴의 shortlist 를 채택했습니다."
    assert result.selection["status"] == epo_agent.TERM_UNAUTHORIZED_TOOL_USE
    violation = result.tool_violations[-1]
    assert violation["phase"] == epo_agent.TURN_SELECTION
    # 검색 루프 자체는 정상 종료로 남는다.
    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
# --- 경계 사례 5: 결과 payload 를 줄일 때 무엇을 먼저 버리는가 --------------
#
# 배열 뒤에서부터 자르면 **가장 나중에 찾은 후보가 언제나 먼저** 없어진다.
# 그 후보는 모델이 아직 한 번도 보지 못한 것이고, 최종 선택 턴은 바로 그것을
# 보여 주려고 있는 턴이다. 그러면 이 턴을 만든 이유가 상한 하나로 사라진다.


def test_the_last_search_candidate_survives_a_tight_payload_budget(
    store, tmp_path
) -> None:
    """작은 문자 상한에서도 마지막 라운드가 데려온 후보가 남는다."""
    provider = FakeProvider(
        # 1라운드는 끝내지 않는다. 2라운드가 새 문헌을 데려와야 한다.
        say(search_action(), analysis=ANALYSIS),
        say(search_action("compliant joint"), FINISH, analysis=None),
    )
    transport = FakeTransport(
        token_response(),
        ok(fx.SEARCH_BIBLIO),
        # 같은 모양에 EP 번호만 다른 응답. 2라운드가 데려오는 새 문헌이다.
        ok(fx.SEARCH_BIBLIO.replace(b"1000000", b"2000000")),
    )
    agent = make_agent(
        provider,
        transport,
        store,
        tmp_path,
        # 후보 한 건(≈366자)만 실을 수 있는 상한이다.
        budget=epo_agent.EpoAgentBudget(max_round_result_chars=400),
    )
    result = run(agent)

    assert set(result.candidates) == {"EP1000000A1", "US9876543B2", "EP2000000A1"}
    message = provider.selection_requests[0].user_message
    # 마지막 검색이 데려온 후보가 최종 선택 턴에 실려 갔다.
    assert "EP2000000A1" in message
    # 자리를 내준 것은 앞 라운드의 후보다.
    assert "EP1000000A1" not in message
    assert "US9876543B2" not in message

    dropped = [
        row
        for row in result.excluded
        if row["reason_code"] == epo_agent.EXCLUDED_RESULT_PAYLOAD_LIMIT
        and "최종 선택 턴" in row["detail"]
    ]
    assert {row["value"] for row in dropped} == {"EP1000000A1", "US9876543B2"}
    assert all("400자" in row["detail"] for row in dropped)


def test_the_payload_drops_the_lowest_ranked_candidate_first(store, tmp_path) -> None:
    """보존 순위: 마지막 라운드 → 상세 조회 → shortlist 밖 → 목록 순서.

    상한을 0 에 가깝게 두면 전부 빠지고, 빠진 순서가 곧 순위의 역순이다.
    후보를 넣은 순서와 일부러 어긋나게 둬서 배열 순서가 아니라 순위가 정하는
    것을 확인한다.
    """
    agent = make_agent(
        FakeProvider(),
        FakeTransport(),
        store,
        tmp_path,
        budget=epo_agent.EpoAgentBudget(max_round_result_chars=1),
    )
    result = epo_agent.EpoSearchRun()
    for number, round_no, detail in (
        ("EP1000000A1", 1, False),   # 아직 shortlist 에 없다
        ("US9876543B2", 1, True),    # 상세 조회까지 했다
        ("EP3000000A1", 1, False),   # 이미 shortlist 에 올라 있다
        ("EP2000000A1", 2, False),   # 마지막 라운드가 데려왔다
    ):
        result.candidates[number] = epo_agent.CandidateRecord(
            doc_number=number, first_seen_round=round_no, detail_fetched=detail
        )
    agent._pending_shortlist.append(
        (1, epo_agent.TURN_SEARCH, epo_actions.ShortlistItem(doc_number="EP3000000A1"))
    )

    assert agent._results_payload(result, None) == []
    assert [row["value"] for row in result.excluded] == [
        "EP3000000A1",
        "EP1000000A1",
        "US9876543B2",
        "EP2000000A1",
    ]


# --- 경계 사례 6: 최종 선택 턴의 사용량 -------------------------------------


def test_the_selection_turn_keeps_the_provider_usage(store, tmp_path) -> None:
    """이 턴도 토큰을 쓴다. Provider 가 알려준 값을 그대로 보존한다."""
    provider = FakeProvider(
        say(search_action(), FINISH),
        selection=FakeOutcome(
            DEFAULT_SELECTION,
            usage={"input_tokens": 1234, "output_tokens": 56, "model": "fake-1"},
        ),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    usage = result.usage["selection_turn"]
    assert usage["provider_usage"] == {
        "input_tokens": 1234,
        "output_tokens": 56,
        "model": "fake-1",
    }
    # 검색 라운드 사용량과 섞이지 않는다.
    assert usage["model_calls"] == 1
    assert result.usage["rounds_used"] == 1


def test_a_provider_without_usage_records_nothing_instead_of_zero(
    store, tmp_path
) -> None:
    """알려주지 않는 Provider 를 0 으로 적지 않는다. 0 은 '안 썼다'로 읽힌다."""
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.usage["selection_turn"]["provider_usage"] == {}


def test_a_rejected_selection_turn_still_records_what_it_spent(
    store, tmp_path
) -> None:
    """응답을 거절해도 쓴 토큰은 쓴 것이다."""
    provider = FakeProvider(
        say(search_action(), FINISH),
        selection=FakeOutcome("JSON 이 아닙니다", usage={"input_tokens": 99}),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.selection["status"] == "parse_error"
    assert result.usage["selection_turn"]["provider_usage"] == {"input_tokens": 99}
