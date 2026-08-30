"""Codex CLI 의 `exec --json` JSONL 파서.

Claude 는 {"type": ...}, agy 는 {"event": "<이름>", "<이름>": {...}} 인데
Codex 는 또 다르다. 봉투는 {"type": "<이름>", ...} 이고 항목 이벤트만
{"type": "item.completed", "item": {"type": "<항목종류>", ...}} 로 한 겹 더
들어간다. 그래서 "무슨 일이 일어났는가"는 두 자리를 함께 봐야 안다.

codex-cli 0.149.0 에서 실측한 이벤트:

  {"type":"thread.started","thread_id":"01a0..."}
  {"type":"turn.started"}
  {"type":"item.completed","item":{"id":"item_0","type":"agent_message",
      "text":"OK"}}
  {"type":"turn.completed","usage":{"input_tokens":14319,
      "cached_input_tokens":9984,"cache_write_input_tokens":0,
      "output_tokens":5,"reasoning_output_tokens":0}}

항목 종류는 설치된 실행 파일에서 직접 확인했다. 봉투 이름과 항목 이름이
한 곳에 연속으로 들어 있다.

  봉투 : thread.started turn.started turn.completed turn.failed
         item.started item.updated item.completed
  항목 : agent_message reasoning command_execution file_change
         mcp_tool_call collab_tool_call web_search todo_list

이 중 뒤의 다섯이 도구다. Codex 는 도구를 끄는 플래그가 없으므로 ARIA 는
이것들을 사후에 탐지할 뿐 차단하지 못한다. 그래서 이름을 추측하지 않고
실행 파일에서 확인한 목록을 그대로 쓰고, 목록에 없는 처음 보는 항목 종류도
버리지 않고 기록한다 — 다음 버전에서 도구가 하나 늘었을 때 조용히 통과하는
것이 가장 나쁘다.

최종 본문은 이 스트림에서 만들지 않는다. 한 번의 실행에서 agent_message 가
여러 번 나올 수 있고("이제 명령을 실행하겠습니다" 같은 중간 발화가 섞인다),
그것들을 이어 붙이면 보고서가 아니라 대화록이 된다. 최종 본문은
`--output-last-message` 파일에서 읽고, 여기 누적분은 그 파일을 읽지 못했을
때의 폴백으로만 쓴다.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime

# 실행 파일에서 확인한 도구 항목 종류.
TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "collab_tool_call",
        "web_search",
    }
)

# 도구가 아닌 정상 항목 종류. 이 둘도 아니고 도구도 아니면 '처음 보는 항목'
# 으로 남긴다.
_BENIGN_ITEM_TYPES = frozenset({"agent_message", "reasoning", "todo_list"})

# 처음 보는 항목 종류가 도구인지 판단하는 보조 패턴. 확정이 아니라 경보다.
_TOOL_HINTS = ("tool", "command", "exec", "shell", "file", "patch", "search", "browser")

# 완료 이벤트의 status. 알려진 성공값만 성공으로 본다 — 실패 목록에 없다는
# 이유로 성공 처리하면 in_progress/incomplete 가 성공이 된다(두 값 모두 실행
# 파일에 실재한다). 모르는 status 는 모른다고 남긴다.
_SUCCESS_STATUSES = frozenset({"completed", "succeeded", "success", "ok", "done"})
_FAILURE_STATUSES = frozenset({"failed", "error", "cancelled"})

_AUTH_MARKERS = (
    "not logged in",
    "login required",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "invalid credentials",
    "please run `codex login`",
)
_RATE_MARKERS = (
    "rate limit",
    "rate_limit_reached",
    "usage limit",
    "usage_limit_reached",
    "quota",
    "credits_depleted",
    "too many requests",
)

# 항목 종류별로 감사 기록에 남길 인수. 검색 작업의 "실제 검색어"는 모델의
# 자기 보고가 아니라 여기서 온다.
# web_search 는 여기 없다. 도구 하나가 검색과 URL 조회를 겸해서 평평한 키
# 목록으로는 종류를 구분할 수 없다. _web_search_input 이 따로 처리한다.
_TOOL_INPUT_KEYS = {
    "command_execution": ("command",),
    "file_change": ("path",),
    "mcp_tool_call": ("server", "tool"),
}
_MAX_INPUT_VALUE = 500


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


INPUT_KIND_QUERY = "query"
INPUT_KIND_URL = "url"

_URL_PREFIXES = ("http://", "https://")


def _looks_like_url(value: str) -> bool:
    """값 **전체**가 공백 없는 http(s) URL 일 때만 참.

    startswith 만으로는 "https://patents.google.com radar EO IR" 같은 검색어가
    URL 조회로 잡힌다. 모델이 검색어에 도메인을 섞는 것은 흔하고, 그렇게 되면
    그 호출이 실제 검색어 목록에서 빠지고 URL 예산까지 먹는다.
    """
    text = value.strip()
    if not text or any(char.isspace() for char in text):
        return False
    if not text.lower().startswith(_URL_PREFIXES):
        return False
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _web_search_input(item: dict) -> dict:
    """web_search 는 도구 하나가 검색과 URL 조회를 겸한다.

    둘을 가르는 신호는 query 필드뿐이다. 검색어면 검색, URL 이면 URL 조회다.
    action.type 은 쓰지 않는다 — codex-cli 0.149.0 실측에서 검색이든 URL
    조회든, 열린 URL 이든 실패한 URL 이든 전부 "other" 로 왔다. 대신 보고된
    값을 reported_action 에 남긴다. 다음 버전에서 실제 open_page 가 오기
    시작하면 그 사실이 기록에 있어야 하고, 우리가 "other" 를 "open_page" 로
    고쳐 쓰면 그 순간을 영영 알 수 없다.

    reported_action 은 **원본 그대로가 아니라 감사용 요약**이다. 중첩된 값은
    버리고 스칼라만 문자열로 바꿔 길이를 자른다. 값을 재분류하지 않는다는
    뜻이지, 바이트 단위로 보존한다는 뜻이 아니다.

    **성공 여부는 여기서 판정하지 않는다.** 완료 이벤트에는 status 도 error 도
    results 도 sources 도 오지 않는다(2026-08-30 실측: 열린 URL 3건과 실패한
    URL 3건의 이벤트가 필드 단위로 완전히 동일했다). 이 함수가 말할 수 있는
    것은 "무엇을 시도했는가" 까지다.
    """
    raw_action = item.get("action")
    action = raw_action if isinstance(raw_action, dict) else {}
    summary: dict = {}
    if action:
        summary["reported_action"] = {
            str(key)[:60]: str(value)[:_MAX_INPUT_VALUE]
            for key, value in list(action.items())[:10]
            if not isinstance(value, (dict, list))
        }
        queries = action.get("queries")
        if isinstance(queries, list):
            picked = [
                str(part).strip()[:_MAX_INPUT_VALUE]
                for part in queries[:20]
                if str(part).strip()
            ]
            if picked:
                summary["queries"] = picked
    raw_query = item.get("query")
    query = raw_query.strip() if isinstance(raw_query, str) else ""
    if not query:
        # 시작 이벤트에는 query 가 빈 문자열로 온다. 종류를 정하지 않는다.
        if summary.get("queries"):
            summary["input_kind"] = INPUT_KIND_QUERY
        return summary
    if _looks_like_url(query):
        summary["input_kind"] = INPUT_KIND_URL
        summary["url"] = query[:_MAX_INPUT_VALUE]
        return summary
    summary["input_kind"] = INPUT_KIND_QUERY
    summary["query"] = query[:_MAX_INPUT_VALUE]
    return summary


def split_call_kinds(calls) -> tuple[int, int]:
    """완료된 호출을 (검색 시도, URL 조회 시도) 로 나눈다.

    종류는 완료 이벤트에서만 정해진다. 시작 이벤트에는 query 가 비어 있어
    아직 어느 쪽도 아니며, 그래서 이 둘만으로는 폭주를 막을 수 없다 —
    시작 기준 전체 hard cap 이 따로 있어야 한다.
    """
    searches = lookups = 0
    for call in calls or ():
        data = call.get("input")
        if not isinstance(data, dict):
            continue
        kind = data.get("input_kind")
        if kind == INPUT_KIND_URL:
            lookups += 1
        elif kind == INPUT_KIND_QUERY:
            searches += 1
    return searches, lookups


def _summarize_input(item_type: str, item: dict) -> dict:
    """감사에 필요한 필드만 뽑는다. 도구 출력 원문은 남기지 않는다."""
    if item_type == "web_search":
        return _web_search_input(item)
    keys = _TOOL_INPUT_KEYS.get(item_type)
    if keys is None:
        return {"keys": sorted(str(key) for key in item if key not in ("id", "type"))[:10]}
    summary: dict = {}
    casefolded = {str(key).casefold(): value for key, value in item.items()}
    for key in keys:
        value = casefolded.get(key.casefold())
        if isinstance(value, str) and value:
            summary[key] = value[:_MAX_INPUT_VALUE]
        elif isinstance(value, list):
            summary[key] = " ".join(str(part) for part in value)[:_MAX_INPUT_VALUE]
    return summary


@dataclass
class CodexStreamState:
    thread_id: str | None = None
    messages: list[str] = field(default_factory=list)
    usage: dict | None = None
    status: str | None = None
    error_message: str | None = None

    tool_uses: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    # item.started 와 item.completed 가 같은 id 로 두 번 오므로 호출 하나로
    # 합친다. 합치지 않으면 검색 횟수와 상한이 두 배로 계산된다.
    tool_calls_by_id: dict[str, dict] = field(default_factory=dict, repr=False)

    item_types: list[str] = field(default_factory=list)
    unknown_item_types: list[str] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    saw_turn_end: bool = False

    @property
    def fallback_text(self) -> str:
        """출력 파일을 읽지 못했을 때만 쓰는 본문."""
        return "\n\n".join(part for part in self.messages if part.strip()).strip()

    @property
    def is_error(self) -> bool:
        return self.status is not None and self.status != "completed"

    def _haystack(self) -> str:
        return " ".join(
            filter(None, [self.error_message or "", " ".join(self.messages)])
        ).lower()

    @property
    def auth_required(self) -> bool:
        return any(marker in self._haystack() for marker in _AUTH_MARKERS)

    @property
    def rate_limited(self) -> bool:
        return any(marker in self._haystack() for marker in _RATE_MARKERS)


class CodexStreamParser:
    def __init__(self) -> None:
        self.state = CodexStreamState()

    def feed(self, line: str) -> list[tuple[str, dict]]:
        line = line.strip()
        if not line:
            return []

        # Codex 는 tracing 로그를 평문으로 섞어 내보낸다(도구가 정책에 막히면
        # `ERROR codex_core::tools::router: ...` 가 그렇게 나온다). JSON 이
        # 아닌 줄은 버리지 않고 그대로 흘려 보낸다.
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

        name = payload.get("type")
        if name == "thread.started":
            return self._on_thread_started(payload)
        if name == "turn.started":
            return [("stage", {"stage": "turn", "message": "Codex 응답 생성 중"})]
        if name in ("item.started", "item.updated", "item.completed"):
            return self._on_item(name, payload)
        if name == "turn.completed":
            return self._on_turn_completed(payload)
        if name == "turn.failed":
            return self._on_turn_failed(payload)
        if name == "error":
            return self._on_error(payload)
        return []

    def _on_thread_started(self, payload: dict) -> list[tuple[str, dict]]:
        self.state.thread_id = payload.get("thread_id")
        return [
            (
                "provider_start",
                {"message": "Codex 세션 시작", "thread_id": self.state.thread_id},
            )
        ]

    def _on_item(self, envelope: str, payload: dict) -> list[tuple[str, dict]]:
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "")
        if not item_type:
            return []

        if envelope == "item.completed":
            self.state.item_types.append(item_type)

        if item_type in TOOL_ITEM_TYPES:
            return self._on_tool_item(envelope, item, item_type, suspected=False)
        if item_type in _BENIGN_ITEM_TYPES:
            return self._on_benign_item(envelope, item, item_type)

        # 처음 보는 항목 종류. 도구일 가능성이 있으면 도구로 다뤄서 정책 판정에
        # 태운다. 놓치는 쪽보다 과탐지가 낫다.
        if item_type not in self.state.unknown_item_types:
            self.state.unknown_item_types.append(item_type)
        lowered = item_type.lower()
        if any(hint in lowered for hint in _TOOL_HINTS):
            return self._on_tool_item(envelope, item, item_type, suspected=True)
        return [("stage", {"stage": item_type, "message": item_type})]

    def _on_benign_item(
        self, envelope: str, item: dict, item_type: str
    ) -> list[tuple[str, dict]]:
        if item_type != "agent_message":
            return [("stage", {"stage": item_type, "message": item_type})]
        if envelope != "item.completed":
            return []
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return []
        self.state.messages.append(text)
        return [("result_stream", {"delta": text})]

    def _on_tool_item(
        self, envelope: str, item: dict, item_type: str, *, suspected: bool
    ) -> list[tuple[str, dict]]:
        state = self.state
        call_id = str(item.get("id") or f"auto-{len(state.tool_calls)}")
        call = state.tool_calls_by_id.get(call_id)
        events: list[tuple[str, dict]] = []

        if call is None:
            call = {
                "id": call_id,
                "name": item_type,
                "ts": _utcnow_iso(),
                "input": _summarize_input(item_type, item),
                "ok": None,
                "error": None,
            }
            state.tool_calls_by_id[call_id] = call
            state.tool_calls.append(call)
            state.tool_uses.append(item_type)
            events.append(
                (
                    "tool_use",
                    {
                        # 시작 시점에는 종류를 모른다. web_search 는 query 가 빈
                        # 문자열로 와서 검색인지 URL 조회인지 가릴 수 없다. 진행
                        # 표시가 도구 이름만 보고 검색으로 세지 않도록 명시한다.
                        "kind_pending": item_type == "web_search",
                        "name": item_type,
                        "id": call_id,
                        "input": call["input"],
                        "index": len(state.tool_uses),
                        "suspected": suspected,
                    },
                )
            )
        else:
            # 완료 이벤트의 인수로 덮어쓴다. 인수가 started 에 없고 completed
            # 에만 실리기도 하지만, 그보다 나쁜 경우가 있다 — web_search 는
            # started 에 action 만 있고 query 가 빈 문자열이다. "비어 있지
            # 않다"는 이유로 갱신을 건너뛰면 그 호출은 검색인지 URL 조회인지
            # 영원히 미상으로 남는다.
            fresh = _summarize_input(item_type, item)
            if fresh and (envelope == "item.completed" or not call["input"]):
                call["input"] = fresh

        if envelope == "item.completed":
            status = str(item.get("status") or "").lower()
            failed = status in _FAILURE_STATUSES or bool(item.get("error"))
            if failed:
                detail = str(item.get("error") or status or "실패")[:300]
                call["ok"] = False
                call["error"] = detail
                events.append(("tool_error", {"detail": detail, "name": item_type}))
            elif status in _SUCCESS_STATUSES:
                # 알려진 성공값을 실제로 보고한 항목만 성공으로 확정한다.
                call["ok"] = True
            else:
                # 구조화된 성공 신호가 없다. item.completed 는 "호출이 끝났다"는
                # 뜻이지 "성공했다"는 뜻이 아니다. 2026-08-30 실측에서 열린 URL
                # 3건과 실패한 URL 3건의 완료 이벤트가 필드 단위로 완전히
                # 동일했다 — status/error/results/sources 어느 것도 오지 않는다.
                # 여기서 True 를 주면 "확인된 실패가 없다"가 "성공했다"로
                # 승격되고, 그 거짓이 그대로 증거 등급이 된다.
                call["ok"] = None
            # 종류가 확정된 시점의 인수를 다시 알린다. 시작 이벤트만 보고 세는
            # 진행 표시는 URL 조회를 "검색어 없는 검색"으로 찍는다 — 최종 감사만
            # 고치면 사용자가 실행 중에 보는 숫자는 계속 틀린다.
            events.append(
                (
                    "tool_use_resolved",
                    {
                        "name": item_type,
                        "id": call_id,
                        "input": call["input"],
                        "ok": call["ok"],
                    },
                )
            )

        return events

    def _on_turn_completed(self, payload: dict) -> list[tuple[str, dict]]:
        state = self.state
        state.saw_turn_end = True
        state.status = "completed"
        usage = payload.get("usage")
        if isinstance(usage, dict):
            state.usage = usage
        return [("provider_done", {"status": "completed", "is_error": False})]

    def _on_turn_failed(self, payload: dict) -> list[tuple[str, dict]]:
        state = self.state
        state.saw_turn_end = True
        state.status = "failed"
        error = payload.get("error")
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("type") or "")
        elif isinstance(error, str):
            message = error
        state.error_message = message[:500] or "Codex 실행이 실패했습니다."
        return [("provider_done", {"status": "failed", "is_error": True})]

    def _on_error(self, payload: dict) -> list[tuple[str, dict]]:
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = "Codex 가 오류를 보고했습니다."
        self.state.error_message = message[:500]
        self.state.status = "failed"
        return [("provider_error", {"message": message[:500]})]
