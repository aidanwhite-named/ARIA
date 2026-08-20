"""상태값 정의.

생명주기(status)와 실패 원인(error_code)을 분리한다. 두 축을 하나의 enum 에
섞으면 Provider 를 추가할 때마다 계속 커지고, UI 와 재시도 정책이 같은 값을
다르게 해석하게 된다.

SUCCESS_WITH_WARNINGS 는 저장하지 않고 status/warnings 에서 파생한다.
저장하면 두 필드가 어긋날 수 있다.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    TIMED_OUT = "TIMED_OUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    EMPTY_RESULT = "EMPTY_RESULT"
    PROCESS_ERROR = "PROCESS_ERROR"
    ATTACHMENT_ERROR = "ATTACHMENT_ERROR"
    TOOL_POLICY_VIOLATION = "TOOL_POLICY_VIOLATION"
    CANCELLED = "CANCELLED"


class ResultQuality(StrEnum):
    SUCCESS = "SUCCESS"
    SUCCESS_WITH_WARNINGS = "SUCCESS_WITH_WARNINGS"


def derive_quality(status: str, warnings: list | None) -> str | None:
    """status 와 warnings 로부터 결과 품질을 계산한다 (저장하지 않는다)."""
    if status != JobStatus.SUCCEEDED:
        return None
    return ResultQuality.SUCCESS_WITH_WARNINGS if warnings else ResultQuality.SUCCESS


class DeliveryMode(StrEnum):
    """첨부 자료가 실제로 모델에게 전달된 방식."""

    INLINE_CONTEXT = "DELIVERED_AS_INLINE_CONTEXT"
    NATIVE_FILE = "NATIVE_FILE"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class AttachmentRole(StrEnum):
    """분석 안에서 첨부 자료가 맡는 역할."""

    APPLICATION = "APPLICATION"
    CITATION = "CITATION"
    SUPPLEMENTAL = "SUPPLEMENTAL"


class RelationType(StrEnum):
    """후속 실행이 원본 실행에서 무엇을 물려받았는지.

    자료 재사용과 맥락 이어받기는 서로 다른 선택이다. 하나의 컬럼에 두 의미를
    섞으면 "보고서는 이어받았는데 자료는 안 받았다" 같은 표현 불가능한 조합이
    스키마상 가능해진다.

      MAPPED     : 첨부 + 이전 청구항 + 검증된 문헌 매핑. 이전 보고서는 넣지
                   않는다. 종속항 추가 분석의 기본 경로다. 번호는 유지되고
                   유사도·발췌문은 앵커링 없이 다시 판단된다.
      CONTINUED  : 여기에 이전 보고서 전체를 더한다. 보고서 자체를 고치거나
                   보완할 때만 쓴다.
      REANALYZED : 첨부만 물려받는다. 번호도 이전 판단도 물려받지 않는다.

    값이 없으면 독립 실행이다.
    """

    MAPPED = "MAPPED"
    CONTINUED = "CONTINUED"
    REANALYZED = "REANALYZED"

    @property
    def inherits_mapping(self) -> bool:
        return self in (RelationType.MAPPED, RelationType.CONTINUED)

    @property
    def inherits_report(self) -> bool:
        return self is RelationType.CONTINUED


class ExtractionMethod(StrEnum):
    RAW_TEXT = "RAW_TEXT"
    PDF_TEXT_LAYER = "PDF_TEXT_LAYER"
    NONE = "NONE"


class OutputMode(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"


class AuthState(StrEnum):
    OK = "OK"
    NOT_LOGGED_IN = "NOT_LOGGED_IN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
