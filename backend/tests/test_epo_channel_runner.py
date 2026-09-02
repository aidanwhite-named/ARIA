"""러너 2채널 — 레인 4개, 격리, 실패 전파 없음, 취소, manifest v6.

**네트워크를 열지 않는다.** 모델은 DeterministicSearchProvider 가, OPS 는
전송 계층 monkeypatch 가 대신한다. 실수로 실제 호출이 나가면 conftest 의
차단이 걸려 실패한다.
"""

from __future__ import annotations

import json

import pytest

from app import settings_service
from app.db import session_scope
from app import search_manifest
from app.patent_search import epo_client

from . import epo_fixtures as fx
from . import fake_provider
from .conftest import wait_for_job

CLAIM = "청구항 1. 로봇 팔과 힘 센서를 포함하는 장치."

#: 출원발명 명세서 본문. 청구항에 없는 문장이라야 "명세서가 실제로 들어갔다"와
#: "청구항만 들어갔다"를 구별할 수 있다.
SPEC = (
    "【발명의 설명】 이 출원에서 힘 센서는 로봇 팔 끝단에 붙인 스트레인 게이지를"
    " 말하며, 제어부는 그 값을 읽어 관절 토크를 보정한다."
)


@pytest.fixture()
def epo_on(client):
    """EPO 를 켜고 전송 계층을 가짜로 바꾼다. 테스트가 끝나면 되돌린다."""
    settings_service.reset_epo_ledger()
    with session_scope() as session:
        settings_service.update(
            session,
            {
                "epo_integration_enabled": True,
                "epo_consumer_key": "TESTKEY000",
                "epo_consumer_secret": "TESTSECRET111",
            },
        )
    fake_provider.EPO_SCRIPT.clear()
    fake_provider.EPO_REQUESTS.clear()
    fake_provider.EPO_ON_ROUND = None

    seen: list[str] = []

    def transport(request, timeout):
        seen.append(request.full_url)
        if "accesstoken" in request.full_url:
            return epo_client.HttpResponse(200, dict(fx.HEADERS_OK), fx.TOKEN_OK)
        body = fx.CLAIMS if "publication" in request.full_url else fx.SEARCH_BIBLIO
        return epo_client.HttpResponse(200, dict(fx.HEADERS_OK), body)

    original = epo_client._live_transport
    epo_client._live_transport = transport
    try:
        yield seen
    finally:
        epo_client._live_transport = original
        fake_provider.EPO_SCRIPT.clear()
        fake_provider.EPO_REQUESTS.clear()
        fake_provider.EPO_ON_ROUND = None
        settings_service.reset_epo_ledger()
        with session_scope() as session:
            settings_service.update(session, {"epo_integration_enabled": False})


def start_search(client, claim: str = CLAIM, **overrides) -> dict:
    body = {
        "job_kind": "similarity_search",
        "claim_text": claim,
        "provider": "test-search",
        "prompt_id": "search_prompt.md",
    }
    body.update(overrides)
    created = client.post("/api/jobs", json=body)
    assert created.status_code in (200, 201), created.text
    job_id = created.json()["id"]
    return wait_for_job(client, job_id)


def upload_spec(client) -> str:
    """출원발명 명세서를 **실제로** 첨부한다.

    레인이 넷이 되는 조건은 이 첨부뿐이다. spec_text 를 손으로 주입하면
    업로드 → 조립 → 러너로 이어지는 구간이 빠져서, 정작 깨지기 쉬운 곳을
    지나가지 않는다.
    """
    response = client.post(
        "/api/uploads",
        files=[("files", ("spec.txt", SPEC.encode(), "text/plain"))],
        data={"roles": json.dumps(["APPLICATION"])},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["batch_id"]


def ops_searches(seen: list[str]) -> list[str]:
    """OPS 로 나간 **검색** 요청만 센다. 토큰 발급은 검색이 아니다."""
    return [url for url in seen if "/search/" in url]


def epo_messages(lane: str) -> list[str]:
    """그 레인이 모델에게 실제로 보낸 사용자 메시지 전부."""
    return [
        entry["message"]
        for entry in fake_provider.EPO_REQUESTS
        if entry["lane"] == lane
    ]


def manifest_of(client, job: dict) -> dict:
    detail = client.get(f"/api/jobs/{job['id']}").json()
    return detail.get("search_manifest") or {}


# ------------------------------------------------------------------ EPO 꺼짐


def test_epo_off_changes_nothing(client) -> None:
    """꺼져 있으면 EPO 모델 호출도 OPS 호출도 0이고 기존 결과가 그대로다."""
    fake_provider.EPO_REQUESTS.clear()
    with session_scope() as session:
        settings_service.update(session, {"epo_integration_enabled": False})

    job = start_search(client)
    manifest = manifest_of(client, job)

    assert fake_provider.EPO_REQUESTS == [], "EPO 가 꺼졌는데 모델을 불렀습니다."
    assert manifest["epo"]["enabled"] is False
    assert manifest["epo"]["lanes"] == []
    assert "꺼져" in manifest["epo"]["reason"]
    # 웹 레인은 예전 그대로다.
    assert [lane["id"] for lane in manifest["search_lanes"]] == ["claim_only"]


def test_epo_enabled_without_credentials_makes_no_calls(client, epo_on) -> None:
    with session_scope() as session:
        settings_service.update(session, {"epo_consumer_secret": ""})
    fake_provider.EPO_REQUESTS.clear()
    epo_on.clear()

    job = start_search(client)
    manifest = manifest_of(client, job)

    assert epo_on == [], "자격증명이 없는데 OPS 를 불렀습니다."
    assert fake_provider.EPO_REQUESTS == []
    assert manifest["epo"]["enabled"] is True
    assert "Consumer Key" in manifest["epo"]["reason"]


# ------------------------------------------------------------------ 레인


def test_four_lane_ids_are_fixed() -> None:
    assert search_manifest.LANE_IDS == (
        "web:claim_only",
        "web:spec_assisted",
        "epo:claim_only",
        "epo:spec_assisted",
    )


def test_epo_lane_is_recorded_with_a_fixed_id(client, epo_on) -> None:
    job = start_search(client)
    manifest = manifest_of(client, job)

    lane_ids = [lane["id"] for lane in manifest["lanes"]]
    assert "web:claim_only" in lane_ids
    assert "epo:claim_only" in lane_ids
    for lane in manifest["lanes"]:
        assert lane["id"] in search_manifest.LANE_IDS
        assert lane["channel"] in ("web", "epo")
        assert lane["origin"] in ("claim_only", "spec_assisted")


def test_manifest_version_matches_the_schema(client, epo_on) -> None:
    manifest = manifest_of(client, start_search(client))
    assert manifest["version"] == search_manifest.MANIFEST_VERSION


# ---------------------------------------------------------------- 격리


def test_web_and_epo_records_are_not_mixed(client, epo_on) -> None:
    """후보·검색어·오류·종료 사유·사용량이 섞이지 않아야 비교가 가능하다."""
    manifest = manifest_of(client, start_search(client))

    epo = manifest["epo"]
    assert epo["enabled"] is True
    lane = epo["lanes"][0]
    assert lane["id"] == "epo:claim_only"
    assert lane["candidates"], "EPO 후보가 없습니다."
    assert lane["queries"], "EPO 검색어가 없습니다."
    assert lane["termination_reason"]

    # 웹 쪽 보고에는 EPO 후보가 섞여 있지 않다.
    web_numbers = {
        candidate.get("doc_number")
        for candidate in (manifest.get("reported") or {}).get("candidates") or []
    }
    epo_numbers = {item["doc_number"] for item in lane["candidates"]}
    assert epo_numbers, "EPO 후보 번호가 비어 있습니다."
    assert not (web_numbers & epo_numbers), "웹 보고에 EPO 후보가 섞였습니다."


def test_epo_lane_does_not_see_web_results(client, epo_on) -> None:
    """논리적 격리. EPO 입력에 웹 후보가 들어가면 비교가 성립하지 않는다."""
    start_search(client)
    assert fake_provider.EPO_REQUESTS, "EPO 레인이 실행되지 않았습니다."
    for entry in fake_provider.EPO_REQUESTS:
        assert "ARIA_SEARCH_LOG_V1" not in entry["message"]
        assert "webfetch_summary" not in entry["message"]


def test_epo_candidates_carry_evidence(client, epo_on) -> None:
    manifest = manifest_of(client, start_search(client))
    candidate = manifest["epo"]["lanes"][0]["candidates"][0]
    assert candidate["artifact_ids"]
    assert candidate["evidence"]
    for ref in candidate["evidence"].values():
        assert ref["profile_id"] == "epo_ops_exchange_xml_v1"


# ------------------------------------------------------- 실패가 번지지 않음


def test_epo_failure_does_not_break_the_web_result(client, epo_on) -> None:
    """EPO 레인이 실패해도 웹 보고서는 그대로 나온다."""
    fake_provider.EPO_SCRIPT["epo:claim_only"] = ["설명만 하고 JSON 을 안 씁니다"] * 5

    job = start_search(client)
    manifest = manifest_of(client, job)

    assert job["status"] == "SUCCEEDED", job.get("error")
    assert manifest["reported"] is not None, "웹 보고가 사라졌습니다."
    lane = manifest["epo"]["lanes"][0]
    assert lane["termination_reason"] == "invalid_response_limit"


def test_raising_epo_lane_is_isolated(client, epo_on) -> None:
    """레인이 예외로 죽어도 작업은 계속되고 그 사실이 기록에 남는다.

    형식 오류는 에이전트가 안에서 처리하므로 러너의 격리를 시험하지 못한다.
    여기서는 레인이 실제로 예외를 던지게 한다.
    """
    fake_provider.EPO_SCRIPT["epo:claim_only"] = ["__RAISE__"]

    job = start_search(client)
    manifest = manifest_of(client, job)

    assert job["status"] == "SUCCEEDED", job.get("error")
    assert manifest["reported"] is not None, "웹 보고가 사라졌습니다."
    lane = manifest["epo"]["lanes"][0]
    assert lane["status"] == "failed"
    assert "테스트용 레인 실패" in lane["error"]
    assert manifest["epo"]["error"]


def test_epo_channel_budget_is_per_job_not_per_lane(client, epo_on) -> None:
    """명세: '작업당 OPS 검색 요청 최대 6회'. 레인당이 아니다."""
    manifest = manifest_of(client, start_search(client))
    budget = manifest["epo"]["channel_budget"]
    assert budget["scope"] == "channel"
    assert budget["max_search_calls"] == 6
    assert budget["max_detail_fetches"] == 12
    assert budget["deadline_seconds"] == 180
    used = sum(lane["search_calls"] for lane in manifest["epo"]["lanes"])
    assert budget["searches_used"] == used


# ---------------------------------------- 명세서를 붙이면 레인이 넷이 된다


def test_attached_spec_runs_all_four_lanes(client, epo_on) -> None:
    """명세서를 실제로 첨부해 네 레인을 끝까지 돌린 결과를 본다.

    이 파일의 다른 테스트는 첨부 없이 돌기 때문에 EPO 레인이 하나뿐이다.
    레인이 둘인 상태에서만 드러나는 것들 — 두 번째 레인이 실제로 도는가,
    manifest 가 넷을 다 적는가, 예산을 나눠 쓰는가 — 은 여기서만 검증된다.
    """
    job = start_search(client, batch_id=upload_spec(client))
    manifest = manifest_of(client, job)

    assert job["status"] == "SUCCEEDED", job.get("errors")
    # 고정 id 네 개가 선언된 순서 그대로 있다.
    assert [lane["id"] for lane in manifest["lanes"]] == list(search_manifest.LANE_IDS)

    lanes = {lane["id"]: lane for lane in manifest["lanes"]}

    # 웹 레인 둘: 예전 기록 모양 그대로에 레인 id 만 붙었다.
    assert lanes["web:claim_only"]["status"] == "SUCCEEDED"
    assert lanes["web:spec_assisted"]["status"] == "SUCCEEDED"
    assert lanes["web:claim_only"]["spec_in_context"] is False
    assert lanes["web:spec_assisted"]["spec_in_context"] is True

    # EPO 레인 둘: 각자 자기 결과를 들고 있다.
    for lane_id in ("epo:claim_only", "epo:spec_assisted"):
        lane = lanes[lane_id]
        assert lane["status"] == "ok", lane.get("error")
        assert lane["candidates"], f"{lane_id} 후보가 없습니다."
        assert lane["queries"], f"{lane_id} 검색어가 없습니다."
        assert lane["termination_reason"] == "llm_finished"
        assert lane["usage"]["lane_id"] == lane_id

    # epo 절도 같은 두 레인을 담는다. lanes 와 epo["lanes"] 가 어긋나면
    # 화면과 통계가 서로 다른 숫자를 보게 된다.
    assert [lane["id"] for lane in manifest["epo"]["lanes"]] == [
        "epo:claim_only",
        "epo:spec_assisted",
    ]
    # 두 레인이 각각 OPS 를 쳤다.
    assert len(ops_searches(epo_on)) == 2

    # v6 파생 비교가 실제 러너 산출물에도 붙고, 사용자 보고서에도 보인다.
    comparison = manifest["channel_comparison"]
    assert comparison["compared"] is True
    assert comparison["match_basis"] == (
        "country_scoped_publication_number_variants"
    )
    assert comparison["epo"]["unique_identified"] >= 1
    assert comparison["counts"]["both"] + comparison["counts"]["epo_only"] == (
        comparison["epo"]["unique_identified"]
    )
    detail = client.get(f"/api/jobs/{job['id']}").json()
    # 교차 발견 비교표는 내부 기록으로만 남는다. 사용자 보고서에는 채널별
    # 성공·실패와 실제 검색식이 대신 들어간다.
    assert "웹/EPO 채널 교차 발견" not in detail["result_text"]
    assert "## 채널별 실행 결과" in detail["result_text"]
    assert "실제로 실행된 EPO 검색식" in detail["result_text"]


def test_only_the_spec_assisted_epo_lane_gets_the_spec_body(client, epo_on) -> None:
    """레인을 가르는 것은 이름이 아니라 **입력**이다.

    epo:claim_only 에 명세서가 새면 두 레인은 같은 실험이 되고, 3-2 에서
    "명세서가 EPO 후보를 넓혔는가"를 물을 수 없게 된다.
    """
    start_search(client, batch_id=upload_spec(client))

    claim_only = epo_messages("epo:claim_only")
    spec_assisted = epo_messages("epo:spec_assisted")
    assert claim_only, "epo:claim_only 가 모델을 부르지 않았습니다."
    assert spec_assisted, "epo:spec_assisted 가 모델을 부르지 않았습니다."

    for message in claim_only:
        assert SPEC not in message, "청구항 단독 레인에 명세서 본문이 들어갔습니다."
        assert "스트레인 게이지" not in message
        assert "<SPEC_TEXT>" not in message
        assert CLAIM in message

    for message in spec_assisted:
        # 있다고만 하면 부족하다. 경계 안에, 청구항 경계 밖에 있어야 한다.
        spec_at = message.index(SPEC)
        assert message.index("<SPEC_TEXT>") < spec_at < message.index("</SPEC_TEXT>")
        assert message.index("</CLAIM_TEXT>") < spec_at
        assert CLAIM in message


def test_two_epo_lanes_draw_from_one_channel_budget(client, epo_on) -> None:
    """검색 예산을 1회로 줄이면 **채널 전체**에서 한 번만 나가야 한다.

    레인마다 예산을 따로 두면 두 번 나간다. 앞의 예산 테스트는 레인이 하나뿐인
    실행이라 그 차이를 잡지 못한다 — 1 × 1 과 2 × 1 이 같은 숫자를 만든다.
    """
    with session_scope() as session:
        settings_service.update(session, {"epo_max_search_calls": 1})
    try:
        manifest = manifest_of(
            client, start_search(client, batch_id=upload_spec(client))
        )
    finally:
        with session_scope() as session:
            settings_service.update(session, {"epo_max_search_calls": 6})

    budget = manifest["epo"]["channel_budget"]
    assert budget["scope"] == "channel"
    assert budget["max_search_calls"] == 1
    assert budget["searches_used"] == 1

    assert len(manifest["epo"]["lanes"]) == 2, "EPO 레인이 둘이 아닙니다."
    first, second = manifest["epo"]["lanes"]
    assert first["id"] == "epo:claim_only"
    assert first["search_calls"] == 1
    assert first["termination_reason"] == "llm_finished"

    # 두 번째 레인은 자기 몫 없이 시작한다. 같은 지갑을 쓴다는 증거다.
    assert second["id"] == "epo:spec_assisted"
    assert second["search_calls"] == 0
    assert second["queries"] == []
    assert second["termination_reason"] == "search_call_limit"

    # 두 레인이 같은 예산 객체를 봤다.
    for lane in manifest["epo"]["lanes"]:
        assert lane["usage"]["channel_budget"]["searches_used"] == 1

    # 그리고 실제로 나간 OPS 검색도 한 번뿐이다.
    assert len(ops_searches(epo_on)) == 1


# ------------------------------------------------------------------ 취소


def test_cancel_inside_a_running_epo_lane_stops_the_next_one(client, epo_on) -> None:
    """EPO 레인이 **이미 돌고 있는** 순간에 취소를 넣는다.

    작업 시작 전에 취소를 넣으면 웹 레인에서 먼저 걸려 EPO 코드는 한 줄도 돌지
    않는다. 그러면 "취소가 EPO 레인까지 갔다"는 아무것도 확인하지 못한 채
    통과한다. 그래서 취소는 epo:claim_only 가 모델을 부르는 바로 그 순간에
    일어나고, 그 뒤로 두 가지를 본다 — 돌고 있던 레인이 cancelled 로 남는가,
    다음 레인이 시작조차 하지 않는가.
    """
    from app.execution import runner as runner_module

    ops_at_cancel: list[int] = []

    def cancel_when_the_lane_starts(lane: str, request) -> None:
        if lane != "epo:claim_only" or ops_at_cancel:
            return
        ops_at_cancel.append(len(epo_on))
        # HTTP 취소가 채우는 바로 그 집합이다. Provider 쪽 취소 플래그는
        # **일부러** 건드리지 않는다 — 그것까지 세우면 레인이 러너가 아니라
        # Provider 응답으로 취소를 알게 되어, 러너→레인 전달을 지워도 이
        # 테스트가 통과해 버린다.
        runner_module.RUNNER._cancel_requested.add(request.job_id)

    fake_provider.EPO_ON_ROUND = cancel_when_the_lane_starts
    job = start_search(client, batch_id=upload_spec(client))
    manifest = manifest_of(client, job)

    assert ops_at_cancel, "EPO 레인이 시작되지 않아 취소를 걸 지점이 없었습니다."
    # 웹 레인은 취소 전에 이미 끝나 있었지만 사용자가 누른 것은 EPO만 건너뛰는
    # 버튼이 아니라 **작업 전체 취소**다. 부분 결과는 보존하되 최종 상태를 웹
    # 성공 verdict 로 다시 덮어쓰면 안 된다.
    assert job["status"] == "CANCELLED", job.get("errors")
    assert job["error_code"] == "CANCELLED"
    assert manifest["reported"] is not None

    lanes = {lane["id"]: lane for lane in manifest["epo"]["lanes"]}
    assert set(lanes) == {"epo:claim_only", "epo:spec_assisted"}

    running = lanes["epo:claim_only"]
    assert running["status"] == "cancelled"
    assert running["cancelled"] is True
    assert running["termination_reason"] == "cancelled"

    following = lanes["epo:spec_assisted"]
    assert following["status"] == "cancelled"
    assert following["rounds"] == []
    assert following["queries"] == []
    assert following["candidates"] == []
    # 시작조차 하지 않았다. 레인이 한 번이라도 돌면 에이전트가 자기 사용량을
    # 남기므로, usage 가 비어 있다는 것은 러너가 레인을 열기 전에 멈췄다는 뜻이다.
    assert following["usage"] == {}
    assert following["error"] == "사용자가 실행을 취소했습니다."

    # 다음 레인은 모델을 부르지 않았다.
    assert [entry["lane"] for entry in fake_provider.EPO_REQUESTS] == [
        "epo:claim_only"
    ], "취소 뒤에 다음 레인이 모델을 불렀습니다."
    # OPS 도 마찬가지다. 취소 시점 이후로 한 건도 나가지 않았다.
    assert len(epo_on) == ops_at_cancel[0], "취소 뒤에 OPS 를 불렀습니다."


# --------------------------------------------------- 실패해도 manifest 는 남는다


def test_manifest_exists_when_the_epo_lane_fails(client, epo_on) -> None:
    fake_provider.EPO_SCRIPT["epo:claim_only"] = ["깨진 응답"] * 5
    manifest = manifest_of(client, start_search(client))
    assert manifest["version"] == search_manifest.MANIFEST_VERSION
    assert manifest["epo"]["lanes"], "실패한 레인의 기록이 없습니다."
    assert manifest["timing"]["completed_at"]


def test_usage_is_recorded_per_lane(client, epo_on) -> None:
    manifest = manifest_of(client, start_search(client))
    lane = manifest["epo"]["lanes"][0]
    assert lane["usage"]["lane_id"] == "epo:claim_only"
    assert lane["usage"]["model_calls"] == 1
    assert "channel_budget" in lane["usage"]
    assert manifest["epo"]["usage"]["calls_by_kind"]["search"]["count"] >= 1


# ------------------------------- 웹 후보의 공식 문헌 2차 검증 (Provider 무관)
#
# EPO **검색 채널**과 다른 단계다. 저쪽은 ARIA 가 OPS 로 직접 검색해 후보를
# 찾는 경로이고, 이쪽은 웹에서 찾은 후보의 번호로 공식 문헌을 받아 대조하는
# 단계다. 예전에는 Codex 실행에서만 돌았고, 그 분기는 제거됐다.


def test_official_verification_is_not_gated_by_provider(client, epo_on) -> None:
    """test-search 는 WEB_SEARCH 정책이다. 그래도 2차 검증이 시도돼야 한다."""
    verification = manifest_of(client, start_search(client))["verification"]

    assert verification["attempted"] is True
    assert verification["counts"]["targets"] >= 1
    assert "Codex" not in verification["reason"]


def test_failed_official_fetch_keeps_the_page_backed_group(client, epo_on) -> None:
    """OPS 에 없는 문헌이라고 페이지 관측 분류를 강등하지 않는다(규칙 4).

    이 픽스처의 OPS 응답에는 후보 번호가 없어 확보가 실패한다. 그것은 "그
    문헌이 없다"가 아니라 "이 채널로 받지 못했다"이므로, 1차에서 페이지 관측
    근거로 받은 정식 분류는 그대로 남아야 한다.
    """
    manifest = manifest_of(client, start_search(client))
    candidate = manifest["reported"]["candidates"][0]

    assert manifest["verification"]["counts"]["fetch_failed"] >= 1
    assert candidate["group"] == "A"
    assert candidate["provisional_group"] is None
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_PAGE
    assert candidate["verification"]["status"] == search_manifest.VERIFY_FETCH_FAILED
    # 공식 근거로 승격된 것이 아니라는 사실은 그대로 남는다.
    assert candidate.get("official_supported_rows", 0) == 0


# ------------------------- EPO 독립 검색 후보를 최종 대응표까지 (전 구간)
#
# 여기서부터는 러너·매니페스트·검증을 한 줄로 꿴다. 단위 테스트가 각 조각을
# 이미 보고 있으므로, 이 절이 보는 것은 "조각들이 실제로 이어져 있는가"다.


def epo_reply(*, shortlist=(), analysis=True) -> str:
    """EPO 레인이 돌려줄 응답. 검색 → 청구항 조회 → 마무리를 한 번에 한다."""
    payload = {
        "strategy": "테스트",
        "actions": [
            {
                "action": "epo_search",
                "query": {"kind": "term", "field": "ta", "value": "robot arm"},
            },
            {
                "action": "epo_fetch_document",
                "doc_number": "EP1000000A1",
                "constituent": "claims",
            },
            {"action": "finish", "notes": "끝"},
        ],
    }
    if analysis:
        payload["claim_analysis"] = {
            "elements": [
                {
                    "id": "E1",
                    "text": "로봇 팔",
                    "essential": True,
                    "synonyms": ["robot arm"],
                },
                {"id": "E2", "text": "힘 센서", "synonyms": ["force sensor"]},
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
                    "reason": "핵심 조합",
                }
            ],
            "search_conditions": [
                {"kind": "ipc", "value": "B25J 9/16", "reason": "로봇 제어 분류"}
            ],
        }
    if shortlist:
        payload["shortlist"] = list(shortlist)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture()
def shortlisted(epo_on):
    """EPO 레인이 EP1000000A1 을 유망 후보로 올린 실행."""
    reply = epo_reply(
        shortlist=[{"doc_number": "EP1000000A1", "reason": "힘 센서가 개시됨"}]
    )
    fake_provider.EPO_SCRIPT["epo:claim_only"] = [reply]
    fake_provider.CLASSIFY_REQUESTS.clear()
    fake_provider.CLASSIFY_FABRICATE = False
    try:
        yield epo_on
    finally:
        fake_provider.CLASSIFY_REQUESTS.clear()
        fake_provider.CLASSIFY_FABRICATE = False


def _epo_only(manifest: dict) -> dict:
    """EPO 독립 검색만 데려온 후보 하나."""
    rows = [
        item
        for item in manifest["reported"]["candidates"]
        if search_manifest.discovery_origins(item) == [search_manifest.DISCOVERY_EPO]
    ]
    assert rows, "EPO 독립 검색 후보가 대응표에 없습니다."
    return rows[0]


def test_claim_analysis_reaches_the_manifest(client, shortlisted) -> None:
    """추가 모델 턴 없이 첫 EPO 응답의 검색 전략이 기록에 남는다."""
    manifest = manifest_of(client, start_search(client))
    lane = next(
        lane for lane in manifest["epo"]["lanes"] if lane["id"] == "epo:claim_only"
    )

    analysis = lane["claim_analysis"]
    assert [item["id"] for item in analysis["elements"]] == ["E1", "E2"]
    assert analysis["elements"][0]["synonyms"] == ["robot arm"]
    # 필수 여부를 적지 않은 구성은 '판단 없음'으로 남는다.
    assert analysis["elements"][1]["essential"] is None
    assert analysis["relations"][0]["description"]
    assert analysis["search_conditions"][0]["value"] == "B25J 9/16"
    # 이 레인의 모델 호출은 검색 라운드 한 번뿐이다.
    assert len(epo_messages("epo:claim_only")) == 1


def test_epo_only_candidate_reaches_the_main_table_with_official_support(
    client, shortlisted
) -> None:
    """공식 응답에 구성 대응이 대조되면 EPO 단독 후보가 정식 분류를 받는다."""
    manifest = manifest_of(client, start_search(client))
    candidate = _epo_only(manifest)

    assert candidate["doc_number"] == "EP1000000A1"
    assert candidate["channel"] == search_manifest.CHANNEL_PATENT_DB
    assert candidate["group"] in ("A", "B", "C")
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_OFFICIAL
    assert candidate["verification"]["status"] == search_manifest.VERIFY_PROMOTED
    assert candidate["official_supported_rows"] >= 1
    # 존재하지 않는 페이지 분류를 만들지 않는다.
    assert "page_classification" not in candidate
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["page_supported_rows"] == 0
    # 원본 레인 기록은 그대로 남는다.
    lane = next(
        lane for lane in manifest["epo"]["lanes"] if lane["id"] == "epo:claim_only"
    )
    assert lane["shortlist"][0]["doc_number"] == "EP1000000A1"
    assert lane["candidates"]


def test_official_mismatch_keeps_the_epo_candidate_provisional(
    client, shortlisted
) -> None:
    """근거 문장이 대조되지 않으면 삭제하지 않고 잠정으로 남긴다."""
    fake_provider.CLASSIFY_FABRICATE = True
    manifest = manifest_of(client, start_search(client))
    candidate = _epo_only(manifest)

    assert candidate["group"] is None
    assert candidate["provisional_group"] in ("A", "B", "C")
    assert candidate["verification"]["status"] == (
        search_manifest.VERIFY_EVIDENCE_MISMATCH
    )
    assert "page_classification" not in candidate


def test_the_verification_reuses_the_lane_artifacts(client, shortlisted) -> None:
    """EPO 레인이 이미 받은 청구항을 검증 단계가 다시 내려받지 않는다."""
    job = start_search(client)
    manifest = manifest_of(client, job)

    usage = manifest["verification"]["usage"]
    assert usage["reused_artifact_calls"] >= 1
    # 같은 문헌의 청구항을 두 번 받지 않았다.
    claims_calls = [
        url
        for url in shortlisted
        if "EP.1000000.A1" in url and url.endswith("/claims")
    ]
    assert len(claims_calls) == 1, claims_calls


def test_a_document_found_by_both_channels_keeps_both_origins(
    client, epo_on
) -> None:
    """웹 후보와 같은 공개번호를 EPO 도 찾으면 후보 하나에 두 출처가 남는다."""
    # 웹 대역이 보고하는 후보 번호는 AB1234 다. EPO 레인도 같은 번호를 골랐다고
    # 하면, 검색 결과에 없는 번호라 shortlist 에서 걸러진다. 그래서 이 검사는
    # 병합 함수를 직접 부른다 — 러너 경로의 계약은 위 테스트가 이미 본다.
    manifest = manifest_of(client, start_search(client))
    lane = next(
        lane for lane in manifest["epo"]["lanes"] if lane["id"] == "epo:claim_only"
    )
    number = lane["candidates"][0]["doc_number"]
    reported = {
        "candidates": [
            {
                "index": 1,
                "doc_number": number,
                "doi": "",
                "group": None,
                "provisional_group": "C",
                "channel": search_manifest.CHANNEL_WEB,
                "discovery_origins": [search_manifest.DISCOVERY_WEB],
                "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
                "mapping": [],
            }
        ]
    }
    lane = {**lane, "shortlist": [{"doc_number": number, "reason": "같은 문헌"}]}
    merged, notes = search_manifest.merge_epo_discoveries(
        reported, {"enabled": True, "lanes": [lane]}
    )

    assert len(merged["candidates"]) == 1
    assert merged["candidates"][0]["discovery_origins"] == ["web", "epo"]
    assert merged["candidates"][0]["provisional_group"] == "C"
    assert notes


def test_limit_exclusions_are_recorded(client, shortlisted) -> None:
    """상한 때문에 빠진 것은 사유와 함께 기록에 남는다."""
    with session_scope() as session:
        settings_service.update(session, {"epo_verification_targets": 1})
    try:
        manifest = manifest_of(client, start_search(client))
    finally:
        with session_scope() as session:
            settings_service.update(session, {"epo_verification_targets": 8})

    verification = manifest["verification"]
    assert verification["limits"]["verification_targets"] == 1
    excluded = verification["excluded_candidates"]
    assert excluded, "상한으로 빠진 후보의 사유가 기록되지 않았습니다."
    assert all(row["reason_code"] == "verification_target_limit" for row in excluded)
    # 그 후보는 사라지지 않고 사유를 든 채 남는다.
    numbers = {item["doc_number"] for item in manifest["reported"]["candidates"]}
    assert {row["doc_number"] for row in excluded} <= numbers


def test_settings_drive_the_lane_budget(client, shortlisted) -> None:
    """상한은 코드가 아니라 설정에서 온다."""
    with session_scope() as session:
        settings_service.update(
            session,
            {"epo_max_results_per_query": 3, "epo_shortlist_limit": 2},
        )
    try:
        manifest = manifest_of(client, start_search(client))
    finally:
        with session_scope() as session:
            settings_service.update(
                session,
                {"epo_max_results_per_query": 20, "epo_shortlist_limit": 5},
            )

    budget = manifest["epo"]["lane_budget"]
    assert budget["max_results_per_query"] == 3
    assert budget["shortlist_limit"] == 2


# ------------------- 경계 사례: 웹이 실패해도 EPO 결과는 살아남는다
#
# 두 채널은 격리되어 있다. 웹 레인의 출력 형식 오류가, EPO 레인이 실제로 받아
# 아티팩트로 보존한 공식 응답을 무효로 만들 이유는 없다. 다만 그 실행이 "두
# 채널을 다 돌린 결과"처럼 보여서도 안 된다.


def test_epo_only_report_when_the_web_audit_block_is_missing(
    client, shortlisted
) -> None:
    """웹 감사 블록이 없어도 EPO 후보로 보고서를 만든다."""
    job = start_search(client, claim=f"{CLAIM}\nSEARCH_NOLOG")
    manifest = manifest_of(client, job)

    # 보고서가 실제로 나왔다. 예전에는 여기서 결과가 통째로 비었다.
    assert (job["result_text"] or "").strip()
    assert manifest["reported"] is not None
    numbers = {item["doc_number"] for item in manifest["reported"]["candidates"]}
    assert "EP1000000A1" in numbers
    # 후보는 EPO 발견뿐이다. 웹이 찾은 문헌은 하나도 없다.
    assert all(
        search_manifest.DISCOVERY_EPO
        in search_manifest.discovery_origins(item)
        for item in manifest["reported"]["candidates"]
    )

    # 웹의 실패 상태는 세 곳에 그대로 남는다.
    assert manifest["error"], "웹 파싱 실패 사유가 사라졌습니다."
    assert manifest["reported"]["web_report_error"]
    assert job["search_manifest_error"]
    assert "웹 채널의 검색 결과를 읽지 못했습니다" in job["result_text"]
    # 실행 기록에도 남긴다. 보고서가 나왔다고 실패가 지워지지 않는다.
    assert any("웹 채널의 검색 감사 블록" in error for error in job["errors"])


def test_epo_only_candidates_still_go_through_official_verification(
    client, shortlisted
) -> None:
    """웹이 실패한 실행에서도 EPO 후보의 공식 대조는 그대로 돈다."""
    job = start_search(client, claim=f"{CLAIM}\nSEARCH_NOLOG")
    manifest = manifest_of(client, job)

    assert manifest["verification"]["attempted"] is True
    candidate = next(
        item
        for item in manifest["reported"]["candidates"]
        if item["doc_number"] == "EP1000000A1"
    )
    assert candidate["verification"]["status"] == search_manifest.VERIFY_PROMOTED
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_OFFICIAL


def test_a_web_failure_without_epo_results_still_fails(client, epo_on) -> None:
    """살릴 EPO 결과가 없으면 예전대로 실패한다. 골격을 지어내지 않는다."""
    fake_provider.EPO_SCRIPT["epo:claim_only"] = ["깨진 응답"] * 5
    job = start_search(client, claim=f"{CLAIM}\nSEARCH_NOLOG")
    manifest = manifest_of(client, job)

    assert job["status"] == "FAILED"
    assert manifest["reported"] is None
    assert not (job["result_text"] or "").strip()


# ------------------- 경계 사례: 검증 대상 선택 정책 (전 구간)


def test_epo_candidates_keep_a_verification_slot_under_a_tight_limit(
    client, shortlisted
) -> None:
    """상한이 1이어도 EPO 후보가 웹 후보에 밀려 통째로 빠지지 않는다."""
    with session_scope() as session:
        settings_service.update(session, {"epo_verification_targets": 1})
    try:
        manifest = manifest_of(client, start_search(client))
    finally:
        with session_scope() as session:
            settings_service.update(session, {"epo_verification_targets": 8})

    order = manifest["verification"]["selection_order"]
    assert len(order) == 1
    assert order[0]["doc_number"] == "EP1000000A1"
    # 검색 응답에서 서지는 이미 손에 있다. 청구항·초록만 더 받으면 되므로
    # 아무것도 없는 후보보다 먼저 뽑힌다. 검색 단계가 상세조회를 하지 않게
    # 되면서 "완전 재사용"은 이 경로에서 나오지 않는다.
    assert order[0]["selection_reason"] == "partially_reusable_artifact"
    # 밀려난 웹 후보는 사유와 함께 남는다.
    excluded = manifest["verification"]["excluded_candidates"]
    assert excluded and all(
        row["reason_code"] == "verification_target_limit" for row in excluded
    )


# ------------------- 경계 사례: 최종 선택 턴 (전 구간)


def test_the_selection_turn_runs_once_per_lane(client, shortlisted) -> None:
    """검색 라운드와 다른 프롬프트로 레인마다 한 번 돈다."""
    fake_provider.EPO_SELECTION_REQUESTS.clear()
    manifest = manifest_of(client, start_search(client))

    lanes = [entry["lane"] for entry in fake_provider.EPO_SELECTION_REQUESTS]
    assert lanes == ["epo:claim_only"]
    lane = next(
        lane for lane in manifest["epo"]["lanes"] if lane["id"] == "epo:claim_only"
    )
    assert lane["selection"]["attempted"] is True
    assert lane["selection"]["status"] == "ok"
    # 검색 라운드 사용량과 섞이지 않는다.
    assert lane["usage"]["selection_turn"]["search_calls"] == 0
    assert lane["usage"]["rounds_used"] == 1


def test_the_selection_turn_can_add_a_candidate_the_search_rounds_missed(
    client, epo_on
) -> None:
    """검색 응답에 shortlist 가 없어도 최종 선택 턴이 후보를 올릴 수 있다."""
    fake_provider.EPO_SCRIPT["epo:claim_only"] = [epo_reply()]
    fake_provider.EPO_SELECTION_SCRIPT["epo:claim_only"] = [
        json.dumps(
            {
                "shortlist": [
                    {"doc_number": "EP1000000A1", "reason": "마지막 결과에서 골랐다"}
                ],
                "actions": [{"action": "finish", "notes": "선택 완료"}],
            },
            ensure_ascii=False,
        )
    ]
    try:
        manifest = manifest_of(client, start_search(client))
    finally:
        fake_provider.EPO_SELECTION_SCRIPT.clear()

    lane = next(
        lane for lane in manifest["epo"]["lanes"] if lane["id"] == "epo:claim_only"
    )
    assert [row["doc_number"] for row in lane["shortlist"]] == ["EP1000000A1"]
    assert lane["shortlist"][0]["turn"] == "selection"
    numbers = {item["doc_number"] for item in manifest["reported"]["candidates"]}
    assert "EP1000000A1" in numbers


# ------------------------------------------- 설정이 누락된 경로의 기본값


def test_missing_settings_fall_back_to_the_settings_defaults() -> None:
    """설정이 비어 있어도 코드에 박힌 옛 숫자로 돌아가지 않는다.

    fallback 을 호출부마다 적어 두면 config.DEFAULTS 를 고쳐도 그 경로만
    옛 값으로 돈다. 화면에서 줄인 값이 거기서는 줄지 않고, 아무도 모른다.
    """
    from app import config
    from app.execution import runner

    for key in (
        "epo_max_results_per_query",
        "epo_verification_targets",
        "epo_max_detail_fetches",
        "epo_max_search_calls",
        "epo_shortlist_limit",
        "epo_channel_timeout_seconds",
    ):
        expected = int(config.DEFAULTS[key])
        # 값이 없을 때, 0 일 때, 읽을 수 없을 때 모두 같은 기본값이어야 한다.
        assert runner._setting({}, key) == expected, key
        assert runner._setting({key: 0}, key) == expected, key
        assert runner._setting({key: "이상한 값"}, key) == expected, key
        # 설정이 있으면 그 값을 쓴다.
        assert runner._setting({key: 3}, key) == 3, key

    assert runner._setting({}, "epo_max_results_per_query") == 8
    assert runner._setting({}, "epo_verification_targets") == 4


def test_no_epo_budget_fallback_number_is_hardcoded_at_the_call_site() -> None:
    """예산 fallback 숫자를 호출부에 다시 적지 않는다.

    숫자가 두 곳에 있으면 언젠가 어긋나고, 어긋난 쪽이 조용히 이긴다.
    """
    import inspect

    from app.execution import runner

    source = inspect.getsource(runner)
    for key in (
        "epo_max_results_per_query",
        "epo_verification_targets",
        "epo_max_search_calls",
        "epo_shortlist_limit",
        "epo_channel_timeout_seconds",
    ):
        assert f'_positive(values.get("{key}")' not in source, key


# --------- 권한 거부: 원인 하나가 오류 두 개로 보이지 않게 한다
#
# agy 는 허용 목록에 없는 주소를 자동 거부하고 **턴 전체를 취소**한다. 그러면
# 응답이 비고, 응답이 비었으므로 감사 블록도 없다. 둘은 원인과 증상이지 두 개의
# 원인이 아니다. 나란히 적으면 사용자는 고칠 곳을 두 군데로 읽는다.


def test_permission_denial_is_not_reported_as_a_second_cause(
    client, shortlisted
) -> None:
    """권한 거부가 원인이면 '감사 블록 없음'을 별개 원인으로 적지 않는다."""
    job = start_search(client, claim=f"{CLAIM}\nSEARCH_DENIED")

    errors = job["errors"] or []
    assert any("권한이 거부되었습니다" in error for error in errors), errors
    # 예전에는 여기에 "웹 채널의 검색 감사 블록을 읽지 못했습니다: …" 가 원인
    # 처럼 한 줄 더 붙었다.
    assert not any("검색 감사 블록을 읽지 못했습니다" in error for error in errors)
    # 대신 결과만 적는다. 웹이 후보를 못 냈다는 사실은 사라지면 안 된다.
    assert any("EPO 후보만으로 보고서를 만들었습니다" in error for error in errors)

    # 채널 상태 표도 파서 오류가 아니라 실제 원인을 말한다.
    manifest = manifest_of(client, job)
    assert "권한이 거부되어" in manifest["reported"]["web_report_error"]
    assert "후속 증상" in job["search_manifest_error"]
    assert "검색 감사 블록" not in manifest["reported"]["web_report_error"]

    # 파서가 무엇을 봤는지는 버리지 않고 정규화 메모로 내려 둔다.
    assert any(
        "감사 블록 파싱 결과" in note for note in manifest["normalization_notes"]
    )


def test_the_epo_channel_still_delivers_after_a_permission_denial(
    client, shortlisted
) -> None:
    """웹 권한이 거부돼도 EPO 후보로 보고서는 나온다.

    두 채널은 격리되어 있다. agy 의 승인 파일 문제가, OPS 로 받아 아티팩트로
    보존한 공식 응답을 무효로 만들 이유는 없다.
    """
    job = start_search(client, claim=f"{CLAIM}\nSEARCH_DENIED")
    manifest = manifest_of(client, job)

    assert (job["result_text"] or "").strip()
    numbers = {item["doc_number"] for item in manifest["reported"]["candidates"]}
    assert "EP1000000A1" in numbers
    # 후보에 웹이 데려온 문헌은 하나도 없다. 그 사실도 보고서에 남는다.
    assert all(
        search_manifest.DISCOVERY_EPO in search_manifest.discovery_origins(item)
        for item in manifest["reported"]["candidates"]
    )
    assert "웹 검색" in job["result_text"]
