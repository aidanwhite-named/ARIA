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
