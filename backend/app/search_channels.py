"""Tool availability, not independent channel execution or candidate policy."""
from __future__ import annotations
from .patent_search import describe

STATUS_LABELS = {
    "available": "사용 가능", "disabled": "연동 꺼짐",
    "not_configured": "인증 미설정", "not_implemented": "접속 미구현",
    "unsupported_transport": "이 Provider의 실행별 MCP 연결 미지원",
}

def availability(values: dict, provider: str = "claude") -> dict:
    result = {"web": {"status": "available", "detail": "Provider 기본 웹 도구"}}
    for name in ("epo", "kiwee", "literature"):
        status = describe(values, name)
        code = "available"
        if not status.enabled:
            code = "disabled"
        elif name == "kiwee":
            code = "not_implemented"
        elif not status.configured:
            code = "not_configured"
        elif provider not in ("claude", "codex"):
            code = "unsupported_transport"
        result[name] = {"status": code, "detail": STATUS_LABELS[code]}
    return result

def available_mcp_names(statuses: dict) -> tuple[str, ...]:
    names = ["mcp__aria-search__search_capabilities"]
    for name in ("epo", "kiwee", "literature"):
        if statuses.get(name, {}).get("status") == "available":
            names += [f"mcp__aria-search__{name}_search", f"mcp__aria-search__{name}_fetch"]
    return tuple(names)

def cell(value) -> str:
    import html
    text = html.escape(str(value or ""), quote=True)
    for char in ("\\", "|", "*", "_", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text.replace("\r", " ").replace("\n", " ")

DEPTH_LIMITS = {"quick": (15, 300), "standard": (40, 900), "deep": (80, 1800)}
def execution_limits(values: dict, depth: str = "standard") -> tuple[int, int]:
    """Presets only bound total calls/time, never channels or candidates."""
    calls, seconds = DEPTH_LIMITS.get(depth, DEPTH_LIMITS["standard"])
    return (min(calls, max(1, int(values.get("max_search_tool_calls", 40)))),
            min(seconds, max(1, int(values.get("default_timeout_seconds", 900)))))
