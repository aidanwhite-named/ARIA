"""자동 테스트에서만 사용하는 결정론적 Provider.

실제 CLI 없이 업로드 → 전처리 → 실행 → 스트리밍 → 판정 → 저장 전 구간을
검증하기 위한 테스트 대역이다. 제품 레지스트리에는 등록하지 않으며 사용자
화면에도 노출하지 않는다.

사용자 입력에 아래 키워드를 넣으면 실패 경로를 재현할 수 있다.
  TEST_FAIL      치명적 오류
  TEST_EMPTY     exit code 0 이지만 결과가 비어 있음
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
    WEB_SEARCH,
)

_STEP_DELAY = 0.12

#: 이 대역이 실제로 받은 실행 요청. preflight 가 안내한 크기와 실행에 나간
#: 크기를 바이트까지 대조하려면 "무엇이 나갔는가"를 결과 텍스트가 아니라 원본
#: 요청에서 읽어야 한다. 테스트 전용이며 제품 경로에는 없다. 읽는 쪽이 먼저
#: clear() 한다.
RECEIVED: list[ExecutionRequest] = []

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


def _component_block(message: str) -> list[str]:
    """구성별 분석 계약을 선언한 프롬프트에 결정론적 결과를 붙인다."""
    if "ARIA_COMPONENT_ANALYSIS_V1" not in message or "TEST_NOCOMPONENTS" in message:
        return []
    payload = {
        "items": [
            {
                "claim": "청구항 1",
                "symbol": "(A)",
                "feature": "제1 센서와 제2 센서를 포함하는 구성",
                "similarity": 92,
                "status": "matched",
                "difference": "",
            },
            {
                "claim": "청구항 1",
                "symbol": "(B)",
                "feature": "두 센서의 신호를 결합하여 제어하는 구성",
                "similarity": 72,
                "status": "below_threshold",
                "difference": "결합 신호에 따른 제어 관계가 확인되지 않음",
            },
            {
                "claim": "청구항 1",
                "symbol": "(C)",
                "feature": "결과를 원격 장치로 전송하는 구성",
                "similarity": None,
                "status": "not_found",
                "difference": "대응 문헌을 찾지 못함",
            },
        ]
    }
    return [
        "\n[ARIA_COMPONENT_ANALYSIS_V1]\n",
        json.dumps(payload, ensure_ascii=False) + "\n",
        "[/ARIA_COMPONENT_ANALYSIS_V1]\n",
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
        RECEIVED.append(request)
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
            *_component_block(request.user_message),
            *_mapping_block(request.user_message),
        ]


# --------------------------------------------------------------- 검색 대역

_SEARCH_KEYWORDS = """유사 문헌 검색 경로 전용 키워드.

  SEARCH_NO_TOOL     도구를 한 번도 부르지 않고 기억만으로 답한다
  SEARCH_STRAY_TOOL  허용 목록 밖의 도구(Bash)를 부른다
  SEARCH_STRAY_ADS   허용 목록 밖의 도구를 광고만 한다
  SEARCH_RAW_CLAIM   후보에 raw_original_verified 를 주장한다
  SEARCH_NOLOG       감사 블록을 출력하지 않는다
  SEARCH_BUDGET      도구를 아주 많이 부른다 (상한 테스트)
  SEARCH_SLOW        오래 걸린다 (취소 테스트)
  SEARCH_FAKE_URL    열어 본 적 없는 URL 에 열람 성공을 주장한다
  SEARCH_PAYWALL_URL 열다가 실패한 URL 에 열람 성공을 주장한다
  SEARCH_QUOTE_PROSE 산문 본문에 원문 인용처럼 보이는 문장을 넣는다
"""


class DeterministicSearchProvider(Provider):
    """도구 목록을 강제할 수 있는 Provider 를 흉내 낸다.

    실제 CLI 없이 검색 작업의 도구 정책·감사 기록·판정 경로를 검증한다.
    제품 레지스트리에는 등록하지 않는다.
    """

    id = "test-search"
    display_name = "Deterministic search provider"
    install_hint = "자동 테스트 전용 Provider 입니다."
    supported_tool_policies = frozenset({"no_tools", "web_search"})
    search_tool_policy = WEB_SEARCH

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
            capabilities={"tool_allowlist": True, "web_search": True},
            notes=["실제 모델을 호출하지 않습니다."],
            install_hint=self.install_hint,
        )

    async def cancel(self, job_id: str) -> bool:
        self._cancelled.add(job_id)
        return True

    async def _sleep(self, job_id: str, seconds: float) -> bool:
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
        policy = request.tool_policy
        outcome = ExecutionOutcome(
            cli_path="(내장)",
            cli_version="0.1.0",
            cli_args=["test-search", "--tools", ",".join(policy.allowed_tools)],
        )
        outcome.tool_policy = policy
        outcome.tools_must_be_disabled = policy.tools_disabled
        outcome.tools_advertised = list(policy.allowed_tools)

        await emit("provider_start", {"provider": self.id, "tools": policy.allowed_tools})

        if "SEARCH_STRAY_ADS" in message:
            outcome.tools_advertised = [*policy.allowed_tools, "Bash"]

        calls: list[dict] = []

        async def call(name: str, payload: dict, ok: bool = True) -> None:
            outcome.tool_uses.append(name)
            record = {
                "id": f"call-{len(outcome.tool_uses)}",
                "name": name,
                "ts": "2026-08-21T00:00:00+00:00",
                "input": payload,
                "ok": ok,
                "error": None if ok else "접근 거부",
            }
            calls.append(record)
            await emit("tool_use", {"name": name, "id": record["id"], "input": payload})

        if "SEARCH_SLOW" in message:
            await call("WebSearch", {"query": "느린 검색"})
            for i in range(1, 61):
                await emit("analyzing", {"message": f"검색 진행 중 ({i}/60)"})
                if await self._sleep(request.job_id, 1.0):
                    outcome.tool_calls = calls
                    outcome.cancelled = True
                    outcome.terminal_reason = "cancelled"
                    return outcome

        if "SEARCH_STRAY_TOOL" in message:
            await call("Bash", {"keys": ["command"]})
        elif "SEARCH_BUDGET" in message:
            for i in range(policy.max_tool_calls + 5):
                await call("WebSearch", {"query": f"budget probe {i}"})
                if len(outcome.tool_uses) > policy.max_tool_calls:
                    outcome.tool_budget_exceeded = True
                    outcome.cancelled = True
                    break
        elif "SEARCH_NO_TOOL" not in message:
            spec_assisted = "<SPEC_TEXT>" in message
            prefix = "명세서 확장 " if spec_assisted else ""
            await call(
                "WebSearch",
                {"query": f"{prefix}테스트 검색식 A", "allowed_domains": []},
            )
            await call("WebFetch", {"url": "https://patents.example.com/AB1234"})
            if spec_assisted:
                await call("WebFetch", {"url": "https://patents.example.com/CD5678"})
            await call("WebFetch", {"url": "https://paywall.example.com/x"}, ok=False)
            await call("WebSearch", {"query": f"{prefix}테스트 검색식 B"})

        outcome.tool_calls = calls
        outcome.exit_code = 0
        outcome.terminal_reason = "completed"
        outcome.result_text = _search_report(message)
        outcome.raw_stdout = outcome.result_text
        outcome.usage = {"note": "테스트 추정치입니다."}
        await emit("provider_done", {"message": "테스트 검색 완료"})
        return outcome


#: 모델 산문이 원문 인용처럼 꾸민 문장. 사용자 보고서에 새어 나가면 안 된다.
FABRICATED_QUOTE = "상기 제1 센서는 제2 센서와 직렬로 연결되며"


def _search_report(message: str) -> str:
    report = (
        "# 유사 문헌 검토 후보 (테스트)\n\n"
        "이 결과는 실제 모델이 생성한 것이 아닙니다.\n\n"
        "## A. 전체 구조와 핵심 특징이 모두 강하게 유사\n\n"
        "- AB1234 · 테스트 특허\n"
    )
    if "SEARCH_QUOTE_PROSE" in message:
        # WebFetch 요약을 원문 인용처럼 제시하는 전형적인 위반 사례.
        report += (
            f'\nAB1234 의 청구항 1에는 "{FABRICATED_QUOTE}"라고 기재되어 있습니다.\n'
            "> 청구항 1, 3컬럼 12행\n"
        )
    if "SEARCH_NOLOG" in message:
        return report

    provenance = (
        "raw_original_verified" if "SEARCH_RAW_CLAIM" in message else "webfetch_summary"
    )
    if "SEARCH_FAKE_URL" in message:
        # 한 번도 열지 않은 주소.
        url = "https://never-fetched.example.com/ZZ9999"
    elif "SEARCH_PAYWALL_URL" in message:
        # 열려고 했지만 실패한 주소.
        url = "https://paywall.example.com/x"
    else:
        # 성공한 WebFetch 와 같은 주소. 끝 슬래시와 대소문자를 일부러 다르게 준다.
        url = "https://PATENTS.example.com/AB1234/"

    # 명세서를 넣은 독립 실행에서만 용어 확장 기록을 보고한다.
    spec_assisted = "<SPEC_TEXT>" in message
    term_expansions = (
        [
            {
                "claim_term": "제어부",
                "alternative_meanings": [
                    "일반적인 제어 회로",
                    "FPGA 로 구현된 신호 처리 회로",
                ],
                "expanded_terms": ["controller", "FPGA", "signal processing circuit"],
                "basis": "명세서 문단 [0021]",
                "excluded_limitations": ["특정 FPGA 모델"],
            }
        ]
        if spec_assisted
        else []
    )

    candidates = [
        {
            "group": "A",
            "provisional": False,
            "channel": "web",
            "doc_type": "patent",
            "doc_number": "AB1234",
            "title": "테스트 특허",
            "applicant": "테스트 주식회사",
            "url": url,
            "family": "확인 필요",
            "provenance": provenance,
            "evidence_status": "source_page_reviewed",
            "verbatim_excerpt": FABRICATED_QUOTE,
            "source_location": "청구항 1, 3컬럼 12행",
            "note": "테스트 후보",
            "mapping": [
                {
                    "feature": "제1 센서",
                    "counterpart": "센서 모듈 110",
                    "degree": "강한 대응",
                    "support_source": "page_text",
                    "support_text": "a sensor module 110 coupled to the housing",
                    "support_scope": "abstract",
                    "support_url": url,
                    "source_location": "청구항 1, 3컬럼 12행",
                    "verbatim_excerpt": FABRICATED_QUOTE,
                    "translation": "the first sensor is connected in series",
                    "similar": "직렬 연결 구조가 같다",
                    "different": "제어부 구성이 다르다",
                }
            ],
        }
    ]
    if spec_assisted:
        candidates.append(
            {
                "group": "B",
                "provisional": True,
                "channel": "web",
                "doc_type": "patent",
                "doc_number": "CD5678",
                "title": "명세서 용어로 추가 발견한 특허",
                "applicant": "확장 검색 주식회사",
                "url": "https://patents.example.com/CD5678",
                "family": "확인 필요",
                "provenance": "webfetch_summary",
                "evidence_status": "source_page_reviewed",
                "verbatim_excerpt": "",
                "source_location": "",
                "note": "명세서 동의어로 추가된 후보",
                "mapping": [],
            }
        )

    payload = {
        "rounds": [
            {"round": 1, "channel": "web", "queries": ["테스트 검색식 A"], "note": "1차"},
            {"round": 2, "channel": "web", "queries": ["테스트 검색식 B"], "note": "확장"},
        ],
        "term_expansions": term_expansions,
        "candidates": candidates,
        "access_failures": [
            {"url": "https://paywall.example.com/x", "reason": "유료 논문"}
        ],
    }
    return (
        report
        + "\n[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]\n"
    )
