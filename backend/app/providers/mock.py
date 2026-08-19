"""MockProvider.

실제 CLI 없이 업로드 → 전처리 → 실행 → 스트리밍 → 판정 → 저장 전 구간을
검증하기 위한 Provider. 현재 이 PC 에는 로그인된 CLI 가 없으므로 v0.1 에서는
사실상 유일하게 끝까지 돌릴 수 있는 Provider 이기도 하다.

사용자 입력에 아래 키워드를 넣으면 실패 경로를 재현할 수 있다.
  MOCK_FAIL      치명적 오류
  MOCK_EMPTY     exit code 0 이지만 결과가 비어 있음
  MOCK_WARN      결과는 정상이나 경고 동반
  MOCK_AUTH      인증 필요
  MOCK_RATELIMIT 사용량 제한
  MOCK_SLOW      긴 실행 (취소 테스트용)
"""

from __future__ import annotations

import asyncio

from ..enums import AuthState
from .base import EmitFn, ExecutionOutcome, ExecutionRequest, Provider, ProbeResult

_STEP_DELAY = 0.12


class MockProvider(Provider):
    id = "mock"
    display_name = "Mock (내장 시뮬레이터)"
    install_hint = "설치가 필요 없습니다. ARIA 에 내장되어 있습니다."

    def __init__(self) -> None:
        self._cancelled: set[str] = set()

    async def probe(self) -> ProbeResult:
        return ProbeResult(
            provider=self.id,
            display_name=self.display_name,
            installed=True,
            executable_path="(내장)",
            executable_kind="builtin",
            executable_ok=True,
            version="0.1.0",
            auth_state=AuthState.NOT_APPLICABLE,
            capabilities={
                "non_interactive": True,
                "stream_json": True,
                "stdin_prompt": True,
                "system_prompt_override": True,
                "tools_disabled": True,
                "model_select": False,
                "cancellable": True,
                "native_pdf": False,
            },
            notes=["실제 모델을 호출하지 않습니다. 실행 흐름 검증용입니다."],
            install_hint=self.install_hint,
        )

    async def cancel(self, job_id: str) -> bool:
        self._cancelled.add(job_id)
        return True

    async def _sleep(self, job_id: str, seconds: float) -> bool:
        """취소되면 True 를 돌려준다."""
        step = 0.05
        waited = 0.0
        while waited < seconds:
            if job_id in self._cancelled:
                return True
            await asyncio.sleep(step)
            waited += step
        return job_id in self._cancelled

    async def execute(self, request: ExecutionRequest, emit: EmitFn) -> ExecutionOutcome:
        self._cancelled.discard(request.job_id)
        message = request.user_message
        outcome = ExecutionOutcome(
            cli_path="(내장)",
            cli_version="0.1.0",
            cli_args=["mock", "--simulate"],
        )

        await emit("provider_start", {"provider": self.id, "message": "Mock 실행기 시작"})
        if await self._sleep(request.job_id, _STEP_DELAY):
            return self._cancelled_outcome(outcome)

        if "MOCK_AUTH" in message:
            await emit("provider_error", {"message": "인증이 필요합니다"})
            outcome.is_error = True
            outcome.auth_required = True
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            outcome.error_message = "Not logged in"
            outcome.errors.append("Mock: 인증 필요 상태를 시뮬레이션했습니다.")
            return outcome

        if "MOCK_RATELIMIT" in message:
            await emit("provider_error", {"message": "사용량 제한에 도달했습니다"})
            outcome.is_error = True
            outcome.rate_limited = True
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            outcome.error_message = "Rate limit exceeded"
            outcome.errors.append("Mock: 사용량 제한을 시뮬레이션했습니다.")
            return outcome

        await emit("analyzing", {"message": "입력 자료 확인 중"})
        if await self._sleep(request.job_id, _STEP_DELAY):
            return self._cancelled_outcome(outcome)

        if "MOCK_FAIL" in message:
            await emit("provider_error", {"message": "치명적 오류 발생"})
            outcome.is_error = True
            outcome.exit_code = 1
            outcome.terminal_reason = "error"
            outcome.error_message = "Mock fatal error"
            outcome.errors.append("Mock: 치명적 오류를 시뮬레이션했습니다.")
            outcome.raw_stderr = "mock: fatal error\n"
            return outcome

        if "MOCK_SLOW" in message:
            for i in range(1, 61):
                await emit("analyzing", {"message": f"장시간 작업 진행 중 ({i}/60)"})
                if await self._sleep(request.job_id, 1.0):
                    return self._cancelled_outcome(outcome)

        if "MOCK_EMPTY" in message:
            await emit("result_stream", {"delta": ""})
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            outcome.result_text = ""
            return outcome

        chunks = self._compose(request)
        collected: list[str] = []
        for chunk in chunks:
            if await self._sleep(request.job_id, _STEP_DELAY):
                return self._cancelled_outcome(outcome)
            collected.append(chunk)
            await emit("result_stream", {"delta": chunk})

        outcome.result_text = "".join(collected)
        outcome.exit_code = 0
        outcome.terminal_reason = "completed"
        outcome.usage = {
            "input_tokens": max(1, len(request.user_message) // 4),
            "output_tokens": max(1, len(outcome.result_text) // 4),
            "note": "Mock 추정치입니다. 실제 사용량이 아닙니다.",
        }
        outcome.raw_stdout = outcome.result_text

        if "MOCK_WARN" in message:
            outcome.warnings.append("Mock: 경고 동반 성공을 시뮬레이션했습니다.")

        await emit("provider_done", {"message": "Mock 실행 완료"})
        return outcome

    def _cancelled_outcome(self, outcome: ExecutionOutcome) -> ExecutionOutcome:
        outcome.cancelled = True
        outcome.terminal_reason = "cancelled"
        return outcome

    def _compose(self, request: ExecutionRequest) -> list[str]:
        prompt_preview = request.user_message.strip()
        total_chars = len(request.user_message)
        head = prompt_preview[:400]
        return [
            "# Mock 실행 결과\n\n",
            "이 결과는 **실제 모델이 생성한 것이 아닙니다.** ARIA 의 실행 경로를 "
            "검증하기 위한 시뮬레이션 출력입니다.\n\n",
            "## 수신한 입력\n\n",
            f"- 전달된 전체 문자 수: {total_chars:,}\n",
            f"- 시스템 프롬프트 문자 수: {len(request.system_prompt):,}\n",
            f"- 작업 폴더: `{request.work_dir}`\n",
            f"- 요청 모델: `{request.model or '(기본값)'}`\n\n",
            "## 입력 앞부분\n\n",
            "```\n",
            f"{head}\n",
            "```\n\n",
            "## 안내\n\n",
            "실제 분석 결과를 얻으려면 Settings 화면에서 사용 가능한 Provider 를 "
            "확인하고 선택하십시오.\n",
        ]
