"""Codex Provider 의 실행 계약.

여기서 지키려는 것은 두 가지다.

1. 분석 실행에서 web_search 가 켜지지 않는다. Codex 는 셸·파일 도구를 끄지
   못하므로, 끌 수 있는 유일한 도구마저 켜둔 채 분석을 돌리면 안 된다.
2. 도구 호출을 하나도 놓치지 않는다. 차단하지 못하고 탐지만 하는 Provider 라
   탐지가 곧 경계다. 탐지에 구멍이 나면 사용자는 도구가 돌았다는 사실조차
   모른 채 결과를 신뢰하게 된다.

이벤트 표본은 codex-cli 0.149.0 을 실제로 실행해서 받은 것이다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.providers.base import CODEX_WEB_SEARCH, NO_TOOLS, ExecutionRequest
from app.providers.codex_cli import CodexCliProvider
from app.providers.codex_stream import TOOL_ITEM_TYPES, CodexStreamParser


def _feed(parser: CodexStreamParser, payload: dict) -> list[tuple[str, dict]]:
    return parser.feed(json.dumps(payload, ensure_ascii=False))


def _request(tmp_path: Path, **kwargs) -> ExecutionRequest:
    return ExecutionRequest(
        job_id="job-1",
        work_dir=tmp_path,
        system_prompt="런타임 규칙",
        user_message="청구항 본문",
        **kwargs,
    )


# ------------------------------------------------------------------- 실행 인수


def test_analysis_run_turns_web_search_off(tmp_path: Path) -> None:
    args = CodexCliProvider().build_args(_request(tmp_path))
    assert "-c" in args
    assert "tools.web_search=false" in args
    assert "tools.web_search=true" not in args


def test_search_run_turns_web_search_on(tmp_path: Path) -> None:
    args = CodexCliProvider().build_args(
        _request(tmp_path, tool_policy=CODEX_WEB_SEARCH)
    )
    assert "tools.web_search=true" in args
    assert "tools.web_search=false" not in args


def test_no_tools_policy_does_not_enable_search(tmp_path: Path) -> None:
    args = CodexCliProvider().build_args(_request(tmp_path, tool_policy=NO_TOOLS))
    assert "tools.web_search=false" in args


def test_never_bypasses_sandbox_or_approvals(tmp_path: Path) -> None:
    for policy in (NO_TOOLS, CODEX_WEB_SEARCH):
        args = CodexCliProvider().build_args(_request(tmp_path, tool_policy=policy))
        for forbidden in (
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--approve-for-me",
            "danger-full-access",
            "workspace-write",
        ):
            assert forbidden not in args, forbidden
        assert args[args.index("--sandbox") + 1] == "read-only"


def test_host_config_is_isolated_and_session_not_persisted(tmp_path: Path) -> None:
    args = CodexCliProvider().build_args(_request(tmp_path))
    for expected in ("--ignore-user-config", "--ignore-rules", "--ephemeral"):
        assert expected in args, expected


def test_prompt_is_read_from_stdin_not_argv(tmp_path: Path) -> None:
    """Windows 명령행 길이 제한 때문에 프롬프트는 인수로 넘길 수 없다."""
    request = _request(tmp_path)
    args = CodexCliProvider().build_args(request)
    assert args[-1] == "-"
    assert request.user_message not in args
    assert request.system_prompt not in args


def test_runtime_context_is_prepended_because_system_prompt_cannot_be_split(
    tmp_path: Path,
) -> None:
    message = CodexCliProvider().compose_message(_request(tmp_path))
    assert message.startswith("[ARIA RUNTIME CONTEXT]")
    assert "런타임 규칙" in message
    assert message.rstrip().endswith("청구항 본문")


# --------------------------------------------------------------- 스트림 파싱


def test_final_text_comes_from_output_file_not_the_stream(tmp_path: Path) -> None:
    """중간 발화를 이어 붙이면 보고서가 아니라 대화록이 된다."""
    provider = CodexCliProvider()
    (tmp_path / "codex_last_message.txt").write_text("최종 보고서", encoding="utf-8")
    assert provider._read_last_message(tmp_path) == "최종 보고서"
    assert provider._read_last_message(tmp_path / "없음") == ""


def test_agent_messages_accumulate_as_fallback() -> None:
    parser = CodexStreamParser()
    _feed(parser, {"type": "thread.started", "thread_id": "t1"})
    events = _feed(
        parser,
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "본문"},
        },
    )
    assert ("result_stream", {"delta": "본문"}) in events
    assert parser.state.fallback_text == "본문"
    assert parser.state.tool_uses == []


def test_usage_and_completion_are_captured() -> None:
    parser = CodexStreamParser()
    usage = {"input_tokens": 14319, "output_tokens": 5}
    _feed(parser, {"type": "turn.completed", "usage": usage})
    assert parser.state.usage == usage
    assert parser.state.status == "completed"
    assert parser.state.is_error is False


def test_every_known_tool_item_is_detected() -> None:
    """도구 하나라도 빠지면 그 실행은 도구 없이 돈 것처럼 보인다."""
    for index, item_type in enumerate(sorted(TOOL_ITEM_TYPES)):
        parser = CodexStreamParser()
        _feed(
            parser,
            {
                "type": "item.completed",
                "item": {"id": f"item_{index}", "type": item_type},
            },
        )
        assert parser.state.tool_uses == [item_type], item_type
        assert parser.state.tool_calls[0]["ok"] is True


def test_started_and_completed_count_as_one_call() -> None:
    parser = CodexStreamParser()
    for envelope in ("item.started", "item.completed"):
        _feed(
            parser,
            {
                "type": envelope,
                "item": {"id": "item_3", "type": "web_search", "query": "청구항 유사"},
            },
        )
    assert parser.state.tool_uses == ["web_search"]
    assert len(parser.state.tool_calls) == 1
    assert parser.state.tool_calls[0]["input"] == {"query": "청구항 유사"}


def test_failed_tool_call_is_recorded_as_failed() -> None:
    parser = CodexStreamParser()
    events = _feed(
        parser,
        {
            "type": "item.completed",
            "item": {
                "id": "item_4",
                "type": "command_execution",
                "command": "echo hi",
                "status": "failed",
            },
        },
    )
    call = parser.state.tool_calls[0]
    assert call["ok"] is False
    assert call["input"] == {"command": "echo hi"}
    assert any(name == "tool_error" for name, _ in events)
    # 실패했어도 호출은 호출이다. 정책 판정에서 빠지면 안 된다.
    assert parser.state.tool_uses == ["command_execution"]


def test_unknown_item_type_that_looks_like_a_tool_is_treated_as_one() -> None:
    """다음 버전에서 도구가 하나 늘었을 때 조용히 통과하는 것이 가장 나쁘다."""
    parser = CodexStreamParser()
    _feed(
        parser,
        {
            "type": "item.completed",
            "item": {"id": "item_5", "type": "browser_tool_call"},
        },
    )
    assert parser.state.tool_uses == ["browser_tool_call"]
    assert parser.state.unknown_item_types == ["browser_tool_call"]


def test_unknown_item_type_that_is_not_a_tool_is_only_recorded() -> None:
    parser = CodexStreamParser()
    events = _feed(
        parser,
        {"type": "item.completed", "item": {"id": "item_6", "type": "summary_note"}},
    )
    assert parser.state.tool_uses == []
    assert parser.state.unknown_item_types == ["summary_note"]
    assert events == [("stage", {"stage": "summary_note", "message": "summary_note"})]


def test_turn_failed_is_an_error() -> None:
    parser = CodexStreamParser()
    _feed(
        parser,
        {"type": "turn.failed", "error": {"message": "usage_limit_reached"}},
    )
    assert parser.state.is_error is True
    assert parser.state.rate_limited is True
    assert "usage_limit_reached" in parser.state.error_message


def test_plain_log_lines_are_passed_through_not_dropped() -> None:
    """Codex 는 tracing 로그를 평문으로 섞어 내보낸다."""
    parser = CodexStreamParser()
    events = parser.feed("2026-08-21T09:42:20Z ERROR codex_core::tools::router: ...")
    assert events and events[0][0] == "stderr"
    assert parser.state.unparsed_lines


def test_broken_json_does_not_discard_earlier_state() -> None:
    parser = CodexStreamParser()
    _feed(
        parser,
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "본문"},
        },
    )
    parser.feed('{"type": "item.completed", "item"')
    assert parser.state.fallback_text == "본문"
    assert parser.state.parse_errors


# ------------------------------------------------- 도구 능력에 맞춘 증거 계약


def test_codex_search_context_never_mentions_tools_it_does_not_have() -> None:
    """없는 도구를 전제한 문구가 남으면 열지도 않은 페이지에 등급이 붙는다."""
    from app.config import CODEX_SEARCH_RUNTIME_CONTEXT

    assert "WebFetch" not in CODEX_SEARCH_RUNTIME_CONTEXT
    assert "WebSearch" not in CODEX_SEARCH_RUNTIME_CONTEXT
    assert "read_url_content" not in CODEX_SEARCH_RUNTIME_CONTEXT
    assert "web_search" in CODEX_SEARCH_RUNTIME_CONTEXT


def test_codex_search_context_caps_evidence_at_snippet_level() -> None:
    from app.config import CODEX_SEARCH_RUNTIME_CONTEXT

    assert '항상 "candidate_only"' in CODEX_SEARCH_RUNTIME_CONTEXT
    assert "raw_original_verified 를 부여하지" in CODEX_SEARCH_RUNTIME_CONTEXT


def test_codex_search_tool_name_is_counted_by_the_manifest() -> None:
    """이름이 목록에서 빠지면 검색은 돌았는데 횟수가 0으로 보인다."""
    from app import search_manifest

    assert "web_search" in search_manifest.SEARCH_TOOL_NAMES
    # Codex 에는 페이지를 여는 도구가 없다. 열람 목록에 넣으면 안 된다.
    assert "web_search" not in search_manifest.FETCH_TOOL_NAMES


def test_runner_picks_the_codex_context_for_the_codex_policy() -> None:
    from app.config import CODEX_SEARCH_RUNTIME_CONTEXT
    from app.execution.runner import _SEARCH_CONTEXT_BY_POLICY

    assert (
        _SEARCH_CONTEXT_BY_POLICY[CODEX_WEB_SEARCH.name]
        is CODEX_SEARCH_RUNTIME_CONTEXT
    )
