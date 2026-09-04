"""Read-only search tools. No candidate selection, ranking or classification."""
from __future__ import annotations
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from . import search_dates, search_channels, search_manifest, settings_service
from .config import PATHS
from .db import session_scope
from .patent_search import get_backend, epo_cql
from .patent_search.base import PatentSearchError, PatentSearchQuery
from .patent_search.epo_client import scrub, credential_tokens

SERVER_NAME = "aria-search"
PROTOCOL_VERSION = "2025-06-18"
LEDGER_NAME = "search_tool_calls.jsonl"

@contextmanager
def _quota_lock():
    """Serialize OPS calls across MCP processes before syncing persistent quota."""
    path = PATHS.data_dir / "epo-mcp.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)

class ToolLimitExceeded(RuntimeError):
    pass

class SearchTools:
    def __init__(self, *, values=None, work_dir=None, max_calls=None, cutoff=None):
        self.work_dir = Path(work_dir or os.environ["ARIA_SEARCH_WORK_DIR"])
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.work_dir / LEDGER_NAME
        self.max_calls = max(1, int(max_calls or os.environ.get("ARIA_SEARCH_MAX_TOOL_CALLS", 40)))
        self.calls = sum(row.get("state") == "started" for row in search_manifest.read_tool_journal(self.work_dir))
        self.cutoff = search_dates.normalize_cutoff(cutoff if cutoff is not None else os.environ.get("ARIA_SEARCH_CUTOFF", ""))
        if values is None:
            with session_scope() as session:
                values = settings_service.get_all(session)
        self.values = values
        self.backends = {}
        self.secrets = credential_tokens(str(values.get("epo_consumer_key") or ""), str(values.get("epo_consumer_secret") or ""))

    def _record(self, row: dict):
        row = {**row, "timestamp": datetime.now(timezone.utc).isoformat()}
        serialized = scrub(json.dumps(row, ensure_ascii=False), *self.secrets)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def statuses(self):
        return search_channels.availability(self.values)

    def tool_definitions(self):
        statuses = self.statuses()
        return [_CAPABILITIES] + [
            tool for tool in (_EPO_SEARCH, _EPO_FETCH, _LITERATURE_SEARCH, _LITERATURE_FETCH, _KIWEE_SEARCH, _KIWEE_FETCH)
            if statuses[tool["name"].split("_")[0]]["status"] == "available"
        ]

    def call(self, name: str, arguments: dict) -> dict:
        row = {"id": str(uuid.uuid4()), "tool": name, "arguments": arguments, "sequence": self.calls}
        if self.calls >= self.max_calls:
            self._record({**row, "state": "rejected", "ok": False, "error_code": "tool_call_limit_exceeded"})
            raise ToolLimitExceeded("tool_call_limit_exceeded")
        self.calls += 1
        row["sequence"] = self.calls
        self._record({**row, "state": "started"})
        try:
            definition = next((tool for tool in self.tool_definitions() if tool["name"] == name), None)
            if definition is None:
                raise ValueError("tool_unavailable")
            _validate(arguments, definition["inputSchema"])
            if name == "search_capabilities":
                result = {"tools": self.statuses(), "publication_cutoff": self.cutoff or None,
                          "tool_calls_used": self.calls, "tool_calls_limit": self.max_calls}
            elif name.startswith("epo_"):
                with _quota_lock():
                    # Sync another process's committed quota before every call.
                    backend = self._backend("epo")
                    with session_scope() as session:
                        backend.use_ledger(settings_service.epo_ledger(session))
                    result = self._execute(name, arguments)
                    if settings_service.epo_persist_error():
                        raise PatentSearchError("quota_persistence_failed")
            else:
                result = self._execute(name, arguments)
        except Exception as exc:
            detail = scrub(str(exc), *self.secrets)[:500]
            code = getattr(exc, "fault_code", "") or type(exc).__name__
            self._record({**row, "state": "completed", "ok": False, "error_code": code, "detail": detail})
            raise
        self._record({**row, "state": "completed", "ok": True, "result": result})
        return result

    def _execute(self, name, arguments):
        if name == "epo_search":
            return self._epo_search(arguments)
        backend_id = name.split("_")[0]
        if name.endswith("_fetch"):
            return self._fetch(backend_id, arguments, "doi" if backend_id == "literature" else "publication_number")
        return self._plain_search(backend_id, arguments)

    def _backend(self, backend_id):
        if backend_id not in self.backends:
            if backend_id == "epo":
                with session_scope() as session:
                    backend = settings_service.epo_backend_for(session)
            else:
                backend = get_backend(self.values, backend_id)
            if backend is None or not backend.status().configured:
                raise PatentSearchError("backend_unavailable")
            self.backends[backend_id] = backend
        return self.backends[backend_id]

    def _epo_search(self, arguments):
        node = _query_node(arguments["query"])
        normalized = []
        # Do not silently hide unknown publication dates with a DB-side cutoff.
        cql = epo_cql.build(node, normalized=normalized)
        response = self._backend("epo").search_structured(node, max_results=arguments.get("max_results", 10))
        return {**_response(response, scope="bibliographic_search"), "cql": cql,
                "normalized_classifications": normalized, "publication_cutoff": self.cutoff or None}

    def _plain_search(self, backend_id, arguments):
        query = arguments["query"]
        response = self._backend(backend_id).search(PatentSearchQuery(query, arguments.get("max_results", 10)))
        return {**_response(response, scope="bibliographic_search"), "query": query,
                "publication_cutoff": self.cutoff or None}

    def _fetch(self, backend_id, arguments, identifier_key):
        identifier = arguments[identifier_key]
        constituent = arguments.get("constituent", "abstract" if backend_id == "literature" else "claims")
        response = self._backend(backend_id).fetch_document(identifier, constituent)
        result = _response(response, scope=constituent)
        def identity(number):
            return search_manifest.identity_key(doi=number) if backend_id == "literature" else search_manifest.identity_key(number)
        result["requested_identifier"] = identifier
        result["identifier_matched"] = any(identity(record["document_number"]) == identity(identifier) for record in result["records"])
        return result

def _validate(value, schema, depth=0):
    if depth > 12:
        raise ValueError("arguments_too_deep")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("expected_object")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise ValueError("unknown_argument")
        if set(schema.get("required", [])) - set(value):
            raise ValueError("missing_argument")
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], depth + 1)
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 500):
            raise ValueError("invalid_string")
    elif kind == "integer":
        if type(value) is not int or not schema.get("minimum", 1) <= value <= schema.get("maximum", 20):
            raise ValueError("invalid_integer")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("invalid_enum")

def _query_node(raw: Any, depth=0):
    if depth > epo_cql.MAX_DEPTH or not isinstance(raw, dict):
        raise ValueError("invalid_cql_structure_or_depth")
    kind = raw.get("type", "term")
    if kind == "term":
        if set(raw) - {"type", "field", "value", "match"}:
            raise ValueError("unknown_cql_term_field")
        return epo_cql.Term(field=raw.get("field", ""), value=raw.get("value", ""),
                            match=raw.get("match", epo_cql.MATCH_ALL))
    if kind == "group":
        items = raw.get("items")
        if set(raw) - {"type", "op", "items"} or not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("invalid_cql_group")
        return epo_cql.Group(op=raw.get("op", ""), items=tuple(_query_node(item, depth+1) for item in items))
    if kind == "date_range":
        if set(raw) - {"type", "field", "begin", "end"}:
            raise ValueError("unknown_cql_date_field")
        return epo_cql.DateRange(field=raw.get("field", "pd"),
                                 begin=raw.get("begin", ""), end=raw.get("end", ""))
    raise ValueError("unsupported_cql_type")

def _response(response, *, scope: str) -> dict:
    return {
        "untrusted_external_data": True,
        "verification_scope": scope,
        "total_found": response.total_found,
        "raw_artifact_id": response.raw_artifact_id,
        "fetched_at": response.fetched_at,
        "http_status": response.http_status,
        "request_url": response.request_url,
        "notes": list(response.notes),
        "failed_sources": list(response.failed_sources),
        "records": [_record(record) for record in response.records],
    }


def _record(record) -> dict:
    fields = {}
    evidence = {}
    for name, field in record.fields.items():
        fields[name] = field.value[:40000]
        if field.evidence is not None:
            evidence[name] = {
                "artifact_id": field.evidence.artifact_id,
                "field_path": field.evidence.field_path,
                "profile_id": field.evidence.profile_id,
            }
    publication_date = fields.get("publication_date", "")
    return {
        "document_number": record.doc_number,
        "title": record.title,
        "url": record.source_url,
        "publication_date": publication_date,
        "fields": fields,
        "evidence_refs": evidence,
        "truncated_fields": [name for name, field in record.fields.items() if len(field.value) > 40000],
    }


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": True},
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_QUERY_SCHEMA = {
    "type": "object",
    "description": 'Term: {type:"term",field:"ta",value:"image matching",match:"all"}. Group: {type:"group",op:"and"|"or"|"not",items:[nodes]}. Term fields: ti,ab,ta,txt,pa,in,pn,ap,pr,ipc,cpc,cl. Match: all/any/exact. Publication-date node: {type:"date_range",field:"pd",begin:"19000101",end:"20240131"}. A date-limited query may omit unknown dates; choose whether an additional unrestricted query is needed. Maximum nesting: 3.',
    "additionalProperties": True,
}
_EPO_SEARCH = _tool(
    "epo_search",
    "Search EPO OPS with a validated structured CQL expression. Returns the actual CQL and artifact references.",
    {"query": _QUERY_SCHEMA, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["query"],
)
_EPO_FETCH = _tool(
    "epo_fetch",
    "Fetch an EPO publication constituent. Use abstract, claims, description, biblio or family.",
    {"publication_number": {"type": "string"}, "constituent": {"type": "string", "enum": ["abstract", "claims", "description", "biblio", "family"]}},
    ["publication_number"],
)
_LITERATURE_SEARCH = _tool(
    "literature_search",
    "Search Crossref and Europe PMC using a literature-specific natural-language query.",
    {"query": {"type": "string", "minLength": 1, "maxLength": 500}, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}},
    ["query"],
)
_LITERATURE_FETCH = _tool(
    "literature_fetch",
    "Fetch bibliographic or abstract evidence for an exact DOI. Mismatched DOI responses are rejected by the backend.",
    {"doi": {"type": "string"}, "constituent": {"type": "string", "enum": ["abstract", "biblio"]}},
    ["doi"],
)
_KIWEE_SEARCH = _tool("kiwee_search", "Search the configured Kiwee patent backend.", {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"])
_KIWEE_FETCH = _tool("kiwee_fetch", "Fetch a document from the configured Kiwee backend.", {"publication_number": {"type": "string"}, "constituent": {"type": "string"}}, ["publication_number"])
_CAPABILITIES = _tool("search_capabilities", "Report which ARIA search tools are enabled and configured without making a network request.", {}, [])


def _reply(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    payload["error" if error is not None else "result"] = error if error is not None else result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    tools = SearchTools()
    while True:
        line = sys.stdin.readline(65537)
        if not line:
            break
        request_id = None
        try:
            if len(line) > 65536:
                raise ValueError("request_too_large")
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("invalid_request")
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                _reply(request_id, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                                    "serverInfo": {"name": SERVER_NAME, "version": "1"}})
            elif method == "ping":
                _reply(request_id, {})
            elif method == "tools/list":
                _reply(request_id, {"tools": tools.tool_definitions()})
            elif method == "tools/call":
                params = request.get("params") or {}
                try:
                    value = tools.call(str(params.get("name") or ""), params.get("arguments", {}))
                    error = False
                except Exception as exc:
                    value = {"error_code": getattr(exc, "fault_code", "") or type(exc).__name__,
                             "detail": scrub(str(exc), *tools.secrets)[:500]}
                    error = True
                _reply(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                                    "structuredContent": value, "isError": error})
            else:
                _reply(request_id, error={"code": -32601, "message": "Method not found"})
        except (ValueError, TypeError):
            _reply(request_id, error={"code": -32700, "message": "Invalid JSON-RPC request"})

if __name__ == "__main__":
    main()
