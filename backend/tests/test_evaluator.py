"""ResultEvaluator.

exit code 만으로 성공을 판정하지 않는다는 규칙을 케이스별로 고정한다.
"""

from __future__ import annotations

from app.enums import DeliveryMode, ErrorCode, JobStatus
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


def test_exit_zero_but_empty_result_is_failure() -> None:
    outcome = ExecutionOutcome(result_text="   ", exit_code=0, terminal_reason="completed")
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.EMPTY_RESULT


def test_provider_truncation_marker_is_failure() -> None:
    # agy 가 큰 입력을 자르면 뒷부분을 `<truncated N bytes>` 로 대체한다. 그
    # 마커가 출력에 남으면, 최종 답변이 정상 분석처럼 보여도(가장 위험한 경우)
    # 성공으로 넘기지 않는다.
    outcome = ok_outcome("정상적으로 보이는 구성대비 분석 결과입니다.")
    outcome.raw_stdout = (
        '{"event":"result","result":{"status":"SUCCESS",'
        '"response":"... database 130 of remote serve <truncated 548974 bytes>"}}'
    )
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.INPUT_TOO_LARGE


def test_clean_output_without_truncation_marker_stays_success() -> None:
    # 마커가 없으면 raw_stdout 이 있어도 오탐하지 않는다.
    outcome = ok_outcome("정상 분석 결과")
    outcome.raw_stdout = '{"event":"result","result":{"status":"SUCCESS"}}'
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


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


def test_optional_attachment_failure_does_not_fail() -> None:
    """필수로 표시하지 않은 자료는 전달되지 않아도 실행을 세우지 않는다."""
    verdict = evaluate(
        ok_outcome(),
        [attachment(required=True, ok=True, name="a.txt"), attachment(required=False, ok=False, name="b.pdf")],
    )
    assert verdict.status == JobStatus.SUCCEEDED
    assert verdict.error_code is None


def test_permission_denials_alone_do_not_fail() -> None:
    """결과가 정상이고 필수 자료도 전달됐다면 비필수 도구 거부로 세우지 않는다."""
    outcome = ok_outcome()
    outcome.permission_denials = [{"tool": "WebFetch"}]
    verdict = evaluate(outcome, [attachment(required=True, ok=True)])
    assert verdict.status == JobStatus.SUCCEEDED


def test_missing_usage_does_not_fail() -> None:
    outcome = ok_outcome()
    outcome.usage = None
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED


def test_nonzero_exit_with_valid_result_is_not_a_failure() -> None:
    outcome = ok_outcome()
    outcome.exit_code = 3
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.SUCCEEDED


def test_launch_failure_with_no_output_is_process_error() -> None:
    outcome = ExecutionOutcome(result_text="", error_message="실행 파일을 찾지 못했습니다")
    verdict = evaluate(outcome)
    assert verdict.status == JobStatus.FAILED
    assert verdict.error_code == ErrorCode.PROCESS_ERROR
