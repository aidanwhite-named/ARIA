"""보안 불변조건: CSRF 가드, 도구 정책 fail-closed, 업로드 메모리 한도."""

from __future__ import annotations

import pytest

from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.providers.base import ExecutionOutcome


# ------------------------------------------------------------------- CSRF


def test_mutating_request_without_client_header_is_rejected(client) -> None:
    response = client.post(
        "/api/prompts",
        json={"name": "csrf", "body": "x"},
        headers={"X-ARIA-Client": ""},
    )
    assert response.status_code == 403
    assert "X-ARIA-Client" in response.json()["detail"]


def test_smoke_test_endpoint_requires_client_header(client) -> None:
    """본문 없는 POST 는 preflight 없이 전송되는 단순 요청이라 표적이 된다."""
    response = client.post(
        "/api/providers/claude/smoke-test", headers={"X-ARIA-Client": ""}
    )
    assert response.status_code == 403


def test_cross_origin_mutating_request_is_rejected(client) -> None:
    response = client.post(
        "/api/prompts",
        json={"name": "evil", "body": "x"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert "교차 출처" in response.json()["detail"]


def test_loopback_origin_with_header_is_allowed(client) -> None:
    response = client.post(
        "/api/prompts",
        json={"name": "loopback ok", "body": "본문"},
        headers={"Origin": "http://127.0.0.1:8765"},
    )
    assert response.status_code == 201


def test_get_requests_are_not_blocked(client) -> None:
    response = client.get("/api/prompts", headers={"X-ARIA-Client": ""})
    assert response.status_code == 200


def test_delete_requires_header(client) -> None:
    created = client.post("/api/prompts", json={"name": "삭제대상", "body": "x"}).json()
    blocked = client.delete(
        f"/api/prompts/{created['id']}", headers={"X-ARIA-Client": ""}
    )
    assert blocked.status_code == 403
    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204


# ------------------------------------------------------------- 도구 정책


def _ok() -> ExecutionOutcome:
    return ExecutionOutcome(
        result_text="정상 결과", exit_code=0, terminal_reason="completed", usage={"t": 1}
    )


def test_advertised_tools_fail_when_provider_promised_none() -> None:
    outcome = _ok()
    outcome.tools_must_be_disabled = True
    outcome.tools_advertised = ["Read", "Bash"]
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_tool_use_fails_even_with_good_output() -> None:
    """결과가 멀쩡해 보여도 정책이 깨졌으면 실패다(fail-closed)."""
    outcome = _ok()
    outcome.tools_must_be_disabled = True
    outcome.tool_uses = ["Read"]
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION
    assert "Read" in " ".join(verdict.errors)


def test_tool_use_fails_by_default_for_providers_without_tool_flag() -> None:
    outcome = _ok()
    outcome.tools_must_be_disabled = False
    outcome.tool_uses = ["run_command"]
    verdict = evaluate(outcome, fail_on_tool_use=True)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_tool_use_downgraded_to_warning_when_opted_out() -> None:
    outcome = _ok()
    outcome.tools_must_be_disabled = False
    outcome.tool_uses = ["run_command"]
    verdict = evaluate(outcome, fail_on_tool_use=False)
    assert verdict.status == JobStatus.SUCCEEDED
    assert any("도구가 호출" in w for w in verdict.warnings)


def test_no_tools_is_clean_success() -> None:
    outcome = _ok()
    outcome.tools_must_be_disabled = True
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_tool_policy_checked_before_empty_result() -> None:
    """정책 위반은 다른 실패 사유보다 먼저 보고한다."""
    outcome = ExecutionOutcome(result_text="", exit_code=0)
    outcome.tools_must_be_disabled = True
    outcome.tool_uses = ["Bash"]
    assert evaluate(outcome).error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_auth_failure_still_wins_over_tool_policy() -> None:
    outcome = ExecutionOutcome(result_text="Not logged in", auth_required=True)
    outcome.tools_must_be_disabled = True
    outcome.tool_uses = ["Bash"]
    assert evaluate(outcome).error_code == ErrorCode.AUTH_REQUIRED


def test_claude_provider_declares_tools_must_be_disabled() -> None:
    from app.providers.claude_cli import ClaudeCliProvider

    args = ClaudeCliProvider().build_args(
        __import__("app.providers.base", fromlist=["ExecutionRequest"]).ExecutionRequest(
            job_id="j", work_dir=__import__("pathlib").Path("."), system_prompt="s",
            user_message="m",
        )
    )
    assert args[args.index("--tools") + 1] == ""


# --------------------------------------------------------------- 업로드


def test_oversized_file_rejected_without_full_read(client) -> None:
    client.put("/api/settings", json={"values": {"max_file_size_bytes": 4096}})
    try:
        response = client.post(
            "/api/uploads",
            files=[("files", ("big.txt", b"x" * 200_000, "text/plain"))],
        )
        assert response.status_code == 400
        assert "너무 큽니다" in response.json()["detail"]
    finally:
        client.put(
            "/api/settings", json={"values": {"max_file_size_bytes": 25 * 1024 * 1024}}
        )


def test_total_upload_limit_enforced(client) -> None:
    client.put("/api/settings", json={"values": {"max_total_upload_bytes": 8192}})
    try:
        response = client.post(
            "/api/uploads",
            files=[
                ("files", ("a.txt", b"a" * 5000, "text/plain")),
                ("files", ("b.txt", b"b" * 5000, "text/plain")),
            ],
        )
        assert response.status_code == 400
        assert "총 업로드" in response.json()["detail"]
    finally:
        client.put(
            "/api/settings",
            json={"values": {"max_total_upload_bytes": 100 * 1024 * 1024}},
        )


def test_file_count_limit_rejected_before_reading(client) -> None:
    client.put("/api/settings", json={"values": {"max_files_per_job": 2}})
    try:
        response = client.post(
            "/api/uploads",
            files=[
                ("files", (f"f{i}.txt", b"data", "text/plain")) for i in range(3)
            ],
        )
        assert response.status_code == 400
        assert "개수" in response.json()["detail"]
    finally:
        client.put("/api/settings", json={"values": {"max_files_per_job": 20}})


# ------------------------------------------------------- 설정 연동 확인


def test_fail_on_tool_use_setting_is_editable(client) -> None:
    data = client.put("/api/settings", json={"values": {"fail_on_tool_use": False}}).json()
    assert data["values"]["fail_on_tool_use"] is False
    client.put("/api/settings", json={"values": {"fail_on_tool_use": True}})


@pytest.mark.parametrize("provider_id", ["agy", "claude", "codex"])
def test_all_providers_probe_without_error(client, provider_id) -> None:
    data = client.get(f"/api/providers/{provider_id}").json()
    assert data["provider"] == provider_id
    assert "usable" in data


def test_removed_mock_provider_cannot_create_jobs(client) -> None:
    prompt = client.post(
        "/api/prompts", json={"name": "제거된 Provider 확인", "body": "요약하십시오."}
    ).json()
    response = client.post(
        "/api/jobs", json={"prompt_id": prompt["id"], "provider": "mock"}
    )
    assert response.status_code == 400
    assert client.get("/api/providers/mock").status_code == 404


# ---------------------------------------------------------- Provider 표시


def test_agy_uses_its_cli_name_without_experimental_warning(client) -> None:
    data = client.get("/api/providers/agy").json()
    assert data["provider"] == "agy"
    assert data["display_name"] == "agy"
    assert "experimental" not in data
    assert "risks" not in data


def test_execution_defaults_are_editable(client) -> None:
    prompt = client.post(
        "/api/prompts", json={"name": "기본 설정", "body": "요약"}
    ).json()
    data = client.put(
        "/api/settings",
        json={
            "values": {
                "default_prompt_id": prompt["id"],
                "default_provider": "agy",
                "default_models": {"agy": "gemini-3.7-flash-high"},
            }
        },
    ).json()
    assert data["values"]["default_prompt_id"] == prompt["id"]
    assert data["values"]["default_provider"] == "agy"
    assert data["values"]["default_models"]["agy"] == "gemini-3.7-flash-high"
    client.put(
        "/api/settings",
        json={
            "values": {
                "default_prompt_id": "",
                "default_provider": "agy",
                "default_models": {},
            }
        },
    )


def test_uncontrollable_tools_cannot_be_relaxed() -> None:
    """도구를 끌 수 없는 Provider 는 설정으로 완화할 수 없다."""
    outcome = _ok()
    outcome.tools_must_be_disabled = False
    outcome.tools_uncontrollable = True
    outcome.tool_uses = ["tool"]
    verdict = evaluate(outcome, fail_on_tool_use=False)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_agy_declares_uncontrollable_tools_and_sandbox() -> None:
    from pathlib import Path

    from app.providers.agy_cli import AgyCliProvider
    from app.providers.base import ExecutionRequest

    provider = AgyCliProvider()
    args = provider.build_args(
        ExecutionRequest(job_id="j", work_dir=Path("."), system_prompt="s", user_message="m")
    )
    assert "--sandbox" in args
    assert "--dangerously-skip-permissions" not in args


def test_agy_resolver_does_not_fall_back_to_gemini(monkeypatch) -> None:
    """구형 gemini CLI 는 계약이 달라 조용히 오작동한다."""
    import app.providers.agy_cli as agy

    calls: list[str] = []

    def fake_resolve_simple(command, override=None):
        calls.append(command)
        return None

    monkeypatch.setattr(agy, "resolve_simple", fake_resolve_simple)
    monkeypatch.setattr(agy, "_KNOWN_INSTALL_DIRS", ())
    assert agy.resolve_agy() is None
    assert calls == ["agy"], f"gemini 로 폴백했습니다: {calls}"
