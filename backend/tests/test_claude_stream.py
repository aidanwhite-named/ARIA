"""Claude stream-json 증분 파싱.

핵심 요구사항: 한 줄이 깨져도 그때까지 받은 결과를 버리지 않는다.
"""

from __future__ import annotations

import json

from app.providers.claude_stream import ClaudeStreamParser


def feed_all(parser: ClaudeStreamParser, lines: list[str]) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for line in lines:
        events.extend(parser.feed(line))
    return events


def test_init_and_result() -> None:
    parser = ClaudeStreamParser()
    events = feed_all(
        parser,
        [
            json.dumps({"type": "system", "subtype": "init", "model": "sonnet", "tools": []}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "최종 답변입니다.",
                    "terminal_reason": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
        ],
    )
    assert ("provider_start", {"model": "sonnet", "tools": [], "message": "Claude 세션 시작"}) in events
    assert parser.state.saw_result
    assert parser.state.final_text == "최종 답변입니다."
    assert parser.state.usage == {"input_tokens": 10, "output_tokens": 5}
    assert not parser.state.is_error


def test_partial_message_deltas_stream() -> None:
    parser = ClaudeStreamParser()
    events = feed_all(
        parser,
        [
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "안녕"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "하세요"},
                    },
                }
            ),
        ],
    )
    deltas = [p["delta"] for t, p in events if t == "result_stream"]
    assert deltas == ["안녕", "하세요"]
    # result 이벤트가 없으면 델타 누적본이 최종 텍스트가 된다.
    assert parser.state.final_text == "안녕하세요"


def test_malformed_line_preserves_prior_state() -> None:
    parser = ClaudeStreamParser()
    feed_all(
        parser,
        [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "앞부분 "}]}}),
            "{ this is not valid json",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "뒷부분"}]}}),
        ],
    )
    assert parser.state.final_text == "앞부분 뒷부분"
    assert len(parser.state.unparsed_lines) == 1
    assert len(parser.state.parse_errors) == 1


def test_malformed_line_emits_warning_event() -> None:
    parser = ClaudeStreamParser()
    events = parser.feed("not json at all")
    assert events[0][0] == "parse_warning"


def test_interrupted_stream_keeps_partial_result() -> None:
    """최종 result 이벤트가 오기 전에 끊긴 경우."""
    parser = ClaudeStreamParser()
    feed_all(
        parser,
        [
            json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "중간까지"}]}}),
        ],
    )
    assert not parser.state.saw_result
    assert parser.state.final_text == "중간까지"


def test_result_event_wins_over_assistant_text() -> None:
    parser = ClaudeStreamParser()
    feed_all(
        parser,
        [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "초안"}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "확정본"}),
        ],
    )
    assert parser.state.final_text == "확정본"


def test_auth_failure_detected_despite_success_subtype() -> None:
    """실측으로 확인된 형태.

    subtype 은 success, terminal_reason 은 completed, 종료 코드도 정상인데
    is_error 만 true 이고 result 에 로그인 안내가 들어온다.
    """
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Not logged in · Please run /login",
                "terminal_reason": "completed",
                "permission_denials": [],
            }
        )
    )
    state = parser.state
    assert state.is_error
    assert state.subtype == "success"
    assert state.terminal_reason == "completed"
    assert state.auth_required
    assert not state.rate_limited


def test_rate_limit_detected() -> None:
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {"type": "result", "subtype": "error", "is_error": True, "result": "Usage limit reached"}
        )
    )
    assert parser.state.rate_limited


def test_permission_denials_captured() -> None:
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "본문",
                "permission_denials": [{"tool": "Bash"}],
            }
        )
    )
    assert parser.state.permission_denials == [{"tool": "Bash"}]


def test_tool_result_error_recorded() -> None:
    parser = ClaudeStreamParser()
    events = parser.feed(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": True, "content": "파일을 찾을 수 없습니다"}
                    ]
                },
            }
        )
    )
    assert events[0][0] == "tool_error"
    assert parser.state.tool_errors


def test_blank_and_unknown_lines_ignored() -> None:
    parser = ClaudeStreamParser()
    assert parser.feed("") == []
    assert parser.feed("   ") == []
    assert parser.feed(json.dumps({"type": "unknown_kind"})) == []
    assert parser.feed(json.dumps([1, 2, 3])) == []
    assert parser.state.parse_errors == []
