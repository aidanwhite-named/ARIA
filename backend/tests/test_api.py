"""API 계층: 프롬프트 CRUD, 업로드, 작업 실행, 이력, 설정."""

from __future__ import annotations

import pytest

from .conftest import wait_for_job
from .pdf_fixture import build_pdf, build_scanned_like_pdf


@pytest.fixture()
def prompt(client):
    return client.post(
        "/api/prompts",
        json={"name": "테스트 프롬프트", "body": "자료를 요약하십시오.", "output_mode": "markdown"},
    ).json()


# ---------------------------------------------------------------- prompts


def test_health(client) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_prompt_crud_and_versioning(client) -> None:
    created = client.post(
        "/api/prompts", json={"name": "CRUD", "body": "본문 1", "tags": ["t1"]}
    ).json()
    assert created["version"] == 1

    updated = client.put(f"/api/prompts/{created['id']}", json={"body": "본문 2"}).json()
    assert updated["version"] == 2

    # 본문이 그대로면 버전을 올리지 않는다.
    same = client.put(f"/api/prompts/{created['id']}", json={"body": "본문 2"}).json()
    assert same["version"] == 2

    versions = client.get(f"/api/prompts/{created['id']}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["body"] == "본문 1"

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204
    assert client.get(f"/api/prompts/{created['id']}").status_code == 404


def test_prompt_clone(client, prompt) -> None:
    clone = client.post(f"/api/prompts/{prompt['id']}/clone").json()
    assert clone["id"] != prompt["id"]
    assert clone["body"] == prompt["body"]
    assert "복제" in clone["name"]


def test_prompt_enable_and_archive(client, prompt) -> None:
    client.put(f"/api/prompts/{prompt['id']}", json={"archived": True})
    names = [p["id"] for p in client.get("/api/prompts").json()]
    assert prompt["id"] not in names
    with_archived = [
        p["id"] for p in client.get("/api/prompts?include_archived=true").json()
    ]
    assert prompt["id"] in with_archived


def test_prompt_search(client) -> None:
    client.post("/api/prompts", json={"name": "고유검색어ABC", "body": "본문"})
    found = client.get("/api/prompts?search=고유검색어ABC").json()
    assert len(found) == 1


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
    assert by_id["mock"]["usable"] is True
    for pid in ("claude", "codex", "gemini"):
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


# ------------------------------------------------------------------- jobs


def test_job_success_flow(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "mock", "user_input": "요약해줘"},
    ).json()
    final = wait_for_job(client, job["id"])

    assert final["status"] == "SUCCEEDED"
    assert final["result_quality"] == "SUCCESS"
    assert final["result_text"]
    assert final["final_prompt_sha256"]
    assert final["prompt_snapshot"] == prompt["body"]
    assert final["prompt_version"] == prompt["version"]
    assert final["duration_ms"] is not None


def test_job_snapshot_survives_prompt_deletion(client) -> None:
    p = client.post("/api/prompts", json={"name": "삭제될 프롬프트", "body": "원본 본문"}).json()
    job = client.post("/api/jobs", json={"prompt_id": p["id"], "provider": "mock"}).json()
    wait_for_job(client, job["id"])
    client.delete(f"/api/prompts/{p['id']}")

    stored = client.get(f"/api/history/{job['id']}").json()
    assert stored["prompt_snapshot"] == "원본 본문"
    assert stored["prompt_name"] == "삭제될 프롬프트"


@pytest.mark.parametrize(
    ("keyword", "status", "code"),
    [
        ("MOCK_FAIL", "FAILED", "PROCESS_ERROR"),
        ("MOCK_EMPTY", "FAILED", "EMPTY_RESULT"),
        ("MOCK_AUTH", "FAILED", "AUTH_REQUIRED"),
        ("MOCK_RATELIMIT", "FAILED", "RATE_LIMITED"),
        ("MOCK_WARN", "SUCCEEDED", None),
    ],
)
def test_job_failure_paths(client, prompt, keyword, status, code) -> None:
    job = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "mock", "user_input": keyword},
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == status
    assert final["error_code"] == code


def test_mock_warn_is_success_with_warnings(client, prompt) -> None:
    job = client.post(
        "/api/jobs",
        json={"prompt_id": prompt["id"], "provider": "mock", "user_input": "MOCK_WARN"},
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["result_quality"] == "SUCCESS_WITH_WARNINGS"
    assert final["warnings"]


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
            "provider": "mock",
            "batch_id": upload["batch_id"],
            "required_map": {attachment_id: True},
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "FAILED"
    assert final["error_code"] == "ATTACHMENT_ERROR"


def test_optional_attachment_failure_only_warns(client, prompt) -> None:
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
            "provider": "mock",
            "batch_id": upload["batch_id"],
            "required_map": required,
        },
    ).json()
    final = wait_for_job(client, job["id"])
    assert final["status"] == "SUCCEEDED"
    assert final["result_quality"] == "SUCCESS_WITH_WARNINGS"


def test_attachment_content_reaches_final_prompt(client, prompt) -> None:
    upload = client.post(
        "/api/uploads",
        files=[("files", ("evidence.txt", "고유표식XYZ123".encode(), "text/plain"))],
    ).json()
    job = client.post(
        "/api/jobs",
        json={
            "prompt_id": prompt["id"],
            "provider": "mock",
            "batch_id": upload["batch_id"],
        },
    ).json()
    wait_for_job(client, job["id"])
    text = client.get(f"/api/jobs/{job['id']}/final-prompt").text
    assert "고유표식XYZ123" in text
    assert "SYSTEM PROMPT" in text


def test_input_too_large(client, prompt) -> None:
    client.put("/api/settings", json={"values": {"max_inline_chars": 1500}})
    try:
        upload = client.post(
            "/api/uploads", files=[("files", ("big.txt", b"A" * 4000, "text/plain"))]
        ).json()
        job = client.post(
            "/api/jobs",
            json={
                "prompt_id": prompt["id"],
                "provider": "mock",
                "batch_id": upload["batch_id"],
            },
        ).json()
        final = wait_for_job(client, job["id"])
        assert final["status"] == "FAILED"
        assert final["error_code"] == "INPUT_TOO_LARGE"
    finally:
        client.put("/api/settings", json={"values": {"max_inline_chars": 300000}})


def test_job_with_unknown_prompt_404(client) -> None:
    response = client.post(
        "/api/jobs", json={"prompt_id": "does-not-exist", "provider": "mock"}
    )
    assert response.status_code == 404


def test_batch_cannot_be_reused(client, prompt) -> None:
    upload = client.post(
        "/api/uploads", files=[("files", ("a.txt", b"content", "text/plain"))]
    ).json()
    body = {
        "prompt_id": prompt["id"],
        "provider": "mock",
        "batch_id": upload["batch_id"],
    }
    first = client.post("/api/jobs", json=body)
    assert first.status_code == 201
    wait_for_job(client, first.json()["id"])
    assert client.post("/api/jobs", json=body).status_code == 400


def test_result_download_formats(client, prompt) -> None:
    job = client.post("/api/jobs", json={"prompt_id": prompt["id"], "provider": "mock"}).json()
    wait_for_job(client, job["id"])

    md = client.get(f"/api/jobs/{job['id']}/result?fmt=md")
    assert md.status_code == 200
    assert "attachment" in md.headers["content-disposition"]

    js = client.get(f"/api/jobs/{job['id']}/result?fmt=json")
    assert js.json()["id"] == job["id"]


def test_cancel_finished_job_is_noop(client, prompt) -> None:
    job = client.post("/api/jobs", json={"prompt_id": prompt["id"], "provider": "mock"}).json()
    wait_for_job(client, job["id"])
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["cancelled"] is False


# ---------------------------------------------------------------- history


def test_history_lists_and_deletes(client, prompt) -> None:
    job = client.post("/api/jobs", json={"prompt_id": prompt["id"], "provider": "mock"}).json()
    wait_for_job(client, job["id"])

    items = client.get("/api/history").json()
    assert any(i["id"] == job["id"] for i in items)

    filtered = client.get("/api/history?provider=mock").json()
    assert all(i["provider"] == "mock" for i in filtered)

    assert client.delete(f"/api/history/{job['id']}").status_code == 204
    assert client.get(f"/api/history/{job['id']}").status_code == 404


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
