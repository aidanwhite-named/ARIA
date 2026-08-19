"""Claude Code stream-json 증분 파서.

한 줄 파싱이 실패해도 절대 이전 상태를 버리지 않는다. 파싱 못 한 원문은
그대로 보관하고 다음 줄로 넘어간다. 중간에 끊긴 스트림에서도 그때까지
받은 결과 텍스트를 살려야 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 인증/사용량 실패는 exit code 로 드러나지 않고 result 텍스트로만 온다.
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "oauth token has expired",
)
_RATE_MARKERS = (
    "rate limit",
    "rate_limit_error",
    "usage limit reached",
    "too many requests",
    "quota exceeded",
)


@dataclass
class ClaudeStreamState:
    assistant_text: list[str] = field(default_factory=list)
    result_text: str | None = None
    saw_result: bool = False
    is_error: bool = False
    subtype: str | None = None
    terminal_reason: str | None = None
    permission_denials: list = field(default_factory=list)
    usage: dict | None = None
    session_id: str | None = None
    model: str | None = None
    tool_names: list[str] = field(default_factory=list)
    tool_uses: list[str] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    stream_deltas: list[str] = field(default_factory=list)

    @property
    def final_text(self) -> str:
        """최종 결과 텍스트.

        우선순위: result 이벤트 → assistant 메시지 누적 → 스트림 델타 누적.
        스트림이 중간에 끊겨도 받은 만큼은 살린다.
        """
        if self.result_text:
            return self.result_text
        joined = "".join(self.assistant_text).strip()
        if joined:
            return joined
        return "".join(self.stream_deltas).strip()

    @property
    def auth_required(self) -> bool:
        haystack = " ".join(
            filter(None, [self.result_text or "", *self.parse_errors, self.subtype or ""])
        ).lower()
        return any(marker in haystack for marker in _AUTH_MARKERS)

    @property
    def rate_limited(self) -> bool:
        haystack = " ".join(
            filter(None, [self.result_text or "", *self.parse_errors, self.subtype or ""])
        ).lower()
        return any(marker in haystack for marker in _RATE_MARKERS)


class ClaudeStreamParser:
    """줄 단위로 먹이면 UI 로 내보낼 이벤트를 돌려준다."""

    def __init__(self) -> None:
        self.state = ClaudeStreamState()

    def feed(self, line: str) -> list[tuple[str, dict]]:
        line = line.strip()
        if not line:
            return []

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            # 원문 보존. 이 줄 하나 때문에 그때까지 받은 결과를 버리지 않는다.
            self.state.unparsed_lines.append(line)
            self.state.parse_errors.append(f"JSON 파싱 실패: {exc.msg}")
            return [("parse_warning", {"line": line[:500], "error": exc.msg})]

        if not isinstance(payload, dict):
            self.state.unparsed_lines.append(line)
            return []

        handler = {
            "system": self._on_system,
            "assistant": self._on_assistant,
            "user": self._on_user,
            "stream_event": self._on_stream_event,
            "result": self._on_result,
        }.get(payload.get("type", ""))

        if handler is None:
            return []
        return handler(payload)

    def _on_system(self, payload: dict) -> list[tuple[str, dict]]:
        if payload.get("subtype") == "init":
            self.state.session_id = payload.get("session_id")
            self.state.model = payload.get("model")
            tools = payload.get("tools")
            if isinstance(tools, list):
                self.state.tool_names = [str(t) for t in tools]
            return [
                (
                    "provider_start",
                    {
                        "model": self.state.model,
                        "tools": self.state.tool_names,
                        "message": "Claude 세션 시작",
                    },
                )
            ]
        return []

    def _on_assistant(self, payload: dict) -> list[tuple[str, dict]]:
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        events: list[tuple[str, dict]] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text") or ""
                if text:
                    self.state.assistant_text.append(text)
            elif kind == "tool_use":
                name = str(block.get("name") or "unknown")
                self.state.tool_uses.append(name)
                events.append(
                    ("tool_use", {"name": name, "id": block.get("id")})
                )
        return events

    def _on_user(self, payload: dict) -> list[tuple[str, dict]]:
        """도구 결과. v0.1 은 도구를 끄고 실행하므로 보통 오지 않는다."""
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        events: list[tuple[str, dict]] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                detail = str(block.get("content"))[:300]
                self.state.tool_errors.append(detail)
                events.append(("tool_error", {"detail": detail}))
        return events

    def _on_stream_event(self, payload: dict) -> list[tuple[str, dict]]:
        event = payload.get("event")
        if not isinstance(event, dict):
            return []
        if event.get("type") != "content_block_delta":
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        text = delta.get("text")
        if not text:
            return []
        self.state.stream_deltas.append(text)
        return [("result_stream", {"delta": text})]

    def _on_result(self, payload: dict) -> list[tuple[str, dict]]:
        state = self.state
        state.saw_result = True
        state.is_error = bool(payload.get("is_error"))
        state.subtype = payload.get("subtype")
        state.terminal_reason = payload.get("terminal_reason")
        result = payload.get("result")
        if isinstance(result, str):
            state.result_text = result
        denials = payload.get("permission_denials")
        if isinstance(denials, list):
            state.permission_denials = denials
        usage = payload.get("usage")
        if isinstance(usage, dict):
            state.usage = usage
        return [
            (
                "provider_done",
                {
                    "is_error": state.is_error,
                    "subtype": state.subtype,
                    "terminal_reason": state.terminal_reason,
                },
            )
        ]
