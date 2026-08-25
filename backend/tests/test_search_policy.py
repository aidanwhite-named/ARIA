"""작업별 도구 정책.

이 파일이 지키는 불변조건은 하나다. 검색 기능을 붙이면서 기존 PDF/문헌 분석의
'도구 없음'이 조금이라도 느슨해지면 안 된다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.providers.base import (
    AGY_WEB_SEARCH,
    NO_TOOLS,
    WEB_SEARCH,
    ExecutionOutcome,
    ExecutionRequest,
    ToolPolicy,
)
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.claude_stream import ClaudeStreamParser


def _request(policy: ToolPolicy | None = None) -> ExecutionRequest:
    kwargs = {} if policy is None else {"tool_policy": policy}
    return ExecutionRequest(
        job_id="j",
        work_dir=Path("."),
        system_prompt="s",
        user_message="m",
        **kwargs,
    )


def _ok(**kwargs) -> ExecutionOutcome:
    outcome = ExecutionOutcome(
        result_text="정상 결과", exit_code=0, terminal_reason="completed"
    )
    for key, value in kwargs.items():
        setattr(outcome, key, value)
    return outcome


# ------------------------------------------------------------- CLI 인수 구성


def test_default_request_still_disables_all_tools() -> None:
    """정책을 지정하지 않은 호출 경로는 예전과 똑같이 도구가 꺼진다."""
    args = ClaudeCliProvider().build_args(_request())
    assert args[args.index("--tools") + 1] == ""
    assert "--allowedTools" not in args
    assert "--permission-mode" not in args


def test_analysis_policy_disables_all_tools() -> None:
    args = ClaudeCliProvider().build_args(_request(NO_TOOLS))
    assert args[args.index("--tools") + 1] == ""
    assert "--allowedTools" not in args
    # 도구가 없으면 물어볼 권한도 없다. 권한 모드를 건드리지 않는다.
    assert "--permission-mode" not in args
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == ""


def test_search_policy_opens_only_web_tools() -> None:
    args = ClaudeCliProvider().build_args(_request(WEB_SEARCH))
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"

    allowed_at = args.index("--allowedTools")
    allowed = args[allowed_at + 1 : allowed_at + 3]
    assert allowed == ["WebSearch", "WebFetch"]

    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    # 검색 실행도 호스트 설정과 외부 MCP 를 상속하지 않는다.
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in args
    assert "--no-chrome" in args
    # 권한 우회 플래그는 어떤 경로에서도 쓰지 않는다.
    assert "--dangerously-skip-permissions" not in args
    assert "--allow-dangerously-skip-permissions" not in args


def test_no_shell_or_file_tool_can_be_requested() -> None:
    for forbidden in ("Bash", "Read", "Write", "Edit", "Task"):
        assert forbidden not in WEB_SEARCH.allowed_tools


def test_providers_declare_their_supported_search_policies() -> None:
    from app.providers.agy_cli import AgyCliProvider
    from app.providers.base import CODEX_WEB_SEARCH
    from app.providers.codex_cli import CodexCliProvider

    assert ClaudeCliProvider().supports_tool_policy(WEB_SEARCH)
    assert ClaudeCliProvider().supports_tool_policy(NO_TOOLS)
    assert AgyCliProvider().supports_tool_policy(AGY_WEB_SEARCH)
    assert AgyCliProvider().search_tool_policy is AGY_WEB_SEARCH
    assert not AGY_WEB_SEARCH.enforce_advertised_allowlist
    assert not AgyCliProvider().supports_tool_policy(WEB_SEARCH)
    assert CodexCliProvider().supports_tool_policy(CODEX_WEB_SEARCH)
    assert CodexCliProvider().search_tool_policy is CODEX_WEB_SEARCH
    assert not CODEX_WEB_SEARCH.enforce_advertised_allowlist
    # Claude 전용 정책을 도구를 끄지 못하는 Provider 가 주장하면 안 된다.
    assert not CodexCliProvider().supports_tool_policy(WEB_SEARCH)
    assert not CodexCliProvider().supports_tool_policy(NO_TOOLS)


# --------------------------------------------------------------- 판정 규칙


def test_analysis_outcome_with_tools_still_fails() -> None:
    verdict = evaluate(_ok(tool_policy=NO_TOOLS, tools_advertised=["Read"]))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_search_outcome_with_allowed_tools_succeeds() -> None:
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["WebSearch", "WebFetch"],
        )
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_search_outcome_with_stray_tool_use_fails() -> None:
    for stray in ("Bash", "Write", "Edit", "Read", "Task"):
        verdict = evaluate(
            _ok(
                tool_policy=WEB_SEARCH,
                tools_advertised=["WebSearch", "WebFetch"],
                tool_uses=["WebSearch", stray],
            )
        )
        assert verdict.status == JobStatus.FAILED, stray
        assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION
        assert stray in " ".join(verdict.errors)


def test_search_outcome_with_stray_advertised_tool_fails() -> None:
    """호출하지 않고 광고만 해도 위반이다. 목록이 깨졌다는 뜻이기 때문이다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch", "Bash"],
            tool_uses=["WebSearch"],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_agy_search_ignores_extra_advertised_tools_but_checks_actual_calls() -> None:
    """agy는 노출 목록을 줄이지 못하므로 광고가 아니라 실제 호출을 판정한다."""
    allowed = evaluate(
        _ok(
            tool_policy=AGY_WEB_SEARCH,
            tools_advertised=["search_web", "read_url_content", "run_command"],
            tool_uses=["search_web", "read_url_content"],
        )
    )
    assert allowed.status == JobStatus.SUCCEEDED

    forbidden = evaluate(
        _ok(
            tool_policy=AGY_WEB_SEARCH,
            tools_advertised=["search_web", "run_command"],
            tool_uses=["search_web", "run_command"],
        )
    )
    assert forbidden.status == JobStatus.FAILED
    assert forbidden.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_global_optout_cannot_widen_search_allowlist() -> None:
    """fail_on_tool_use 를 꺼도 검색의 허용 목록은 넓어지지 않는다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["Bash"],
        ),
        fail_on_tool_use=False,
    )
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_global_optout_cannot_reopen_analysis_tools() -> None:
    verdict = evaluate(
        _ok(tool_policy=NO_TOOLS, tool_uses=["Read"]), fail_on_tool_use=False
    )
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_search_without_any_search_call_fails() -> None:
    """도구를 안 쓰고 기억으로 쓴 보고서는 검색 결과가 아니다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=[],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_NOT_PERFORMED


def test_fetch_only_run_is_not_a_search() -> None:
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["WebFetch"],
        )
    )
    assert verdict.error_code == ErrorCode.SEARCH_NOT_PERFORMED


def test_auth_failure_wins_over_search_not_performed() -> None:
    outcome = _ok(tool_policy=WEB_SEARCH, tool_uses=[], auth_required=True)
    assert evaluate(outcome).error_code == ErrorCode.AUTH_REQUIRED


def test_process_error_wins_over_search_not_performed() -> None:
    outcome = ExecutionOutcome(
        result_text="", error_message="실행 실패", tool_policy=WEB_SEARCH
    )
    assert evaluate(outcome).error_code == ErrorCode.PROCESS_ERROR


def test_budget_exceeded_is_reported_as_its_own_failure() -> None:
    """ARIA 가 끊은 것이므로 cancelled 도 참이다. 사용자 취소와 구별한다."""
    outcome = _ok(
        tool_policy=WEB_SEARCH,
        tool_uses=["WebSearch"] * 41,
        tool_budget_exceeded=True,
        cancelled=True,
    )
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_BUDGET_EXCEEDED


def test_stray_tool_outranks_budget_exceeded() -> None:
    """둘 다 걸렸으면 알아야 할 것은 '많이 불렀다'가 아니라 '뭘 불렀다'이다."""
    outcome = _ok(
        tool_policy=WEB_SEARCH,
        tools_advertised=["WebSearch", "WebFetch"],
        tool_uses=["WebSearch", "Bash"],
        tool_budget_exceeded=True,
        cancelled=True,
    )
    assert evaluate(outcome).error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_user_cancel_is_still_cancelled() -> None:
    outcome = _ok(tool_policy=WEB_SEARCH, cancelled=True)
    assert evaluate(outcome).error_code == ErrorCode.CANCELLED


# ------------------------------------------------------- stream 도구 이벤트


def test_stream_records_search_queries_and_urls() -> None:
    parser = ClaudeStreamParser()
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "sonnet",
                "tools": ["WebFetch", "WebSearch"],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "WebSearch",
                            "input": {
                                "query": "claim similar patent",
                                "allowed_domains": ["patents.google.com"],
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "WebFetch",
                            "input": {
                                "url": "https://patents.google.com/patent/US1",
                                "prompt": "x" * 5000,
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "is_error": True,
                            "content": "403 Forbidden",
                        }
                    ]
                },
            }
        ),
    ]
    events = []
    for line in lines:
        events.extend(parser.feed(line))

    state = parser.state
    assert state.tool_names == ["WebFetch", "WebSearch"]
    assert state.tool_uses == ["WebSearch", "WebFetch"]

    search, fetch = state.tool_calls
    assert search["name"] == "WebSearch"
    assert search["input"]["query"] == "claim similar patent"
    assert search["input"]["allowed_domains"] == ["patents.google.com"]
    assert search["ok"] is True
    assert search["ts"]

    assert fetch["input"]["url"] == "https://patents.google.com/patent/US1"
    # WebFetch 의 prompt 본문은 감사 기록에 옮겨 담지 않는다.
    assert "prompt" not in fetch["input"]
    assert fetch["ok"] is False
    assert "403" in fetch["error"]

    tool_events = [payload for kind, payload in events if kind == "tool_use"]
    assert tool_events[0]["input"]["query"] == "claim similar patent"
    assert [payload for kind, payload in events if kind == "tool_error"]


def test_stream_does_not_record_arguments_of_unexpected_tools() -> None:
    """허용 목록 밖 도구의 인수는 그 자체가 명령일 수 있다. 키 이름만 남긴다."""
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t9",
                            "name": "Bash",
                            "input": {"command": "rm -rf /"},
                        }
                    ]
                },
            }
        )
    )
    recorded = parser.state.tool_calls[0]
    assert recorded["name"] == "Bash"
    assert recorded["input"] == {"keys": ["command"]}
    assert "rm -rf" not in json.dumps(recorded, ensure_ascii=False)
