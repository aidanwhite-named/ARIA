"""유사 문헌 검색 작업의 전 구간.

작업 생성 → 프롬프트 조립 → 실행 → 감사 기록 → 판정 → 저장.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.enums import ErrorCode, JobKind, JobStatus

from .conftest import wait_for_job
from .fake_provider import FABRICATED_QUOTE

CLAIM = "청구항 1. 제1 센서와 제2 센서를 포함하고, 상기 제1 센서는 …"


def _start(client, claim: str = CLAIM, **overrides) -> dict:
    body = {
        "job_kind": JobKind.SIMILARITY_SEARCH.value,
        "provider": "test-search",
        "claim_text": claim,
    }
    body.update(overrides)
    return client.post("/api/jobs", json=body).json()


def test_search_job_runs_and_stores_manifest(client) -> None:
    created = _start(client)
    assert created["job_kind"] == JobKind.SIMILARITY_SEARCH.value
    # 검색 작업은 검색 프롬프트로 돈다. Master Prompt 가 아니다.
    assert created["prompt_id"] == "search_prompt.md"

    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    manifest = job["search_manifest"]
    assert manifest is not None
    assert job["search_manifest_error"] is None

    # ARIA 가 스트림에서 직접 본 것.
    observed = manifest["observed"]
    assert observed["search_queries"] == ["테스트 검색식 A", "테스트 검색식 B"]
    # 열려고 한 것과 실제로 열린 것을 구분한다.
    assert observed["attempted_fetch_urls"] == [
        "https://patents.example.com/AB1234",
        "https://paywall.example.com/x",
    ]
    assert observed["succeeded_fetch_urls"] == ["https://patents.example.com/AB1234"]
    assert observed["tool_call_counts"] == {"WebSearch": 2, "WebFetch": 2}
    assert observed["tool_failures"][0]["input"]["url"].startswith("https://paywall")

    # 모델이 보고한 것.
    reported = manifest["reported"]
    assert [row["round"] for row in reported["rounds"]] == [1, 2]
    assert reported["candidates"][0]["doc_number"] == "AB1234"
    assert reported["access_failures"][0]["reason"] == "유료 논문"

    # 입력과 프롬프트 신원.
    assert manifest["input"]["claim_text"] == CLAIM
    assert manifest["prompt"]["id"] == "search_prompt.md"
    assert len(manifest["prompt"]["sha256"]) == 64
    assert manifest["policy"]["name"] == "web_search"
    assert manifest["policy"]["allowed_tools"] == ["WebSearch", "WebFetch"]


def test_report_is_generated_from_structured_fields(client) -> None:
    """사용자 보고서는 ARIA 가 만든다. 모델 산문이 본문이 되지 않는다."""
    job = wait_for_job(client, _start(client)["id"])
    report = job["result_text"] or ""

    assert "ARIA_SEARCH_LOG_V1" not in report
    assert "유사 특허·논문 검토 후보" not in report
    assert "현재 검색 결과는 문헌 검토 후보 탐색 자료" not in report
    assert "이 보고서는 ARIA 가 검증한 구조화 기록에서 생성했습니다" not in report
    # 모델이 쓴 제목은 보고서 본문이 아니다.
    assert "유사 문헌 검토 후보 (테스트)" not in report
    # 구조화 필드에서 온 값은 들어간다.
    assert "AB1234" in report
    assert "테스트 특허" in report


def test_fabricated_excerpt_does_not_reach_the_user_report(client) -> None:
    """WebFetch 요약 문장이 '원문 직접 발췌' 칸으로 승격되지 않는다."""
    job = wait_for_job(client, _start(client)["id"])
    report = job["result_text"] or ""

    assert FABRICATED_QUOTE not in report
    assert "3컬럼 12행" not in report
    assert "미검증" in report
    assert "직접 인용 검증 불가" in report
    # 대응 설명 자체는 살아남아야 한다.
    assert "센서 모듈 110" in report
    assert "직렬 연결 구조가 같다" in report


def test_model_prose_quotes_never_reach_the_user_report(client) -> None:
    """산문에 원문 인용처럼 쓴 문장이 있어도 보고서로 나가지 않는다."""
    job = wait_for_job(
        client, _start(client, claim=f"{CLAIM}\nSEARCH_QUOTE_PROSE")["id"]
    )
    report = job["result_text"] or ""
    assert job["status"] == JobStatus.SUCCEEDED
    assert FABRICATED_QUOTE not in report
    assert "라고 기재되어 있습니다" not in report

    # 산문은 버리지 않고 감사 자료로 남긴다.
    raw = client.get(f"/api/jobs/{job['id']}/raw?which=model").text
    assert FABRICATED_QUOTE in raw
    assert "원문 직접 발췌가 아닙니다" in raw


def test_manifest_is_written_as_an_artifact(client) -> None:
    job = wait_for_job(client, _start(client)["id"])
    path = Path(job["id"])  # placeholder to keep the intent explicit
    assert path.name == job["id"]

    stored = client.get(f"/api/jobs/{job['id']}").json()["search_manifest"]
    manifest_file = None
    from app.config import PATHS

    manifest_file = PATHS.run_dir(job["id"]) / "search_manifest.json"
    assert manifest_file.exists()
    on_disk = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert on_disk["observed"]["search_queries"] == stored["observed"]["search_queries"]


def test_final_prompt_carries_claim_inside_the_boundary(client) -> None:
    job = wait_for_job(client, _start(client)["id"])
    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    system, _, user = text.partition("===== USER MESSAGE =====")

    assert "{{CLAIM_TEXT}}" not in user
    assert user.count("<CLAIM_TEXT>") == 1
    assert user.index("<CLAIM_TEXT>") < user.index(CLAIM) < user.index("</CLAIM_TEXT>")

    # 검색 실행의 시스템 프롬프트는 신뢰 경계이자 증거 등급 계약이다.
    assert "WebFetch" in system
    assert "원문 확인이 불가능하면" in system
    # 첨부 분석용 런타임 컨텍스트가 섞이면 안 된다.
    assert "별도의 도구는 제공되지 않습니다" not in text


def test_search_without_a_search_call_fails(client) -> None:
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_NO_TOOL")["id"])
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.SEARCH_NOT_PERFORMED
    # 실패해도 관측 기록은 남는다.
    assert job["search_manifest"]["observed"]["tool_call_counts"] == {}


def test_stray_tool_use_fails_the_search(client) -> None:
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_STRAY_TOOL")["id"])
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.TOOL_POLICY_VIOLATION
    assert "Bash" in " ".join(job["errors"])


def test_stray_advertised_tool_fails_the_search(client) -> None:
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_STRAY_ADS")["id"])
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.TOOL_POLICY_VIOLATION


def test_raw_original_claim_is_not_certified_end_to_end(client):
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_RAW_ORIGINAL")["id"])
    assert job["status"] == JobStatus.SUCCEEDED
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    assert candidate["evidence_level"] == "source_page_reviewed"
    assert candidate["verbatim_excerpt"] == ""
    assert candidate["source_location"] == ""
    assert not candidate["mapping"][0]["quote_verified"]
    assert candidate["mapping"][0]["translation"] == ""
    assert "3컬럼 12행" not in job["result_text"]


def test_reviewed_status_is_confirmed_against_observed_fetches(client) -> None:
    """모델이 보고한 URL 이 성공한 WebFetch 와 대조되면 열람 성공으로 인정한다."""
    job = wait_for_job(client, _start(client)["id"])
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    # 대소문자와 끝 슬래시가 달라도 같은 페이지로 본다.
    assert candidate["url"] == "https://PATENTS.example.com/AB1234/"
    assert candidate["evidence_level"] == "source_page_reviewed"
    assert "identifier_unverified" in candidate["verification_issues"]


def test_unread_page_does_not_erase_model_group_or_explanation(client):
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_FAKE_URL")["id"])
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    assert candidate["evidence_level"] == "search_snippet_only"
    assert "source_not_read" in candidate["verification_issues"]
    assert candidate["group"] == "A"
    assert candidate["mapping"]
    assert "LLM 그룹: A" in job["result_text"]


def test_runner_only_calls_mechanical_verification(client, monkeypatch):
    from app import search_verification
    original = search_verification.verify
    calls = []
    def verify(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(search_verification, "verify", verify)
    job = wait_for_job(client, _start(client)["id"])
    assert job["status"] == "SUCCEEDED"
    assert len(calls) == 1
    assert "verification" not in job["search_manifest"]


def test_reviewed_claim_on_failed_fetch_is_downgraded(client) -> None:
    """열려다 실패한 주소를 열람 성공으로 세지 않는다."""
    job = wait_for_job(
        client, _start(client, claim=f"{CLAIM}\nSEARCH_PAYWALL_URL")["id"]
    )
    manifest = job["search_manifest"]
    paywalled = "https://paywall.example.com/x"
    assert paywalled in manifest["observed"]["attempted_fetch_urls"]
    assert paywalled not in manifest["observed"]["succeeded_fetch_urls"]

    candidate = manifest["reported"]["candidates"][0]
    assert candidate["evidence_level"] == "search_snippet_only"
    assert "source_not_read" in candidate["verification_issues"]


def test_missing_audit_block_fails_instead_of_shipping_unverified_prose(client) -> None:
    """보고서를 만들 구조가 없으면 검증되지 않은 산문을 대신 내보내지 않는다."""
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_NOLOG")["id"])
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.INVALID_OUTPUT
    assert not (job["result_text"] or "").strip()
    assert job["search_manifest_error"]
    assert job["search_manifest"]["reported"] is None
    # 관측 기록과 모델 원문은 남는다.
    assert job["search_manifest"]["observed"]["search_queries"]
    assert client.get(f"/api/jobs/{job['id']}/raw?which=model").text.strip()


def test_tool_call_budget_stops_the_run(client) -> None:
    client.put("/api/settings", json={"values": {"max_search_tool_calls": 3}})
    try:
        job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_BUDGET")["id"])
    finally:
        client.put("/api/settings", json={"values": {"max_search_tool_calls": 40}})
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.SEARCH_BUDGET_EXCEEDED


def test_search_job_can_be_cancelled(client) -> None:
    created = _start(client, claim=f"{CLAIM}\nSEARCH_SLOW")
    for _ in range(120):
        if client.get(f"/api/jobs/{created['id']}").json()["status"] == JobStatus.RUNNING:
            break
        import time

        time.sleep(0.1)
    assert client.post(f"/api/jobs/{created['id']}/cancel").json()["cancelled"] is True
    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.CANCELLED


# ------------------------------------------------------------- 입력 검증


def test_search_requires_a_claim(client) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "claim_text": "   ",
        },
    )
    assert response.status_code == 400
    assert "청구항" in response.json()["detail"]


SPEC = "【발명의 설명】 이 출원에서 제어부는 FPGA 로 구현된 신호 처리 회로를 말한다."


def _user_message(client, job_id: str) -> str:
    """최종 프롬프트의 사용자 메시지 부분.

    시스템 프롬프트에는 신뢰 경계 설명이 있어서 경계 태그 이름이 그대로 나온다.
    자료가 실제로 어디에 놓였는지는 사용자 메시지에서만 판단할 수 있다.
    """
    text = client.get(f"/api/jobs/{job_id}/final-prompt").text
    return text.split("===== USER MESSAGE =====", 1)[1]


def _lane_user_message(client, job_id: str, origin: str) -> str:
    text = client.get(f"/api/jobs/{job_id}/final-prompt").text
    lane = text.split(f"===== SEARCH LANE: {origin} =====", 1)[1]
    lane = lane.split("===== SEARCH LANE:", 1)[0]
    return lane.split("===== USER MESSAGE =====", 1)[1]


def _lane_parts(client, job_id, origin):
    text = client.get(f"/api/jobs/{job_id}/final-prompt").text
    system = text.split("===== SYSTEM PROMPT =====\n", 1)[1]
    return tuple(system.split("\n\n===== USER MESSAGE =====\n", 1))


def _upload_spec(client, name: str = "spec.txt", body: bytes | None = None) -> str:
    response = client.post(
        "/api/uploads",
        files=[("files", (name, body if body is not None else SPEC.encode(), "text/plain"))],
        data={"roles": json.dumps(["APPLICATION"])},
    )
    return response.json()["batch_id"]


def test_search_keeps_claim_and_spec_in_one_execution(client):
    from .fake_provider import RECEIVED
    batch = _upload_spec(client)
    job = wait_for_job(client, _start(client, batch_id=batch)["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]
    requests = [r for r in RECEIVED if r.job_id == job["id"]]
    assert len(requests) == 1
    message = requests[0].user_message
    assert message.index("<CLAIM_TEXT>") < message.index(CLAIM) < message.index("</CLAIM_TEXT>")
    assert message.index("<SPEC_TEXT>") < message.index(SPEC) < message.index("</SPEC_TEXT>")
    assert message.index("</CLAIM_TEXT>") < message.index("<SPEC_TEXT>")
    assert job["search_manifest"]["input"]["spec_document"]["filename"] == "spec.txt"
    assert "search_lanes" not in job["search_manifest"]


def test_spec_expansion_is_the_single_models_output(client):
    job = wait_for_job(client, _start(client, batch_id=_upload_spec(client))["id"])
    manifest = job["search_manifest"]
    assert manifest["reported"]["term_expansions"][0]["claim_term"] == "제어부"
    assert [c["doc_number"] for c in manifest["reported"]["candidates"]] == ["AB1234", "CD5678"]
    assert "candidate_merge" not in manifest["policy"]





def test_search_without_a_spec_says_nothing_about_one(client) -> None:
    """명세서를 넣지 않은 실행은 이 기능이 없던 때와 같아야 한다."""
    job = wait_for_job(client, _start(client)["id"])
    message = _user_message(client, job["id"])
    assert "SPEC_TEXT" not in message
    assert "출원발명 문서" not in message

    assert job["search_manifest"]["input"]["spec_document"] is None
    assert job["search_manifest"]["reported"]["term_expansions"] == []
    assert "출원발명 문서를 이용한 별도 검색 확장" not in job["result_text"]


def test_search_rejects_a_second_attachment(client) -> None:
    batch = client.post(
        "/api/uploads",
        files=[
            ("files", ("spec.txt", SPEC.encode(), "text/plain")),
            ("files", ("more.txt", b"another", "text/plain")),
        ],
        data={"roles": json.dumps(["APPLICATION", "APPLICATION"])},
    ).json()["batch_id"]
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "claim_text": CLAIM,
            "batch_id": batch,
        },
    )
    assert response.status_code == 400
    assert "1건" in response.json()["detail"]


def test_search_rejects_a_spec_it_could_not_read(client) -> None:
    """본문을 못 읽은 명세서로 조용히 실행하지 않는다."""
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "claim_text": CLAIM,
            "batch_id": _upload_spec(client, "empty.txt", b"   \n  "),
        },
    )
    assert response.status_code == 400
    assert "본문을 읽지 못했습니다" in response.json()["detail"]


def test_search_rejects_attachments(client) -> None:
    upload = client.post(
        "/api/uploads",
        files=[("files", ("a.txt", b"hello", "text/plain"))],
        data={"roles": json.dumps(["CITATION"])},
    ).json()
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "claim_text": CLAIM,
            "batch_id": upload["batch_id"],
        },
    )
    assert response.status_code == 400
    assert "첨부" in response.json()["detail"]


def test_search_rejects_followup_lineage(client) -> None:
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "claim_text": CLAIM,
            "source_job_id": "whatever",
            "relation_type": "CONTINUED",
        },
    )
    assert response.status_code == 400


def _gap_search_source() -> str:
    from app.db import session_scope
    from app.models import ExecutionJob

    manifest = {
        "version": 1,
        "threshold": 80,
        "items": [
            {
                "id": "C001",
                "claim": "청구항 1",
                "symbol": "(A)",
                "feature": "이미 대응된 일반 센서 구성",
                "similarity": 92,
                "status": "matched",
                "difference": "",
                "search_eligible": False,
            },
            {
                "id": "C002",
                "claim": "청구항 1",
                "symbol": "(B)",
                "feature": "두 센서 신호를 결합하여 제어하는 구성",
                "similarity": 72,
                "status": "below_threshold",
                "difference": "결합 신호에 따른 제어 관계가 확인되지 않음",
                "search_eligible": True,
            },
            {
                "id": "C003",
                "claim": "청구항 1",
                "symbol": "(C)",
                "feature": "결과를 원격 장치로 전송하는 구성",
                "similarity": None,
                "status": "not_found",
                "difference": "대응 문헌을 찾지 못함",
                "search_eligible": True,
            },
        ],
    }
    with session_scope() as session:
        source = ExecutionJob(
            job_kind=JobKind.PATENT_ANALYSIS,
            prompt_name="구성대비 원본",
            prompt_snapshot="테스트",
            output_mode="markdown",
            claim_text=CLAIM,
            prompt_capabilities=["claim_component_analysis_v1"],
            analysis_manifest=manifest,
            provider="test",
            status=JobStatus.SUCCEEDED,
            result_text="SEARCH_SOURCE_REPORT_MUST_NOT_BE_COPIED",
        )
        session.add(source)
        session.flush()
        return source.id


def test_gap_search_uses_selected_components_in_combined_then_individual_order(
    client,
) -> None:
    source_id = _gap_search_source()
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "source_job_id": source_id,
            "search_component_ids": ["C003", "C002"],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["claim_text"] == CLAIM
    assert "strategy" not in created["search_focus"]
    # 선택 순서가 아니라 원 분석의 구성 순서를 보존한다.
    assert [row["id"] for row in created["search_focus"]["components"]] == [
        "C002",
        "C003",
    ]

    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]
    manifest = job["search_manifest"]
    assert "search_strategy" not in manifest["policy"]
    assert manifest["input"]["search_focus"]["source_job_id"] == source_id
    report = job["result_text"] or ""
    assert "# 미대응 구성 보완 검색 후보" not in report
    assert "## 검색 대상 미대응 구성" in report
    assert "1차 조합 검색 → 2차 개별 검색" not in (job["result_text"] or "")

    final_prompt = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "1차 — 조합 검색" not in final_prompt
    assert "<SEARCH_FOCUS>" in final_prompt
    assert "두 센서 신호를 결합하여 제어하는 구성" in final_prompt
    assert "결과를 원격 장치로 전송하는 구성" in final_prompt
    assert "이미 대응된 일반 센서 구성" not in final_prompt
    assert "SEARCH_SOURCE_REPORT_MUST_NOT_BE_COPIED" not in final_prompt


def test_gap_search_rejects_a_component_that_is_not_searchable(client) -> None:
    source_id = _gap_search_source()
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test-search",
            "source_job_id": source_id,
            "search_component_ids": ["C001"],
        },
    )
    assert response.status_code == 400
    assert "검색할 수 없거나" in response.json()["detail"]


def test_search_rejects_provider_without_search_policy(client) -> None:
    """검색 정책을 선언하지 않은 Provider 로는 검색을 시작하지 않는다."""
    response = client.post(
        "/api/jobs",
        json={
            "job_kind": JobKind.SIMILARITY_SEARCH.value,
            "provider": "test",
            "claim_text": CLAIM,
        },
    )
    assert response.status_code == 400
    assert "웹 검색 정책을 지원하지 않습니다" in response.json()["detail"]


def test_unknown_job_kind_is_rejected(client) -> None:
    response = client.post(
        "/api/jobs",
        json={"job_kind": "whatever", "provider": "test", "claim_text": CLAIM},
    )
    assert response.status_code == 422


# --------------------------------------------- 기존 분석 경로 회귀 확인


def test_analysis_job_is_unchanged_and_uses_no_tools(client) -> None:
    prompt = client.post(
        "/api/prompts", json={"name": "회귀 확인용", "body": "분석하십시오."}
    ).json()
    upload = client.post(
        "/api/uploads",
        files=[("files", ("citation.txt", b"citation document", "text/plain"))],
    ).json()
    created = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    job = wait_for_job(client, created["id"])

    assert job["job_kind"] == JobKind.PATENT_ANALYSIS.value
    assert job["status"] == JobStatus.SUCCEEDED
    # 분석 작업에는 검색 기록이 생기지 않는다.
    assert job["search_manifest"] is None
    assert job["search_manifest_error"] is None


def test_history_reports_job_kind(client) -> None:
    search_job = wait_for_job(client, _start(client)["id"])
    rows = client.get("/api/history").json()
    row = next(item for item in rows if item["id"] == search_job["id"])
    assert row["job_kind"] == JobKind.SIMILARITY_SEARCH.value


# ------------------------------------------------- 실행 전 크기 안내(preflight)


def _preflight(client, **overrides) -> dict:
    body = {
        "job_kind": JobKind.SIMILARITY_SEARCH.value,
        "provider": "test-search",
        "claim_text": CLAIM,
    }
    body.update(overrides)
    response = client.post("/api/jobs/preflight", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_preflight_matches_what_the_runner_actually_sends(client) -> None:
    """화면이 안내하는 크기는 실행이 실제로 보내는 크기여야 한다.

    두 곳이 각자 조립하면 안내한 숫자와 실행이 막히는 지점이 어긋나고, 그
    어긋남은 실행이 실패한 뒤에야 드러난다. 그래서 같은 함수를 부르는지를
    숫자로 고정한다.
    """
    batch_id = _upload_spec(client)
    ahead = _preflight(client, batch_id=batch_id)
    job = wait_for_job(client, _start(client, batch_id=batch_id)["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    # 검색은 레인이 둘이고, 한도는 레인마다 따로 걸린다.
    lanes = {lane["id"]: lane for lane in ahead["lanes"]}
    assert set(lanes) == {"single"}
    assert ahead["bytes"] == max(lane["bytes"] for lane in ahead["lanes"])

    # 실행이 남긴 레인 프롬프트와 대조한다. 저장 파일은 구분 머리글을 붙이므로
    # 시스템 프롬프트와 사용자 메시지 본문만 떼어 낸다.
    for origin, lane in lanes.items():
        system, user = _lane_parts(client, job["id"], origin)
        assert lane["chars"] == len(system) + len(user), origin
        assert lane["bytes"] == len(system.encode("utf-8")) + len(
            user.encode("utf-8")
        ), origin

    # 저장된 합계와도 어긋나지 않아야 한다.
    assert sum(lane["chars"] for lane in ahead["lanes"]) == job["final_prompt_chars"]


def test_preflight_reports_the_provider_byte_limit_not_just_chars(client) -> None:
    """ARIA 글자 수 제한을 꺼도 최종 UTF-8 바이트 수는 보고한다."""
    ahead = _preflight(client, batch_id=_upload_spec(client))
    assert ahead["char_budget"] is None
    # 이 테스트 Provider 는 바이트 한도를 선언하지 않는다. 그 사실이 그대로
    # 드러나야 한다 — 없는 한도를 0 이나 추정값으로 채우지 않는다.
    assert ahead["byte_budget"] is None
    assert ahead["over_bytes"] is False
    assert ahead["blocked"] is False
    # 바이트는 언제나 계산해서 돌려준다. 한도가 없어도 크기는 사실이다.
    assert ahead["bytes"] > ahead["chars"]


def test_preflight_does_not_create_a_job(client) -> None:
    before = len(client.get("/api/history").json())
    _preflight(client)
    after = len(client.get("/api/history").json())
    assert before == after


# ------------------------------------------- 접근 실패는 실행을 끝내지 않는다
#
# 허용 목록 밖 호스트, 403, 로그인 요구, 유료벽 — 검색 실행에서 흔한 일이다.
# 이것들이 실행을 중단시키면 그때까지 한 검색이 통째로 버려진다. 열지 못한
# 문헌은 미검증 후보로 남고, 감사 블록은 어떤 경우에도 나가야 한다.


def test_blocked_pages_keep_the_audit_block_and_the_rest_of_the_search(
    client,
) -> None:
    """403·로그인·유료벽에 다 막혀도 후보와 감사 블록이 살아남는다."""
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_BLOCKED")["id"])

    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]
    manifest = job["search_manifest"]
    # 감사 블록을 읽었다. 이것이 없으면 결과가 통째로 사라진다.
    assert job["search_manifest_error"] is None
    assert manifest["reported"] is not None

    observed = manifest["observed"]
    # 한 건도 열지 못했다는 사실은 그대로 기록된다.
    assert observed["succeeded_fetch_urls"] == []
    assert len(observed["attempted_fetch_urls"]) == 2

    candidates = manifest["reported"]["candidates"]
    assert len(candidates) == 2
    for item in candidates:
        assert item["evidence_level"] == "search_snippet_only"
        assert item["group"] is None
        assert item["mapping"] == []
        # 열지 못했어도 검색 결과에서 본 제목은 남는다.
        assert item["reported_title"]
        assert item["title"] == ""

    # 접근 실패 사유가 남는다 — 허용 목록 밖 호스트도 여기에 적힌다.
    reasons = {row["reason"] for row in manifest["reported"]["access_failures"]}
    assert "로그인 요구" in reasons
    assert "유료벽 403" in reasons
    assert "허용 목록에 없는 호스트라 열지 않음" in reasons

    # 보고서에도 미검증 제목이 링크·상태와 함께 나간다.
    report = job["result_text"]
    assert "미검증" in report
    assert "미확인" in report


def test_a_host_outside_the_allowlist_is_never_opened(client) -> None:
    """허용 목록 밖 주소로는 열람 호출 자체가 나가지 않는다."""
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_BLOCKED")["id"])
    attempted = job["search_manifest"]["observed"]["attempted_fetch_urls"]

    assert not any("sciencedirect" in url for url in attempted)
    blocked = next(
        item
        for item in job["search_manifest"]["reported"]["candidates"]
        if "sciencedirect" in item["url"]
    )
    assert blocked["evidence_level"] == "search_snippet_only"
    assert "source_not_read" in blocked["verification_issues"]


# --- 선택적 검색 기준일 -----------------------------------------------------


def test_a_search_without_a_cutoff_applies_no_date_condition(client) -> None:
    """비어 있으면 날짜 조건이 없다. 실행일이 대신 들어가지 않는다."""
    created = _start(client)
    assert created["search_cutoff_date"] is None

    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    date_filter = job["search_manifest"]["date_filter"]
    assert date_filter["cutoff"] == ""
    assert date_filter["applied"] is False
    assert date_filter["excluded"] == []
    assert "날짜 제한 없음" in job["result_text"]


def test_an_empty_cutoff_string_is_stored_as_null(client) -> None:
    """빈 문자열과 미지정을 다르게 저장하지 않는다. 둘 다 '조건 없음'이다."""
    created = _start(client, search_cutoff_date="")
    assert created["search_cutoff_date"] is None


def test_a_search_with_a_cutoff_records_it_end_to_end(client) -> None:
    created = _start(client, search_cutoff_date="2024-12-31")
    assert created["search_cutoff_date"] == "2024-12-31"

    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    date_filter = job["search_manifest"]["date_filter"]
    assert date_filter["cutoff"] == "2024-12-31"
    assert date_filter["applied"] is True
    # 판정 기준은 공개일이다.
    assert date_filter["basis"] == "publication_date"
    # 어느 채널이 검색 단계에서 좁혔고 어느 채널이 뒤에서 걸렀는가.
    assert "channel_applied" not in date_filter
    assert "2024-12-31 까지 공개된 문헌" in job["result_text"]


def test_a_compact_cutoff_is_normalised(client) -> None:
    created = _start(client, search_cutoff_date="20241231")
    assert created["search_cutoff_date"] == "2024-12-31"


def test_an_unreadable_cutoff_is_refused(client) -> None:
    """조용히 고치지 않는다. 검색어가 틀렸다는 것은 사람이 알아야 할 사실이다."""
    body = {
        "job_kind": JobKind.SIMILARITY_SEARCH.value,
        "provider": "test-search",
        "claim_text": CLAIM,
        "search_cutoff_date": "2024/12/31",
    }
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 422


def test_a_web_candidate_without_a_publication_date_is_marked_not_dropped(
    client,
) -> None:
    """공개일을 모르는 것과 기준일 뒤에 공개된 것은 다른 사실이다.

    웹 후보는 모델이 공개일을 보고하지 않으므로 이 상태로 도착하는 일이 흔하다.
    그 후보를 지우면 "확인하지 못했다"가 "대상이 아니다"로 바뀐다.
    """
    created = _start(client, search_cutoff_date="1900-01-01")
    job = wait_for_job(client, created["id"])
    manifest = job["search_manifest"]

    candidate = next(
        item
        for item in manifest["reported"]["candidates"]
        if item["doc_number"] == "AB1234"
    )
    assert candidate["publication_date_status"] == "publication_date_unknown"
    assert "공개일" in candidate["publication_date_detail"]

    date_filter = manifest["date_filter"]
    assert date_filter["applied"] is True
    assert date_filter["unknown_publication_date"] >= 1
    assert all(row["doc_number"] != "AB1234" for row in date_filter["excluded"])
    # 보고서도 지우지 않았다는 사실을 적는다.
    assert "공개일 미확인 후보" in job["result_text"]


def test_search_prompt_never_carries_the_analysis_output_rules(client) -> None:
    """분석용 기계 판독 블록 규칙은 검색 실행에 붙지 않는다.

    규칙을 ARIA 가 붙이게 되면서, 붙이지 않는 경로를 못박아 둘 필요가 생겼다.
    검색은 자기 출력 계약(search_manifest)이 따로 있다. 두 계약이 한 프롬프트에
    같이 들어가면 모델이 어느 형식으로 답할지가 흔들린다.
    """
    created = _start(client)
    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "ARIA_COMPONENT_ANALYSIS_V1" not in text
    assert "ARIA_CITATION_MAPPING_V1" not in text

    # 규칙을 주지 않았으니 읽지도 않는다. 없는 블록을 찾다 실패를 기록하면
    # 검색 실행마다 근거 없는 오류가 남는다.
    assert job["analysis_manifest"] is None
    assert job["analysis_manifest_error"] is None
    assert job["citation_mapping"] is None
    assert job["citation_mapping_error"] is None
