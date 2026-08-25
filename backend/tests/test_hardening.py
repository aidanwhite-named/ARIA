"""보안 불변조건: CSRF 가드, 도구 정책 fail-closed, 업로드 메모리 한도."""

from __future__ import annotations

import json

import pytest

from app.enums import AuthState, ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.providers.base import ExecutionOutcome, ProbeResult


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


def test_tool_use_is_accepted_when_opted_out() -> None:
    """fail_on_tool_use 를 끄면 도구 호출이 실행을 세우지 않는다.

    설정으로 완화할 수 있는 것은 이 경로뿐이다. 도구를 꺼야 하는 Provider 나
    끌 수단이 없는 Provider 는 위 테스트대로 설정과 무관하게 실패한다.
    """
    outcome = _ok()
    outcome.tools_must_be_disabled = False
    outcome.tool_uses = ["run_command"]
    verdict = evaluate(outcome, fail_on_tool_use=False)
    assert verdict.status == JobStatus.SUCCEEDED


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


def test_agy_uses_its_cli_name(client) -> None:
    data = client.get("/api/providers/agy").json()
    assert data["provider"] == "agy"
    assert data["display_name"] == "agy"


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
                # 기본값은 빈 문자열이다. 제한된 안전성 Provider 를 기본으로 남겨두면
                # 다른 테스트가 그것을 자동 선택하게 된다.
                "default_provider": "",
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


# ------------------------------------- 도구를 끄지 못하는 Provider 의 취급


def test_no_provider_and_no_default_refuses_instead_of_auto_selecting(client) -> None:
    """안전 정책을 만족한 Provider 가 없으면 자동 선택하지 않는다."""
    client.put("/api/settings", json={"values": {"default_provider": ""}})
    prompt = client.post(
        "/api/prompts", json={"name": "자동선택 금지", "body": "요약하십시오."}
    ).json()
    response = client.post("/api/jobs", json={"prompt_id": prompt["id"]})
    assert response.status_code == 400
    assert "Settings 에서 기본" in response.json()["detail"]


def test_default_provider_default_value_is_empty(client) -> None:
    """기본값이 제한된 안전성 Provider 면 위험을 확인하지 않고 실행하게 된다."""
    from app.config import DEFAULTS

    assert DEFAULTS["default_provider"] == ""


def test_logged_out_agy_is_rejected_before_search_job_submission(
    client, monkeypatch
) -> None:
    """Settings 캐시가 OK여도 fresh probe가 로그아웃이면 작업을 만들지 않는다."""
    from app.api import jobs as jobs_api
    from app.providers import registry

    stale = ProbeResult(
        provider="agy",
        display_name="agy",
        installed=True,
        executable_ok=True,
        auth_state=AuthState.OK,
        capabilities={"models": ["gemini-3.7-flash-high"]},
    )
    logged_out = ProbeResult(
        provider="agy",
        display_name="agy",
        installed=True,
        executable_ok=True,
        auth_state=AuthState.NOT_LOGGED_IN,
    )
    probe_calls: list[str] = []
    submitted: list[str] = []

    async def fresh_probe(provider_id, overrides=None):
        del overrides
        probe_calls.append(provider_id)
        return logged_out

    async def forbidden_submit(job_id):
        submitted.append(job_id)

    monkeypatch.setitem(registry._cache, "agy", stale)
    monkeypatch.setattr(jobs_api, "probe_one", fresh_probe)
    monkeypatch.setattr(jobs_api.RUNNER, "submit", forbidden_submit)

    response = client.post(
        "/api/jobs",
        json={
            "job_kind": "similarity_search",
            "provider": "agy",
            "model": "gemini-3.7-flash-high",
            "claim_text": "청구항 1. 제1 센서를 포함하는 장치.",
        },
    )

    assert response.status_code == 400
    assert "agy 로그인이 필요합니다" in response.json()["detail"]
    assert probe_calls == ["agy"]
    assert submitted == []




def test_tool_uncontrollable_providers_no_longer_need_approval(client) -> None:
    """사전 동의 관문을 걷어냈다. 설치·로그인만 끝나면 바로 쓸 수 있다.

    예전에는 Settings 의 체크박스를 켜야만 usable 이 되었다. 매번 같은 화면을
    넘기게 만들 뿐이라 없앴다. 위험 고지는 Provider 상세에 그대로 남는다.
    """
    for pid in ("agy", "codex"):
        data = client.get(f"/api/providers/{pid}").json()
        assert data["experimental"] is True, pid
        assert data["risks"], f"{pid} 위험 고지가 비어 있습니다."
        # 관문이 사라졌으므로 usable 은 설치·인증 상태와 같아야 한다.
        assert data["usable"] == data["runnable"], pid
        assert "opted_in" not in data, f"{pid} 에 폐기한 필드가 남아 있습니다."


def test_the_execution_gate_is_gone_from_the_code() -> None:
    """관문 함수가 남아 있으면 어딘가에서 다시 불릴 수 있다.

    실제 작업을 만들어 확인하지 않는다. agy 로 작업을 만들면 진짜 CLI 가
    돌면서 사용자 계정의 사용량을 쓴다. 여기서 지키려는 것은 '관문이
    없다'는 사실이고, 그건 코드에서 직접 확인하는 편이 정확하다.
    """
    from app.providers import registry

    assert not hasattr(registry, "is_allowed")
    assert not hasattr(registry, "apply_optin")
    # 화면 문구용 목록은 남는다. 실행을 막는 데 쓰지 않을 뿐이다.
    assert registry.TOOL_UNCONTROLLABLE_PROVIDERS == frozenset({"agy", "codex"})


def test_retired_optin_setting_is_rejected_and_hidden(client) -> None:
    """폐기한 설정 키는 쓸 수도 없고 응답에도 나오지 않는다."""
    from app.db import session_scope
    from app.models import AppSetting

    response = client.put(
        "/api/settings",
        json={"values": {"enabled_experimental_providers": ["agy"]}},
    )
    assert response.status_code == 400

    # 옛 버전이 저장해 둔 행이 남아 있어도 응답에 새지 않는다. 행 자체는
    # 지우지 않는다 — 사용자 데이터를 조용히 삭제하지 않는다.
    with session_scope() as session:
        session.add(
            AppSetting(key="enabled_experimental_providers", value=["agy"])
        )
    values = client.get("/api/settings").json()["values"]
    assert "enabled_experimental_providers" not in values


def test_warning_follows_the_selected_provider_not_a_gate(client) -> None:
    """경고는 '켜 두었는가'가 아니라 '지금 무엇으로 실행하는가'를 본다."""
    data = client.put(
        "/api/settings", json={"values": {"default_provider": "agy"}}
    ).json()
    assert any("셸·파일 도구를 끄는 수단이 없습니다" in note for note in data["warnings"])

    data = client.put(
        "/api/settings", json={"values": {"default_provider": "claude"}}
    ).json()
    assert not any("끄는 수단이 없습니다" in note for note in data["warnings"])
