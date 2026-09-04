"""No-tools isolation survives removal of the old EPO planning loop."""
import pytest
from app.providers.base import NO_TOOLS, ExecutionOutcome
from app.evaluation.evaluator import evaluate
from app.enums import JobStatus
from app.providers.agy_cli import AgyCliProvider
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider

@pytest.mark.parametrize("provider", [AgyCliProvider, ClaudeCliProvider, CodexCliProvider])
@pytest.mark.parametrize("tool", ["Bash", "mcp__aria-search__epo_search", "web_search"])
def test_no_tools_policy_rejects_actual_calls_even_without_name_summary(provider, tool):
    outcome = ExecutionOutcome(result_text="not a valid isolated result", exit_code=0,
        terminal_reason="completed", tool_policy=NO_TOOLS,
        tool_calls=[{"name": tool, "input": {}, "ok": True}])
    outcome.cli_path = provider().id
    assert evaluate(outcome, [], fail_on_tool_use=False).status == JobStatus.FAILED
