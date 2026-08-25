"""유사 문헌 검색 작업의 전 구간.

작업 생성 → 프롬프트 조립 → 실행 → 감사 기록 → 판정 → 저장.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import search_report
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
    assert "# 유사 특허·논문 검토 후보" in report
    assert search_report.DISCLAIMER in report
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
    assert "원문에서 확인되지 않음" in report
    assert "확인 필요" in report
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
    assert "직접 인용문처럼 표시하지" in system
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


def test_raw_original_claim_is_downgraded_end_to_end(client) -> None:
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_RAW_CLAIM")["id"])
    assert job["status"] == JobStatus.SUCCEEDED
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    assert candidate["provenance"] == "webfetch_summary"
    assert candidate["original_verified"] is False
    assert candidate["verbatim_excerpt"] == "원문에서 확인되지 않음"
    # 위치는 값이 있어도 보존하지 않는다.
    assert candidate["source_location"] == "확인 필요"
    assert candidate["mapping"][0]["source_location"] == "확인 필요"
    assert candidate["mapping"][0]["verbatim_excerpt"] == "원문에서 확인되지 않음"
    assert candidate["mapping"][0]["translation"] == "원문에서 확인되지 않음"
    assert job["search_manifest"]["normalization_notes"]
    assert "3컬럼 12행" not in (job["result_text"] or "")


def test_reviewed_status_is_confirmed_against_observed_fetches(client) -> None:
    """모델이 보고한 URL 이 성공한 WebFetch 와 대조되면 열람 성공으로 인정한다."""
    job = wait_for_job(client, _start(client)["id"])
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    # 대소문자와 끝 슬래시가 달라도 같은 페이지로 본다.
    assert candidate["url"] == "https://PATENTS.example.com/AB1234/"
    assert candidate["page_fetch_succeeded"] is True
    assert candidate["evidence_status"] == "source_page_reviewed"


def test_reviewed_claim_on_never_fetched_url_is_downgraded(client) -> None:
    job = wait_for_job(client, _start(client, claim=f"{CLAIM}\nSEARCH_FAKE_URL")["id"])
    candidate = job["search_manifest"]["reported"]["candidates"][0]
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["evidence_status"] == "candidate_only"
    assert candidate["provenance"] == "search_snippet"
    assert any(
        "대조되지 않아" in note
        for note in job["search_manifest"]["normalization_notes"]
    )
    # 열람 기록이 없는 후보는 그룹이 아니라 미확인 단서로 간다.
    report = job["result_text"] or ""
    assert "## 미확인 검색 단서" in report
    assert "성공한 페이지 열람 기록이 없습니다" in report
    assert "테스트 특허" not in report


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
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["evidence_status"] == "candidate_only"


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


def _upload_spec(client, name: str = "spec.txt", body: bytes | None = None) -> str:
    response = client.post(
        "/api/uploads",
        files=[("files", (name, body if body is not None else SPEC.encode(), "text/plain"))],
        data={"roles": json.dumps(["APPLICATION"])},
    )
    return response.json()["batch_id"]


def test_search_runs_claim_only_and_spec_assisted_in_isolated_contexts(client) -> None:
    """기본 검색 컨텍스트에는 명세서가 한 글자도 들어가지 않는다."""
    job = wait_for_job(
        client, _start(client, batch_id=_upload_spec(client))["id"]
    )
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]

    claim_message = _lane_user_message(client, job["id"], "claim_only")
    assisted_message = _lane_user_message(client, job["id"], "spec_assisted")
    assert "SPEC_TEXT" not in claim_message
    assert SPEC not in claim_message
    assert CLAIM in claim_message

    # 명세서는 자기 경계 안에, 청구항 경계 밖에 있다.
    spec_at = assisted_message.index(SPEC)
    assert (
        assisted_message.index("<SPEC_TEXT>")
        < spec_at
        < assisted_message.index("</SPEC_TEXT>")
    )
    assert assisted_message.index("</CLAIM_TEXT>") < spec_at
    # 인용발명 문헌처럼 첨부 절로 붙지 않는다.
    assert "[ATTACHMENTS / 첨부 자료]" not in assisted_message

    spec = job["search_manifest"]["input"]["spec_document"]
    assert spec["filename"] == "spec.txt"
    assert spec["char_count"] == len(SPEC)
    assert job["search_manifest"]["input"]["spec_boundary_neutralized"] is False
    manifest = job["search_manifest"]
    assert manifest["policy"]["search_strategy"] == "isolated_union"
    assert manifest["policy"]["candidate_merge"] == "union"
    assert sum(manifest["policy"]["lane_budgets"].values()) == 40
    assert [lane["spec_in_context"] for lane in manifest["search_lanes"]] == [
        False,
        True,
    ]
    # 어떤 파일이 입력이었는지는 작업에도 남는다.
    assert [a["role"] for a in job["attachments"]] == ["APPLICATION"]


def test_spec_assisted_candidates_are_added_without_deleting_baseline(client) -> None:
    job = wait_for_job(
        client, _start(client, batch_id=_upload_spec(client))["id"]
    )
    manifest = job["search_manifest"]
    rows = manifest["reported"]["term_expansions"]
    assert rows[0]["claim_term"] == "제어부"
    assert rows[0]["alternative_meanings"] == [
        "일반적인 제어 회로",
        "FPGA 로 구현된 신호 처리 회로",
    ]
    assert rows[0]["excluded_limitations"] == ["특정 FPGA 모델"]

    candidates = manifest["reported"]["candidates"]
    assert [row["doc_number"] for row in candidates] == ["AB1234", "CD5678"]
    assert candidates[0]["search_origins"] == ["claim_only", "spec_assisted"]
    assert candidates[1]["search_origins"] == ["spec_assisted"]
    assert manifest["observed"]["search_queries_by_origin"] == {
        "claim_only": ["테스트 검색식 A", "테스트 검색식 B"],
        "spec_assisted": [
            "명세서 확장 테스트 검색식 A",
            "명세서 확장 테스트 검색식 B",
        ],
    }

    report = job["result_text"]
    assert "출원발명 문서를 이용한 별도 검색 확장" in report
    assert "spec.txt" in report
    assert "명세서 문단 [0021]" in report
    assert "명세서 보조 검색으로 새로 추가된 후보 1건" in report
    assert "두 검색에서 모두 발견된 후보 1건" in report


def test_spec_dual_search_does_not_exceed_the_configured_total_budget(client) -> None:
    client.put("/api/settings", json={"values": {"max_search_tool_calls": 1}})
    try:
        job = wait_for_job(
            client, _start(client, batch_id=_upload_spec(client))["id"]
        )
    finally:
        client.put("/api/settings", json={"values": {"max_search_tool_calls": 40}})
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == ErrorCode.SEARCH_BUDGET_EXCEEDED
    assert "2 이상" in " ".join(job["errors"])


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
    assert created["search_focus"]["strategy"] == "combined_then_individual"
    # 선택 순서가 아니라 원 분석의 구성 순서를 보존한다.
    assert [row["id"] for row in created["search_focus"]["components"]] == [
        "C002",
        "C003",
    ]

    job = wait_for_job(client, created["id"])
    assert job["status"] == JobStatus.SUCCEEDED, job["errors"]
    manifest = job["search_manifest"]
    assert manifest["policy"]["search_strategy"] == "combined_then_individual"
    assert manifest["input"]["search_focus"]["source_job_id"] == source_id
    assert "# 미대응 구성 보완 검색 후보" in (job["result_text"] or "")
    assert "1차 조합 검색 → 2차 개별 검색" in (job["result_text"] or "")

    final_prompt = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert final_prompt.index("1차 — 조합 검색") < final_prompt.index("2차 — 개별 검색")
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
