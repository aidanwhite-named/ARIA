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

import re
from dataclasses import dataclass, field

from ..enums import DeliveryMode, ErrorCode, JobStatus
from ..ingestion.service import IngestedFile
from ..providers.base import NO_TOOLS, ExecutionOutcome, ToolPolicy

# Provider 가 입력을 잘랐을 때 삽입하는 마커. agy 는 `<truncated 548974 bytes>`
# 형태로 남긴다. 이 정확한 형태만 잡아 오탐(문서에 우연히 든 유사 문구)을 줄인다.
_PROVIDER_TRUNCATION = re.compile(r"<truncated\s+\d+\s+bytes>")


@dataclass
class Verdict:
    status: str
    error_code: str | None = None
    errors: list[str] = field(default_factory=list)


def effective_policy(outcome: ExecutionOutcome) -> ToolPolicy | None:
    """이 결과를 어떤 도구 정책으로 판정할 것인가.

    Provider 가 정책을 선언했으면 그것을 쓴다. 선언하지 않은 Provider 는 예전
    계약(tools_must_be_disabled 불리언)으로 판정한다. 둘 다 없으면 정책이 없는
    실행이며, 이때만 전역 fail_on_tool_use 설정이 개입한다.
    """
    if outcome.tool_policy is not None:
        return outcome.tool_policy
    if outcome.tools_must_be_disabled:
        return NO_TOOLS
    return None


def evaluate(
    outcome: ExecutionOutcome,
    attachments: list[IngestedFile] | None = None,
    fail_on_tool_use: bool = True,
) -> Verdict:
    attachments = attachments or []
    errors = list(outcome.errors)
    policy = effective_policy(outcome)

    # --- 종료 상태가 먼저다 -------------------------------------------------
    # 도구 상한을 넘겨 ARIA 가 프로세스를 끊은 경우에도 cancelled 는 참이다.
    # 그건 사용자가 누른 취소가 아니므로 여기서 삼키지 않고 아래로 넘긴다.
    if outcome.cancelled and not outcome.tool_budget_exceeded:
        return Verdict(JobStatus.CANCELLED, ErrorCode.CANCELLED, errors)

    if outcome.timed_out:
        errors.append("실행 제한 시간을 초과했습니다.")
        return Verdict(JobStatus.FAILED, ErrorCode.TIMED_OUT, errors)

    # --- 인증/사용량은 exit code 로 드러나지 않는다 --------------------------
    if outcome.auth_required:
        errors.append(
            "CLI 에 로그인되어 있지 않습니다. 별도 터미널에서 로그인한 뒤 "
            "Settings 에서 다시 검사하십시오."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.AUTH_REQUIRED, errors)

    if outcome.rate_limited:
        errors.append("Provider 사용량 제한에 도달했습니다. 잠시 후 다시 시도하십시오.")
        return Verdict(JobStatus.FAILED, ErrorCode.RATE_LIMITED, errors)

    # --- 도구 정책 위반 -----------------------------------------------------
    # '도구 없음'은 편의 설정이 아니라 보안 불변조건이다. 결과가 멀쩡해 보여도
    # 정책이 깨졌으면 실패로 처리한다(fail-closed).
    #
    # 허용 목록이 있는 정책(검색)에서도 fail-closed 는 그대로다. 목록 밖의
    # 도구는 광고되기만 해도 위반이다. 전역 fail_on_tool_use 는 여기에 개입하지
    # 못한다 — 그 설정은 정책을 선언하지 않은 Provider 에만 남는 마지막 방어선
    # 이며, 이걸 꺼서 검색 작업의 허용 목록을 넓힐 수 있으면 안 된다.
    if policy is not None and policy.tools_disabled:
        if outcome.tools_advertised:
            errors.append(
                "도구를 비활성화하고 실행했는데 Provider 가 도구를 노출했습니다: "
                + ", ".join(outcome.tools_advertised[:10])
            )
            return Verdict(JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, errors)
        if outcome.tool_uses:
            # 선언된 정책이 판단 근거다. 전역 설정으로 완화할 수 없다.
            errors.append(
                "실행 중 도구가 호출되었습니다: "
                + ", ".join(sorted(set(outcome.tool_uses))[:10])
                + ". ARIA 는 첨부 자료를 프롬프트에 직접 넣어 전달하므로 "
                "도구 호출이 필요하지 않습니다."
            )
            return Verdict(JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, errors)
    elif policy is not None:
        # Claude 는 --tools 로 노출 목록을 강제하므로 광고 단계부터 검사한다.
        # agy 는 모든 도구를 항상 광고하고 이를 줄일 CLI 플래그가 없다. agy 전용
        # 정책에서는 이 검사를 건너뛰되, 아래 실제 호출 검사는 그대로 적용한다.
        if policy.enforce_advertised_allowlist:
            stray_advertised = policy.unexpected(outcome.tools_advertised)
            if stray_advertised:
                errors.append(
                    f"{policy.name} 정책은 "
                    + ", ".join(policy.allowed_tools)
                    + " 만 허용하는데 Provider 가 다른 도구를 노출했습니다: "
                    + ", ".join(stray_advertised[:10])
                )
                return Verdict(JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, errors)
        stray_used = policy.unexpected(outcome.tool_uses)
        if stray_used:
            errors.append(
                "허용되지 않은 도구가 호출되었습니다: "
                + ", ".join(stray_used[:10])
                + f". {policy.name} 정책은 "
                + ", ".join(policy.allowed_tools)
                + " 만 허용합니다."
            )
            return Verdict(JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, errors)

    # --- 도구 호출 상한 초과 ------------------------------------------------
    # 정책 위반보다 뒤에 둔다. 상한을 넘긴 실행이 허용 목록도 깼다면, 사용자가
    # 알아야 할 것은 "많이 불렀다"가 아니라 "부르면 안 되는 것을 불렀다"이다.
    if outcome.tool_budget_exceeded:
        limit = policy.max_tool_calls if policy else 0
        errors.append(
            f"도구 호출이 상한({limit}회)을 넘어 실행을 중단했습니다. "
            "검색 범위를 좁혀서 다시 시도하십시오."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.SEARCH_BUDGET_EXCEEDED, errors)

    # 정책을 선언하지 않은 Provider. 도구를 끌 수단이 없으므로 ARIA 는 호출을
    # 탐지할 뿐 막지 못한다. 여기서만 전역 설정이 개입한다 — 사용자가 완화할 수
    # 있는 것은 이 마지막 경로뿐이고, 위의 정책 검사에는 닿지 않는다.
    if policy is None and outcome.tool_uses and (
        outcome.tools_uncontrollable or fail_on_tool_use
    ):
        errors.append(
            "실행 중 도구가 호출되었습니다: "
            + ", ".join(sorted(set(outcome.tool_uses))[:10])
            + ". ARIA 는 첨부 자료를 프롬프트에 직접 넣어 전달하므로 도구 호출이 필요하지 않습니다."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.TOOL_POLICY_VIOLATION, errors)

    # --- 프로세스 자체가 실패한 경우 ---------------------------------------
    if outcome.error_message and not outcome.result_text.strip():
        errors.append(outcome.error_message)
        return Verdict(JobStatus.FAILED, ErrorCode.PROCESS_ERROR, errors)

    # --- 필수 첨부가 전달되지 않은 경우 -------------------------------------
    missing_required = [
        a
        for a in attachments
        if a.required and (not a.read_ok or a.delivery_mode != DeliveryMode.INLINE_CONTEXT)
    ]
    if missing_required:
        names = ", ".join(a.original_filename for a in missing_required)
        errors.append(f"필수 첨부 자료를 전달하지 못했습니다: {names}")
        return Verdict(JobStatus.FAILED, ErrorCode.ATTACHMENT_ERROR, errors)

    # --- 모델이 오류를 보고한 경우 ------------------------------------------
    if outcome.is_error:
        message = outcome.error_message or "Provider 가 오류를 보고했습니다."
        if message not in errors:
            errors.append(message)
        return Verdict(JobStatus.FAILED, ErrorCode.PROCESS_ERROR, errors)

    # --- 검색 작업인데 검색을 하지 않은 경우 --------------------------------
    # 모델이 도구를 부르지 않고 기억만으로 문헌 목록을 쓸 수 있다. 그 결과는
    # 형식상 완벽해 보이지만 검색 결과가 아니고, 공개번호가 실재하는지조차
    # 확인된 바 없다. 프로세스/인증 오류를 먼저 거른 뒤 여기서 잡는다.
    if policy is not None and policy.required_tools:
        used = set(outcome.tool_uses)
        if not used.intersection(policy.required_tools):
            errors.append(
                "검색 도구를 한 번도 호출하지 않았습니다("
                + ", ".join(policy.required_tools)
                + " 중 최소 1회 필요). 이 결과는 웹 검색으로 확인된 내용이 아니라 "
                "모델이 기억에서 작성한 것이므로 검토 후보로 쓸 수 없습니다."
            )
            return Verdict(JobStatus.FAILED, ErrorCode.SEARCH_NOT_PERFORMED, errors)

    # --- exit code 0 이지만 결과가 비어 있는 경우 ---------------------------
    if not outcome.result_text.strip():
        errors.append(
            "실행은 정상 종료했지만 결과 텍스트가 비어 있습니다."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.EMPTY_RESULT, errors)

    # --- Provider 가 입력을 조용히 자른 흔적 --------------------------------
    # agy 처럼 큰 입력을 `<truncated N bytes>` 로 대체하는 Provider 는 종료 코드
    # 0 에 답변까지 내놓지만, 그 답변은 앞부분만 보고 쓴 것이다. 사전 바이트
    # 검사(runner)가 1차 방어지만, 그것을 빠져나간 경우의 안전망으로 출력에 남은
    # 마커를 잡아 성공으로 넘기지 않는다.
    if outcome.raw_stdout and _PROVIDER_TRUNCATION.search(outcome.raw_stdout):
        errors.append(
            "Provider 가 입력을 잘랐습니다(<truncated ... bytes>). 앞부분만 모델에 "
            "전달되어 나머지 자료가 분석에서 빠졌습니다. 입력을 나눠 다시 "
            "실행하거나 입력 한도가 더 큰 Provider 를 사용하십시오."
        )
        return Verdict(JobStatus.FAILED, ErrorCode.INPUT_TOO_LARGE, errors)

    # --- 여기까지 걸리지 않았으면 성공이다 ----------------------------------
    # 성공한 실행에 덧붙이던 경고는 없앴다. 매번 같은 문구가 반복돼 보고서를
    # 가릴 뿐 사람이 그것을 보고 할 일이 없었고, 실패로 다뤄야 할 사정은 위에서
    # 이미 전부 error_code 로 확정된다.
    return Verdict(JobStatus.SUCCEEDED, None, errors)
