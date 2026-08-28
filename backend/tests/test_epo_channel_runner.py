"""러너 2채널 — 레인 4개, 격리, 실패 전파 없음, 취소, manifest v5.

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


def test_manifest_version_is_five(client, epo_on) -> None:
    manifest = manifest_of(client, start_search(client))
    assert manifest["version"] == 5


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
    assert manifest["version"] == 5
    assert manifest["epo"]["lanes"], "실패한 레인의 기록이 없습니다."
    assert manifest["timing"]["completed_at"]


def test_usage_is_recorded_per_lane(client, epo_on) -> None:
    manifest = manifest_of(client, start_search(client))
    lane = manifest["epo"]["lanes"][0]
    assert lane["usage"]["lane_id"] == "epo:claim_only"
    assert lane["usage"]["max_rounds"] == 2
    assert "channel_budget" in lane["usage"]
    assert manifest["epo"]["usage"]["calls_by_kind"]["search"]["count"] >= 1
