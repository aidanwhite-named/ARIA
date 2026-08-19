"""ResultEvaluator.

exit code 만으로 성공을 판정하지 않는다는 규칙을 케이스별로 고정한다.
"""

from __future__ import annotations

from app.enums import DeliveryMode, ErrorCode, JobStatus, derive_quality
from app.evaluation.evaluator import evaluate
from app.ingestion.service import IngestedFile
from app.providers.base import ExecutionOutcome


def attachment(required: bool, ok: bool, name: str = "a.txt") -> IngestedFile:
    return IngestedFile(
        attachment_id=name,
        original_filename=name,
        internal_filename=name,
        mime_type="text/plain",
        size_bytes=10,
        sha256="0" * 64,
        required=required,
        stored_path=f"/tmp/{name}",
        read_ok=ok,
        delivery_mode=DeliveryMode.INLINE_CONTEXT if ok else DeliveryMode.UNSUPPORTED,
        error=None if ok else "추출 실패",
    )


def ok_outcome(text: str = "분석 결과") -> ExecutionOutcome:
    return ExecutionOutcome(
        result_text=text, exit_code=0, terminal_reason="completed", usage={"input_tokens": 1}
    )


def test_plain_success() -> None:
    verdict = evaluate(ok_outcome())
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None
    assert derive_quality(verdict.status, verdict.warnings) == "SUCCESS"


def test_exit_zero_but_empty_result_is_failure() -> None:
    outcome = ExecutionOutcome(result_text="   ", exit_code=0, terminal_reason="completed")
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.EMPTY_RESULT


def test_exit_zero_but_is_error_is_failure() -> None:
    outcome = ok_outcome()
    outcome.is_error = True
    outcome.error_message = "모델이 오류를 보고함"
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.PROCESS_ERROR


def test_auth_required_wins_over_everything() -> None:
    outcome = ExecutionOutcome(
        result_text="Not logged in · Please run /login",
        exit_code=0,
        terminal_reason="completed",
        is_error=True,
        auth_required=True,
    )
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.AUTH_REQUIRED


def test_rate_limited() -> None:
    outcome = ExecutionOutcome(result_text="limit", exit_code=0, rate_limited=True, is_error=True)
    assert evaluate(outcome).error_code == ErrorCode.RATE_LIMITED


def test_timeout() -> None:
    outcome = ExecutionOutcome(result_text="일부 결과", timed_out=True)
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.TIMED_OUT


def test_cancelled_is_not_failed() -> None:
    outcome = ExecutionOutcome(result_text="", cancelled=True)
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.CANCELLED
    assert verdict.error_code == ErrorCode.CANCELLED


def test_required_attachment_failure_is_failure() -> None:
    verdict = evaluate(ok_outcome(), [attachment(required=True, ok=False, name="필수.pdf")])
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.ATTACHMENT_ERROR
    assert "필수.pdf" in " ".join(verdict.errors)


def test_optional_attachment_failure_is_only_a_warning() -> None:
    verdict = evaluate(
        ok_outcome(),
        [attachment(required=True, ok=True, name="a.txt"), attachment(required=False, ok=False, name="b.pdf")],
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert derive_quality(verdict.status, verdict.warnings) == "SUCCESS_WITH_WARNINGS"
    assert any("b.pdf" in w for w in verdict.warnings)


def test_permission_denials_alone_do_not_fail() -> None:
    """결과가 정상이고 필수 자료도 전달됐다면 비필수 도구 거부는 경고다."""
    outcome = ok_outcome()
    outcome.permission_denials = [{"tool": "WebFetch"}]
    verdict = evaluate(outcome, [attachment(required=True, ok=True)])
    assert verdict.status == JobStatus.SUCCEEDED
    assert any("권한이 거부된" in w for w in verdict.warnings)


def test_missing_usage_is_a_warning() -> None:
    outcome = ok_outcome()
    outcome.usage = None
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED
    assert any("사용량" in w for w in verdict.warnings)


def test_nonzero_exit_with_valid_result_is_warning_not_failure() -> None:
    outcome = ok_outcome()
    outcome.exit_code = 3
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED
    assert any("종료 코드" in w for w in verdict.warnings)


def test_launch_failure_with_no_output_is_process_error() -> None:
    outcome = ExecutionOutcome(result_text="", error_message="실행 파일을 찾지 못했습니다")
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.PROCESS_ERROR


def test_text_mode_markdown_heading_warns() -> None:
    verdict = evaluate(ok_outcome("# 제목\n본문"), [], output_mode="text")
    assert verdict.status == JobStatus.SUCCEEDED
    assert any("Markdown" in w for w in verdict.warnings)


def test_provider_warnings_are_carried_through() -> None:
    outcome = ok_outcome()
    outcome.warnings.append("스트림 3줄을 해석하지 못했습니다.")
    verdict = evaluate(outcome)
    assert "스트림 3줄을 해석하지 못했습니다." in verdict.warnings


def test_derive_quality_is_none_for_non_success() -> None:
    assert derive_quality(JobStatus.FAILED, []) is None
    assert derive_quality(JobStatus.CANCELLED, ["w"]) is None
