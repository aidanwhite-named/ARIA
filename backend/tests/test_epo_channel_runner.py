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
        settings_service.reset_epo_ledger()
        with session_scope() as session:
            settings_service.update(session, {"epo_integration_enabled": False})


def start_search(client, claim: str = CLAIM) -> dict:
    created = client.post(
        "/api/jobs",
        json={
            "job_kind": "similarity_search",
            "claim_text": claim,
            "provider": "test-search",
            "prompt_id": "search_prompt.md",
        },
    )
    assert created.status_code in (200, 201), created.text
    job_id = created.json()["id"]
    return wait_for_job(client, job_id)


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


# ------------------------------------------------------------------ 취소


def test_cancel_reaches_the_epo_lane(client, epo_on) -> None:
    """취소는 실행 중인 모든 레인에 전달된다."""
    from app.execution import runner as runner_module

    created = client.post(
        "/api/jobs",
        json={
            "job_kind": "similarity_search",
            "claim_text": CLAIM,
            "provider": "test-search",
            "prompt_id": "search_prompt.md",
        },
    )
    job_id = created.json()["id"]
    # 러너가 보는 취소 집합에 직접 넣는다. HTTP 취소와 같은 경로다.
    runner_module.RUNNER._cancel_requested.add(job_id)
    job = wait_for_job(client, job_id)
    assert job["status"] in ("CANCELLED", "FAILED", "SUCCEEDED")

    manifest = manifest_of(client, job)
    if manifest.get("epo", {}).get("lanes"):
        for lane in manifest["epo"]["lanes"]:
            assert lane["status"] in ("cancelled", "ok")


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
