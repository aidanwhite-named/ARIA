"""ResultEvaluator.

프로세스 exit code 만으로 성공을 판정하지 않는다.

이건 이론이 아니다. 실제로 이 PC 에서 Claude CLI 를 미로그인 상태로 실행하면
아래를 돌려준다.

  {"type":"result", "subtype":"success", "terminal_reason":"completed",
   "permission_denials":[], "is_error":true,
   "result":"Not logged in · Please run /login"}

subtype 은 success, terminal_reason 은 completed, 종료 코드도 정상이다.
is_error 를 보지 않으면 성공으로 오판한다.

v0.1 은 도구를 끄고 첨부를 인라인으로 전달하므로 "필수 파일을 못 읽었다"는
실패는 모델 동작이 아니라 전처리 단계에서 확정된다. 모델이 "파일을 읽었다"고
말한 것을 신뢰할 필요 자체가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..enums import DeliveryMode, ErrorCode, JobStatus
from ..ingestion.service import IngestedFile
from ..providers.base import ExecutionOutcome


@dataclass
class Verdict:
    status: str
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def evaluate(
    outcome: ExecutionOutcome,
    attachments: list[IngestedFile] | None = None,
    output_mode: str = "markdown",
    fail_on_tool_use: bool = True,
) -> Verdict:
    attachments = attachments or []
    warnings = list(outcome.warnings)
    errors = list(outcome.errors)

    # --- 종료 상태가 먼저다 -------------------------------------------------
    if outcome.cancelled:
        return Verdict(JobStatus.CANCELLED, ErrorCode.CANCELLED, warnings, errors)

    if outcome.timed_out:
        errors.append("실행 제한 시간을 초과했습니다.")
        return Verdict(JobStatus.FAILED, ErrorCode.TIMED_OUT, warnings, errors)

    # --- 인증/사용량은 exit code 로 드러나지 않는다 --------------------------
    if outcome.auth_required:
        errors.append(
            "CLI 에 로그인되어 있지 않습니다. 별도 터미널에서 로그인한 뒤 "
            "Settings 에서 다시 검사하십시오."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.AUTH_REQUIRED, warnings, errors)

    if outcome.rate_limited:
        errors.append("Provider 사용량 제한에 도달했습니다. 잠시 후 다시 시도하십시오.")
        return Verdict(JobStatus.FAILED, ErrorCode.RATE_LIMITED, warnings, errors)

    # --- 도구 정책 위반 -----------------------------------------------------
    # v0.1 에서 '도구 없음'은 편의 설정이 아니라 보안 불변조건이다.
    # 결과가 멀쩡해 보여도 정책이 깨졌으면 실패로 처리한다(fail-closed).
    if outcome.tools_must_be_disabled and outcome.tools_advertised:
        errors.append(
            "도구를 비활성화하고 실행했는데 Provider 가 도구를 노출했습니다: "
            + ", ".join(outcome.tools_advertised[:10])
        )
        return Verdict(
            JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, warnings, errors
        )

    if outcome.tool_uses and (
        outcome.tools_must_be_disabled
        or outcome.tools_uncontrollable
        or fail_on_tool_use
    ):
        errors.append(
            "실행 중 도구가 호출되었습니다: "
            + ", ".join(sorted(set(outcome.tool_uses))[:10])
            + ". ARIA 는 첨부 자료를 프롬프트에 직접 넣어 전달하므로 도구 호출이 필요하지 않습니다."
        )
        return Verdict(
            JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, warnings, errors
        )

    if outcome.tool_uses:
        warnings.append(
            "도구가 호출되었습니다: " + ", ".join(sorted(set(outcome.tool_uses))[:10])
        )

    # --- 프로세스 자체가 실패한 경우 ---------------------------------------
    if outcome.error_message and not outcome.result_text.strip():
        errors.append(outcome.error_message)
        return Verdict(JobStatus.FAILED, ErrorCode.PROCESS_ERROR, warnings, errors)

    # --- 필수 첨부가 전달되지 않은 경우 -------------------------------------
    missing_required = [
        a
        for a in attachments
        if a.required and (not a.read_ok or a.delivery_mode != DeliveryMode.INLINE_CONTEXT)
    ]
    if missing_required:
        names = ", ".join(a.original_filename for a in missing_required)
        errors.append(f"필수 첨부 자료를 전달하지 못했습니다: {names}")
        return Verdict(JobStatus.FAILED, ErrorCode.ATTACHMENT_ERROR, warnings, errors)

    # --- 모델이 오류를 보고한 경우 ------------------------------------------
    if outcome.is_error:
        message = outcome.error_message or "Provider 가 오류를 보고했습니다."
        if message not in errors:
            errors.append(message)
        return Verdict(JobStatus.FAILED, ErrorCode.PROCESS_ERROR, warnings, errors)

    # --- exit code 0 이지만 결과가 비어 있는 경우 ---------------------------
    if not outcome.result_text.strip():
        errors.append(
            "실행은 정상 종료했지만 결과 텍스트가 비어 있습니다."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.EMPTY_RESULT, warnings, errors)

    # --- 여기부터는 성공. 경고만 모은다 -------------------------------------
    if outcome.exit_code not in (0, None):
        warnings.append(
            f"결과는 정상이지만 종료 코드가 {outcome.exit_code} 입니다."
        )

    optional_failed = [
        a
        for a in attachments
        if not a.required and (not a.read_ok or a.delivery_mode != DeliveryMode.INLINE_CONTEXT)
    ]
    if optional_failed:
        names = ", ".join(a.original_filename for a in optional_failed)
        warnings.append(f"선택 첨부 자료를 전달하지 못했습니다: {names}")

    if outcome.permission_denials:
        warnings.append(
            f"권한이 거부된 비필수 도구 호출이 {len(outcome.permission_denials)}건 있습니다."
        )

    if outcome.usage is None:
        warnings.append("Provider 가 사용량 정보를 제공하지 않았습니다.")

    if output_mode == "text" and outcome.result_text.strip().startswith("#"):
        warnings.append(
            "출력 형식이 text 인데 결과가 Markdown 제목으로 시작합니다."
        )

    return Verdict(JobStatus.SUCCEEDED, None, warnings, errors)
