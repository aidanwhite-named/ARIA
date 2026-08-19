"""공통 Provider 인터페이스.

세 Provider 의 분석 결과를 하나의 업무 스키마로 강제하지 않는다.
공통화하는 것은 실행 메타데이터뿐이다.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..enums import AuthState


@dataclass
class ProbeResult:
    """모델 호출 없이 확인 가능한 Provider 상태."""

    provider: str
    display_name: str
    installed: bool = False
    executable_path: str | None = None
    executable_kind: str | None = None
    executable_ok: bool = False
    version: str | None = None
    auth_state: str = AuthState.UNKNOWN
    capabilities: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    install_hint: str = ""

    @property
    def usable(self) -> bool:
        return self.installed and self.executable_ok and self.auth_state in (
            AuthState.OK,
            AuthState.NOT_APPLICABLE,
        )


@dataclass
class ExecutionRequest:
    job_id: str
    work_dir: Path
    system_prompt: str
    user_message: str
    model: str | None = None
    timeout_seconds: int = 900


@dataclass
class ExecutionOutcome:
    """Provider 가 돌려주는 원시 실행 결과.

    성공/실패 판정은 여기서 하지 않는다. ResultEvaluator 가 첨부 정보까지
    합쳐서 판정한다.
    """

    result_text: str = ""
    exit_code: int | None = None
    terminal_reason: str | None = None
    is_error: bool = False
    error_message: str | None = None
    usage: dict | None = None
    permission_denials: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""
    cli_path: str | None = None
    cli_version: str | None = None
    cli_args: list[str] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    auth_required: bool = False
    rate_limited: bool = False

    # 도구 정책. v0.1 에서 '도구 없음'은 편의 설정이 아니라 보안 불변조건이다.
    # tools_must_be_disabled 인 Provider 가 도구를 광고하거나 실제로 호출하면
    # 경고가 아니라 실패로 처리한다.
    tools_advertised: list[str] = field(default_factory=list)
    tool_uses: list[str] = field(default_factory=list)
    tools_must_be_disabled: bool = False


# 실행 중 진행 상황을 밖으로 흘려보내는 콜백.
EmitFn = Callable[[str, dict], Awaitable[None]]


class Provider(abc.ABC):
    id: str = ""
    display_name: str = ""
    install_hint: str = ""

    @abc.abstractmethod
    async def probe(self) -> ProbeResult:
        """설치/실행 가능/버전/인증 상태를 확인한다. 모델 호출은 하지 않는다."""

    @abc.abstractmethod
    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        """프롬프트를 실행한다."""

    @abc.abstractmethod
    async def cancel(self, job_id: str) -> bool:
        """실행 중인 작업의 프로세스 트리를 종료한다."""

    async def smoke_test(self, emit: EmitFn | None = None) -> ExecutionOutcome:
        """실제 모델을 호출하는 검증. 사용량이 발생한다."""
        raise NotImplementedError
