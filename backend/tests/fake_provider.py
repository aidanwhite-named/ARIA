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

로컬 검색(retrieval) 라운드는 시스템 프롬프트로 구분해서 action JSON 을
돌려준다. 청구항에 아래 키워드를 넣으면 각 경로를 재현할 수 있다.

  RETRIEVAL_NOEXPAND    구성마다 검색어를 하나만 쓴다 (확장 검색 미수행)
  RETRIEVAL_BADATT      존재하지 않는 자료 번호를 요청한다
  RETRIEVAL_BADPAGE     범위 밖 페이지를 요청한다
  RETRIEVAL_NOFINALIZE  끝까지 finalize 하지 않는다 (라운드 예산 테스트)
  RETRIEVAL_NOTFOUND    근거 없이 not_found 를 주장한다
  RETRIEVAL_FAKETEXT    존재하지 않는 chunk_id 를 근거로 제시한다
  RETRIEVAL_BADJSON     JSON 이 아닌 응답을 한 번 돌려준다
  RETRIEVAL_TOOL        도구를 끈 실행에서 도구를 호출한다
  RETRIEVAL_SLOW        오래 걸린다 (취소 테스트)
  RETRIEVAL_UNSEEN      본 적 없는 chunk_id 를 추측해서 근거로 제시한다
  RETRIEVAL_ONEDOC      첫 문헌만 검색하고 나머지는 건드리지 않는다
  RETRIEVAL_NOCOMPONENT 구성 분해 없이 빈 finalize 를 돌려준다
  RETRIEVAL_PARTIAL     선언한 구성 중 하나를 finalize 에서 빠뜨린다
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

from app.enums import AuthState
from app.patent_search import epo_prompts
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


# --------------------------------------------------- 로컬 검색 라운드 대역

#: 라운드 payload 앞에 붙는 표시. render_round 가 찍는다.
_ROUND_MARKER = "[ARIA 로컬 검색 라운드]"


def _round_payload(message: str) -> dict:
    """라운드 메시지에서 ARIA 가 넣은 JSON 을 되읽는다."""
    start = message.find("{", message.find(_ROUND_MARKER))
    depth = 0
    for index in range(start, len(message)):
        if message[index] == "{":
            depth += 1
        elif message[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(message[start : index + 1])
    raise AssertionError("라운드 payload 를 찾지 못했습니다.")


def _claim_text(message: str) -> str:
    head = message.find("<CLAIM_TEXT>")
    tail = message.find("</CLAIM_TEXT>")
    return message[head + len("<CLAIM_TEXT>") : tail] if head >= 0 < tail else ""


#: 구성 분해. 실제 모델이 하듯 청구항을 몇 조각으로 나눈다.
_COMPONENTS = [
    {"label": "청구항 1 (A)", "feature": "제1 센서와 제2 센서를 포함하는 구성"},
    {"label": "청구항 1 (B)", "feature": "두 센서의 신호를 결합하여 제어하는 구성"},
]

#: 확장 검색어. MIN_EXPANSION_TERMS 를 넘긴다.
_QUERIES = ["센서", "결합", "제어", "sensor"]

#: 실재하지만 위 검색어로는 절대 반환되지 않는 청크. 노출 게이트 테스트용이며,
#: 테스트 fixture 의 마지막 페이지(검색어가 하나도 없는 페이지)를 가리킨다.
UNSEEN_CHUNK_ID = "P0004-001"


def _retrieval_response(message: str) -> str:
    payload = _round_payload(message)
    claim = _claim_text(message)
    round_no = int(payload.get("round") or 1)

    if "RETRIEVAL_BADJSON" in claim and round_no == 1:
        return "여기 JSON 이 없습니다. 설명만 씁니다."

    if "RETRIEVAL_NOCOMPONENT" in claim:
        # 구성 분해 없이 빈 마무리. 서버가 받아 주면 근거 0개짜리 패키지가
        # 최종 분석에 그대로 들어간다.
        return json.dumps(
            {
                "components": [],
                "notes": "분해 없이 끝냅니다.",
                "actions": [{"action": "finalize_evidence", "components": []}],
            },
            ensure_ascii=False,
        )

    if not payload.get("components"):
        queries = ["센서"] if "RETRIEVAL_NOEXPAND" in claim else _QUERIES
        # 첫 문헌만 검색한다. 나머지 문헌은 검색 기록이 남지 않아야 한다.
        target = (
            payload["documents"][0]["attachment"]
            if "RETRIEVAL_ONEDOC" in claim and payload.get("documents")
            else "*"
        )
        actions = [
            {
                "action": "search_document",
                "component_id": f"R{index:03d}",
                "attachment": target,
                "queries": queries,
                "limit": 5,
            }
            for index in range(1, len(_COMPONENTS) + 1)
        ]
        if "RETRIEVAL_BADATT" in claim:
            actions.append(
                {
                    "action": "get_document_status",
                    "attachment": "ATT-99",
                }
            )
        if "RETRIEVAL_BADPAGE" in claim:
            actions.append(
                {"action": "read_page", "attachment": "ATT-01", "page": 9999}
            )
        return json.dumps(
            {"components": _COMPONENTS, "notes": "1라운드", "actions": actions},
            ensure_ascii=False,
        )

    # 2라운드 이후: 결과에서 chunk_id 를 모아 근거로 확정한다.
    found: dict[str, list[dict]] = {}
    for entry in payload.get("results") or []:
        component_id = entry.get("component_id") or ""
        for document in entry.get("documents") or []:
            for hit in document.get("hits") or []:
                found.setdefault(component_id, []).append(
                    {"attachment": hit["alias"], "chunk_id": hit["chunk_id"]}
                )

    if "RETRIEVAL_NOFINALIZE" in claim:
        return json.dumps(
            {
                "notes": "계속 찾습니다.",
                "actions": [
                    {
                        "action": "search_document",
                        "component_id": "R001",
                        "attachment": "*",
                        "queries": [f"추가 검색 {round_no}"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    components = []
    for index in range(1, len(_COMPONENTS) + 1):
        component_id = f"R{index:03d}"
        if "RETRIEVAL_PARTIAL" in claim and index == len(_COMPONENTS):
            # 마지막 구성을 통째로 빠뜨린다. 받아 주면 그 구성은 근거도 사유도
            # 없이 보고서에서 사라진다.
            continue
        hits = found.get(component_id, [])[:2]
        if "RETRIEVAL_UNSEEN" in claim:
            # 실재하지만 이번 실행에서 반환받은 적 없는 청크. 형식만 맞다.
            # 마지막 페이지에는 이 실행의 검색어가 하나도 없으므로 검색
            # 결과로 돌아온 적이 없다.
            hits = [{"attachment": "ATT-01", "chunk_id": UNSEEN_CHUNK_ID}]
        if "RETRIEVAL_NOTFOUND" in claim:
            components.append(
                {
                    "component_id": component_id,
                    "status_claim": "not_found",
                    "searched_terms": _QUERIES,
                    "evidence": [],
                    "note": "찾지 못했습니다.",
                }
            )
            continue
        if "RETRIEVAL_FAKETEXT" in claim:
            hits = [{"attachment": "ATT-01", "chunk_id": "P9999-999"}]
        components.append(
            {
                "component_id": component_id,
                "status_claim": "matched" if hits else "not_found",
                "searched_terms": _QUERIES,
                "evidence": [
                    {
                        "attachment": hit["attachment"],
                        "chunk_id": hit["chunk_id"],
                        "relevance": "테스트 관련성 메모",
                    }
                    for hit in hits
                ],
                "note": "테스트 근거",
            }
        )
    return json.dumps(
        {
            "notes": "마무리",
            "actions": [{"action": "finalize_evidence", "components": components}],
        },
        ensure_ascii=False,
    )


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

        # 로컬 검색 라운드는 최종 분석 호출과 계약이 다르다. 시스템 프롬프트로
        # 구분한다 — 사용자 메시지에는 청구항이 들어 있어 키워드가 섞일 수 있다.
        if _ROUND_MARKER in message:
            return await self._retrieval_round(request, emit, outcome)

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

    async def _retrieval_round(
        self, request: ExecutionRequest, emit: EmitFn, outcome: ExecutionOutcome
    ) -> ExecutionOutcome:
        """로컬 검색 라운드 하나. action JSON 만 돌려준다."""
        claim = _claim_text(request.user_message)
        await emit("provider_start", {"provider": self.id, "message": "검색 라운드"})

        if "RETRIEVAL_SLOW" in claim:
            for index in range(1, 61):
                await emit("analyzing", {"message": f"검색 중 ({index}/60)"})
                if await self._sleep(request.job_id, 1.0):
                    return self._cancelled_outcome(outcome)
        elif await self._sleep(request.job_id, _STEP_DELAY):
            return self._cancelled_outcome(outcome)

        if "RETRIEVAL_TOOL" in claim:
            # 도구를 끈 실행에서 도구를 부른 경우. ARIA 가 사후 탐지해서 실패로
            # 처리해야 한다.
            outcome.tool_uses.append("Bash")
            outcome.tool_calls.append({"name": "Bash", "input": {}, "ok": True})

        outcome.result_text = _retrieval_response(request.user_message)
        outcome.exit_code = 0
        outcome.terminal_reason = "completed"
        outcome.raw_stdout = outcome.result_text
        outcome.usage = {"input_tokens": 100, "output_tokens": 50}
        await emit("provider_done", {"message": "검색 라운드 완료"})
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
  SEARCH_DENIED      허용 목록에 없는 주소를 열려다 자동 거부되고 빈 응답으로 끝난다
  SEARCH_BLOCKED     403·로그인·유료벽에 막혀도 후보와 감사 블록은 남긴다
"""



_EPO_MARKER = "EPO OPS 특허 검색 실행기"

# 테스트가 EPO 레인의 응답을 바꿔 끼우는 자리. 레인 id 별로 넣으면 그 레인만
# 다르게 답한다. 비어 있으면 검색 한 번 + 마무리를 돌려준다.
EPO_SCRIPT: dict[str, list[str]] = {}
EPO_REQUESTS: list = []

#: 최종 선택 턴의 응답을 레인별로 바꿔 끼우는 자리. 비어 있으면 아무것도 더
#: 고르지 않고 끝낸다(기본값). 검색 라운드와 **다른 큐**를 쓰는 이유는 두 턴의
#: 계약이 다르기 때문이다 — 선택 턴은 검색을 할 수 없다.
EPO_SELECTION_SCRIPT: dict[str, list[str]] = {}
EPO_SELECTION_REQUESTS: list = []

#: EPO 레인이 모델을 부르는 **바로 그 순간** 테스트가 끼어드는 자리.
#: (lane_id, request) 를 받는다. 취소처럼 "레인이 실제로 시작한 뒤에" 일어나야
#: 하는 사건을 재현하려면 이 자리가 필요하다 — 작업 시작 전에 취소를 넣으면
#: 웹 레인에서 먼저 걸려 EPO 가 돌기도 전에 끝나고, 그러면 EPO 취소 경로는
#: 한 줄도 실행되지 않은 채 테스트가 통과한다.
EPO_ON_ROUND = None


#: 공식 근거 2차 분류 턴의 시스템 프롬프트 표식.
_CLASSIFY_MARKER = "ARIA 유사문헌 검색의 공식 근거 분류 단계"

#: 2차 분류 턴에서 이 대역이 매길 등급. 테스트가 바꿔 끼운다.
CLASSIFY_GROUP = "B"
#: 참이면 보존 응답에 없는 문장을 근거로 적는다(대조 실패 경로).
CLASSIFY_FABRICATE = False
#: 이 대역이 실제로 받은 2차 분류 요청.
CLASSIFY_REQUESTS: list = []


def _official_records(message: str) -> list:
    """2차 분류 프롬프트에서 (문헌번호, 필드명, 본문) 을 읽어 온다.

    실제 모델이 하는 일과 같다 — 프롬프트에 실려 온 본문만 보고 답한다.
    """
    records: list = []
    doc_number = ""
    field = ""
    body: list = []

    def flush() -> None:
        if doc_number and field and body:
            joined = chr(10).join(body).rstrip()
            records.append((doc_number, field, joined))

    for line in (message or "").splitlines():
        if line.startswith("### "):
            flush()
            body = []
            field = ""
            doc_number = line[4:].strip()
            continue
        if line.startswith("[field:"):
            flush()
            body = []
            field = line[len("[field:") :].partition("]")[0]
            continue
        if line.startswith("</OFFICIAL_RECORDS>"):
            flush()
            body = []
            field = ""
            continue
        if field:
            body.append(line)
    flush()
    return records


def _quote(body: str) -> str:
    """본문에서 **그대로** 옮길 수 있는 연속 문장 하나를 고른다.

    한 줄을 골라 앞뒤 공백만 떼어 낸다. 그러면 원문 안의 연속 부분 문자열이라는
    성질이 유지되므로 아티팩트 대조를 통과한다.
    """
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    return max(lines, key=len) if lines else ""


#: 기본 EPO 응답의 청구항 분석. 계약이라 없으면 검색 action 이 실행되지 않는다.
EPO_DEFAULT_ANALYSIS = {
    "elements": [
        {"id": "E1", "text": "로봇 팔", "essential": True, "synonyms": ["robot arm"]}
    ],
    "concept_combinations": [{"elements": ["E1"], "terms": ["robot arm"]}],
}


def _epo_default_reply() -> str:
    return json.dumps(
        {
            "strategy": "테스트",
            "claim_analysis": EPO_DEFAULT_ANALYSIS,
            "actions": [
                {
                    "action": "epo_search",
                    "query": {"kind": "term", "field": "ta", "value": "test claim"},
                },
                {"action": "finish", "notes": "끝"},
            ],
        },
        ensure_ascii=False,
    )


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

    @staticmethod
    def _epo_lane(request: ExecutionRequest) -> str:
        for candidate in ("epo:claim_only", "epo:spec_assisted"):
            if candidate.replace(":", "-") in str(request.work_dir):
                return candidate
        return ""

    async def _epo_selection(self, request: ExecutionRequest):
        """검색이 끝난 뒤의 최종 선택 턴. 검색 action 을 돌려주지 않는다."""
        lane = self._epo_lane(request)
        EPO_SELECTION_REQUESTS.append(
            {"lane": lane, "message": request.user_message}
        )
        outcome = ExecutionOutcome(
            cli_path="(내장)", cli_version="0.1.0", cli_args=["test-epo-selection"]
        )
        outcome.tool_policy = request.tool_policy
        if request.job_id in self._cancelled:
            outcome.cancelled = True
            outcome.terminal_reason = "cancelled"
            return outcome
        queue = EPO_SELECTION_SCRIPT.get(lane)
        if queue:
            outcome.result_text = queue.pop(0)
        else:
            outcome.result_text = json.dumps(
                {"actions": [{"action": "finish", "notes": "선택 완료"}]},
                ensure_ascii=False,
            )
        return outcome

    async def _epo_round(self, request: ExecutionRequest, emit: EmitFn):
        """EPO 레인 한 라운드. 도구를 쓰지 않는다."""
        lane = self._epo_lane(request)
        EPO_REQUESTS.append({"lane": lane, "message": request.user_message})
        if EPO_ON_ROUND is not None:
            hooked = EPO_ON_ROUND(lane, request)
            if inspect.isawaitable(hooked):
                await hooked

        outcome = ExecutionOutcome(
            cli_path="(내장)", cli_version="0.1.0", cli_args=["test-epo"]
        )
        outcome.tool_policy = request.tool_policy
        if request.job_id in self._cancelled:
            outcome.cancelled = True
            outcome.terminal_reason = "cancelled"
            return outcome
        queue = EPO_SCRIPT.get(lane)
        if queue:
            reply = queue.pop(0)
            if reply == "__RAISE__":
                # 레인 하나가 예외로 죽는 경우. 러너가 격리하는지 본다.
                raise RuntimeError("테스트용 레인 실패")
            outcome.result_text = reply
        else:
            outcome.result_text = _epo_default_reply()
        return outcome

    def _classify_round(self, request: ExecutionRequest) -> ExecutionOutcome:
        """공식 근거 2차 분류 턴. 도구를 쓰지 않는다."""
        CLASSIFY_REQUESTS.append(request.user_message)
        outcome = ExecutionOutcome(
            cli_path="(내장)", cli_version="0.1.0", cli_args=["test-classify"]
        )
        outcome.tool_policy = request.tool_policy
        outcome.tools_must_be_disabled = request.tool_policy.tools_disabled
        outcome.exit_code = 0
        outcome.terminal_reason = "completed"

        seen: dict = {}
        for doc_number, field, body in _official_records(request.user_message):
            entry = seen.setdefault(
                doc_number,
                {"doc_number": doc_number, "group": CLASSIFY_GROUP, "note": "테스트", "mapping": []},
            )
            quote = _quote(body)
            if not quote:
                continue
            entry["mapping"].append(
                {
                    "feature": "청구항의 기술적 특징",
                    "support_text": (
                        "보존된 응답에 없는 문장입니다" if CLASSIFY_FABRICATE else quote
                    ),
                    "support_field": field,
                    "support_scope": "claims" if field.startswith("claims") else "abstract",
                    "degree": "부분 대응",
                    "counterpart": "근거 문장이 개시하는 대응 내용",
                    "source_location": "확인 필요",
                    "verbatim_excerpt": "",
                    "translation": "",
                    "similar": "유사점",
                    "different": "차이점",
                }
            )
        payload = json.dumps(
            {"candidates": list(seen.values())}, ensure_ascii=False, indent=2
        )
        outcome.result_text = chr(10).join(
            ["[ARIA_CLASSIFY_V1]", payload, "[/ARIA_CLASSIFY_V1]", ""]
        )
        outcome.raw_stdout = outcome.result_text
        return outcome

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
        # 분석 대역과 같은 기록을 남긴다. 검색 경로에서도 "무엇이 실제로
        # 나갔는가"를 결과 텍스트가 아니라 원본 요청에서 읽어야 한다 — 검색
        # 전략 본문이 최종 프롬프트의 어느 자리에 들어갔는지는 결과로 알 수 없다.
        RECEIVED.append(request)
        message = request.user_message
        policy = request.tool_policy

        # EPO 레인은 계약이 다르다. 도구를 쓰지 않고 구조화된 action JSON 을
        # 돌려준다. 시스템 프롬프트로 구분한다 — 사용자 메시지에는 청구항이
        #들어 있어 키워드가 섞일 수 있다.
        if epo_prompts.SELECTION_MARKER in (request.system_prompt or ""):
            return await self._epo_selection(request)

        if _EPO_MARKER in (request.system_prompt or ""):
            return await self._epo_round(request, emit)

        # 공식 근거 2차 분류 턴. 도구를 쓰지 않고, ARIA 가 프롬프트에 실어 준
        # 본문에서 문장을 **그대로** 옮겨 온다. 지어내면 아티팩트 대조에서
        # 탈락하고, 그 실패 경로는 다른 테스트가 따로 검사한다.
        if _CLASSIFY_MARKER in (request.system_prompt or ""):
            return self._classify_round(request)

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

        async def call(
            name: str, payload: dict, ok: bool = True, error: str | None = None
        ) -> None:
            outcome.tool_uses.append(name)
            record = {
                "id": f"call-{len(outcome.tool_uses)}",
                "name": name,
                "ts": "2026-08-21T00:00:00+00:00",
                "input": payload,
                "ok": ok,
                # 오류 문구를 바꿔 끼울 수 있어야 한다. 권한 거부와 403 은
                # 판정이 다른데 문구가 하나뿐이면 그 차이를 재현할 수 없다.
                "error": error if error is not None else (None if ok else "접근 거부"),
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

        if "SEARCH_DENIED" in message:
            # agy 의 실측 동작. 검색은 끝냈는데 허용 목록에 없는 주소를 열려다
            # 자동 거부됐고, 그 한 번의 거부가 **턴 전체**를 취소시켰다. 이미
            # 끝난 검색 결과도 감사 블록도 함께 사라진다.
            await call("WebSearch", {"query": "권한 거부 재현"})
            await call(
                "WebFetch",
                {"url": "https://arxiv.org/abs/2412.02317"},
                ok=False,
                error="auto-denied: read_url permission",
            )
            outcome.tool_calls = calls
            outcome.exit_code = 0
            outcome.terminal_reason = "cancelled"
            outcome.result_text = ""
            outcome.raw_stdout = ""
            outcome.raw_stderr = (
                'jetski: no output produced - a tool required the "read_url" '
                "permission that headless mode cannot prompt for, so it was "
                "auto-denied."
            )
            await emit("provider_done", {"message": "권한 거부"})
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
        elif "SEARCH_BLOCKED" in message:
            # 검색은 정상, 열람만 전멸. 허용 목록 밖 주소(elsevier)는 아예
            # 부르지 않고, 허용된 주소는 403·로그인·유료벽에 막힌다.
            await call("WebSearch", {"query": "열람 실패 재현"})
            await call(
                "WebFetch",
                {"url": "https://ieeexplore.ieee.org/document/1"},
                ok=False,
                error="HTTP 403 - institutional login required",
            )
            await call(
                "WebFetch",
                {"url": "https://dl.acm.org/doi/10.1145/1"},
                ok=False,
                error="HTTP 403 - paywall",
            )
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

    if "SEARCH_BLOCKED" in message:
        # 한 건도 열지 못했다. 그래도 후보는 버리지 않고 검색 결과에서 본
        # 제목을 reported_title 에 남기며, 감사 블록은 반드시 출력한다.
        payload = {
            "rounds": [
                {
                    "round": 1,
                    "channel": "web",
                    "queries": ["열람 실패 재현"],
                    "note": "1차",
                }
            ],
            "term_expansions": [],
            "candidates": [
                {
                    "group": None,
                    "provisional": True,
                    "channel": "web",
                    "doc_type": "paper",
                    "doc_number": "",
                    "doi": "10.1145/1",
                    "title": "",
                    "reported_title": "Learning Automatic Rigging for Humanoid Characters",
                    "applicant": "",
                    "url": "https://dl.acm.org/doi/10.1145/1",
                    "provenance": "search_snippet",
                    "evidence_status": "candidate_only",
                    "note": "유료벽에 막혀 본문을 보지 못했습니다.",
                    "mapping": [],
                },
                {
                    "group": None,
                    "provisional": True,
                    "channel": "web",
                    "doc_type": "paper",
                    "doc_number": "",
                    "doi": "10.1000/blocked",
                    "title": "",
                    "reported_title": "A Survey of Skinning Weight Prediction",
                    "applicant": "",
                    "url": "https://www.sciencedirect.com/science/article/pii/S1",
                    "provenance": "search_snippet",
                    "evidence_status": "candidate_only",
                    "note": "허용 목록에 없는 호스트라 열지 않았습니다.",
                    "mapping": [],
                },
            ],
            "access_failures": [
                {
                    "url": "https://ieeexplore.ieee.org/document/1",
                    "reason": "로그인 요구",
                },
                {"url": "https://dl.acm.org/doi/10.1145/1", "reason": "유료벽 403"},
                {
                    "url": "https://www.sciencedirect.com/science/article/pii/S1",
                    "reason": "허용 목록에 없는 호스트라 열지 않음",
                },
            ],
        }
        return (
            report
            + chr(10)
            + "[ARIA_SEARCH_LOG_V1]"
            + chr(10)
            + json.dumps(payload, ensure_ascii=False)
            + chr(10)
            + "[/ARIA_SEARCH_LOG_V1]"
            + chr(10)
        )

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
