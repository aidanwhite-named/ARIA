"""agy(Gemini) stream-json 파서.

여기 쓰인 이벤트는 agy 1.1.14 를 실제로 실행해서 받은 것을 그대로 옮긴
것이다(대화 ID 등 식별자만 교체).
"""

from __future__ import annotations

import json

from app.providers.agy_stream import AgyStreamParser, build_stdin_message

INIT = {
    "event": "init",
    "conversation_id": "conv-1",
    "init": {
        "cwd": "C:\\runs\\job",
        "tools": ["run_command", "view_file", "write_to_file", "search_web"],
        "permission_mode": "request-review",
    },
}
STEP_USER = {
    "event": "step_update",
    "step_update": {"conversation_id": "conv-1", "step_index": 0, "state": "DONE", "step_type": "user_input"},
}
STEP_CHECKPOINT = {
    "event": "step_update",
    "step_update": {"conversation_id": "conv-1", "step_index": 1, "state": "DONE", "step_type": "checkpoint", "duration_seconds": 3.28},
}
STEP_RESPONSE = {
    "event": "step_update",
    "step_update": {
        "conversation_id": "conv-1",
        "step_index": 2,
        "state": "DONE",
        "step_type": "agent_response",
        "text_delta": "분석 결과입니다.",
        "duration_seconds": 1.58,
        "usage": {"input_tokens": 13740, "output_tokens": 39, "total_tokens": 13779},
    },
}
RESULT_OK = {
    "event": "result",
    "result": {
        "conversation_id": "conv-1",
        "status": "SUCCESS",
        "response": "분석 결과입니다.",
        "duration_seconds": 5.39,
        "num_turns": 1,
        "usage": {"input_tokens": 13740, "output_tokens": 39, "total_tokens": 13779},
    },
}


def feed(parser: AgyStreamParser, payloads: list[dict]) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for payload in payloads:
        events.extend(parser.feed(json.dumps(payload, ensure_ascii=False)))
    return events


def test_full_success_flow() -> None:
    parser = AgyStreamParser()
    events = feed(parser, [INIT, STEP_USER, STEP_CHECKPOINT, STEP_RESPONSE, RESULT_OK])
    state = parser.state

    assert state.conversation_id == "conv-1"
    assert state.permission_mode == "request-review"
    assert len(state.tools_advertised) == 4
    assert state.saw_result
    assert not state.is_error
    assert state.final_text == "분석 결과입니다."
    assert state.usage["total_tokens"] == 13779
    assert state.num_turns == 1
    assert ("result_stream", {"delta": "분석 결과입니다."}) in events


def test_text_delta_accumulates_without_result_event() -> None:
    parser = AgyStreamParser()
    first = dict(STEP_RESPONSE)
    first["step_update"] = {**STEP_RESPONSE["step_update"], "text_delta": "앞부분 "}
    second = dict(STEP_RESPONSE)
    second["step_update"] = {**STEP_RESPONSE["step_update"], "text_delta": "뒷부분"}
    feed(parser, [INIT, first, second])
    assert not parser.state.saw_result
    assert parser.state.final_text == "앞부분 뒷부분"


def test_error_status_detected() -> None:
    parser = AgyStreamParser()
    feed(
        parser,
        [
            INIT,
            {
                "event": "result",
                "result": {"status": "ERROR", "response": "", "error": "something failed"},
            },
        ],
    )
    assert parser.state.is_error
    assert parser.state.error_message == "something failed"


def test_auth_failure_detected() -> None:
    parser = AgyStreamParser()
    parser.feed(
        json.dumps(
            {"event": "result", "result": {"status": "ERROR", "error": "unauthenticated: please log in"}}
        )
    )
    assert parser.state.auth_required
    assert not parser.state.rate_limited


def test_rate_limit_detected() -> None:
    parser = AgyStreamParser()
    parser.feed(
        json.dumps({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exceeded"}})
    )
    assert parser.state.rate_limited


def test_tool_like_step_recorded_as_tool_use() -> None:
    """agy 는 도구를 끌 수 없으므로 실제 호출을 반드시 감지해야 한다."""
    parser = AgyStreamParser()
    events = feed(
        parser,
        [
            INIT,
            {
                "event": "step_update",
                "step_update": {"step_index": 3, "state": "DONE", "step_type": "tool_call"},
            },
        ],
    )
    assert parser.state.tool_uses == ["tool_call"]
    assert any(t == "tool_use" for t, _ in events)


def test_benign_steps_are_not_tool_uses() -> None:
    parser = AgyStreamParser()
    feed(parser, [INIT, STEP_USER, STEP_CHECKPOINT, STEP_RESPONSE])
    assert parser.state.tool_uses == []


def test_plain_text_warning_line_is_preserved() -> None:
    """agy 는 경고를 평문으로 stdout 에 섞어 내보낸다."""
    parser = AgyStreamParser()
    events = parser.feed('warning: ignoring unsupported stream input message event "x"')
    assert events[0][0] == "stderr"
    assert parser.state.unparsed_lines
    assert parser.state.parse_errors == []


def test_malformed_json_preserves_prior_state() -> None:
    parser = AgyStreamParser()
    feed(parser, [INIT, STEP_RESPONSE])
    parser.feed("{ broken json")
    feed(parser, [RESULT_OK])
    assert parser.state.final_text == "분석 결과입니다."
    assert len(parser.state.parse_errors) == 1


def test_stdin_message_matches_verified_contract() -> None:
    """실측으로 확인한 형태. message 는 user 안이 아니라 최상위에 있어야 한다."""
    line = build_stdin_message("본문 텍스트")
    assert line.endswith("\n")
    payload = json.loads(line)
    assert payload["event"] == "user"
    assert payload["message"] == {"role": "user", "content": "본문 텍스트"}


def test_stdin_message_keeps_korean_unescaped() -> None:
    line = build_stdin_message("한글")
    assert "한글" in line


def test_build_args_and_policy() -> None:
    from pathlib import Path

    from app.providers.agy_cli import AgyCliProvider
    from app.providers.base import ExecutionRequest

    provider = AgyCliProvider()
    request = ExecutionRequest(
        job_id="j", work_dir=Path("."), system_prompt="규칙", user_message="본문", model="gemini-3.1-pro-low"
    )
    args = provider.build_args(request)

    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--model") + 1] == "gemini-3.1-pro-low"
    assert "--disable-slash-commands" in args
    # 절대 쓰면 안 되는 플래그
    assert "--dangerously-skip-permissions" not in args
    assert "--mode" not in args

    # 시스템 프롬프트를 분리할 수 없으므로 사용자 메시지 앞에 붙는다.
    composed = provider.compose_message(request)
    assert composed.startswith("[ARIA RUNTIME CONTEXT]")
    assert "규칙" in composed
    assert composed.endswith("본문")
