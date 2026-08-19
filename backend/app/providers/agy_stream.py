"""agy(Gemini) CLI 의 stream-json 파서.

Claude 와 봉투 구조가 다르다. Claude 는 {"type": ...}, agy 는
{"event": "<이름>", "<이름>": {...}} 형태다.

agy 1.1.14 에서 실측한 이벤트:

  {"event":"init","conversation_id":"..","init":{
      "cwd":"..","tools":[...57개...],"permission_mode":"request-review"}}

  {"event":"step_update","step_update":{
      "conversation_id":"..","step_index":0,"state":"DONE",
      "step_type":"user_input"}}

  {"event":"step_update","step_update":{
      ...,"step_type":"agent_response","text_delta":"본문",
      "duration_seconds":1.58,"usage":{...}}}

  {"event":"result","result":{
      "conversation_id":"..","status":"SUCCESS","response":"본문",
      "duration_seconds":5.39,"num_turns":1,"usage":{
        "input_tokens":13740,"output_tokens":39,"thinking_tokens":32,
        "cache_read_tokens":0,"total_tokens":13779}}}

한 줄 파싱이 실패해도 이전 상태를 버리지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 정상 진행 단계. 이 외의 step_type 은 기록해 두고, 도구성으로 보이면
# tool_uses 에 넣는다.
_BENIGN_STEPS = frozenset(
    {"user_input", "checkpoint", "agent_response", "thinking", "finish", "done"}
)

# 실측으로 확인한 도구 단계 이름. agy 1.1.15 에 파일 쓰기와 셸 명령을
# 요청했을 때 step_type 이 정확히 "tool" 로 왔다.
_KNOWN_TOOL_STEPS = frozenset({"tool", "tool_call", "tool_use", "command"})

# 아직 관찰하지 못한 이름을 놓치지 않기 위한 보조 패턴.
_TOOL_HINTS = ("tool", "command", "action", "browser", "shell", "edit", "write")

_AUTH_MARKERS = (
    "not logged in",
    "unauthenticated",
    "unauthorized",
    "authentication",
    "please log in",
    "login required",
    "invalid credentials",
)
_RATE_MARKERS = (
    "rate limit",
    "quota exceeded",
    "resource_exhausted",
    "too many requests",
    "usage limit",
)


@dataclass
class AgyStreamState:
    conversation_id: str | None = None
    cwd: str | None = None
    tools_advertised: list[str] = field(default_factory=list)
    permission_mode: str | None = None

    response_text: str | None = None
    deltas: list[str] = field(default_factory=list)
    status: str | None = None
    error_message: str | None = None
    usage: dict | None = None
    num_turns: int | None = None
    saw_result: bool = False

    step_types: list[str] = field(default_factory=list)
    tool_uses: list[str] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    @property
    def final_text(self) -> str:
        if self.response_text:
            return self.response_text
        return "".join(self.deltas).strip()

    @property
    def is_error(self) -> bool:
        if self.status is None:
            return False
        return self.status.upper() != "SUCCESS"

    def _haystack(self) -> str:
        return " ".join(filter(None, [self.error_message or "", self.response_text or ""])).lower()

    @property
    def auth_required(self) -> bool:
        return any(marker in self._haystack() for marker in _AUTH_MARKERS)

    @property
    def rate_limited(self) -> bool:
        return any(marker in self._haystack() for marker in _RATE_MARKERS)


class AgyStreamParser:
    def __init__(self) -> None:
        self.state = AgyStreamState()

    def feed(self, line: str) -> list[tuple[str, dict]]:
        line = line.strip()
        if not line:
            return []

        # agy 는 경고를 평문으로 stdout 에 섞어 내보낸다.
        if not line.startswith("{"):
            self.state.unparsed_lines.append(line)
            return [("stderr", {"line": line[:500]})]

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            self.state.unparsed_lines.append(line)
            self.state.parse_errors.append(f"JSON 파싱 실패: {exc.msg}")
            return [("parse_warning", {"line": line[:500], "error": exc.msg})]

        if not isinstance(payload, dict):
            self.state.unparsed_lines.append(line)
            return []

        name = payload.get("event")
        body = payload.get(name) if isinstance(name, str) else None
        if not isinstance(body, dict):
            body = {}

        if name == "init":
            return self._on_init(payload, body)
        if name == "step_update":
            return self._on_step(body)
        if name == "result":
            return self._on_result(body)
        return []

    def _on_init(self, payload: dict, body: dict) -> list[tuple[str, dict]]:
        state = self.state
        state.conversation_id = payload.get("conversation_id")
        state.cwd = body.get("cwd")
        state.permission_mode = body.get("permission_mode")
        tools = body.get("tools")
        if isinstance(tools, list):
            state.tools_advertised = [str(t) for t in tools]
        return [
            (
                "provider_start",
                {
                    "message": "agy 세션 시작",
                    "tools": len(state.tools_advertised),
                    "permission_mode": state.permission_mode,
                },
            )
        ]

    def _on_step(self, body: dict) -> list[tuple[str, dict]]:
        state = self.state
        step_type = str(body.get("step_type") or "")
        if step_type:
            state.step_types.append(step_type)

        events: list[tuple[str, dict]] = []
        lowered = step_type.lower()
        if lowered and lowered not in _BENIGN_STEPS:
            if lowered in _KNOWN_TOOL_STEPS or any(
                hint in lowered for hint in _TOOL_HINTS
            ):
                state.tool_uses.append(step_type)
                events.append(("tool_use", {"name": step_type}))
            else:
                events.append(("stage", {"stage": step_type, "message": step_type}))

        delta = body.get("text_delta")
        if isinstance(delta, str) and delta:
            state.deltas.append(delta)
            events.append(("result_stream", {"delta": delta}))

        usage = body.get("usage")
        if isinstance(usage, dict):
            state.usage = usage

        return events

    def _on_result(self, body: dict) -> list[tuple[str, dict]]:
        state = self.state
        state.saw_result = True
        status = body.get("status")
        state.status = str(status) if status is not None else None
        response = body.get("response")
        if isinstance(response, str) and response:
            state.response_text = response
        error = body.get("error")
        if isinstance(error, str) and error:
            state.error_message = error
        usage = body.get("usage")
        if isinstance(usage, dict):
            state.usage = usage
        turns = body.get("num_turns")
        if isinstance(turns, int):
            state.num_turns = turns
        return [
            (
                "provider_done",
                {"status": state.status, "is_error": state.is_error},
            )
        ]


def build_stdin_message(text: str) -> str:
    """agy 의 stream-json 입력 한 줄을 만든다.

    실측으로 확인한 형태. message 는 user 안이 아니라 최상위에 있어야 한다.
    Windows 의 명령행 길이 제한(32,767자) 때문에 -p 인수로는 긴 프롬프트를
    넘길 수 없어서 stdin 을 쓴다.
    """
    payload = {
        "event": "user",
        "message": {"role": "user", "content": text},
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"
