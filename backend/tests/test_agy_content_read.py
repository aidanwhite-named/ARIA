"""agy 의 read_url_content 산출물을 읽는 호출의 계약.

agy 는 가져온 페이지를 돌려주지 않는다. 파일에 저장하고 경로만 알려준다.

    The full content of the article at <url> has been saved to:
    <brain>\\<conversation_id>\\.system_generated\\steps\\<n>\\content.md
    You can use the view_file tool to read specific sections if needed.

이 메시지는 agy 1.1.19 실행 기록에서 그대로 옮긴 것이다. 그래서 본문을 읽는
유일한 통로가 view_file 이고, 근거 문장을 요구하는 검색 프롬프트(v6)는 모델을
그 호출로 밀어 넣는다. 2026-08-25 실행이 여기서 죽었다 — 정책이 본문을 읽는
유일한 도구를 금지하고 있었다.

여기서 지키는 것은 두 가지다.

  범위     이번 대화의 read_url_content 산출물만 정상 열람으로 인정한다.
  정직함   그 판정은 사후 감사다. 호출을 막지는 못한다.
"""

from __future__ import annotations

import json

import pytest

from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.providers.agy_cli import (
    audit_content_reads,
    content_artifact_step,
    split_tool_calls,
)
from app.providers.agy_stream import AgyStreamParser
from app.providers.base import AGY_WEB_SEARCH, ExecutionOutcome

CONV = "00f60881-7b13-4fd4-9077-e52939a74400"


def artifact(tmp_path, conversation: str = CONV, step: int = 9):
    """실측 경로 규칙 그대로의 산출물 파일을 만든다."""
    path = (
        tmp_path / conversation / ".system_generated" / "steps" / str(step) / "content.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Title: Live Content\n", encoding="utf-8")
    return path


def feed(parser: AgyStreamParser, payloads) -> None:
    for payload in payloads:
        parser.feed(json.dumps(payload))


def tool_step(step_index: int, name: str, parameters: dict, state: str = "DONE") -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": CONV,
            "step_index": step_index,
            "state": state,
            "step_type": "tool",
            "tool_name": name,
            "tool_info": {"parameters": parameters},
        },
    }


INIT = {
    "event": "init",
    "conversation_id": CONV,
    "init": {"tools": ["search_web", "read_url_content", "view_file"]},
}


# ------------------------------------------------------------------ 경로 판정


def test_this_conversations_fetch_artifact_is_recognized(tmp_path) -> None:
    path = artifact(tmp_path, step=9)
    assert content_artifact_step(str(path), CONV) == 9


def test_other_conversations_artifact_is_rejected(tmp_path) -> None:
    """대화 ID 가 다르면 이전 실행의 페이지다. 이번 실행의 근거가 아니다."""
    path = artifact(tmp_path, conversation="7d8ad235-a9cc-4d11-ac99-d9580bc0c211")
    assert content_artifact_step(str(path), CONV) is None


def test_general_local_file_is_rejected(tmp_path) -> None:
    other = tmp_path / "secrets.env"
    other.write_text("TOKEN=1", encoding="utf-8")
    assert content_artifact_step(str(other), CONV) is None


def test_other_file_inside_the_step_dir_is_rejected(tmp_path) -> None:
    """산출물 디렉터리라도 content.md 가 아니면 페이지 본문이 아니다."""
    path = artifact(tmp_path, step=9)
    sibling = path.parent / "notes.md"
    sibling.write_text("x", encoding="utf-8")
    assert content_artifact_step(str(sibling), CONV) is None


def test_traversal_out_of_the_artifact_dir_is_rejected(tmp_path) -> None:
    path = artifact(tmp_path, step=9)
    escaped = path.parent / ".." / ".." / ".." / ".." / "content.md"
    assert content_artifact_step(str(escaped), CONV) is None


def test_symlink_pointing_outside_is_rejected(tmp_path) -> None:
    """경로는 규칙에 맞아도 링크가 밖을 가리키면 그 파일은 페이지 본문이 아니다."""
    path = artifact(tmp_path, step=9)
    secret = tmp_path / "outside.md"
    secret.write_text("secret", encoding="utf-8")
    link = path.parent / "linked" / "content.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다.")
    assert content_artifact_step(str(link), CONV) is None


def test_missing_conversation_id_rejects_everything(tmp_path) -> None:
    """대화 ID 를 모르면 대조할 기준이 없다. 열어 주지 않는다."""
    path = artifact(tmp_path, step=9)
    assert content_artifact_step(str(path), None) is None


# ------------------------------------------------------------------ 사후 감사


def test_view_file_on_this_runs_fetch_artifact_is_in_scope(tmp_path) -> None:
    path = artifact(tmp_path, step=9)
    parser = AgyStreamParser()
    feed(
        parser,
        [
            INIT,
            tool_step(9, "read_url_content", {"Url": "https://patents.google.com/patent/KR102477584B1/en"}),
            tool_step(13, "view_file", {"AbsolutePath": str(path), "StartLine": 1, "EndLine": 100}),
        ],
    )
    audit_content_reads(parser.state, AGY_WEB_SEARCH)

    fetch, view = parser.state.tool_calls
    assert view["scope_ok"] is True
    assert view["scope"] == "read_url_content:9"
    # 분할 읽기의 범위가 감사 기록에 남아야 한다.
    assert view["input"] == {"path": str(path), "start_line": 1, "end_line": 100}
    # 이 호출이 "본문을 실제로 읽었다"의 유일한 증거다.
    assert fetch["content_read"] is True


def test_view_file_on_a_failed_fetch_step_is_out_of_scope(tmp_path) -> None:
    """열람이 실패한 단계의 파일은 이번 실행이 가져온 페이지가 아니다."""
    path = artifact(tmp_path, step=9)
    parser = AgyStreamParser()
    feed(
        parser,
        [
            INIT,
            tool_step(9, "read_url_content", {"Url": "https://example.invalid/x"}, state="ERROR"),
            tool_step(13, "view_file", {"AbsolutePath": str(path)}),
        ],
    )
    audit_content_reads(parser.state, AGY_WEB_SEARCH)
    assert parser.state.tool_calls[1]["scope_ok"] is False
    assert parser.state.tool_calls[1]["scope"] == "out_of_scope"


def test_view_file_on_a_non_fetch_step_is_out_of_scope(tmp_path) -> None:
    """단계 번호는 맞아도 그 단계가 검색이었으면 페이지 본문이 아니다."""
    path = artifact(tmp_path, step=7)
    parser = AgyStreamParser()
    feed(
        parser,
        [
            INIT,
            tool_step(7, "search_web", {"query": "radar EO/IR"}),
            tool_step(13, "view_file", {"AbsolutePath": str(path)}),
        ],
    )
    audit_content_reads(parser.state, AGY_WEB_SEARCH)
    assert parser.state.tool_calls[1]["scope_ok"] is False


def test_fetch_without_any_read_is_not_marked_as_read(tmp_path) -> None:
    """포인터만 받은 열람은 본문을 읽은 것이 아니다."""
    parser = AgyStreamParser()
    feed(parser, [INIT, tool_step(9, "read_url_content", {"Url": "https://x.invalid/a"})])
    audit_content_reads(parser.state, AGY_WEB_SEARCH)
    assert "content_read" not in parser.state.tool_calls[0]


# ------------------------------------------------------------------ 예산 분리


def test_content_reads_do_not_consume_the_search_budget(tmp_path) -> None:
    """2026-08-25 실행의 실제 호출 구성. 검색 예산으로는 죽지 않아야 한다."""
    calls = (
        [{"name": "search_web"}] * 4
        + [{"name": "read_url_content"}] * 3
        + [{"name": "view_file"}] * 14
    )
    search_calls, content_calls = split_tool_calls(
        calls, AGY_WEB_SEARCH.content_read_tools
    )
    assert (search_calls, content_calls) == (7, 14)
    # 그날 죽은 이유가 이것이었다: 21 > 20.
    assert len(calls) > 20
    assert search_calls <= 20


def test_out_of_scope_reads_still_count_as_content_reads() -> None:
    """위반은 정책 검사에서 잡는다. 검색 예산에 얹어 두 번 벌주지 않는다."""
    calls = [{"name": "search_web"}, {"name": "view_file", "scope_ok": False}]
    assert split_tool_calls(calls, AGY_WEB_SEARCH.content_read_tools) == (1, 1)


# ------------------------------------------------------------------ 정책 판정


def _outcome(**kwargs) -> ExecutionOutcome:
    outcome = ExecutionOutcome(
        result_text="정상 결과", exit_code=0, terminal_reason="completed"
    )
    outcome.tool_policy = AGY_WEB_SEARCH
    for key, value in kwargs.items():
        setattr(outcome, key, value)
    return outcome


def test_in_scope_view_file_passes_the_policy() -> None:
    verdict = evaluate(
        _outcome(
            tool_uses=["search_web", "read_url_content", "view_file"],
            tool_calls=[
                {"name": "search_web"},
                {"name": "read_url_content"},
                {"name": "view_file", "scope_ok": True},
            ],
        )
    )
    assert verdict.status == JobStatus.SUCCEEDED


def test_out_of_scope_view_file_violates_the_policy() -> None:
    verdict = evaluate(
        _outcome(
            tool_uses=["search_web", "view_file"],
            tool_calls=[
                {"name": "search_web"},
                {"name": "view_file", "scope_ok": False},
            ],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION
    assert "view_file" in verdict.errors[0]


def test_unjudged_view_file_violates_the_policy() -> None:
    """판정이 없으면 통과시키지 않는다(fail-closed)."""
    verdict = evaluate(
        _outcome(
            tool_uses=["search_web", "view_file"],
            tool_calls=[{"name": "search_web"}, {"name": "view_file"}],
        )
    )
    assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION


def test_scope_does_not_open_other_tools() -> None:
    """열린 것은 view_file 의 한 경로뿐이다. 셸·파일 쓰기는 그대로 위반이다."""
    for name in ("run_command", "write_to_file", "replace_file_content"):
        verdict = evaluate(
            _outcome(
                tool_uses=["search_web", name],
                tool_calls=[
                    {"name": "search_web"},
                    # scope_ok 를 참으로 적어 넣어도 열리지 않아야 한다.
                    {"name": name, "scope_ok": True},
                ],
            )
        )
        assert verdict.error_code == ErrorCode.TOOL_POLICY_VIOLATION, name


def test_content_read_budget_is_reported_separately() -> None:
    """사용자가 받아야 할 지시가 다르다: 검색이 아니라 문헌 수를 줄여야 한다."""
    verdict = evaluate(
        _outcome(
            cancelled=True,
            content_read_budget_exceeded=True,
            tool_uses=["search_web", "view_file"],
            tool_calls=[
                {"name": "search_web"},
                {"name": "view_file", "scope_ok": True},
            ],
        )
    )
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.SEARCH_BUDGET_EXCEEDED
    assert "본문 읽기" in verdict.errors[-1]





def test_agy_context_tells_the_model_that_fetch_returns_a_path() -> None:
    """이걸 안 알려주면 모델은 포인터를 받고 "페이지를 봤다"고 착각한다.

    2026-08-25 06:34 실행이 그랬다 — 5건을 가져와 2건만 읽었고, 읽지 않은 3건에
    쓴 대응표를 ARIA 가 통째로 버렸다.
    """
    from app.config import AGY_SEARCH_RUNTIME_CONTEXT as text

    assert "content.md" in text
    assert "view_file" in text
    assert "가져오기만 하고 읽지 않은" in text
    # Claude 도구 이름이 남아 있으면 모델이 없는 도구를 부른다.
    assert "WebFetch" not in text
    assert "WebSearch" not in text


def test_agy_context_keeps_model_explanations_separate_from_quotes():
    from app.config import AGY_SEARCH_RUNTIME_CONTEXT as text
    assert "직접 인용을 주장하지" in text
    assert "기술적 설명을 남길 수" in text


def test_agy_context_does_not_demand_reading_every_candidate() -> None:
    """모든 후보를 열라고 하면 도구 호출이 폭증하고 예산이 마른다.

    열지 못한 후보를 미확인 단서로 남기는 것은 정상 동작이다.
    """
    from app.config import AGY_SEARCH_RUNTIME_CONTEXT as text

    assert "모든 후보를 다 열어야 한다는 뜻이 아닙니다" in text


def test_agy_context_contains_no_other_providers_native_tool_names():
    from app.config import AGY_SEARCH_RUNTIME_CONTEXT as text
    assert "WebSearch" not in text and "WebFetch" not in text
