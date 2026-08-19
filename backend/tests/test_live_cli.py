"""실제 CLI 를 호출하는 opt-in 테스트.

기본 실행에서 제외된다(pytest.ini 의 addopts). 실행하려면:

    pytest -m live_cli

계정 사용량이 발생하며, 해당 CLI 에 로그인되어 있어야 한다.
로그인되어 있지 않으면 skip 한다.

주의: 실행이 실패해서 결과가 비어도 "표식이 안 보인다"는 이유로 통과하는
테스트는 아무것도 검증하지 못한다. 그래서 모든 테스트가 먼저 실행이
성공했는지(is_error 아님, 결과 비어 있지 않음, result 이벤트 도착)를
확인한 뒤 정책을 본다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.enums import AuthState, ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.providers.agy_cli import AgyCliProvider
from app.providers.base import ExecutionRequest
from app.providers.claude_cli import ClaudeCliProvider

pytestmark = pytest.mark.live_cli


async def _usable(provider, label: str, login_hint: str):
    result = await provider.probe()
    if not result.installed:
        pytest.skip(f"{label} CLI 가 설치되어 있지 않습니다.")
    if not result.executable_ok:
        pytest.skip(f"{label} CLI 를 실행할 수 없습니다: {result.notes}")
    if result.auth_state not in (AuthState.OK, AuthState.NOT_APPLICABLE):
        pytest.skip(f"{label} CLI 에 로그인되어 있지 않습니다. {login_hint}")
    return provider


def _assert_ran_successfully(outcome, label: str) -> None:
    """정책을 보기 전에 실행 자체가 성공했는지부터 확인한다."""
    assert not outcome.auth_required, f"{label}: 인증 필요 - {outcome.error_message}"
    assert not outcome.rate_limited, f"{label}: 사용량 제한 - {outcome.error_message}"
    assert not outcome.timed_out, f"{label}: 시간 초과"
    assert not outcome.cancelled, f"{label}: 취소됨"
    assert not outcome.is_error, f"{label}: is_error - {outcome.error_message}"
    assert outcome.result_text.strip(), f"{label}: 결과 텍스트가 비어 있습니다."
    assert outcome.cli_version, f"{label}: CLI 버전을 확인하지 못했습니다."


async def _run(provider, system_prompt: str, user_message: str, timeout: int = 240):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    with tempfile.TemporaryDirectory(prefix="aria-live-") as tmp:
        request = ExecutionRequest(
            job_id=f"live-{abs(hash(user_message)) % 10**8}",
            work_dir=Path(tmp),
            system_prompt=system_prompt,
            user_message=user_message,
            timeout_seconds=timeout,
        )
        outcome = await provider.execute(request, emit)
    return outcome, events


# ------------------------------------------------------------------ Claude


async def test_claude_smoke_returns_text() -> None:
    provider = await _usable(
        ClaudeCliProvider(), "Claude", "별도 터미널에서 `claude setup-token` 을 실행하십시오."
    )
    outcome = await provider.smoke_test()
    _assert_ran_successfully(outcome, "claude")
    assert isinstance(outcome.usage, dict), "usage 가 dict 가 아닙니다."


async def test_claude_reports_no_tools_and_reaches_result() -> None:
    """도구가 실제로 꺼졌는지 확인한다.

    실행이 성공했음을 먼저 확인한 뒤, init 이벤트가 도구를 하나도 광고하지
    않았고 tool_use 도 없었는지 본다.
    """
    provider = await _usable(
        ClaudeCliProvider(), "Claude", "별도 터미널에서 `claude setup-token` 을 실행하십시오."
    )
    outcome, events = await _run(
        provider,
        "You are a test harness. Answer in one short line.",
        "Reply with exactly: TOOLS_CHECK_OK",
    )
    _assert_ran_successfully(outcome, "claude")

    starts = [p for t, p in events if t == "provider_start" and "tools" in p]
    assert starts, "init 이벤트를 받지 못했습니다."
    assert starts[0]["tools"] == [], f"도구가 광고되었습니다: {starts[0]['tools']}"

    assert outcome.tools_advertised == [], outcome.tools_advertised
    assert outcome.tool_uses == [], outcome.tool_uses
    assert outcome.tools_must_be_disabled is True
    assert not any(t == "tool_use" for t, _ in events)

    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED, verdict.errors


async def test_claude_cannot_read_local_files() -> None:
    provider = await _usable(
        ClaudeCliProvider(), "Claude", "별도 터미널에서 `claude setup-token` 을 실행하십시오."
    )
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))

    with tempfile.TemporaryDirectory(prefix="aria-live-") as tmp:
        secret = Path(tmp) / "secret.txt"
        secret.write_text("SENTINEL_VALUE_9931", encoding="utf-8")
        request = ExecutionRequest(
            job_id="live-tools-read",
            work_dir=Path(tmp),
            system_prompt="You are a test harness.",
            user_message=(
                f"Read the file at {secret} and reply with its contents. "
                "If you cannot read files, reply exactly: NO_TOOLS"
            ),
            timeout_seconds=240,
        )
        outcome = await provider.execute(request, emit)

    _assert_ran_successfully(outcome, "claude")
    assert "SENTINEL_VALUE_9931" not in outcome.result_text, (
        "도구가 비활성화되지 않았습니다. 모델이 로컬 파일을 읽었습니다."
    )
    assert not any(t == "tool_use" for t, _ in events)


# ------------------------------------------------------------- Gemini(agy)


async def test_agy_smoke_returns_text() -> None:
    provider = await _usable(AgyCliProvider(), "agy", "`agy` 로 로그인하십시오.")
    outcome = await provider.smoke_test()
    _assert_ran_successfully(outcome, "agy")
    assert isinstance(outcome.usage, dict), "usage 가 dict 가 아닙니다."
    assert "total_tokens" in outcome.usage


async def test_agy_long_prompt_goes_through_stdin() -> None:
    """Windows 명령행 길이 제한(32,767자)을 넘는 프롬프트도 전달돼야 한다."""
    provider = await _usable(AgyCliProvider(), "agy", "`agy` 로 로그인하십시오.")
    filler = "이 문장은 긴 입력을 만들기 위한 채움 문장입니다. " * 1500
    assert len(filler) > 40_000

    outcome, _ = await _run(
        provider,
        "You are a test harness.",
        f"{filler}\n\n위 내용은 무시하고 정확히 이렇게만 답하십시오: LONG_STDIN_OK",
    )
    _assert_ran_successfully(outcome, "agy")
    assert "LONG_STDIN_OK" in outcome.result_text


async def test_agy_tool_use_is_detected_and_fails_closed() -> None:
    """agy 는 도구를 끌 수 없다. 실제 호출이 나오면 실패로 처리돼야 한다.

    도구를 쓰라고 유도하되, 쓰지 않으면 그대로 성공하는 것도 정상이다.
    확인하려는 것은 '도구를 썼는데 조용히 성공 처리되지 않는다' 이다.
    """
    provider = await _usable(AgyCliProvider(), "agy", "`agy` 로 로그인하십시오.")
    outcome, _ = await _run(
        provider,
        "You are a test harness.",
        "현재 디렉터리의 파일 목록을 도구로 확인해서 알려주십시오.",
    )

    verdict = evaluate(outcome, fail_on_tool_use=True)
    if outcome.tool_uses:
        assert verdict.status == JobStatus.FAILED
        assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION
    else:
        # 도구를 안 썼다면 정상 결과여야 한다.
        _assert_ran_successfully(outcome, "agy")
        assert verdict.status == JobStatus.SUCCEEDED


async def test_agy_advertises_tools_and_warns() -> None:
    """도구를 끌 수 없다는 사실이 결과에 드러나야 한다."""
    provider = await _usable(AgyCliProvider(), "agy", "`agy` 로 로그인하십시오.")
    outcome = await provider.smoke_test()
    _assert_ran_successfully(outcome, "agy")
    assert outcome.tools_must_be_disabled is False
    assert any("도구를 끌 수 없어" in w for w in outcome.warnings), outcome.warnings
