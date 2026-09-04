"""Claude Code stream-json 증분 파서.

한 줄 파싱이 실패해도 절대 이전 상태를 버리지 않는다. 파싱 못 한 원문은
그대로 보관하고 다음 줄로 넘어간다. 중간에 끊긴 스트림에서도 그때까지
받은 결과 텍스트를 살려야 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 도구 입력에서 감사 기록으로 남길 필드. 전체 입력을 그대로 저장하지 않는다.
# WebFetch 의 prompt 는 길고, 남겨야 할 것은 "무엇을 검색했고 어디를 열었는가"다.
_TOOL_INPUT_KEYS = {
    "WebSearch": ("query", "allowed_domains", "blocked_domains"),
    "WebFetch": ("url",),
    "mcp__aria-search__search_capabilities": (),
    "mcp__aria-search__epo_search": ("query", "max_results"),
    "mcp__aria-search__epo_fetch": ("publication_number", "constituent"),
    "mcp__aria-search__kiwee_search": ("query", "max_results"),
    "mcp__aria-search__kiwee_fetch": ("publication_number", "constituent"),
    "mcp__aria-search__literature_search": ("query", "max_results"),
    "mcp__aria-search__literature_fetch": ("doi", "constituent"),
}
_MAX_INPUT_VALUE = 500
# 한 실행에서 남기는 도구 호출 기록 상한. 감사 기록이 DB 를 밀어내지 않게 한다.
_MAX_TOOL_CALLS = 500

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
    # 호출 단위 감사 기록. {id, name, ts, input, ok, error}
    # 검색 작업의 "실제 검색어"와 "접근 실패"가 여기서 나온다. 모델이 보고서에
    # 무엇이라고 쓰든, 실제로 무엇을 호출했는지는 이 목록이 근거다.
    tool_calls: list[dict] = field(default_factory=list)
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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summarize_input(name: str, raw) -> dict:
    """도구 입력에서 감사에 필요한 필드만 뽑는다.

    알려진 도구는 화이트리스트로 뽑고, 모르는 도구는 키 이름만 남긴다.
    허용 목록 밖의 도구가 호출되면 그 자체가 정책 위반이므로 인수 값까지
    기록에 옮겨 담을 이유가 없다(그 값이 곧 명령이나 경로일 수 있다).
    """
    if not isinstance(raw, dict):
        return {}
    keys = _TOOL_INPUT_KEYS.get(name)
    if keys is None:
        return {"keys": sorted(str(k) for k in raw)[:10]}
    summary: dict = {}
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, str):
            summary[key] = value[:_MAX_INPUT_VALUE]
        elif isinstance(value, (int, float, bool)):
            summary[key] = value
        elif isinstance(value, dict):
            summary[key] = json.dumps(value, ensure_ascii=False)[:_MAX_INPUT_VALUE]
        elif isinstance(value, list):
            summary[key] = [str(item)[:_MAX_INPUT_VALUE] for item in value[:20]]
    return summary


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
                call_id = block.get("id")
                self.state.tool_uses.append(name)
                summary = _summarize_input(name, block.get("input"))
                if len(self.state.tool_calls) < _MAX_TOOL_CALLS:
                    self.state.tool_calls.append(
                        {
                            "id": call_id,
                            "name": name,
                            "ts": _utcnow_iso(),
                            "input": summary,
                            "ok": None,
                            "error": None,
                        }
                    )
                events.append(
                    (
                        "tool_use",
                        {
                            "name": name,
                            "id": call_id,
                            "input": summary,
                            "index": len(self.state.tool_uses),
                        },
                    )
                )
        return events

    def _on_user(self, payload: dict) -> list[tuple[str, dict]]:
        """도구 결과.

        분석 작업은 도구를 끄고 실행하므로 보통 오지 않는다. 검색 작업에서는
        호출마다 하나씩 오며, 성공 여부를 호출 기록에 되짚어 적는다. 접근 실패
        (유료 논문, 403, 리다이렉트)를 남기는 것이 이 경로의 목적이다.

        결과 본문은 저장하지 않는다. 검색 결과 페이지 내용은 비신뢰 외부
        데이터이고, ARIA 의 이벤트 DB 에 옮겨 담을 이유가 없다.
        """
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        events: list[tuple[str, dict]] = []
        by_id = {call["id"]: call for call in self.state.tool_calls if call.get("id")}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call = by_id.get(block.get("tool_use_id"))
            if block.get("is_error"):
                detail = str(block.get("content"))[:300]
                self.state.tool_errors.append(detail)
                if call is not None:
                    call["ok"] = False
                    call["error"] = detail
                events.append(("tool_error", {"detail": detail}))
            elif call is not None:
                call["ok"] = True
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
