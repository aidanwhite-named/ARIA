"""Replacement contract tests: one judgment, mechanical evidence, bounded tools."""
import copy
import json
from dataclasses import replace
from pathlib import Path
import pytest
from app import search_manifest as sm, search_verification as sv, search_report, search_legacy
from app import search_channels, search_dates
from app.search_mcp_server import SearchTools, ToolLimitExceeded, _query_node, _response
from app.patent_search import epo_backend, epo_parser, epo_cql, artifacts, base
from app.providers.base import NO_TOOLS, WEB_SEARCH, ExecutionRequest, ARIA_MCP_TOOLS
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider
from . import epo_fixtures as fx
from .conftest import wait_for_job

def parsed(**fields):
    return sm.parse(json.dumps({"candidates": [{"doc_number": "EP1000000A1",
        "group": "A", "mapping": [], **fields}]}))[0]

@pytest.mark.parametrize("group", ["A", "B", "C", None])
def test_unverified_evidence_never_reclassifies(group, tmp_path):
    data = parsed(group=group, mapping=[{"feature": "F", "degree": "matched",
        "counterpart": "model judgment", "support_text": "invented",
        "verbatim_excerpt": "fake quote", "translation": "fake translation"}])
    before = copy.deepcopy(data)
    result = sv.verify(data, {}, [], store=artifacts.ArtifactStore(tmp_path))
    candidate = result["candidates"][0]
    assert data == before
    assert candidate["group"] == group
    assert candidate["mapping"][0]["counterpart"] == "model judgment"
    assert candidate["mapping"][0]["verbatim_excerpt"] == ""
    assert candidate["mapping"][0]["translation"] == ""
    assert candidate["evidence_level"] == "search_snippet_only"
    assert set(candidate["verification_issues"]) <= set(sv.ISSUE_LABELS)

@pytest.mark.parametrize("payload", ['{}', '{"candidates":{}}',
    '{"candidates":[{"group":"D"}]}', '{"candidates":[{"mapping":"bad"}]}',
    '{"candidates":[{"group":"A","title":5}]}'])
def test_invalid_json_schema_is_not_a_partial_report(payload):
    with pytest.raises(sm.SearchLogError):
        sm.parse(payload)

def test_duplicate_keeps_first_group_and_mapping_not_family(tmp_path):
    rows = [parsed(group=group)["candidates"][0] for group in ("C", "A")]
    rows.append(parsed(doc_number="EP1000000B1", group="B")["candidates"][0])
    result = sv.verify({"candidates": rows}, {}, [], store=artifacts.ArtifactStore(tmp_path))
    assert [c["group"] for c in result["candidates"]] == ["C", "B"]
    assert "duplicate_group_conflict" in result["candidates"][0]["verification_issues"]

def test_doi_punctuation_is_not_collapsed():
    assert sm.identity_key(doi="10.1000/a-b") != sm.identity_key(doi="10.1000/ab")
    assert sm.identity_key(doi="https://doi.org/10.1000/A") == "doi:10.1000/a"


def test_agent_can_choose_a_validated_publication_date_query():
    node = _query_node({"type": "group", "op": "and", "items": [
        {"type": "term", "field": "ta", "value": "image matching"},
        {"type": "date_range", "field": "pd", "begin": "19000101", "end": "20240131"},
    ]})
    assert 'pd within "19000101 20240131"' in epo_cql.build(node)


def test_capability_probe_is_not_a_search_but_optional_db_fetch_is():
    name = "mcp__aria-search__search_capabilities"
    assert not sm.has_retrieval_attempt([{"name": name}], [name],
        [{"state": "started", "tool": "search_capabilities"}])
    assert sm.has_retrieval_attempt([], [], [{"state": "started", "tool": "epo_fetch"}])
    assert sm.has_retrieval_attempt([{"name": "WebFetch"}], [])


def test_reading_different_publication_does_not_verify_candidate(tmp_path):
    url = "https://patents.google.com/patent/EP2000000A1/en"
    verified = sv.verify(parsed(url=url, group="B"), {"succeeded_fetch_urls": [url]}, [],
                         store=artifacts.ArtifactStore(tmp_path))["candidates"][0]
    assert verified["group"] == "B"
    assert verified["evidence_level"] == "search_snippet_only"
    assert "identifier_mismatch" in verified["verification_issues"]


def test_previous_run_candidates_keep_order_groups_and_technical_mapping(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures" /
                          "run_30dc39f8_candidates.json").read_text(encoding="utf-8"))
    before = sm.parse(json.dumps({"candidates": fixture["candidates"]}))[0]
    after = sv.verify(before, {}, [], store=artifacts.ArtifactStore(tmp_path))
    assert len(after["candidates"]) == 18
    for original, verified in zip(before["candidates"], after["candidates"]):
        for key in ("doc_number", "doi", "group", "rank", "note"):
            assert verified[key] == original[key]
        for source_row, target_row in zip(original["mapping"], verified["mapping"]):
            for key in ("feature", "degree", "counterpart", "similar", "different"):
                assert target_row[key] == source_row[key]


def test_provenance_requires_delivered_response_and_correct_artifact(tmp_path):
    store = artifacts.ArtifactStore(tmp_path)
    aid = store.put(fx.CLAIMS)
    document = epo_parser.read_documents(fx.CLAIMS)[0]
    record = epo_backend._record_for(document, aid)
    response = base.PatentSearchResponse(records=(record,), total_found=1,
        raw_artifact_id=aid)
    result = _response(response, scope="claims")
    field_name = next(name for name in result["records"][0]["fields"] if name.startswith("claims"))
    text = result["records"][0]["fields"][field_name]
    ref = result["records"][0]["evidence_refs"][field_name]
    data = parsed(doc_number=record.doc_number, group="C", mapping=[
        {"feature": "F", "support_text": text, "evidence_ref": ref}])
    journal = [{"state": "completed", "ok": True, "tool": "epo_fetch", "id": "1",
        "arguments": {"publication_number": record.doc_number, "constituent": "claims"}, "result": result}]
    verified = sv.verify(data, {}, journal, store=store)["candidates"][0]
    assert verified["group"] == "C"
    assert verified["verification_scope"]["claims"] == "verified"
    assert verified["verification_scope"]["abstract"] == "not_requested"
    assert verified["mapping"][0]["support_verified"]
    assert not verified["mapping"][0]["quote_verified"]
    # A real but undelivered artifact is not enough.
    assert not sv.verify(data, {}, [], store=store)["candidates"][0]["mapping"][0]["support_verified"]
    store._path(aid).write_bytes(b"tampered")
    assert not sv.verify(data, {}, journal, store=store)["candidates"][0]["mapping"][0]["support_verified"]

def test_unknown_dates_are_kept_and_verified_later_dates_are_audited():
    data = {"candidates": [{"doc_number": "EP1A1", "publication_date": ""},
                            {"doc_number": "EP2A1", "publication_date": "20250101"}]}
    audit = search_dates.filter_candidates(data, "2024-01-01")
    assert [c["doc_number"] for c in data["candidates"]] == ["EP1A1"]
    assert audit["excluded"][0]["doc_number"] == "EP2A1"

def test_model_date_does_not_exclude_unverified_document(tmp_path):
    data = sv.verify(parsed(publication_date="20250101"), {}, [], store=artifacts.ArtifactStore(tmp_path))
    audit = search_dates.filter_candidates(data, "2024-01-01")
    assert audit["kept"] == 1
    assert data["candidates"][0]["reported_publication_date"] == "20250101"

def test_tool_cap_rejects_n_plus_one_and_survives_restart(tmp_path):
    tools = SearchTools(values={}, work_dir=tmp_path, max_calls=1)
    assert tools.call("search_capabilities", {})["tool_calls_used"] == 1
    with pytest.raises(ToolLimitExceeded):
        tools.call("search_capabilities", {})
    restarted = SearchTools(values={}, work_dir=tmp_path, max_calls=1)
    assert restarted.calls == 1
    with pytest.raises(ToolLimitExceeded):
        restarted.call("search_capabilities", {})
    journal = sm.read_tool_journal(tmp_path)
    assert [row["state"] for row in journal] == ["started", "completed", "rejected", "rejected"]
    assert "result" in journal[1]

def test_query_and_failures_are_recorded_without_consuming_network(tmp_path):
    tools = SearchTools(values={}, work_dir=tmp_path)
    with pytest.raises(ValueError):
        tools.call("literature_search", {"query": "한국어 actual query"})
    journal = sm.read_tool_journal(tmp_path)
    assert journal[-1]["arguments"]["query"] == "한국어 actual query"
    assert journal[-1]["ok"] is False

def test_available_tools_do_not_create_independent_searches():
    status = search_channels.availability({"epo_integration_enabled": False,
        "kiwee_integration_enabled": True, "literature_integration_enabled": True}, "agy")
    assert status["epo"]["status"] == "disabled"
    assert status["kiwee"]["status"] == "not_implemented"
    assert status["literature"]["status"] == "unsupported_transport"

def test_cql_error_is_structured_and_not_free_text():
    with pytest.raises(epo_cql.CqlError):
        epo_cql.build(_query_node({"field": "shell", "value": "run"}))
    with pytest.raises(ValueError):
        _query_node({"type": "group", "items": [None], "op": "and"})
    assert "image" in epo_cql.build(_query_node({"field": "ta", "value": "image matching"}))

@pytest.mark.parametrize("provider", [ClaudeCliProvider, CodexCliProvider])
def test_no_tools_cannot_register_mcp(provider, tmp_path):
    req = ExecutionRequest(job_id="x", work_dir=tmp_path, system_prompt="", user_message="",
        tool_policy=NO_TOOLS, mcp_servers={"aria-search": {"command": "python"}})
    with pytest.raises(ValueError):
        provider().build_args(req)

def test_claude_mcp_names_use_allowed_tools_not_builtin_list(tmp_path):
    req = ExecutionRequest(job_id="x", work_dir=tmp_path, system_prompt="", user_message="",
        tool_policy=replace(WEB_SEARCH, mcp_tools=ARIA_MCP_TOOLS),
        mcp_servers={"aria-search": {"command": "python"}})
    args = ClaudeCliProvider().build_args(req)
    assert args[args.index("--tools")+1] == "WebSearch,WebFetch"
    assert "mcp__aria-search__epo_search" in args
    assert "--strict-mcp-config" in args

def test_legacy_view_never_mutates_or_reexecutes():
    original = {"version": 13, "reported": {"candidates": [{"group": None, "provisional_group": "A", "mapping": []}]}}
    before = copy.deepcopy(original)
    result = search_legacy.view(original)
    assert original == before
    assert result["reported"]["candidates"][0]["group"] is None
    assert "이전 형식" in search_report.render(original)

def test_api_one_execute_no_secondary_classifier(client, monkeypatch):
    from .fake_provider import DeterministicSearchProvider
    calls = []
    original = DeterministicSearchProvider.execute
    async def wrapped(self, request, emit):
        calls.append(request)
        return await original(self, request, emit)
    monkeypatch.setattr(DeterministicSearchProvider, "execute", wrapped)
    created = client.post("/api/jobs", json={"job_kind":"similarity_search",
        "provider":"test-search", "claim_text":"청구항 1. 센서를 포함하는 장치"}).json()
    job = wait_for_job(client, created["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]
    assert len(calls) == 1
    assert job["search_manifest"]["version"] == 14
    assert job["search_manifest"]["reported"]["candidates"]
    assert "official_verification" not in job["search_manifest"]


def test_search_depth_reaches_job_and_execution_limits(client):
    created = client.post("/api/jobs", json={"job_kind": "similarity_search",
        "provider": "test-search", "claim_text": "청구항 1. 센서를 포함하는 장치",
        "search_depth": "quick"}).json()
    job = wait_for_job(client, created["id"])
    assert job["status"] == "SUCCEEDED", job["errors"]
    assert job["search_depth"] == "quick"
    assert job["search_manifest"]["limits"] == {
        "depth": "quick", "max_tool_calls": 15, "timeout_seconds": 300}
    invalid = client.post("/api/jobs", json={"job_kind": "similarity_search",
        "provider": "test-search", "claim_text": "청구항 1. 센서", "search_depth": "unbounded"})
    assert invalid.status_code == 422

@pytest.mark.parametrize("payload", [
    '{"candidates":[],"candidates":[{"group":"A"}]}',
    '{"candidates":[],"rounds":{}}',
    '{"candidates":[],"access_failures":"fake"}',
])
def test_ambiguous_or_invalid_auxiliary_json_is_rejected(payload):
    with pytest.raises(sm.SearchLogError):
        sm.parse(payload)

def test_presets_only_limit_total_calls_and_time():
    assert search_channels.execution_limits({"max_search_tool_calls": 200,
        "default_timeout_seconds": 3600}, "deep") == (80, 1800)
    assert search_channels.execution_limits({"max_search_tool_calls": 8,
        "default_timeout_seconds": 90}, "deep") == (8, 90)
    assert search_channels.execution_limits({}, "quick") == (15, 300)

def test_backend_instance_is_reused_for_cumulative_budgets(tmp_path, monkeypatch):
    from app import search_mcp_server as mcp
    made = []
    class Backend:
        def status(self):
            return base.BackendStatus("literature", "test", True, True)
    def factory(*args):
        made.append(Backend())
        return made[-1]
    monkeypatch.setattr(mcp, "get_backend", factory)
    tools = SearchTools(values={}, work_dir=tmp_path)
    assert tools._backend("literature") is tools._backend("literature")
    assert len(made) == 1

def test_codex_cli_override_keys_are_not_quoted(tmp_path):
    from app.providers.base import CODEX_WEB_SEARCH
    req = ExecutionRequest(job_id="x", work_dir=tmp_path, system_prompt="", user_message="",
        tool_policy=replace(CODEX_WEB_SEARCH, mcp_tools=ARIA_MCP_TOOLS),
        mcp_servers={"aria-search": {"command": "python", "env": {"ARIA_DATA_DIR": "test"}}})
    args = CodexCliProvider().build_args(req)
    assert any(arg.startswith("mcp_servers.aria-search.env.ARIA_DATA_DIR=") for arg in args)
    assert not any('mcp_servers."' in arg for arg in args)
    assert 'mcp_servers.aria-search.default_tools_approval_mode="writes"' in args

def test_mcp_protocol_is_utf8_and_tool_errors_are_not_protocol_errors(client, tmp_path):
    import os
    import subprocess
    import sys
    requests = [
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
        {"jsonrpc":"2.0","id":2,"method":"tools/list"},
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_capabilities","arguments":{}}},
        {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"not_a_tool","arguments":{"query":"한글"}}},
    ]
    env = {**os.environ, "ARIA_SEARCH_WORK_DIR": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    wire = "\n".join(json.dumps(row) for row in requests) + "\n{broken\n"
    completed = subprocess.run([sys.executable, "-m", "app.search_mcp_server"], input=wire,
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=20)
    assert completed.returncode == 0, completed.stderr
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "aria-search"
    assert replies[2]["result"]["isError"] is False
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["id"] is None
    assert sm.read_tool_journal(tmp_path)[-1]["arguments"]["query"] == "한글"
