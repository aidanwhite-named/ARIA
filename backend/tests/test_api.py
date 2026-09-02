"""API 계층: 프롬프트 CRUD, 업로드, 작업 실행, 이력, 설정."""

from __future__ import annotations

import json

import pytest

from .conftest import wait_for_job
from .pdf_fixture import build_pdf, build_scanned_like_pdf
from app.config import PATHS, PROMPT_DIR


@pytest.fixture()
def prompt(client):
    return client.post(
        "/api/prompts",
        json={"name": "테스트 프롬프트", "body": "자료를 요약하십시오.", "output_mode": "markdown"},
    ).json()


def citation_batch(client, filename: str = "citation.txt") -> str:
    """분석 실행을 만들기 위한 최소 첨부.

    구성대비 분석은 인용발명 문헌 없이는 시작할 수 없다. 문헌 내용을 보지 않는
    테스트도 실행을 만들려면 한 건은 올려야 한다.
    """
    response = client.post(
        "/api/uploads",
        files=[("files", (filename, b"citation document", "text/plain"))],
    )
    assert response.status_code == 200, response.text
    return response.json()["batch_id"]


# ---------------------------------------------------------------- prompts


def test_health(client) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_prompt_crud(client) -> None:
    created = client.post(
        "/api/prompts", json={"name": "CRUD", "body": "본문 1", "tags": ["t1"]}
    ).json()
    assert "version" not in created

    updated = client.put(f"/api/prompts/{created['id']}", json={"body": "본문 2"}).json()
    assert updated["body"] == "본문 2"

    same = client.put(f"/api/prompts/{created['id']}", json={"body": "본문 2"}).json()
    assert same["body"] == "본문 2"

    assert client.get(f"/api/prompts/{created['id']}/versions").status_code == 404

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204
    assert client.get(f"/api/prompts/{created['id']}").status_code == 404


def test_prompt_enable_and_disable(client, prompt) -> None:
    disabled = client.put(
        f"/api/prompts/{prompt['id']}", json={"enabled": False}
    ).json()
    assert disabled["enabled"] is False
    assert prompt["id"] in [p["id"] for p in client.get("/api/prompts").json()]

    enabled = client.put(
        f"/api/prompts/{prompt['id']}", json={"enabled": True}
    ).json()
    assert enabled["enabled"] is True


def test_prompt_search(client) -> None:
    client.post("/api/prompts", json={"name": "고유검색어ABC", "body": "본문"})
    found = client.get("/api/prompts?search=고유검색어ABC").json()
    assert len(found) == 1


def test_prompt_catalog_contains_analysis_and_search_prompts(client) -> None:
    analysis = client.post(
        "/api/prompts", json={"name": "분석 카탈로그 확인", "body": "분석 본문"}
    ).json()
    try:
        regular_ids = {item["id"] for item in client.get("/api/prompts").json()}
        assert "search_prompt.md" not in regular_ids

        catalog = client.get("/api/prompts/catalog").json()
        by_id = {item["id"]: item for item in catalog}
        assert by_id[analysis["id"]]["kind"] == "analysis"
        assert by_id[analysis["id"]]["deletable"] is True
        assert by_id["search_prompt.md"]["kind"] == "search"
        assert by_id["search_prompt.md"]["name"] == "검색 프롬프트"
        assert by_id["search_prompt.md"]["deletable"] is False
    finally:
        client.delete(f"/api/prompts/{analysis['id']}")


def test_search_prompt_catalog_edit_validates_execution_contract(client) -> None:
    current = next(
        item
        for item in client.get("/api/prompts/catalog").json()
        if item["id"] == "search_prompt.md"
    )
    unchanged = client.put(
        "/api/prompts/reserved/search_prompt.md", json={"name": current["name"]}
    )
    assert unchanged.status_code == 200

    invalid = client.put(
        "/api/prompts/reserved/search_prompt.md",
        json={"body": "청구항 placeholder와 경계가 없는 잘못된 검색 프롬프트"},
    )
    assert invalid.status_code == 422
    assert "{{CLAIM_TEXT}}" in invalid.json()["detail"]

    assert (
        client.get("/api/prompts/reserved/search_prompt.md/versions").status_code
        == 404
    )


def test_prompt_file_is_the_live_source(client) -> None:
    created = client.post(
        "/api/prompts", json={"name": "파일 원본 확인", "body": "처음 본문"}
    ).json()
    target = PROMPT_DIR / created["id"]
    assert target.is_file()

    target.write_text("# 외부에서 수정한 프롬프트\n\n바뀐 본문", encoding="utf-8")
    loaded = client.get(f"/api/prompts/{created['id']}").json()
    assert loaded["body"] == "# 외부에서 수정한 프롬프트\n\n바뀐 본문"

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204


def test_prompt_export_import_roundtrip(client) -> None:
    exported = client.get("/api/prompts/export").json()
    assert exported["version"] == 1
    payload = [
        {"name": "가져온 프롬프트", "description": "", "body": "가져온 본문", "output_mode": "markdown"}
    ]
    result = client.post(
        "/api/prompts/import", json={"prompts": payload, "replace_existing": False}
    ).json()
    assert result["created"] == 1
    # 같은 이름을 다시 넣으면 건너뛴다.
    again = client.post(
        "/api/prompts/import", json={"prompts": payload, "replace_existing": False}
    ).json()
    assert again["created"] == 0


def test_invalid_output_mode_rejected(client) -> None:
    response = client.post(
        "/api/prompts", json={"name": "x", "body": "y", "output_mode": "yaml"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------- providers


def test_provider_list_reports_usability(client) -> None:
    providers = client.get("/api/providers").json()["providers"]
    by_id = {p["provider"]: p for p in providers}
    assert "mock" not in by_id
    for pid in ("agy", "claude", "codex"):
        assert pid in by_id
        assert "install_hint" in by_id[pid]


def test_unknown_provider_404(client) -> None:
    assert client.get("/api/providers/nope").status_code == 404


# ---------------------------------------------------------------- uploads


def test_upload_analysis_and_blocking(client) -> None:
    files = [
        ("files", ("ok.txt", b"content here", "text/plain")),
        ("files", ("doc.pdf", build_pdf(["Page one body text."]), "application/pdf")),
        ("files", ("bad.exe", b"MZ\x00\x00", "application/octet-stream")),
        ("files", ("CLAUDE.md", b"# config", "text/markdown")),
        ("files", ("../up.txt", b"traversal", "text/plain")),
    ]
    data = client.post("/api/uploads", files=files).json()
    accepted = {f["original_filename"]: f for f in data["files"]}
    rejected = {r["filename"] for r in data["rejected"]}

    assert accepted["ok.txt"]["delivery_mode"] == "DELIVERED_AS_INLINE_CONTEXT"
    assert accepted["doc.pdf"]["page_count"] == 1
    assert {"bad.exe", "CLAUDE.md", "../up.txt"} <= rejected


def test_upload_requires_files(client) -> None:
    assert client.post("/api/uploads", files=[]).status_code in (400, 422)


def test_upload_preserves_application_and_citation_roles(client) -> None:
    files = [
        ("files", ("application.txt", b"claim body", "text/plain")),
        ("files", ("citation.txt", b"prior art body", "text/plain")),
    ]
    response = client.post(
        "/api/uploads",
        files=files,
        data={"roles": json.dumps(["APPLICATION", "CITATION"])},
    )
    assert response.status_code == 200
    assert [item["role"] for item in response.json()["files"]] == [
        "APPLICATION",
        "CITATION",
    ]


def test_upload_rejects_mismatched_roles(client) -> None:
    response = client.post(
        "/api/uploads",
        files=[("files", ("one.txt", b"body", "text/plain"))],
        data={"roles": json.dumps(["APPLICATION", "CITATION"])},
    )
    assert response.status_code == 400


# ------------------------------------------------------------------- jobs


def test_job_success_flow(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    final = wait_for_job(client, job["id"])

    assert final["status"] == "SUCCEEDED"
    assert final["result_text"]
    assert final["final_prompt_sha256"]
    assert final["prompt_snapshot"] == prompt["body"]
    assert "prompt_version" not in final
    assert final["duration_ms"] is not None


def test_claim_and_document_roles_reach_final_prompt(client, prompt) -> None:
    upload = client.post(
        "/api/uploads",
        files=[
            ("files", ("application.txt", b"application document", "text/plain")),
            ("files", ("citation.txt", b"citation document", "text/plain")),
        ],
        data={"roles": json.dumps(["APPLICATION", "CITATION"])},
    ).json()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 전용 청구항 표식",
            "batch_id": upload["batch_id"],
        },
    ).json()
    final = wait_for_job(client, job["id"])
    prompt_text = client.get(f"/api/jobs/{job['id']}/final-prompt").text

    assert final["claim_text"] == "청구항 1. 전용 청구항 표식"
    assert {
        item["original_filename"]: item["role"] for item in final["attachments"]
    } == {
        "application.txt": "APPLICATION",
        "citation.txt": "CITATION",
    }
    assert "[출원발명 청구항]" in prompt_text
    assert "[출원발명 문서]" in prompt_text
    assert "[인용발명 문헌]" in prompt_text


def test_job_snapshot_survives_prompt_deletion(client) -> None:
    p = client.post("/api/prompts", json={"name": "삭제될 프롬프트", "body": "원본 본문"}).json()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": p["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    wait_for_job(client, job["id"])
    client.delete(f"/api/prompts/{p['id']}")

    stored = client.get(f"/api/history/{job['id']}").json()
    assert stored["prompt_snapshot"] == "원본 본문"
    assert stored["prompt_name"] == "삭제될 프롬프트"


@pytest.mark.parametrize(
    ("keyword", "status", "code"),
    [
        ("TEST_FAIL", "FAILED", "PROCESS_ERROR"),
        ("TEST_EMPTY", "FAILED", "EMPTY_RESULT"),
        ("TEST_AUTH", "FAILED", "AUTH_REQUIRED"),
        ("TEST_RATELIMIT", "FAILED", "RATE_LIMITED"),
    ],
)
def test_job_failure_paths(client, prompt, keyword, status, code) -> None:
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": keyword,
            "batch_id": citation_batch(client),
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == status
    assert final["error_code"] == code


def test_required_attachment_failure_fails_job(client, prompt) -> None:
    upload = client.post(
        "/api/uploads",
        files=[("files", ("scan.pdf", build_scanned_like_pdf(2), "application/pdf"))],
    ).json()
    attachment_id = upload["files"][0]["attachment_id"]
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": upload["batch_id"],
            "required_map": {attachment_id: True},
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "FAILED"
    assert final["error_code"] == "ATTACHMENT_ERROR"


def test_optional_attachment_failure_does_not_fail_job(client, prompt) -> None:
    upload = client.post(
        "/api/uploads",
        files=[
            ("files", ("good.txt", b"usable content", "text/plain")),
            ("files", ("scan.pdf", build_scanned_like_pdf(2), "application/pdf")),
        ],
    ).json()
    required = {
        f["attachment_id"]: f["original_filename"] == "good.txt" for f in upload["files"]
    }
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": upload["batch_id"],
            "required_map": required,
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED"
    assert final["error_code"] is None


def test_attachment_content_reaches_final_prompt(client, prompt) -> None:
    upload = client.post(
        "/api/uploads",
        files=[("files", ("evidence.txt", "고유표식XYZ123".encode(), "text/plain"))],
    ).json()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": upload["batch_id"],
        },
    ).json()
    wait_for_job(client, job["id"])
    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "고유표식XYZ123" in text
    assert "SYSTEM PROMPT" in text


def test_input_too_large(client, prompt) -> None:
    """사용자가 직접 건 글자 수 한도는 그대로 지킨다.

    기본값은 제한 없음(0)이지만, 값을 넣어 두면 그 한도에서 막는다. 자르거나
    요약하지 않고 INPUT_TOO_LARGE 로 끝낸다.
    """
    client.put("/api/settings", json={"values": {"max_inline_chars": 1500}})
    try:
        upload = client.post(
            "/api/uploads", files=[("files", ("big.txt", b"A" * 4000, "text/plain"))]
        ).json()
        job = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "test",
                "claim_text": "청구항 1.",
                "batch_id": upload["batch_id"],
            },
        ).json()
        final = wait_for_job(client, job["id"])
        assert final["status"] == "FAILED"
        assert final["error_code"] == "INPUT_TOO_LARGE"
    finally:
        # 기본값으로 되돌린다. 0 = 제한 없음.
        client.put("/api/settings", json={"values": {"max_inline_chars": 0}})


def test_inline_char_limit_defaults_to_unlimited(client, prompt) -> None:
    """ARIA 자체 글자 수 한도는 기본적으로 없다.

    0 과 null 을 모두 '제한 없음'으로 받고, 그 상태에서는 큰 입력도 글자 수를
    이유로 막지 않는다. 실행을 막아야 하는 한도는 Provider 전송 한도와 모델
    컨텍스트 한도뿐이며, 그 둘은 이 설정과 무관하게 남는다
    (test_provider_byte_budget_blocks_oversized_input 참조).
    """
    assert client.get("/api/settings").json()["values"]["max_inline_chars"] == 0

    # null 도 같은 뜻으로 받는다.
    values = client.put(
        "/api/settings", json={"values": {"max_inline_chars": None}}
    ).json()["values"]
    assert values["max_inline_chars"] == 0

    upload = client.post(
        "/api/uploads",
        files=[("files", ("big.txt", "가".encode("utf-8") * 300_000, "text/plain"))],
    ).json()
    # 업로드 응답의 한도도 "제한 없음"을 그대로 전한다.
    assert upload["max_inline_chars"] is None

    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1.",
            "batch_id": upload["batch_id"],
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED", final.get("error_message")


def test_provider_byte_budget_blocks_oversized_input(client, prompt, monkeypatch) -> None:
    """문자수 한도는 통과해도 Provider 의 바이트 한도를 넘으면 실행 전에 막는다.

    agy 처럼 큰 입력을 조용히 자르는 Provider 를 위한 방어다. 자르기 전에 실패로
    끝내, 앞부분만 분석하고 '성공'으로 남는 낭비를 없앤다. ARIA 의 글자 수
    한도를 꺼 두어도(기본값) 이 검사는 남는다는 것을 고정한다 — 사용자 입력
    제한이 아니라 Provider 가 자료 전체를 손실 없이 전달할 수 있는 한도다.
    """
    from .fake_provider import DeterministicTestProvider

    # 테스트 대역에 작은 바이트 예산을 심는다. 기본 프롬프트는 이보다 작아
    # 통과하고, 아래의 거대한 청구항만 이를 넘긴다.
    monkeypatch.setattr(DeterministicTestProvider, "max_input_bytes", 100_000)

    # 글자 수 한도는 기본이 '제한 없음'이라 아무리 길어도 문자 검사는 통과한다.
    # 그러나 한글은 UTF-8 로 3 bytes 라서 ~600 KB, 100 KB 예산을 크게 넘는다.
    huge_claim = "청구항 1. " + "가" * 200_000

    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": huge_claim,
            "batch_id": citation_batch(client),
        },
    ).json()
    final = wait_for_job(client, job["id"])

    assert final["status"] == "FAILED"
    assert final["error_code"] == "INPUT_TOO_LARGE"


def test_job_with_unknown_prompt_404(client) -> None:
    response = client.post(
        "/api/jobs", json={"prompt_id": "does-not-exist", "provider": "test"}
    )
    assert response.status_code == 404


def test_analysis_without_documents_is_rejected(client, prompt) -> None:
    """대비할 문헌이 없는 구성대비 분석은 실행을 만들지 않는다.

    문헌 없이 시작하면 모델이 없는 자료를 찾으러 파일 도구를 부르고, 도구를 끌
    수단이 없는 Provider 에서는 그 호출 하나로 실행이 죽는다. 살아남아도 내용은
    "인용발명 문헌 미제공" 뿐이다. 어느 쪽이든 사용량만 쓴다.
    """
    before_runs = set(PATHS.runs_dir.iterdir())
    before_history = len(client.get("/api/history").json())

    response = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "test", "claim_text": "청구항 1."},
    )
    assert response.status_code == 400
    assert "인용발명 문헌" in response.json()["detail"]

    # 거절된 요청은 이력에도 실행 폴더에도 흔적을 남기지 않는다.
    assert len(client.get("/api/history").json()) == before_history
    assert set(PATHS.runs_dir.iterdir()) == before_runs


def test_analysis_without_claims_is_rejected(client, prompt) -> None:
    """청구항이 빈 구성대비 분석은 실행을 만들지 않는다.

    [출원발명 청구항]이 이번 실행의 분석 대상이다. 비어 있으면 대비할 기준이
    없어 사용량만 쓰고 끝난다. 문헌 첨부가 있어도 청구항이 없으면 거절한다.
    """
    before_history = len(client.get("/api/history").json())
    # 거절은 batch 를 소비하지 않으므로(청구항 검사가 앞선다) 하나를 재사용한다.
    batch = citation_batch(client)

    for empty in ("", "   ", "\n"):
        response = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "test",
                "claim_text": empty,
                "batch_id": batch,
            },
        )
        assert response.status_code == 400, response.text
        assert "청구항" in response.json()["detail"]

    # 아예 청구항 필드를 생략해도 마찬가지다.
    omitted = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "batch_id": batch,
        },
    )
    assert omitted.status_code == 400
    assert "청구항" in omitted.json()["detail"]

    # 거절된 요청은 이력에 흔적을 남기지 않는다.
    assert len(client.get("/api/history").json()) == before_history


def test_batch_cannot_be_reused(client, prompt) -> None:
    upload = client.post(
        "/api/uploads", files=[("files", ("a.txt", b"content", "text/plain"))]
    ).json()
    body = {
        "prompt_id": prompt["id"],
        "provider": "test",
        "claim_text": "청구항 1. 테스트 청구항",
        "batch_id": upload["batch_id"],
    }
    first = client.post("/api/jobs", json=body)
    assert first.status_code == 201
    wait_for_job(client, first.json()["id"])
    assert client.post("/api/jobs", json=body).status_code == 400


def test_result_download_endpoint_is_removed(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    wait_for_job(client, job["id"])

    assert client.get(f"/api/jobs/{job['id']}/result?fmt=md").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/result?fmt=json").status_code == 404


def test_cancel_finished_job_is_noop(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    wait_for_job(client, job["id"])
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["cancelled"] is False


# ---------------------------------------------------------------- history


def test_history_lists_and_deletes(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    wait_for_job(client, job["id"])

    items = client.get("/api/history").json()
    assert any(i["id"] == job["id"] for i in items)

    filtered = client.get("/api/history?provider=test").json()
    assert all(i["provider"] == "test" for i in filtered)

    assert client.delete(f"/api/history/{job['id']}").status_code == 204
    assert client.get(f"/api/history/{job['id']}").status_code == 404


def test_history_delete_all_clears_database_and_stored_files(client, prompt) -> None:
    # 작업에 붙지 않은 업로드. 일괄 삭제가 이런 고아 행까지 지우는지 본다.
    upload = client.post(
        "/api/uploads", files=[("files", ("unused.txt", b"unused", "text/plain"))]
    ).json()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "test",
            "claim_text": "청구항 1. 테스트 청구항",
            "batch_id": citation_batch(client),
        },
    ).json()
    wait_for_job(client, job["id"])

    orphan_dir = PATHS.runs_dir / "orphan-record"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "leftover.log").write_text("leftover", encoding="utf-8")
    legacy_artifact = PATHS.artifacts_dir / "legacy-result.txt"
    legacy_artifact.write_text("legacy", encoding="utf-8")

    response = client.delete("/api/history")
    assert response.status_code == 200
    assert response.json()["deleted"] >= 1
    assert client.get("/api/history").json() == []
    assert client.get(f"/api/history/{job['id']}").status_code == 404
    assert list(PATHS.runs_dir.iterdir()) == []
    assert list(PATHS.artifacts_dir.iterdir()) == []

    from app.db import session_scope
    from app.models import Attachment

    with session_scope() as session:
        assert session.query(Attachment).filter(
            Attachment.upload_batch == upload["batch_id"]
        ).count() == 0


# --------------------------------------------------------------- settings


def test_settings_roundtrip(client) -> None:
    original = client.get("/api/settings").json()
    assert "runtime_context" in original["values"]
    assert original["env_filtering"]["blocked_prefixes"]

    updated = client.put(
        "/api/settings", json={"values": {"default_timeout_seconds": 123}}
    ).json()
    assert updated["values"]["default_timeout_seconds"] == 123
    client.put("/api/settings", json={"values": {"default_timeout_seconds": 900}})


def test_settings_reject_unknown_key(client) -> None:
    response = client.put("/api/settings", json={"values": {"secret_api_key": "abc"}})
    assert response.status_code == 400


def test_settings_reject_out_of_range(client) -> None:
    assert (
        client.put(
            "/api/settings", json={"values": {"max_concurrency_per_provider": 999}}
        ).status_code
        == 400
    )


def test_concurrency_warning_surfaces(client) -> None:
    try:
        data = client.put(
            "/api/settings", json={"values": {"max_concurrency_per_provider": 3}}
        ).json()
        assert any("동시 실행" in w for w in data["warnings"])
    finally:
        client.put("/api/settings", json={"values": {"max_concurrency_per_provider": 1}})


def test_runtime_context_disable_warns(client) -> None:
    try:
        data = client.put(
            "/api/settings", json={"values": {"runtime_context_enabled": False}}
        ).json()
        assert any("첨부 문서" in w for w in data["warnings"])
    finally:
        client.put("/api/settings", json={"values": {"runtime_context_enabled": True}})


def test_runtime_context_reset(client) -> None:
    client.put("/api/settings", json={"values": {"runtime_context": "임시"}})
    restored = client.post("/api/settings/runtime-context/reset").json()
    assert "첨부 자료" in restored["values"]["runtime_context"]


def test_no_api_key_endpoint_exists(client) -> None:
    """API Key 를 받는 경로가 있어서는 안 된다."""
    from app.main import app

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert not any("key" in p.lower() or "token" in p.lower() for p in paths)


# ------------------------------------------------------------------- 추론강도


def test_reasoning_effort_defaults_to_the_model_default(client) -> None:
    """기본값은 "모델 기본값" 이다. ARIA 가 레벨을 대신 정해 주지 않는다."""
    values = client.get("/api/settings").json()["values"]
    assert values["reasoning_effort"] == {}


def test_reasoning_effort_accepts_a_known_level(client) -> None:
    updated = client.put(
        "/api/settings", json={"values": {"reasoning_effort": {"codex": "high"}}}
    ).json()
    assert updated["values"]["reasoning_effort"] == {"codex": "high"}
    # 빈 값으로 되돌리면 키가 사라진다 = 모델 기본값.
    restored = client.put(
        "/api/settings", json={"values": {"reasoning_effort": {"codex": ""}}}
    ).json()
    assert restored["values"]["reasoning_effort"] == {}


def test_reasoning_effort_rejects_an_unknown_level(client) -> None:
    """오타 하나가 실행 전체를 실패로 만든다. 설정 화면에서 막는다."""
    assert (
        client.put(
            "/api/settings", json={"values": {"reasoning_effort": {"codex": "higher"}}}
        ).status_code
        == 400
    )
