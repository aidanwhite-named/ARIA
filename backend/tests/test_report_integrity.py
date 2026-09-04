"""job d39dc2cc 에서 확인된 네 가지 회귀에 대한 테스트.

각 테스트는 그 실행에서 실제로 관측된 값을 재현한다.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app import analysis_completeness, analysis_manifest, report_symbols
from app.enums import ErrorCode, JobStatus
from app.evaluation.evaluator import evaluate
from app.execution import process as proc
from app.providers.base import AGY_WEB_SEARCH, ExecutionOutcome


# --------------------------------------------------------------------- (1) 종료

def _python(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


# 최종 결과를 낸 뒤 stdout 을 열어 둔 채 살아 있는 CLI 를 흉내 낸다.
_LINGERING = """
import sys, time
sys.stdout.write('{"event":"result","result":{"status":"SUCCESS","response":"done"}}\\n')
sys.stdout.flush()
time.sleep(60)
"""

_SILENT = """
import time
time.sleep(60)
"""


@pytest.mark.parametrize("grace", [0.2])
def test_completion_signal_stops_waiting_for_a_lingering_cli(tmp_path, grace):
    """최종 결과를 받으면 프로세스가 안 죽어도 타임아웃으로 끝나지 않는다."""
    signal = asyncio.Event()
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)
        if '"result"' in line:
            signal.set()

    async def go() -> proc.ProcessResult:
        return await proc.run_streaming(
            job_id="test-lingering",
            argv=_python(_LINGERING),
            cwd=tmp_path,
            env=None,
            on_stdout_line=on_line,
            timeout_seconds=30,
            completion_signal=signal,
            completion_grace_seconds=grace,
        )

    result = asyncio.run(go())
    assert result.completed_without_exit is True
    assert result.timed_out is False
    assert any('"result"' in line for line in seen)


def test_real_timeout_without_a_final_result_stays_a_timeout(tmp_path):
    """최종 응답이 없는 실행은 예전 그대로 타임아웃이다."""
    signal = asyncio.Event()

    async def go() -> proc.ProcessResult:
        return await proc.run_streaming(
            job_id="test-silent",
            argv=_python(_SILENT),
            cwd=tmp_path,
            env=None,
            timeout_seconds=1,
            completion_signal=signal,
            completion_grace_seconds=0.2,
        )

    result = asyncio.run(go())
    assert result.timed_out is True
    assert result.completed_without_exit is False


def test_lingering_success_is_not_failed_as_timeout():
    outcome = ExecutionOutcome(
        result_text="보고서 본문",
        terminal_reason="SUCCESS",
        completed_without_exit=True,
    )
    verdict = evaluate(outcome, [], fail_on_tool_use=True)
    assert verdict.status is JobStatus.SUCCEEDED


def test_lingering_does_not_swallow_a_tool_policy_violation():
    """SUCCESS 문자열 하나로 정책 위반을 덮지 않는다."""
    outcome = ExecutionOutcome(
        result_text="보고서 본문",
        terminal_reason="SUCCESS",
        completed_without_exit=True,
        tool_policy=AGY_WEB_SEARCH,
        tool_uses=["run_command"],
        tool_calls=[{"name": "run_command"}],
    )
    verdict = evaluate(outcome, [], fail_on_tool_use=False)
    assert verdict.status is JobStatus.FAILED
    assert verdict.error_code is ErrorCode.TOOL_POLICY_VIOLATION


def test_lingering_does_not_swallow_auth_or_cancel():
    auth = evaluate(
        ExecutionOutcome(
            result_text="x", terminal_reason="SUCCESS",
            completed_without_exit=True, auth_required=True,
        ),
        [],
    )
    assert auth.error_code is ErrorCode.AUTH_REQUIRED

    cancelled = evaluate(
        ExecutionOutcome(
            result_text="x", terminal_reason="SUCCESS",
            completed_without_exit=True, cancelled=True,
        ),
        [],
    )
    assert cancelled.status is JobStatus.CANCELLED


def test_real_timeout_still_fails():
    verdict = evaluate(ExecutionOutcome(timed_out=True), [])
    assert verdict.error_code is ErrorCode.TIMED_OUT


# ------------------------------------------------------------------- (3) 심볼

# job d39dc2cc 의 Master Prompt 에서 그대로 가져온 등급표와 형식 지정.
_LEGEND_PROMPT = """## 유사도

- `95~100%: 동일 대응 🔵`
- `80~94%: 실질 대응 🟢`
- `1~79%: 부분 대응 🟡`
- `0%: 대응 없음—확인 범위 기준 ⚪`

형식: `대응 정도: [등급명] (XX%)`
"""


def test_legend_is_read_from_the_prompt():
    legend = report_symbols.parse_legend(_LEGEND_PROMPT)
    assert [(entry.low, entry.high, entry.symbol) for entry in legend] == [
        (95, 100, "🔵"),
        (80, 94, "🟢"),
        (1, 79, "🟡"),
        (0, 0, "⚪"),
    ]


@pytest.mark.parametrize(
    "label,value,symbol",
    [
        ("실질 대응", 90, "🟢"),
        ("실질 대응", 92, "🟢"),
        ("동일 대응", 97, "🔵"),
        ("부분 대응", 40, "🟡"),
        ("대응 없음—확인 범위 기준", 0, "⚪"),
    ],
)
def test_symbol_is_restored_without_changing_the_score(label, value, symbol):
    body = f"`대응 정도: {label} ({value}%)`"
    out = report_symbols.apply(body, _LEGEND_PROMPT)
    assert symbol in out
    assert f"({value}%)" in out
    assert label in out


def test_existing_symbol_is_left_alone():
    body = "`대응 정도: 실질 대응 🟢 (90%)`"
    assert report_symbols.apply(body, _LEGEND_PROMPT) == body


def test_no_legend_means_no_change():
    body = "`대응 정도: 실질 대응 (90%)`"
    assert report_symbols.apply(body, "등급표가 없는 프롬프트") == body


def test_body_outside_the_grade_line_is_untouched():
    body = (
        "코드 `x = (90%)` 와 표 | 90% | 는 그대로다.\n"
        "`대응 정도: 부분 대응 (40%)`\n"
    )
    out = report_symbols.apply(body, _LEGEND_PROMPT)
    assert "코드 `x = (90%)` 와 표 | 90% | 는 그대로다." in out
    assert "🟡" in out
    assert "🟢" not in out


# -------------------------------------------------------------- (4) 완전성

# job d39dc2cc 의 실제 형태.
_RETRIEVAL = {
    "status": "partial",
    "budget_exhausted": True,
    "budget": {"max_rounds": 5},
    "rounds": [{"round": n} for n in range(1, 6)],
    "deferred_pending": [{"action": "search_document"}] * 20,
    "components": [
        {"id": "R001", "label": "청구항 12 (전제부)", "search_completeness": "limited"},
        {"id": "R002", "label": "청구항 12 (A)"},
        {"id": "R003", "label": "청구항 12 (B)"},
        {"id": "R004", "label": "청구항 12 (C)"},
        {"id": "R005", "label": "청구항 12 (D)"},
        {"id": "R006", "label": "청구항 12 (E)"},
    ],
}
_ANALYSIS = {
    "items": [
        {"claim": "청구항 12", "symbol": f"({s})", "status": "matched",
         "similarity": v, "basis": basis}
        for s, v, basis in [
            ("A", 90, "direct"), ("B", 92, "direct"), ("C", 88, "inferred"),
            ("D", 91, "inferred"), ("E", 89, "inferred"),
        ]
    ]
}


def test_process_success_is_not_analysis_completeness():
    result = analysis_completeness.check(
        retrieval_manifest=_RETRIEVAL,
        analysis_manifest=_ANALYSIS,
        process_succeeded=True,
    )
    assert result["process_succeeded"] is True
    assert result["manifest_parsed"] is True
    assert result["complete"] is False


def test_declared_component_missing_from_the_report_is_reported():
    result = analysis_completeness.check(
        retrieval_manifest=_RETRIEVAL, analysis_manifest=_ANALYSIS
    )
    assert result["missing_components"] == ["청구항 12 (전제부)"]
    assert result["comparable"] is True
    notice = analysis_completeness.render(result)
    assert "청구항 12 (전제부)" in notice
    assert "미처리 검색 요청 20건" in notice


def test_inferred_components_are_named_without_touching_scores():
    result = analysis_completeness.check(
        retrieval_manifest=_RETRIEVAL, analysis_manifest=_ANALYSIS
    )
    assert result["inferred_components"] == [
        "청구항 12 (C)", "청구항 12 (D)", "청구항 12 (E)"
    ]
    # 점수는 그대로다.
    assert [row["similarity"] for row in _ANALYSIS["items"]] == [90, 92, 88, 91, 89]


def test_no_false_missing_when_naming_schemes_differ():
    result = analysis_completeness.check(
        retrieval_manifest={"components": [{"label": "구성 1"}, {"label": "구성 2"}]},
        analysis_manifest={"items": [{"claim": "청구항 3", "symbol": "(A)"}]},
    )
    assert result["missing_components"] == []
    assert result["comparable"] is False
    assert "구성 단위 대조를 하지 못했습니다" in analysis_completeness.render(result)


def test_complete_run_gets_no_notice():
    result = analysis_completeness.check(
        retrieval_manifest={
            "status": "ok",
            "components": [{"label": "청구항 1 (A)"}],
            "rounds": [{"round": 1}],
        },
        analysis_manifest={
            "items": [
                {"claim": "청구항 1", "symbol": "(A)", "status": "matched",
                 "basis": "direct"}
            ]
        },
    )
    assert result["complete"] is True
    assert analysis_completeness.render(result) == ""


def test_basis_is_optional_and_validated():
    text = (
        "[ARIA_COMPONENT_ANALYSIS_V1]\n"
        '{"items":[{"claim":"청구항 1","symbol":"(A)","feature":"f",'
        '"similarity":90,"status":"matched"}]}\n'
        "[/ARIA_COMPONENT_ANALYSIS_V1]"
    )
    assert analysis_manifest.parse(text)["items"][0]["basis"] == ""

    bad = text.replace('"status":"matched"', '"status":"matched","basis":"maybe"')
    with pytest.raises(analysis_manifest.ComponentAnalysisError):
        analysis_manifest.parse(bad)


# ------------------------------------------------------ (3b) 등급명·점수 불일치

def test_symbol_is_not_attached_when_label_and_score_disagree():
    body = "`대응 정도: 부분 대응 (90%)`"
    assert report_symbols.apply(body, _LEGEND_PROMPT) == body


def test_matching_label_and_score_still_gets_a_symbol():
    body = "`대응 정도: 부분 대응 (40%)`"
    assert "🟡" in report_symbols.apply(body, _LEGEND_PROMPT)


def test_unknown_label_is_scored_by_range_not_rejected():
    """등급표에 없는 이름은 불일치가 아니다. 구간으로 판단한다."""
    body = "`대응 정도: 매우 유사 (90%)`"
    assert "🟢" in report_symbols.apply(body, _LEGEND_PROMPT)


def test_completeness_is_derivable_and_json_safe():
    """저장하지 않고 조회 시점에 계산한다. 입력은 두 매니페스트뿐이다."""
    import json

    result = analysis_completeness.check(
        retrieval_manifest=_RETRIEVAL, analysis_manifest=_ANALYSIS
    )
    reloaded = json.loads(json.dumps(result))
    assert reloaded["missing_components"] == ["청구항 12 (전제부)"]
    assert reloaded["scope"]["pending_actions"] == 20
