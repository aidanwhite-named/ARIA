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


def test_agy_uses_its_cli_name(client) -> None:
    data = client.get("/api/providers/agy").json()
    assert data["provider"] == "agy"
    assert data["display_name"] == "agy"


def test_agy_is_experimental_and_off_by_default(client) -> None:
    """agy 는 도구를 끌 수 없으므로 기본적으로 꺼져 있어야 한다."""
    data = client.get("/api/providers/agy").json()
    assert data["experimental"] is True
    assert data["opted_in"] is False
    assert data["usable"] is False
    assert data["risks"], "위험 고지가 비어 있습니다."
    # 설치·인증 자체는 별도로 보고한다.
    assert "runnable" in data


def test_non_experimental_providers_are_never_gated(client) -> None:
    for pid in ("claude", "codex"):
        data = client.get(f"/api/providers/{pid}").json()
        assert data["experimental"] is False
        assert data["opted_in"] is True


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
                # 기본값은 빈 문자열이다. 실험적 Provider 를 기본으로 남겨두면
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


# ------------------------------------------- 실험적 Provider opt-in 게이트


def test_job_creation_refused_for_experimental_provider_without_optin(client) -> None:
    """UI 를 우회한 직접 호출도 막혀야 한다."""
    prompt = client.post(
        "/api/prompts", json={"name": "게이트 확인", "body": "요약하십시오."}
    ).json()
    response = client.post(
        "/api/jobs", json={"prompt_id": prompt["id"], "provider": "agy"}
    )
    assert response.status_code == 403
    assert "실험적" in response.json()["detail"]


def test_smoke_test_refused_for_experimental_provider_without_optin(client) -> None:
    response = client.post("/api/providers/agy/smoke-test")
    assert response.status_code == 403
    assert "실험적" in response.json()["detail"]


def test_no_provider_and_no_default_refuses_instead_of_auto_selecting(client) -> None:
    """안전 정책을 만족한 Provider 가 없으면 자동 선택하지 않는다."""
    client.put("/api/settings", json={"values": {"default_provider": ""}})
    prompt = client.post(
        "/api/prompts", json={"name": "자동선택 금지", "body": "요약하십시오."}
    ).json()
    response = client.post("/api/jobs", json={"prompt_id": prompt["id"]})
    assert response.status_code == 400
    assert "Settings 에서 기본" in response.json()["detail"]


def test_experimental_default_provider_still_requires_optin(client) -> None:
    """기본 Provider 로 지정돼 있어도 opt-in 없이는 실행되지 않는다."""
    client.put("/api/settings", json={"values": {"default_provider": "agy"}})
    try:
        prompt = client.post(
            "/api/prompts", json={"name": "기본값 게이트", "body": "요약하십시오."}
        ).json()
        response = client.post("/api/jobs", json={"prompt_id": prompt["id"]})
        assert response.status_code == 403
        assert "실험적" in response.json()["detail"]
    finally:
        client.put("/api/settings", json={"values": {"default_provider": ""}})


def test_optin_makes_experimental_provider_usable(client) -> None:
    try:
        client.put(
            "/api/settings",
            json={"values": {"enabled_experimental_providers": ["agy"]}},
        )
        data = client.get("/api/providers/agy").json()
        assert data["opted_in"] is True
        # usable 은 이제 설치·인증 상태에만 달려 있다.
        assert data["usable"] == data["runnable"]
    finally:
        client.put(
            "/api/settings", json={"values": {"enabled_experimental_providers": []}}
        )


def test_optin_surfaces_a_settings_warning(client) -> None:
    try:
        data = client.put(
            "/api/settings",
            json={"values": {"enabled_experimental_providers": ["agy"]}},
        ).json()
        assert any("실험적 Provider" in w for w in data["warnings"])
    finally:
        client.put(
            "/api/settings", json={"values": {"enabled_experimental_providers": []}}
        )


async def test_queued_job_blocked_when_optin_revoked_before_execution(client) -> None:
    """이미 큐에 들어간 작업도 opt-in 이 해제되면 실행되지 않아야 한다.

    작업 생성 시점의 검사만으로는 부족하다. 사용자가 생성 직후 Provider 를
    다시 끄면, 대기 중이던 작업이 그대로 실행돼 opt-in 해제가 의미를 잃는다.
    """
    from app.db import session_scope
    from app.execution.runner import RUNNER
    from app.models import ExecutionJob

    # opt-in 이 켜진 상태에서 작업이 큐에 들어갔다고 본다.
    client.put(
        "/api/settings", json={"values": {"enabled_experimental_providers": ["agy"]}}
    )
    with session_scope() as session:
        job = ExecutionJob(
            prompt_name="큐 대기 작업",
            prompt_snapshot="요약하십시오.",
            provider="agy",
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    # 실행 전에 사용자가 다시 껐다.
    client.put(
        "/api/settings", json={"values": {"enabled_experimental_providers": []}}
    )

    await RUNNER._run_inner(job_id)

    with session_scope() as session:
        stored = session.get(ExecutionJob, job_id)
        assert stored.status == JobStatus.FAILED
        assert stored.error_code == ErrorCode.PROVIDER_UNAVAILABLE
        assert any("활성화되어 있지 않" in e for e in (stored.errors or []))


def test_default_provider_default_value_is_empty(client) -> None:
    """기본값이 실험적 Provider 면 사용자가 위험을 확인하지 않고 실행하게 된다."""
    from app.config import DEFAULTS

    assert DEFAULTS["default_provider"] == ""
    assert DEFAULTS["enabled_experimental_providers"] == []


async def test_waiting_job_blocked_when_optin_revoked_during_semaphore_wait(
    client, monkeypatch
) -> None:
    """세마포어에서 대기하던 작업도 opt-in 해제를 따라야 한다.

    작업 시작 시점의 검사만으로는 부족하다. Provider 당 동시 실행이 1이면
    두 번째 작업은 앞 작업이 끝날 때까지 세마포어에서 기다리는데, 그동안
    사용자가 Provider 를 꺼도 대기가 풀리는 순간 그대로 실행돼 버린다.

    여기서는 테스트가 직접 세마포어를 점유해 그 상황을 만든 뒤, 대기가
    풀렸을 때 Provider 가 아예 호출되지 않는지 확인한다.
    """
    import asyncio

    from app.db import session_scope
    from app.execution import runner as runner_module
    from app.execution.runner import RUNNER
    from app.models import ExecutionJob

    executed: list[str] = []

    class SpyProvider:
        id = "agy"

        async def execute(self, request, emit):  # pragma: no cover - 호출되면 실패
            executed.append(request.job_id)
            raise AssertionError("차단됐어야 할 작업에서 Provider 가 호출되었습니다.")

        async def cancel(self, job_id: str) -> bool:
            return False

    monkeypatch.setattr(
        runner_module, "build_provider", lambda pid, overrides=None: SpyProvider()
    )

    client.put(
        "/api/settings",
        json={
            "values": {
                "enabled_experimental_providers": ["agy"],
                "max_concurrency_per_provider": 1,
            }
        },
    )

    with session_scope() as session:
        job = ExecutionJob(
            prompt_name="세마포어 대기 작업",
            prompt_snapshot="요약하십시오.",
            provider="agy",
            status=JobStatus.QUEUED,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    # 앞선 작업이 실행 중인 것처럼 세마포어를 점유한다. 러너가 쓰는 것과
    # 같은 객체여야 하므로 동일한 limit 으로 가져온다.
    semaphore = RUNNER._semaphore("agy", 1)
    await semaphore.acquire()
    try:
        task = asyncio.create_task(RUNNER._run_inner(job_id))
        # 세마포어 앞 검사를 통과해 대기 상태에 들어갈 시간을 준다.
        # 이 구간의 작업은 로컬 sqlite 읽기뿐이다.
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert not task.done(), "작업이 세마포어에서 대기하지 않았습니다."

        # 대기하는 동안 사용자가 Provider 를 껐다.
        client.put(
            "/api/settings", json={"values": {"enabled_experimental_providers": []}}
        )
    finally:
        semaphore.release()

    await asyncio.wait_for(task, timeout=30)

    assert executed == [], "Provider 가 호출되었습니다."
    with session_scope() as session:
        stored = session.get(ExecutionJob, job_id)
        assert stored.status == JobStatus.FAILED
        assert stored.error_code == ErrorCode.PROVIDER_UNAVAILABLE
        # 세마포어 앞 검사가 아니라 대기 후 재확인이 걸렸는지 구분한다.
        assert any("대기 중 비활성화" in e for e in (stored.errors or [])), stored.errors
