"""Opt-in installed-CLI transport checks. No patent/literature network calls."""
from dataclasses import replace
import pytest
from app.providers.base import ExecutionRequest
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider
from app.execution.runner import _search_mcp_servers
from app.search_manifest import read_tool_journal

@pytest.mark.live_cli
@pytest.mark.parametrize("provider_type", [ClaudeCliProvider, CodexCliProvider])
async def test_installed_cli_can_call_only_capabilities(provider_type, client, tmp_path):
    provider = provider_type()
    name = "mcp__aria-search__search_capabilities"
    policy = replace(provider.search_tool_policy, allowed_tools=(), mcp_tools=(name,),
        required_tools=(name,), max_tool_calls=2)
    servers = _search_mcp_servers(tmp_path, "", 2)
    # TestClient owns an isolated database, never the user's settings.
    client.put("/api/settings", json={"values": {
        "epo_integration_enabled": False, "literature_integration_enabled": False,
        "kiwee_integration_enabled": False,
    }})
    request = ExecutionRequest(job_id="mcp-smoke-" + provider.id, work_dir=tmp_path,
        system_prompt="Call only the aria-search search_capabilities tool exactly once. Do not call other tools. Then reply ARIA_MCP_SMOKE_OK.",
        user_message="Verify the locally provided capability tool now.",
        tool_policy=policy, mcp_servers=servers, timeout_seconds=120)
    async def emit(kind, payload):
        pass
    outcome = await provider.execute(request, emit)
    if name not in outcome.tool_uses:
        print(outcome.raw_stdout)
        print(outcome.raw_stderr)
    assert not outcome.timed_out, outcome.errors
    assert name in outcome.tool_uses, (outcome.error_message, outcome.errors, outcome.result_text, outcome.raw_stdout[-6000:], outcome.raw_stderr[-1000:])
    assert all(call == name for call in outcome.tool_uses)
    journal = read_tool_journal(tmp_path)
    assert any(row.get("tool") == "search_capabilities" and row.get("ok") for row in journal), (outcome.tool_calls, journal, outcome.raw_stdout, outcome.raw_stderr)
