"""작업별 도구 정책.

이 파일이 지키는 불변조건은 하나다. 검색 기능을 붙이면서 기존 PDF/문헌 분석의
'도구 없음'이 조금이라도 느슨해지면 안 된다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.execution.runner import _progress_counts_as, _progress_should_count
from app import search_manifest
from app.providers.base import (
    AGY_WEB_SEARCH,
    CODEX_WEB_SEARCH,
    NO_TOOLS,
    WEB_SEARCH,
    ExecutionOutcome,
    ExecutionRequest,
    ToolPolicy,
)
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.claude_stream import ClaudeStreamParser


def _request(policy: ToolPolicy | None = None) -> ExecutionRequest:
    kwargs = {} if policy is None else {"tool_policy": policy}
    return ExecutionRequest(
        job_id="j",
        work_dir=Path("."),
        system_prompt="s",
        user_message="m",
        **kwargs,
    )


def _ok(**kwargs) -> ExecutionOutcome:
    outcome = ExecutionOutcome(
        result_text="정상 결과", exit_code=0, terminal_reason="completed"
    )
    for key, value in kwargs.items():
        setattr(outcome, key, value)
    return outcome


# ------------------------------------------------------------- CLI 인수 구성


def test_default_request_still_disables_all_tools() -> None:
    """정책을 지정하지 않은 호출 경로는 예전과 똑같이 도구가 꺼진다."""
    args = ClaudeCliProvider().build_args(_request())
    assert args[args.index("--tools") + 1] == ""
    assert "--allowedTools" not in args
    assert "--permission-mode" not in args


def test_analysis_policy_disables_all_tools() -> None:
    args = ClaudeCliProvider().build_args(_request(NO_TOOLS))
    assert args[args.index("--tools") + 1] == ""
    assert "--allowedTools" not in args
    # 도구가 없으면 물어볼 권한도 없다. 권한 모드를 건드리지 않는다.
    assert "--permission-mode" not in args
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == ""


def test_search_policy_opens_only_web_tools() -> None:
    args = ClaudeCliProvider().build_args(_request(WEB_SEARCH))
    assert args[args.index("--tools") + 1] == "WebSearch,WebFetch"

    allowed_at = args.index("--allowedTools")
    allowed = args[allowed_at + 1 : allowed_at + 3]
    assert allowed == ["WebSearch", "WebFetch"]

    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    # 검색 실행도 호스트 설정과 외부 MCP 를 상속하지 않는다.
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in args
    assert "--no-chrome" in args
    # 권한 우회 플래그는 어떤 경로에서도 쓰지 않는다.
    assert "--dangerously-skip-permissions" not in args
    assert "--allow-dangerously-skip-permissions" not in args


def test_no_shell_or_file_tool_can_be_requested() -> None:
    for forbidden in ("Bash", "Read", "Write", "Edit", "Task"):
        assert forbidden not in WEB_SEARCH.allowed_tools


def test_providers_declare_their_supported_search_policies() -> None:
    from app.providers.agy_cli import AgyCliProvider
    from app.providers.base import CODEX_WEB_SEARCH
    from app.providers.codex_cli import CodexCliProvider

    assert ClaudeCliProvider().supports_tool_policy(WEB_SEARCH)
    assert ClaudeCliProvider().supports_tool_policy(NO_TOOLS)
    assert AgyCliProvider().supports_tool_policy(AGY_WEB_SEARCH)
    assert AgyCliProvider().search_tool_policy is AGY_WEB_SEARCH
    assert not AGY_WEB_SEARCH.enforce_advertised_allowlist
    assert not AgyCliProvider().supports_tool_policy(WEB_SEARCH)
    assert CodexCliProvider().supports_tool_policy(CODEX_WEB_SEARCH)
    assert CodexCliProvider().search_tool_policy is CODEX_WEB_SEARCH
    assert not CODEX_WEB_SEARCH.enforce_advertised_allowlist
    # Claude 전용 정책을 도구를 끄지 못하는 Provider 가 주장하면 안 된다.
    assert not CodexCliProvider().supports_tool_policy(WEB_SEARCH)
    assert not CodexCliProvider().supports_tool_policy(NO_TOOLS)


# --------------------------------------------------------------- 판정 규칙


def test_analysis_outcome_with_tools_still_fails() -> None:
    verdict = evaluate(_ok(tool_policy=NO_TOOLS, tools_advertised=["Read"]))
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_search_outcome_with_allowed_tools_succeeds() -> None:
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["WebSearch", "WebFetch"],
        )
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_search_outcome_with_stray_tool_use_fails() -> None:
    for stray in ("Bash", "Write", "Edit", "Read", "Task"):
        verdict = evaluate(
            _ok(
                tool_policy=WEB_SEARCH,
                tools_advertised=["WebSearch", "WebFetch"],
                tool_uses=["WebSearch", stray],
            )
        )
        assert verdict.status == JobStatus.FAILED, stray
        assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION
        assert stray in " ".join(verdict.errors)


def test_search_outcome_with_stray_advertised_tool_fails() -> None:
    """호출하지 않고 광고만 해도 위반이다. 목록이 깨졌다는 뜻이기 때문이다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch", "Bash"],
            tool_uses=["WebSearch"],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_agy_search_ignores_extra_advertised_tools_but_checks_actual_calls() -> None:
    """agy는 노출 목록을 줄이지 못하므로 광고가 아니라 실제 호출을 판정한다."""
    allowed = evaluate(
        _ok(
            tool_policy=AGY_WEB_SEARCH,
            tools_advertised=["search_web", "read_url_content", "run_command"],
            tool_uses=["search_web", "read_url_content"],
        )
    )
    assert allowed.status == JobStatus.SUCCEEDED

    forbidden = evaluate(
        _ok(
            tool_policy=AGY_WEB_SEARCH,
            tools_advertised=["search_web", "run_command"],
            tool_uses=["search_web", "run_command"],
        )
    )
    assert forbidden.status == JobStatus.FAILED
    assert forbidden.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_agy_headless_read_url_denial_is_not_reported_as_empty_result() -> None:
    """agy 의 soft-deny + 빈 SUCCESS 는 원인 그대로 사용자에게 보여 준다."""
    outcome = ExecutionOutcome(
        result_text="",
        exit_code=0,
        terminal_reason="SUCCESS",
        usage={"input_tokens": 100},
        tool_policy=AGY_WEB_SEARCH,
        tool_uses=["search_web", "read_url_content"],
        tool_calls=[
            {
                "name": "search_web",
                "input": {"query": "radar patent"},
                "ok": True,
                "error": None,
            },
            {
                "name": "read_url_content",
                "input": {
                    "url": "https://patents.google.com/patent/KR102477584B1/en"
                },
                "ok": False,
                "error": (
                    'permission check failed for read_url "patents.google.com": '
                    "user denied permission for read_url(patents.google.com)"
                ),
            },
        ],
        raw_stderr=(
            'a tool required the "read_url" permission that headless mode cannot '
            "prompt for, so it was auto-denied"
        ),
    )

    verdict = evaluate(outcome)

    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_PERMISSION_DENIED
    assert "read_url(patents.google.com)" in " ".join(verdict.errors)


def test_agy_permission_denial_does_not_discard_a_nonempty_search_report() -> None:
    """모델이 접근 실패를 포함한 보고서를 냈다면 한 URL 거부만으로 폐기하지 않는다."""
    outcome = _ok(
        tool_policy=AGY_WEB_SEARCH,
        tool_uses=["search_web", "read_url_content"],
        tool_calls=[
            {
                "name": "read_url_content",
                "input": {"url": "https://paywall.example.com/paper"},
                "ok": False,
                "error": "permission check failed: user denied permission",
            }
        ],
    )

    verdict = evaluate(outcome)

    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_agy_headless_stderr_denial_is_detected_without_a_tool_error_record() -> None:
    """Provider 가 거부 원인을 stderr 에만 남겨도 EMPTY_RESULT 로 뭉개지 않는다."""
    outcome = ExecutionOutcome(
        result_text="",
        exit_code=0,
        usage={"input_tokens": 100},
        tool_policy=AGY_WEB_SEARCH,
        tool_uses=["search_web"],
        raw_stderr=(
            'a tool required the "read_url" permission that headless mode cannot '
            "prompt for, so it was auto-denied"
        ),
    )

    verdict = evaluate(outcome)

    assert verdict.error_code == ErrorCode.SEARCH_PERMISSION_DENIED
    assert "웹페이지 읽기 권한" in " ".join(verdict.errors)


def test_global_optout_cannot_widen_search_allowlist() -> None:
    """fail_on_tool_use 를 꺼도 검색의 허용 목록은 넓어지지 않는다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["Bash"],
        ),
        fail_on_tool_use=False,
    )
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_global_optout_cannot_reopen_analysis_tools() -> None:
    verdict = evaluate(
        _ok(tool_policy=NO_TOOLS, tool_uses=["Read"]), fail_on_tool_use=False
    )
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_search_without_any_search_call_fails() -> None:
    """도구를 안 쓰고 기억으로 쓴 보고서는 검색 결과가 아니다."""
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=[],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_NOT_PERFORMED


def test_fetch_only_run_is_not_a_search() -> None:
    verdict = evaluate(
        _ok(
            tool_policy=WEB_SEARCH,
            tools_advertised=["WebSearch", "WebFetch"],
            tool_uses=["WebFetch"],
        )
    )
    assert verdict.error_code == ErrorCode.SEARCH_NOT_PERFORMED


def test_auth_failure_wins_over_search_not_performed() -> None:
    outcome = _ok(tool_policy=WEB_SEARCH, tool_uses=[], auth_required=True)
    assert evaluate(outcome).error_code == ErrorCode.AUTH_REQUIRED


def test_process_error_wins_over_search_not_performed() -> None:
    outcome = ExecutionOutcome(
        result_text="", error_message="실행 실패", tool_policy=WEB_SEARCH
    )
    assert evaluate(outcome).error_code == ErrorCode.PROCESS_ERROR


def test_budget_exceeded_is_reported_as_its_own_failure() -> None:
    """ARIA 가 끊은 것이므로 cancelled 도 참이다. 사용자 취소와 구별한다."""
    outcome = _ok(
        tool_policy=WEB_SEARCH,
        tool_uses=["WebSearch"] * 41,
        tool_budget_exceeded=True,
        cancelled=True,
    )
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_BUDGET_EXCEEDED


def test_stray_tool_outranks_budget_exceeded() -> None:
    """둘 다 걸렸으면 알아야 할 것은 '많이 불렀다'가 아니라 '뭘 불렀다'이다."""
    outcome = _ok(
        tool_policy=WEB_SEARCH,
        tools_advertised=["WebSearch", "WebFetch"],
        tool_uses=["WebSearch", "Bash"],
        tool_budget_exceeded=True,
        cancelled=True,
    )
    assert evaluate(outcome).error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_user_cancel_is_still_cancelled() -> None:
    outcome = _ok(tool_policy=WEB_SEARCH, cancelled=True)
    assert evaluate(outcome).error_code == ErrorCode.CANCELLED


# ------------------------------------------------------- stream 도구 이벤트


def test_stream_records_search_queries_and_urls() -> None:
    parser = ClaudeStreamParser()
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "sonnet",
                "tools": ["WebFetch", "WebSearch"],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "WebSearch",
                            "input": {
                                "query": "claim similar patent",
                                "allowed_domains": ["patents.google.com"],
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "WebFetch",
                            "input": {
                                "url": "https://patents.google.com/patent/US1",
                                "prompt": "x" * 5000,
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "is_error": True,
                            "content": "403 Forbidden",
                        }
                    ]
                },
            }
        ),
    ]
    events = []
    for line in lines:
        events.extend(parser.feed(line))

    state = parser.state
    assert state.tool_names == ["WebFetch", "WebSearch"]
    assert state.tool_uses == ["WebSearch", "WebFetch"]

    search, fetch = state.tool_calls
    assert search["name"] == "WebSearch"
    assert search["input"]["query"] == "claim similar patent"
    assert search["input"]["allowed_domains"] == ["patents.google.com"]
    assert search["ok"] is True
    assert search["ts"]

    assert fetch["input"]["url"] == "https://patents.google.com/patent/US1"
    # WebFetch 의 prompt 본문은 감사 기록에 옮겨 담지 않는다.
    assert "prompt" not in fetch["input"]
    assert fetch["ok"] is False
    assert "403" in fetch["error"]

    tool_events = [payload for kind, payload in events if kind == "tool_use"]
    assert tool_events[0]["input"]["query"] == "claim similar patent"
    assert [payload for kind, payload in events if kind == "tool_error"]


def test_stream_does_not_record_arguments_of_unexpected_tools() -> None:
    """허용 목록 밖 도구의 인수는 그 자체가 명령일 수 있다. 키 이름만 남긴다."""
    parser = ClaudeStreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t9",
                            "name": "Bash",
                            "input": {"command": "rm -rf /"},
                        }
                    ]
                },
            }
        )
    )
    recorded = parser.state.tool_calls[0]
    assert recorded["name"] == "Bash"
    assert recorded["input"] == {"keys": ["command"]}
    assert "rm -rf" not in json.dumps(recorded, ensure_ascii=False)


# ------------------------------------ Codex: 도구 하나가 검색과 URL 조회를 겸한다
#
# 성공 신호가 오지 않는다는 사실(2026-08-30 실측)이 아래 계약의 근거다.
# "확인된 실패가 없다"를 "성공했다"로 승격하지 않는 것이 요점이다.


def _codex_call(call_id: str, *, url: str = "", query: str = "") -> dict:
    data: dict = {"reported_action": {"type": "other"}}
    if url:
        data.update({"input_kind": "url", "url": url})
    else:
        data.update({"input_kind": "query", "query": query})
    return {
        "id": call_id,
        "name": "web_search",
        "ts": "2026-08-30T00:00:00+00:00",
        "input": data,
        # 성공도 실패도 관측되지 않았다.
        "ok": None,
        "error": None,
    }


OPENED = "https://en.wikipedia.org/wiki/Radome"
FAILED = "https://patents.google.com/patent/EP2881320A1/en"


def test_url_lookups_never_enter_the_page_fetch_lists() -> None:
    """URL 조회는 페이지 열람이 아니다. 성공 신호가 없으므로 열람으로 승격하면
    열지 못한 URL 이 그대로 열람 기록이 된다."""
    calls = [_codex_call("a", url=OPENED), _codex_call("b", url=FAILED)]
    observed = search_manifest.observed(calls, ["web_search"] * 2)
    assert observed["attempted_fetch_urls"] == []
    assert observed["succeeded_fetch_urls"] == []
    assert observed["url_lookup_attempts"] == [OPENED, FAILED]


def test_url_is_not_recorded_as_an_executed_search_query() -> None:
    calls = [_codex_call("a", url=OPENED), _codex_call("b", query="radar EO IR")]
    observed = search_manifest.observed(calls, ["web_search"] * 2)
    assert observed["search_queries"] == ["radar EO IR"]
    assert observed["url_lookup_attempts"] == [OPENED]


def test_unknown_outcomes_are_listed_apart_from_confirmed_failures() -> None:
    confirmed = _codex_call("c", query="x")
    confirmed["ok"] = False
    confirmed["error"] = "boom"
    calls = [_codex_call("a", url=OPENED), confirmed]
    observed = search_manifest.observed(calls, ["web_search"] * 2)
    assert [row["name"] for row in observed["tool_failures"]] == ["web_search"]
    assert len(observed["unknown_tool_outcomes"]) == 1
    assert observed["unknown_tool_outcomes"][0]["input"]["url"] == OPENED


def test_url_lookup_only_run_is_not_a_search() -> None:
    """URL 만 조회한 실행은 검색을 한 것이 아니다. 도구 이름으로는 구분되지
    않으므로 실제 검색 시도로 판정해야 한다."""
    verdict = evaluate(
        _ok(
            tool_policy=CODEX_WEB_SEARCH,
            tool_uses=["web_search", "web_search"],
            tool_calls=[_codex_call("a", url=OPENED), _codex_call("b", url=FAILED)],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_NOT_PERFORMED


def test_unknown_outcome_is_not_promoted_to_success_or_failure() -> None:
    """성공 여부를 모르는 검색 호출은 성공으로도 실패로도 읽히면 안 된다.

    성공을 요구하면 검색을 하고도 SEARCH_NOT_PERFORMED 로 떨어지고, 실패로
    읽으면 정상 실행이 도구 오류가 된다. 검색 시도가 있었으면 통과다.
    """
    verdict = evaluate(
        _ok(
            tool_policy=CODEX_WEB_SEARCH,
            tool_uses=["web_search"],
            tool_calls=[_codex_call("a", query="radar EO IR rotating turret")],
        )
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_mixed_run_counts_each_kind_exactly() -> None:
    calls = [
        _codex_call("a", query="radar EO IR"),
        _codex_call("b", query="감시정찰 회전부"),
        _codex_call("c", url=OPENED),
    ]
    observed = search_manifest.observed(calls, ["web_search"] * 3)
    assert len(observed["search_queries"]) == 2
    assert len(observed["url_lookup_attempts"]) == 1
    # 전체 호출 수는 종류와 무관하게 셋이다.
    assert observed["tool_call_counts"]["web_search"] == 3


# ------------------------------------- 실시간 진행 표시의 분류 (runner 순수 함수)


def test_progress_does_not_count_a_pending_start_event() -> None:
    assert (
        _progress_counts_as(
            "tool_use",
            {"name": "web_search", "kind_pending": True, "input": {}},
        )
        == ""
    )


def test_progress_counts_a_resolved_url_lookup_as_a_lookup() -> None:
    assert (
        _progress_counts_as(
            "tool_use_resolved",
            {"name": "web_search", "input": {"input_kind": "url", "url": OPENED}},
        )
        == "url_lookup"
    )


def test_progress_counts_a_resolved_search_as_a_search() -> None:
    assert (
        _progress_counts_as(
            "tool_use_resolved",
            {"name": "web_search", "input": {"input_kind": "query", "query": "x"}},
        )
        == "search"
    )


def test_progress_keeps_the_name_based_path_for_unlabelled_providers() -> None:
    """Claude·agy 는 시작 이벤트에 이미 완전한 인수가 실려 온다. 종전대로 센다."""
    assert (
        _progress_counts_as("tool_use", {"name": "WebSearch", "input": {"query": "x"}})
        == "search"
    )
    assert (
        _progress_counts_as("tool_use", {"name": "WebFetch", "input": {"url": OPENED}})
        == "fetch"
    )


# --------------------------------------------- 호출 수와 질의 수는 다른 값이다


def test_one_batched_call_is_one_search_with_four_queries() -> None:
    """2026-08-30 실측: 한 호출이 질의 4개를 묶어 보냈고, 최상위 query 는 CLI 가
    첫 질의를 잘라 만든 표시용 문자열이었다."""
    call = {
        "id": "a",
        "name": "web_search",
        "ts": "2026-08-30T00:00:00+00:00",
        "input": {
            "input_kind": "query",
            "query": (
                "patent independently rotating dual axis radar EO IR camera "
                "on-device AI surveillance system ..."
            ),
            "queries": [
                "patent independently rotating dual axis radar EO IR camera "
                "on-device AI surveillance system",
                "patent radar mounted rotating turret internal EO IR camera "
                "artificial intelligence surveillance",
                "paper radar EO IR sensor fusion edge AI rotating surveillance turret",
                "특허 레이더 EO IR 카메라 인공지능 감시정찰 회전부",
            ],
            "reported_action": {"type": "search"},
        },
        "ok": None,
        "error": None,
    }
    observed = search_manifest.observed([call], ["web_search"])
    assert observed["search_call_count"] == 1
    assert len(observed["search_queries"]) == 4
    # 잘린 표시용 문자열은 목록에 들어가지 않는다.
    assert not any(query.endswith(" ...") for query in observed["search_queries"])


def test_search_call_count_falls_back_to_one_query_per_call() -> None:
    """queries 가 없는 Provider 는 예전대로 호출당 질의 하나다."""
    calls = [
        {
            "id": "a",
            "name": "WebSearch",
            "ts": "t",
            "input": {"query": "radar"},
            "ok": True,
            "error": None,
        }
    ]
    observed = search_manifest.observed(calls, ["WebSearch"])
    assert observed["search_call_count"] == 1
    assert observed["search_queries"] == ["radar"]


def test_progress_dedupes_per_lane_not_globally() -> None:
    """두 레인은 각자 별도의 CLI 프로세스다. 호출 ID 는 프로세스 안에서만
    고유하므로, ID 만으로 중복을 막으면 뒤 레인의 첫 호출이 통째로 누락된다."""
    counted: set = set()
    assert _progress_should_count(counted, "claim_only", "item_0") is True
    # 같은 레인의 같은 ID(시작·완료) 는 한 번만 센다.
    assert _progress_should_count(counted, "claim_only", "item_0") is False
    # 다른 레인의 같은 ID 는 다른 호출이다.
    assert _progress_should_count(counted, "spec_assisted", "item_0") is True
    assert _progress_should_count(counted, "spec_assisted", "item_0") is False
    assert len(counted) == 2


def test_progress_without_an_id_is_always_counted() -> None:
    """ID 를 못 읽었다고 세지 않으면 진행 표시가 멈춘 것처럼 보인다."""
    counted: set = set()
    assert _progress_should_count(counted, "claim_only", "") is True
    assert _progress_should_count(counted, "claim_only", "") is True


def test_resolved_event_without_a_kind_is_not_counted_by_name() -> None:
    """종류를 표시하는 Provider 가 이번엔 표시하지 못했다는 뜻이다.

    이름으로 되돌리면 URL 조회가 다시 검색으로 잡힌다 — 이름 기반 가정을
    버리려고 만든 이벤트다. 모르면 세지 않는다.
    """
    assert (
        _progress_counts_as("tool_use_resolved", {"name": "web_search", "input": {}})
        == ""
    )
    assert (
        _progress_counts_as(
            "tool_use_resolved",
            {"name": "web_search", "input": {"query": "radar"}},
        )
        == ""
    )
