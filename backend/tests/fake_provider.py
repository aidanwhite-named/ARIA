"""자동 테스트에서만 사용하는 결정론적 Provider.

실제 CLI 없이 업로드 → 전처리 → 실행 → 스트리밍 → 판정 → 저장 전 구간을
검증하기 위한 테스트 대역이다. 제품 레지스트리에는 등록하지 않으며 사용자
화면에도 노출하지 않는다.

사용자 입력에 아래 키워드를 넣으면 실패 경로를 재현할 수 있다.
  TEST_FAIL      치명적 오류
  TEST_EMPTY     exit code 0 이지만 결과가 비어 있음
  TEST_WARN      결과는 정상이나 경고 동반
  TEST_AUTH      인증 필요
  TEST_RATELIMIT 사용량 제한
  TEST_SLOW      긴 실행 (취소 테스트용)

문헌 매핑 블록은 기본적으로 첨부에 붙은 자료 번호를 그대로 되돌려준다.
  TEST_BADMAP    깨진 매핑 블록
  TEST_NOMAP     매핑 블록 없음
"""

from __future__ import annotations

import asyncio
import json
import re

from app.enums import AuthState
from app.providers.base import (
    EmitFn,
    ExecutionOutcome,
    ExecutionRequest,
    ProbeResult,
    Provider,
)

_STEP_DELAY = 0.12

# 최종 프롬프트의 첨부 헤더에 ARIA 가 찍어 두는 자료 번호.
_ALIAS_LINE = re.compile(r"^자료 번호: (ATT-\d+)$", re.MULTILINE)


def _mapping_block(message: str) -> list[str]:
    """실제 모델이 하듯 자료 번호를 읽어 매핑 블록을 만든다.

    인용발명 문헌 절에 나온 자료 번호만 대상으로 삼는다. 출원발명 문서에는
    인용발명 번호를 붙이지 않는다.
    """
    if "TEST_NOMAP" in message:
        return []
    if "TEST_BADMAP" in message:
        return [
            "\n[ARIA_CITATION_MAPPING_V1]\n",
            '{"items": [{"citation_number": 1, "attachment": "ATT-99"}]}\n',
            "[/ARIA_CITATION_MAPPING_V1]\n",
        ]

    parts = message.split("[인용발명 문헌]", 1)
    if len(parts) < 2:
        return []
    tail = parts[1].split("[기타 첨부 자료]", 1)[0]
    aliases = _ALIAS_LINE.findall(tail)
    if not aliases:
        return []
    items = [
        {
            "citation_number": index,
            "attachment": alias,
            "document_number": f"KR10-{1000000 + index}",
        }
        for index, alias in enumerate(aliases, start=1)
    ]
    return [
        "\n[ARIA_CITATION_MAPPING_V1]\n",
        json.dumps({"items": items}, ensure_ascii=False) + "\n",
        "[/ARIA_CITATION_MAPPING_V1]\n",
    ]


class DeterministicTestProvider(Provider):
    id = "test"
    display_name = "Deterministic test provider"
    install_hint = "자동 테스트 전용 Provider 입니다."

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
            cli_args=["test-provider", "--simulate"],
        )

        await emit("provider_start", {"provider": self.id, "message": "테스트 실행기 시작"})
        if await self._sleep(request.job_id, _STEP_DELAY):
            return self._cancelled_outcome(outcome)

        if "TEST_AUTH" in message:
            await emit("provider_error", {"message": "인증이 필요합니다"})
            outcome.is_error = True
            outcome.auth_required = True
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            outcome.error_message = "Not logged in"
            outcome.errors.append("테스트 Provider: 인증 필요 상태를 시뮬레이션했습니다.")
            return outcome

        if "TEST_RATELIMIT" in message:
            await emit("provider_error", {"message": "사용량 제한에 도달했습니다"})
            outcome.is_error = True
            outcome.rate_limited = True
            outcome.exit_code = 0
            outcome.terminal_reason = "completed"
            outcome.error_message = "Rate limit exceeded"
            outcome.errors.append("테스트 Provider: 사용량 제한을 시뮬레이션했습니다.")
            return outcome

        await emit("analyzing", {"message": "입력 자료 확인 중"})
        if await self._sleep(request.job_id, _STEP_DELAY):
            return self._cancelled_outcome(outcome)

        if "TEST_FAIL" in message:
            await emit("provider_error", {"message": "치명적 오류 발생"})
            outcome.is_error = True
            outcome.exit_code = 1
            outcome.terminal_reason = "error"
            outcome.error_message = "Test provider fatal error"
            outcome.errors.append("테스트 Provider: 치명적 오류를 시뮬레이션했습니다.")
            outcome.raw_stderr = "test-provider: fatal error\n"
            return outcome

        if "TEST_SLOW" in message:
            for i in range(1, 61):
                await emit("analyzing", {"message": f"장시간 작업 진행 중 ({i}/60)"})
                if await self._sleep(request.job_id, 1.0):
                    return self._cancelled_outcome(outcome)

        if "TEST_EMPTY" in message:
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
            "note": "테스트 추정치입니다. 실제 사용량이 아닙니다.",
        }
        outcome.raw_stdout = outcome.result_text

        if "TEST_WARN" in message:
            outcome.warnings.append("테스트 Provider: 경고 동반 성공을 시뮬레이션했습니다.")

        await emit("provider_done", {"message": "테스트 실행 완료"})
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
            "# 테스트 실행 결과\n\n",
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
            *_mapping_block(request.user_message),
        ]
