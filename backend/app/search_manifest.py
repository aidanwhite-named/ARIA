"""Single-agent search JSON and audit manifest. No ranking or reclassification."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CAPABILITY = "similarity_search_v1"
MANIFEST_VERSION = 14
GROUP_SCHEMA_VERSION = 3
GROUPS = WRITE_GROUPS = ("A", "B", "C")
GROUP_DEFINITIONS = {
    "A": "전체 구조와 핵심 특징이 모두 강하게 유사",
    "B": "전체 구조는 다르지만 핵심 특징 또는 핵심 관계가 강하게 유사",
    "C": "전체 구조는 유사하지만 핵심 대응은 부분적",
}
INPUT_KIND_QUERY, INPUT_KIND_URL = "query", "url"
SEARCH_TOOL_NAMES = frozenset(("WebSearch", "search_web", "web_search"))
FETCH_TOOL_NAMES = frozenset(("WebFetch", "read_url_content"))
OPEN, CLOSE = "[ARIA_SEARCH_LOG_V1]", "[/ARIA_SEARCH_LOG_V1]"
_MAX_OUTPUT_BYTES = 2_000_000
_BLOCK = re.compile(r"(?m)^[ \t]*\[ARIA_SEARCH_LOG_V1\][ \t]*\r?\n(.*?)^[ \t]*\[/ARIA_SEARCH_LOG_V1\][ \t]*$", re.S | re.M)

class SearchLogError(ValueError):
    pass

def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value

def normalize_url(raw) -> str:
    value = str(raw or "").strip()
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        if any(ord(ch) < 32 or ch.isspace() for ch in value):
            return ""
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return ""

def is_linkable_url(raw) -> bool:
    return bool(normalize_url(raw)) and not any(ch in str(raw) for ch in '<>"')

def identity_key(number: str = "", doi: str = "") -> str:
    """Exact publication identity: preserve country and kind code, never family."""
    if doi:
        text = str(doi).strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return "doi:" + text
    return "patent:" + re.sub(r"[\s/.-]", "", str(number).upper())

def parse_payload(text: str) -> dict:
    if len(text.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise SearchLogError("최종 JSON이 출력 크기 한도를 넘었습니다.")
    blocks = _BLOCK.findall(text.strip())
    if len(blocks) > 1:
        raise SearchLogError("검색 JSON 블록은 한 개여야 합니다.")
    source = blocks[0] if blocks else text.strip()
    if source.startswith("```"):
        source = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", source)
    try:
        value = json.loads(source, object_pairs_hook=_unique_object)
    except (ValueError, TypeError) as exc:
        raise SearchLogError("최종 검색 JSON을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
        raise SearchLogError("candidates 배열이 필요합니다.")
    return value

def parse(text: str, observed_section=None, **_context) -> tuple[dict, list[str]]:
    value = parse_payload(text)
    for key in ("term_expansions", "rounds", "access_failures"):
        if key in value and (not isinstance(value[key], list) or len(value[key]) > 200):
            raise SearchLogError(f"{key}는 최대 200개의 배열이어야 합니다.")
    if len(value["candidates"]) > 100:
        raise SearchLogError("최종 후보는 최대 100개입니다. 순위 절단 없이 출력 오류로 처리합니다.")
    result = {"candidates": [], "term_expansions": value.get("term_expansions", []),
              "rounds": value.get("rounds", []), "access_failures": value.get("access_failures", [])}
    for index, raw in enumerate(value["candidates"], 1):
        if not isinstance(raw, dict) or raw.get("group") not in (*GROUPS, None):
            raise SearchLogError(f"후보 {index}: group은 A/B/C/null이어야 합니다.")
        mapping = raw.get("mapping", [])
        if not isinstance(mapping, list) or any(not isinstance(row, dict) for row in mapping):
            raise SearchLogError(f"후보 {index}: mapping은 객체 배열이어야 합니다.")
        if len(mapping) > 100:
            raise SearchLogError(f"후보 {index}: 구성 대응표가 너무 큽니다.")
        candidate = {"index": index, "rank": index, "group": raw.get("group")}
        for key in ("doc_type", "doc_number", "doi", "title", "reported_title", "applicant",
                    "url", "family", "note", "publication_date", "verbatim_excerpt", "source_location"):
            item = raw.get(key, "")
            if item is not None and not isinstance(item, str):
                raise SearchLogError(f"후보 {index}: {key}는 문자열이어야 합니다.")
            candidate[key] = item or ""
        candidate["mapping"] = []
        for row in mapping:
            clean = {}
            for key in ("feature", "degree", "counterpart", "similar", "different", "support_text",
                        "support_source", "support_scope", "support_url", "verbatim_excerpt",
                        "translation", "source_location"):
                item = row.get(key, "")
                if item is not None and not isinstance(item, str):
                    raise SearchLogError(f"후보 {index}: mapping.{key}는 문자열이어야 합니다.")
                clean[key] = item or ""
            ref = row.get("evidence_ref")
            clean["evidence_ref"] = copy.deepcopy(ref) if isinstance(ref, dict) else None
            candidate["mapping"].append(clean)
        result["candidates"].append(candidate)
    return result, []

def strip_block(text: str) -> str:
    return _BLOCK.sub("", text).strip()

def has_retrieval_attempt(calls, tool_uses=None, journal=None) -> bool:
    """Capabilities checks alone do not constitute a literature search."""
    names = set(tool_uses or []) | {call.get("name") for call in (calls or [])}
    names.update("mcp__aria-search__" + str(row.get("tool", ""))
                 for row in (journal or []) if row.get("state") == "started")
    eligible = SEARCH_TOOL_NAMES | FETCH_TOOL_NAMES | {
        f"mcp__aria-search__{source}_{action}"
        for source in ("epo", "literature", "kiwee") for action in ("search", "fetch")
    }
    return bool(names & eligible)


def observed(calls, tool_uses=None, **_context) -> dict:
    calls = [copy.deepcopy(call) for call in (calls or []) if isinstance(call, dict)]
    queries, attempted, read, lookups = [], [], [], []
    counts, search_count = {}, 0
    for call in calls:
        name = str(call.get("name") or "")
        counts[name] = counts.get(name, 0) + 1
        args = call.get("input") or {}
        if not isinstance(args, dict):
            continue
        args = args.get("arguments", args)
        if not isinstance(args, dict):
            continue
        url = str(args.get("url") or "")
        if args.get("input_kind") == INPUT_KIND_URL:
            if url:
                lookups.append(url)
            continue
        if name in SEARCH_TOOL_NAMES or name.endswith("_search"):
            batch = args.get("queries")
            query = args.get("query")
            if isinstance(batch, list) and batch:
                queries.extend(q for q in batch if isinstance(q, str))
                search_count += 1
            elif query:
                queries.append(json.dumps(query, ensure_ascii=False) if isinstance(query, dict) else str(query))
                search_count += 1
        if url and name in FETCH_TOOL_NAMES:
            attempted.append(url)
            if call.get("ok") is True and (name != "read_url_content" or call.get("content_read") is True):
                read.append(url)
    return {
        "tool_calls": calls, "tool_call_counts": counts,
        "search_queries": list(dict.fromkeys(queries)), "search_call_count": search_count,
        "attempted_fetch_urls": list(dict.fromkeys(attempted)),
        "succeeded_fetch_urls": list(dict.fromkeys(read)),
        "url_lookup_attempts": list(dict.fromkeys(lookups)),
        "tool_failures": [call for call in calls if call.get("ok") is False],
        "unknown_tool_outcomes": [call for call in calls if call.get("ok") is None],
    }

def read_tool_journal(work_dir: Path) -> list[dict]:
    path = work_dir / "search_tool_calls.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except ValueError:
            records.append({"state": "incomplete", "error_code": "journal_record_incomplete"})
    return records

def build(*, claim_text, provider="", model="", prompt_id="", prompt_name="",
          prompt_sha256="", runtime_context_sha256="", spec_document=None,
          search_focus=None, started_at=None, completed_at=None, tool_calls=None,
          tool_uses=None, observed_section=None, tool_journal=None,
          tool_availability=None, reported=None, notes=None, error=None,
          date_filter=None, max_tool_calls_total=40, timeout_seconds=900, usage=None,
          raw_output="", search_depth="standard", claim_boundary_neutralized=False, spec_boundary_neutralized=False,
          focus_boundary_neutralized=False, template_mode="", strategy_boundary_neutralized=False,
          tool_policy_name="", allowed_tools=(), mcp_tools=(), advertised_tools_enforced=False) -> dict:
    try:
        llm_output = parse_payload(raw_output) if raw_output else None
    except SearchLogError:
        llm_output = None
    return {
        "version": MANIFEST_VERSION, "status": "incomplete" if error else "complete",
        "provider": provider, "model": model, "group_definitions": dict(GROUP_DEFINITIONS),
        "input": {"claim_text": claim_text, "spec_document": spec_document, "search_focus": search_focus,
                  "claim_boundary_neutralized": claim_boundary_neutralized,
                  "spec_boundary_neutralized": spec_boundary_neutralized,
                  "focus_boundary_neutralized": focus_boundary_neutralized},
        "prompt": {"id": prompt_id, "name": prompt_name, "sha256": prompt_sha256,
                   "runtime_context_sha256": runtime_context_sha256, "template_mode": template_mode,
                   "strategy_boundary_neutralized": strategy_boundary_neutralized},
        "policy": {"name": tool_policy_name, "allowed_tools": list(allowed_tools),
                   "mcp_tools": list(mcp_tools), "advertised_tools_enforced": advertised_tools_enforced},
        "started_at": started_at, "completed_at": completed_at,
        "limits": {"max_tool_calls": max_tool_calls_total, "timeout_seconds": timeout_seconds, "depth": search_depth},
        "tool_availability": tool_availability or {}, "tool_journal": tool_journal or [],
        "observed": observed_section or observed(tool_calls, tool_uses),
        "llm_output": llm_output, "reported": reported,
        "date_filter": date_filter or {}, "usage": usage,
        "normalization_notes": list(notes or []), "error": error,
    }
