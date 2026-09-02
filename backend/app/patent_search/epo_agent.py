"""EPO OPS 검색 루프 — 모델이 전략을 세우고 ARIA 가 호출한다.

retrieval/agent.py 와 같은 모양이다. 라운드마다 모델을 한 번 부르고, 모델이
돌려준 구조화 action 을 ARIA 가 실행하고, 결과를 다음 라운드 입력으로 넣는다.
다른 점은 호출 하나하나가 **계정 할당량을 쓴다**는 것이라, 예산이 프롬프트의
부탁이 아니라 코드의 상한이다.

무엇을 이 루프가 하지 않는가
----------------------------
A/B/C 분류도, 보고서도, 웹 채널과의 병합도 하지 않는다. 이 루프의 산출물은
"검증 가능한 후보 목록 + 무엇을 얼마나 썼는지"뿐이다. 그 뒤는 3단계다.

상한은 전부 여기서 강제된다
---------------------------
프롬프트에도 숫자를 적지만 그건 안내다. 실제로 멈추는 것은 이 파일이다. 둘이
어긋나면 프롬프트가 낡은 것이므로 system_prompt 가 예산 객체에서 숫자를 읽어
간다.

취소는 다섯 지점에서 본다
-------------------------
모델 호출 전 · 모델 호출 후 · 검색 호출 전 · 검색 호출 후 · **재시도 대기 중**.
마지막이 중요하다. Retry-After 로 20초를 기다리는 동안 취소를 못 보면, 사용자가
멈춘 실행이 20초 더 살아 있으면서 할당량을 쓴다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import epo_actions, epo_client, epo_cql, epo_prompts, epo_quota
from .base import PatentSearchError, PatentSearchNotConfigured

# --- 종료 사유. 화면·기록이 같은 값을 쓴다 --------------------------------
TERM_LLM_FINISHED = "llm_finished"
TERM_ROUND_LIMIT = "round_limit"
TERM_SEARCH_CALL_LIMIT = "search_call_limit"
TERM_DETAIL_FETCH_LIMIT = "detail_fetch_limit"
TERM_TIMEOUT = "timeout"
TERM_THROTTLED = "throttled"
TERM_QUOTA_EXCEEDED = "quota_exceeded"
TERM_CANCELLED = "cancelled"
TERM_AUTH_FAILED = "authentication_failed"
TERM_PROVIDER_ERROR = "provider_error"
TERM_INVALID_RESPONSE_LIMIT = "invalid_response_limit"
# 계획 턴은 NO_TOOLS 다. 모델이 그 턴에서 외부 도구를 불렀다면 그 응답은
# 계획이 아니라 실행이며, 안에 든 action 을 우리가 대신 실행할 수 없다.
TERM_UNAUTHORIZED_TOOL_USE = "unauthorized_tool_use"

TERMINATION_REASONS = (
    TERM_LLM_FINISHED,
    TERM_ROUND_LIMIT,
    TERM_SEARCH_CALL_LIMIT,
    TERM_DETAIL_FETCH_LIMIT,
    TERM_TIMEOUT,
    TERM_THROTTLED,
    TERM_QUOTA_EXCEEDED,
    TERM_CANCELLED,
    TERM_AUTH_FAILED,
    TERM_PROVIDER_ERROR,
    TERM_INVALID_RESPONSE_LIMIT,
    TERM_UNAUTHORIZED_TOOL_USE,
)

# --- 도구 격리 수준. 같은 위반이라도 무엇이 막았는지가 다르다 --------------
#
# Claude 는 --tools 로 도구 노출 자체를 막을 수 있다(정책 강제). agy·Codex 는
# 도구를 끌 수단이 없어 ARIA 가 스트림을 보고 **사후에** 탐지한다. 결과는 둘 다
# "그 출력을 버린다"로 같지만, 기록에는 달라야 한다 — 앞의 것은 "일어날 수
# 없다"이고 뒤의 것은 "일어났고 우리가 알아챘다"이다. 이 둘을 같은 값으로 적으면
# 감사 기록이 실제로 아는 것보다 강해진다.
ISOLATION_ENFORCED = "provider_enforced"      # CLI 단계에서 도구를 껐다
ISOLATION_POST_HOC = "post_hoc_detection"     # 끌 수 없어 호출을 사후 탐지한다
ISOLATION_UNKNOWN = "unknown"                 # Provider 가 알려주지 않았다
ISOLATION_LEVELS = (ISOLATION_ENFORCED, ISOLATION_POST_HOC, ISOLATION_UNKNOWN)


@dataclass
class ChannelBudget:
    """EPO **채널 전체**가 나눠 쓰는 예산. 레인마다 따로 두지 않는다.

    근거는 원 명세의 문구다.

        "**작업당** OPS 검색 요청 최대 6회"
        "EPO **채널 전체** 제한시간 180초"

    둘 다 작업/채널 단위이지 레인 단위가 아니다. 레인마다 6회를 주면 EPO 레인
    두 개에서 12회가 나가고, 그건 명세가 정한 예산의 두 배다. 반면 "라운드당
    3회"와 "최대 2라운드"는 루프 하나의 성질이므로 레인마다 따로 센다.

    상세 조회 12건도 같은 목록에 있으므로 작업당으로 읽는다.
    """

    max_search_calls: int = 6
    max_detail_fetches: int = 12
    # 채널 전체 벽시계. OPS HTTP 대기 시간 예산과 **다른 축**이다 — 이쪽은
    # 모델이 생각하는 시간을 포함한다.
    deadline_seconds: float = 180.0

    searches_used: int = 0
    details_used: int = 0

    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _started: float = field(default_factory=time.monotonic, init=False, repr=False)

    def start(self) -> None:
        """벽시계를 지금부터 잰다. 채널 시작 시점에 한 번 부른다."""
        with self._lock:
            self._started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def expired(self) -> bool:
        if not self.deadline_seconds:
            return False
        return self.elapsed >= self.deadline_seconds

    def take_search(self) -> bool:
        """검색 한 번을 차지한다. 남지 않았으면 False."""
        with self._lock:
            if self.searches_used >= self.max_search_calls:
                return False
            self.searches_used += 1
            return True

    def take_detail(self) -> bool:
        with self._lock:
            if self.details_used >= self.max_detail_fetches:
                return False
            self.details_used += 1
            return True

    def to_dict(self) -> dict:
        return {
            "scope": "channel",
            "max_search_calls": self.max_search_calls,
            "max_detail_fetches": self.max_detail_fetches,
            "deadline_seconds": self.deadline_seconds,
            "searches_used": self.searches_used,
            "details_used": self.details_used,
            "elapsed_seconds": round(self.elapsed, 3),
        }


@dataclass(frozen=True)
class EpoAgentBudget:
    """**레인 하나**의 예산. 채널 전체 예산은 ChannelBudget 이 따로 든다.

    max_search_calls / max_detail_fetches 는 채널 예산을 주지 않았을 때의
    기본값으로만 쓴다(단일 레인 실행·테스트). 레인이 둘 이상이면 반드시
    ChannelBudget 을 공유해야 한다.
    """

    # 라운드는 없다. 검색계획 턴은 한 번이고, 그 한 번의 응답에 서로 다른
    # 목적의 CQL 을 max_search_calls_per_round 개까지 담는다. 첫 결과를 보고
    # 다시 검색어를 짜는 적응형 2라운드는 없앴다 — 결과를 읽는 판단은 검색하지
    # 않는 최종 선택 턴 하나로 모았다.
    max_search_calls: int = 6
    max_search_calls_per_round: int = 3
    max_detail_fetches: int = 12
    # 질의 하나가 받아 오는 결과 건수의 상한. OPS 자체 상한
    # (epo_client.MAX_RESULTS_PER_QUERY) 보다 크게 잡아도 그쪽이 먼저 막는다.
    # 여기 값을 낮추면 모델이 더 큰 수를 적어도 ARIA 가 깎고 그 사실을 남긴다.
    max_results_per_query: int = epo_client.MAX_RESULTS_PER_QUERY
    # 최종 대응표로 넘길 유망 후보 수의 상한. 검색 결과 상한과 다른 축이다 —
    # 20건을 받아 보는 것과 20건을 공식 검증까지 끌고 가는 것은 비용이 다르다.
    shortlist_limit: int = 5
    # 형식 오류·잘못된 질의를 몇 번까지 되돌려 줄 것인가. 이 상한이 없으면
    # 모델이 같은 실수를 반복하는 동안 라운드가 계속 소모된다.
    max_invalid_responses: int = 3
    # 한 라운드 결과 payload 의 최대 문자 수. 넘으면 후보를 줄이고 그 사실을
    # 기록한다. 조용히 자르면 모델은 전부 봤다고 믿는다.
    max_round_result_chars: int = 40_000

    def to_dict(self) -> dict:
        return {
            "max_search_calls": self.max_search_calls,
            "max_search_calls_per_round": self.max_search_calls_per_round,
            "max_detail_fetches": self.max_detail_fetches,
            "max_results_per_query": self.max_results_per_query,
            "shortlist_limit": self.shortlist_limit,
            "max_invalid_responses": self.max_invalid_responses,
            "max_round_result_chars": self.max_round_result_chars,
        }


@dataclass
class RoundRecord:
    """라운드 하나에서 실제로 일어난 일."""

    # 모델 호출 순번. 형식 오류로 거절된 시도까지 전부 센다.
    round: int
    started_at: str
    completed_at: str = ""
    # 이 시도가 검색 라운드를 소모했는가. 형식 오류는 소모하지 않는다 —
    # 그러지 않으면 형식 실수 두 번으로 검색을 한 번도 못 해 보고 끝난다.
    counts_as_round: bool = False
    status: str = "running"
    input_sha256: str = ""
    output_sha256: str = ""
    input_chars: int = 0
    output_chars: int = 0
    actions: int = 0
    search_calls: int = 0
    detail_fetches: int = 0
    new_candidates: int = 0
    errors: list = field(default_factory=list)
    # OPS 가 돌려준 오류의 구조화 형태. errors 는 사람이 읽는 문장이고 이쪽은
    # 화면·보고서가 상태와 fault code 를 따로 읽을 수 있는 값이다.
    faults: list = field(default_factory=list)
    # 이 라운드에서 감지된 도구 호출. NO_TOOLS 턴이므로 비어 있어야 한다.
    tool_uses: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "counts_as_round": self.counts_as_round,
            "status": self.status,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "actions": self.actions,
            "search_calls": self.search_calls,
            "detail_fetches": self.detail_fetches,
            "new_candidates": self.new_candidates,
            "errors": list(self.errors),
            "faults": [dict(item) for item in self.faults],
            "tool_uses": list(self.tool_uses),
        }


@dataclass
class CandidateRecord:
    """후보 하나. 값은 전부 보존된 응답에서 나왔다.

    evidence 는 필드별 (artifact_id, field_path, profile_id) 다. 3단계의 발췌
    검증이 이 참조로 원본을 다시 읽는다.
    """

    doc_number: str
    title: str = ""
    source_url: str = ""
    first_seen_round: int = 0
    artifact_ids: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    def to_dict(self) -> dict:
        return {
            "doc_number": self.doc_number,
            "title": self.title,
            "source_url": self.source_url,
            "first_seen_round": self.first_seen_round,
            "artifact_ids": list(self.artifact_ids),
            "evidence": dict(self.evidence),
        }


#: 상한 때문에 처리하지 못한 것의 사유 코드. 화면·보고서가 같은 값을 읽는다.
EXCLUDED_SHORTLIST_LIMIT = "shortlist_limit"
EXCLUDED_UNKNOWN_DOC_NUMBER = "not_in_search_results"
EXCLUDED_RESULT_LIMIT = "max_results_per_query"
#: 최종 선택 턴에서 검색·조회를 요청했다. 실행하지 않고 사유만 남긴다.
EXCLUDED_SEARCH_IN_SELECTION = "search_action_in_selection_turn"
#: 결과 payload 의 문자 상한에 걸려 모델 입력에서 뺀 후보.
EXCLUDED_RESULT_PAYLOAD_LIMIT = "max_round_result_chars"
#: 계약 검사에서 거절된 응답이라 채택하지 않은 shortlist.
EXCLUDED_REJECTED_RESPONSE = "rejected_response"
#: 검색 단계에서 더 이상 실행하지 않는 action 을 모델이 요청했다.
EXCLUDED_RETIRED_ACTION = "retired_action"

#: 결과 payload 가 문자 상한을 넘을 때 무엇을 먼저 지키는가.
#:
#: 뒤에서부터 자르면 **가장 나중에 찾은 문헌이 언제나 먼저** 없어진다. 그것이
#: 모델이 아직 한 번도 보지 못한 후보이고, 최종 선택 턴은 바로 그것을 보여
#: 주려고 있는 턴이다. 그래서 배열 순서가 아니라 아래 순위로 남긴다.
KEEP_LAST_ROUND = "last_round"
KEEP_NOT_SHORTLISTED = "not_shortlisted"
KEEP_CANDIDATE_ORDER = "candidate_order"
KEEP_RANKING = (
    KEEP_LAST_ROUND,
    KEEP_NOT_SHORTLISTED,
    KEEP_CANDIDATE_ORDER,
)
_KEEP_LABELS = {
    KEEP_LAST_ROUND: "마지막 라운드가 데려온 후보라 모델이 아직 보지 못했음",
    KEEP_NOT_SHORTLISTED: "아직 shortlist 판단을 받지 못한 후보",
    KEEP_CANDIDATE_ORDER: "후보 목록 순서",
}

#: shortlist 항목이 어느 턴에서 왔는가. 검색 라운드와 최종 선택 턴은 예산도
#: 권한도 다르므로 기록에서 구분한다.
TURN_SEARCH = "search"
TURN_SELECTION = "selection"

#: 최종 선택 턴을 줄 수 있는 종료 사유.
#:
#: 검색이 정상적으로 끝난 경우에만 준다. 취소·인증 실패·할당량 초과·도구 위반
#: 뒤에 모델을 한 번 더 부르는 것은, 사용자가 멈춘 실행을 이어 가거나 신뢰할 수
#: 없는 출력을 한 번 더 받는 것이다.
#: 이 사유로 끝난 레인은 **성공이 아니다.**
#:
#: 에이전트가 예외를 던지지 않고 run 객체를 돌려줬다는 사실과 "EPO 검색이
#: 됐다"는 다른 말이다. 인증이 막혀도, OPS 가 질의를 거절해도, 채널 시간이
#: 말라도 루프는 정상적으로 끝나고 객체는 만들어진다. 그 객체를 ``ok`` 로
#: 적으면 화면과 매니페스트에서 "검색 0건"과 "검색 실패"가 같아 보인다.
FAILED_TERMINATIONS = frozenset(
    {
        TERM_TIMEOUT,
        TERM_THROTTLED,
        TERM_QUOTA_EXCEEDED,
        TERM_AUTH_FAILED,
        TERM_PROVIDER_ERROR,
        TERM_INVALID_RESPONSE_LIMIT,
        TERM_UNAUTHORIZED_TOOL_USE,
    }
)


def lane_status(run) -> str:
    """레인 하나의 상태. ``ok`` / ``failed`` / ``cancelled``.

    상태를 정하는 곳을 하나로 둔다. 호출부마다 판정하면 화면과 보고서가 같은
    실행을 다르게 읽는다.
    """
    if run is None:
        return "failed"
    if getattr(run, "cancelled", False):
        return "cancelled"
    if getattr(run, "termination_reason", "") in FAILED_TERMINATIONS:
        return "failed"
    return "ok"


SELECTION_TURN_TERMINATIONS = frozenset(
    {
        TERM_LLM_FINISHED,
        TERM_ROUND_LIMIT,
        TERM_SEARCH_CALL_LIMIT,
        TERM_DETAIL_FETCH_LIMIT,
    }
)


@dataclass
class EpoSearchRun:
    """루프 하나의 결과 전부."""

    rounds: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)
    search_calls: int = 0
    detail_fetches: int = 0
    invalid_responses: int = 0
    termination_reason: str = ""
    termination_detail: str = ""
    cancelled: bool = False
    notes: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    queries: list = field(default_factory=list)
    # 첫 응답의 검색 전략. 뒤 라운드가 다시 보내도 첫 것을 유지한다.
    claim_analysis: dict = field(default_factory=dict)
    # 모델이 고른 유망 후보. 상한을 넘긴 것은 여기 없고 excluded 에 있다.
    shortlist: list = field(default_factory=list)
    # 상한·검증 때문에 처리되지 않은 것. **조용히 누락하지 않는다.**
    excluded: list = field(default_factory=list)
    # 계획 턴에서 감지된 도구 호출. 비어 있지 않으면 그 라운드의 출력은 버렸다.
    tool_violations: list = field(default_factory=list)
    tool_isolation: str = ISOLATION_UNKNOWN
    # 검색하지 않는 최종 선택 턴의 기록. 돌리지 않았으면 빈 dict 다.
    selection: dict = field(default_factory=dict)

    def exclude(self, *, kind: str, reason_code: str, detail: str, value: str = "") -> None:
        self.excluded.append(
            {
                "kind": kind,
                "value": value,
                "reason_code": reason_code,
                "detail": detail,
            }
        )

    def to_dict(self) -> dict:
        return {
            "rounds": [record.to_dict() for record in self.rounds],
            "candidates": [item.to_dict() for item in self.candidates.values()],
            "search_calls": self.search_calls,
            "detail_fetches": self.detail_fetches,
            "invalid_responses": self.invalid_responses,
            "termination_reason": self.termination_reason,
            "termination_detail": self.termination_detail,
            "cancelled": self.cancelled,
            "notes": list(self.notes),
            "usage": dict(self.usage),
            "queries": list(self.queries),
            "claim_analysis": dict(self.claim_analysis),
            "shortlist": [dict(item) for item in self.shortlist],
            "excluded": [dict(item) for item in self.excluded],
            "tool_violations": [dict(item) for item in self.tool_violations],
            "tool_isolation": self.tool_isolation,
            "selection": dict(self.selection),
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _compact_number(value) -> str:
    """공개번호 대조용 표기. 국가코드는 **남긴다**.

    떼어 내면 EP1000000 과 US1000000 이 같아진다. 이 함수가 맞추는 것은 표기
    차이(공백·하이픈·대소문자)뿐이고, 종류코드(A1/B2)는 뒤에 붙었을 때만
    떼어 낸다 — 모델은 "EP 1 000 000" 이라고 적고 OPS 는 "EP1000000A1" 을
    돌려주기 때문이다.
    """
    compact = re.sub(r"[^0-9A-Z]", "", str(value or "").upper())
    if not compact:
        return ""
    if re.match(r"^[A-Z]{2}\d", compact):
        return re.sub(r"[A-Z]\d?$", "", compact)
    return compact


def _provider_usage(value) -> dict:
    """Provider 가 준 사용량을 감사 기록에 넣을 수 있는 모양으로 줄인다.

    Provider 마다 모양이 다르고(토큰 수·비용·모델 이름), ARIA 는 그 스키마를
    정하지 않는다. 그래서 해석하지 않고 **옮기기만** 한다. 다만 감사 기록은
    JSON 으로 직렬화되므로 스칼라와 한 겹 아래의 숫자만 남긴다 — 여기로
    본문이나 객체가 들어와 매니페스트를 부풀리지 않게 한다.
    """
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for name, item in value.items():
        if len(out) >= 40:
            break
        key = str(name)[:80]
        if isinstance(item, (bool, int, float)):
            out[key] = item
        elif isinstance(item, str):
            out[key] = item[:200]
        elif isinstance(item, dict):
            nested = {
                str(inner)[:80]: number
                for inner, number in item.items()
                if isinstance(number, (int, float)) and not isinstance(number, bool)
            }
            if nested:
                out[key] = dict(list(nested.items())[:40])
    return out


async def _noop_emit(_event_type: str, _payload: dict) -> None:
    return None


class EpoSearchAgent:
    """청구항 하나에 대한 EPO 검색 루프."""

    def __init__(
        self,
        *,
        job_id: str,
        provider,
        model: str | None,
        timeout_seconds: int,
        work_dir: Path,
        claim_text: str,
        spec_text: str = "",
        backend=None,
        budget: EpoAgentBudget | None = None,
        channel: ChannelBudget | None = None,
        lane_id: str = "",
        emit=None,
        is_cancelled=None,
        sleep=None,
    ) -> None:
        self.job_id = job_id
        self.provider = provider
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.work_dir = Path(work_dir)
        self.claim_text = claim_text
        # 명세서는 검색어를 넓히는 자료이지 검색 범위를 정하는 기준이 아니다.
        # 웹 레인과 같은 원칙이라 별도 경계 안에 넣는다.
        self.spec_text = spec_text
        self.backend = backend
        self.budget = budget or EpoAgentBudget()
        # 채널 예산을 주지 않으면 이 레인만 쓰는 것을 하나 만든다. 레인이
        # 둘 이상인 실행에서는 러너가 하나를 만들어 모든 레인에 같은 것을
        # 넘겨야 한다 — 안 그러면 레인 수만큼 예산이 늘어난다.
        self.channel = channel or ChannelBudget(
            max_search_calls=self.budget.max_search_calls,
            max_detail_fetches=self.budget.max_detail_fetches,
        )
        self.lane_id = lane_id
        self.emit = emit or _noop_emit
        self.is_cancelled = is_cancelled or (lambda: False)
        # 재시도 대기의 바닥 함수. 테스트가 실제로 20초를 자지 않도록 바꿔 낀다.
        self.sleep = sleep or time.sleep
        # 아직 검증하지 않은 shortlist 항목. 마지막 라운드의 검색이 데려온
        # 후보를 가리킬 수 있으므로, 대조는 루프가 끝난 뒤에 한 번만 한다.
        self._pending_shortlist: list = []

    # ------------------------------------------------------------ 실행

    async def run(self) -> EpoSearchRun:
        """검색 루프를 돌고, 그 뒤 **검색하지 않는** 최종 선택 턴을 한 번 준다.

        두 단계로 나눈 이유는 하나다. 검색 루프는 마지막 라운드의 결과를 모델에게
        보여 주지 못한 채 끝난다 — 모델이 [검색, finish] 를 한 응답에 보내면 그
        검색이 데려온 문헌을 모델은 한 번도 보지 못한다. 그 결과가 shortlist 평가를
        받지 못하면, 가장 마지막에(= 가장 좁혀서) 찾은 문헌이 늘 버려진다.

        최종 선택 턴은 검색 예산을 쓰지 않는다. OPS 호출을 아예 허용하지 않고
        shortlist 와 finish 만 받는다.
        """
        run = EpoSearchRun()
        # 재시도 대기 중에도 취소를 본다. OpsClient 는 자기 sleep 을 쓰므로
        # 여기서 갈아 끼운다.
        self._install_cancellable_sleep()
        await self._search_loop(run)
        await self._selection_turn(run)
        return self._finish(run)

    async def _search_loop(self, run: EpoSearchRun) -> None:
        """검색계획 턴을 **한 번** 돈다. 종료 사유를 run 에 적고 돌아온다.

        아래 while 은 라운드가 아니라 **형식 오류 재시도**다. 응답을 action 으로
        읽지 못했거나 계약(claim_analysis)을 지키지 않았을 때만 다시 묻고, 그
        횟수는 max_invalid_responses 가 센다. 계약을 통과한 응답을 실행하고
        나면 곧바로 빠져나온다.

        첫 결과를 모델에게 다시 주고 두 번째 검색계획을 받는 적응형 라운드는
        없다. 검색 결과를 읽는 판단은 OPS 를 부르지 않는 최종 선택 턴 하나로
        모았다 — 같은 판단을 두 군데서 하면 어느 쪽이 정본인지 알 수 없다.
        """
        pending_error = ""
        results_payload: list[dict] = []

        executed = False
        attempt = 0
        while not executed:
            if self._stop_for_cancel(run):
                return

            attempt += 1
            round_no = attempt
            record = RoundRecord(round=round_no, started_at=_utcnow())
            user_message = self._render(round_no, run, results_payload, pending_error)
            system_prompt = epo_prompts.system_prompt(self.budget)
            record.input_chars = len(system_prompt) + len(user_message)
            record.input_sha256 = _sha256(system_prompt + "\n\x00\n" + user_message)
            self._write(round_no, "in", f"{system_prompt}\n\n---\n\n{user_message}")

            await self.emit(
                "epo_progress",
                {
                    "phase": "round",
                    "round": round_no,
                    "search_calls": run.search_calls,
                    "max_search_calls": self.budget.max_search_calls,
                },
            )

            outcome = await self._ask_model(system_prompt, user_message, round_no)
            record.completed_at = _utcnow()
            record.output_chars = len(outcome.result_text or "")
            record.output_sha256 = _sha256(outcome.result_text or "")
            self._write(round_no, "out", outcome.result_text or "")

            # 모델 호출 **후** 취소 확인. 여기서 안 보면 방금 받은 응답으로
            # OPS 호출까지 나가고, 그 할당량은 되돌릴 수 없다.
            if self._stop_for_cancel(run, record=record):
                return

            # 도구 검사는 파싱보다 **먼저** 한다. 순서를 뒤집으면 위반 응답의
            # action 이 이미 만들어진 뒤에 버리게 되고, 그 사이에 실수로 한
            # 줄만 실행돼도 되돌릴 수 없다.
            violation = self._tool_violation(outcome)
            if violation is not None:
                record.status = TERM_UNAUTHORIZED_TOOL_USE
                record.errors.append(violation["detail"])
                record.tool_uses = list(violation["tools"])
                run.rounds.append(record)
                run.tool_violations.append(violation)
                run.termination_reason = TERM_UNAUTHORIZED_TOOL_USE
                run.termination_detail = violation["detail"]
                return

            stopped = self._provider_problem(outcome)
            if stopped is not None:
                reason, detail = stopped
                record.status = reason
                record.errors.append(detail)
                run.rounds.append(record)
                run.termination_reason = reason
                run.termination_detail = detail
                run.cancelled = reason == TERM_CANCELLED
                return

            try:
                response = epo_actions.parse_response(outcome.result_text or "")
            except epo_actions.ActionError as exc:
                record.status = "parse_error"
                record.errors.append(str(exc))
                run.rounds.append(record)
                run.invalid_responses += 1
                if run.invalid_responses >= self.budget.max_invalid_responses:
                    run.termination_reason = TERM_INVALID_RESPONSE_LIMIT
                    run.termination_detail = (
                        f"응답을 {run.invalid_responses}번 읽지 못했습니다. "
                        "같은 실수가 반복되어 루프를 끝냅니다."
                    )
                    return
                pending_error = (
                    f"이전 응답을 action 으로 읽지 못했습니다: {exc} "
                    "JSON 객체 하나만, 설명 없이 돌려주십시오."
                )
                results_payload = []
                # 형식 오류는 라운드를 소모하지 않는다. 대신 위의 상한이 센다.
                continue

            pending_error = ""
            self._record_retired(response, run)
            record.actions = len(response.actions)
            if response.strategy:
                run.notes.append(f"round {round_no}: {response.strategy}")
            self._absorb_analysis(response, run, round_no)

            # 청구항 분석은 **계약**이다. 없으면 검색을 실행하지 않는다.
            #
            # 안내로만 두면 모델은 검색부터 하고 분석은 나중에 적는다. 그러면
            # 기록에 남는 것은 "이 검색어를 왜 골랐는가"가 아니라 "찾고 나서
            # 어떻게 설명했는가"이고, 그 둘은 다른 문서다. 여기서 막는 것이
            # 그 차이를 지키는 유일한 지점이다.
            if self._needs_claim_analysis(response, run):
                record.status = "missing_claim_analysis"
                record.errors.append(
                    "첫 응답에 claim_analysis 가 없어 검색 action 을 실행하지 "
                    "않았습니다."
                )
                run.rounds.append(record)
                run.invalid_responses += 1
                self._discard_shortlist(
                    response,
                    run,
                    round_no,
                    why="claim_analysis 가 없어 이 응답을 실행하지 않았으므로",
                )
                if run.invalid_responses >= self.budget.max_invalid_responses:
                    run.termination_reason = TERM_INVALID_RESPONSE_LIMIT
                    run.termination_detail = (
                        "청구항 분석 없이 검색하려는 응답이 반복되어 루프를 "
                        "끝냅니다. OPS 호출은 한 번도 나가지 않았습니다."
                    )
                    return
                pending_error = (
                    "claim_analysis 가 없어 이번 검색을 실행하지 않았습니다. "
                    "검색어를 만들기 전에 청구항을 어떻게 나눠 읽었는지 "
                    "claim_analysis 로 먼저 적고, 같은 응답에 검색 action 을 "
                    "함께 돌려주십시오."
                )
                # 검색을 하지 않았으므로 보여 줄 새 결과도 없다.
                continue

            if not response.actions:
                record.status = "no_actions"
                record.errors.append("action 이 비어 있습니다.")
                run.rounds.append(record)
                run.invalid_responses += 1
                self._discard_shortlist(
                    response,
                    run,
                    round_no,
                    why="action 이 비어 있어 이 응답을 거절했으므로",
                )
                if run.invalid_responses >= self.budget.max_invalid_responses:
                    run.termination_reason = TERM_INVALID_RESPONSE_LIMIT
                    run.termination_detail = "빈 응답이 반복되어 루프를 끝냅니다."
                    return
                pending_error = (
                    "action 이 비어 있습니다. 검색을 더 할 것이 없으면 "
                    f'{{"action":"{epo_actions.ACTION_FINISH}"}} 를 돌려주십시오.'
                )
                results_payload = []
                continue

            # 계약을 모두 통과했다. 이 응답은 채택됐고, shortlist 도 여기서
            # 처음 받는다. 위의 거절 경로들은 이 줄에 오지 못한다.
            self._absorb_shortlist(response, round_no)
            before = set(run.candidates)
            invalid_before = run.invalid_responses
            outcome_reason = await self._execute(response.actions, run, record)
            record.new_candidates = len(set(run.candidates) - before)
            query_errors = run.invalid_responses - invalid_before

            # 검색이 한 줄도 나가지 않았고 그 이유가 잘못된 질의라면 이 턴은
            # **실행된 것이 아니다.** 계획 턴이 하나뿐이라 여기서 되묻지 않으면
            # 레인은 OPS 를 한 번도 부르지 못한 채 정상 종료로 읽힌다. 되묻는
            # 횟수는 형식 오류와 같은 상한(max_invalid_responses)이 센다.
            if record.search_calls == 0 and query_errors:
                record.status = "invalid_query"
                run.rounds.append(record)
                if run.invalid_responses >= self.budget.max_invalid_responses:
                    run.termination_reason = TERM_INVALID_RESPONSE_LIMIT
                    run.termination_detail = (
                        "검색식을 만들지 못한 응답이 반복되어 끝냅니다. "
                        "OPS 호출은 한 번도 나가지 않았습니다."
                    )
                    return
                pending_error = (
                    "보내 주신 검색식을 하나도 만들지 못했습니다. 아래 오류를 "
                    "보고 질의를 고쳐 다시 보내십시오."
                )
                results_payload = []
                continue

            executed = True
            record.counts_as_round = True
            record.status = record.status if record.status != "running" else "ok"
            run.rounds.append(record)

            if outcome_reason is not None:
                run.termination_reason = outcome_reason[0]
                run.termination_detail = outcome_reason[1]
                run.cancelled = outcome_reason[0] == TERM_CANCELLED
                return

            # 검색 예산을 다 썼으면 그것이 멈추는 이유다. "새 후보가 없어서"로
            # 적으면 더 찾을 수 있었는데 안 찾은 것처럼 읽힌다.
            if self.channel.searches_used >= self.channel.max_search_calls:
                run.termination_reason = TERM_SEARCH_CALL_LIMIT
                run.termination_detail = (
                    f"OPS 검색 호출 상한({self.channel.max_search_calls}회, 작업 "
                    "전체)을 다 썼습니다."
                )
                return
            if self.channel.expired():
                run.termination_reason = TERM_TIMEOUT
                run.termination_detail = (
                    f"EPO 채널 제한시간({self.channel.deadline_seconds:.0f}초)을 "
                    "넘겼습니다."
                )
                return

        run.termination_reason = TERM_ROUND_LIMIT
        run.termination_detail = (
            "검색계획 턴을 마쳤습니다. 결과 판단은 최종 선택 턴에서 합니다."
        )
        return

    # --------------------------------------------------------- 내부 단계

    def _install_cancellable_sleep(self) -> None:
        client = getattr(self.backend, "_client", None)
        if client is not None:
            client.sleep = epo_client.cancellable_sleep(
                self.is_cancelled, sleep=self.sleep
            )

    def _stop_for_cancel(self, run: EpoSearchRun, record: RoundRecord | None = None) -> bool:
        if not self.is_cancelled():
            return False
        if record is not None:
            record.status = "cancelled"
            record.completed_at = record.completed_at or _utcnow()
            run.rounds.append(record)
        run.cancelled = True
        run.termination_reason = TERM_CANCELLED
        run.termination_detail = "사용자가 실행을 취소했습니다."
        return True

    async def _ask_model(self, system_prompt: str, user_message: str, round_no: int):
        from ..providers.base import NO_TOOLS, ExecutionRequest

        round_dir = self.work_dir / "rounds"
        round_dir.mkdir(parents=True, exist_ok=True)
        request = ExecutionRequest(
            job_id=self.job_id,
            work_dir=round_dir,
            system_prompt=system_prompt,
            user_message=user_message,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            tool_policy=NO_TOOLS,
        )
        return await self.provider.execute(request, self.emit)

    # ------------------------------------------------- 계획 턴의 도구 금지

    def _isolation_level(self, outcome=None) -> str:
        """이 Provider 가 도구를 **막았는가**, 아니면 사후에 볼 뿐인가.

        Provider 가 실행 결과에 적어 둔 것으로만 판정한다. Provider id 로
        분기하지 않는다 — 목록을 코드에 박아 두면 새 Provider 가 조용히
        '강제됨'으로 기록된다.
        """
        if outcome is not None:
            if getattr(outcome, "tools_uncontrollable", False):
                return ISOLATION_POST_HOC
            if getattr(outcome, "tools_must_be_disabled", False):
                return ISOLATION_ENFORCED
        supported = getattr(self.provider, "supported_tool_policies", None)
        if supported:
            from ..providers.base import NO_TOOLS

            return (
                ISOLATION_ENFORCED
                if NO_TOOLS.name in supported
                else ISOLATION_POST_HOC
            )
        return ISOLATION_UNKNOWN

    def _tool_violation(self, outcome) -> dict | None:
        """계획 턴에서 외부 도구를 불렀는가. 불렀으면 그 출력은 쓰지 않는다.

        이 턴의 정책은 NO_TOOLS 다. Claude 처럼 CLI 단계에서 도구를 끌 수 있는
        Provider 에서는 여기 걸릴 일이 없고, agy·Codex 처럼 끌 수단이 없는
        Provider 에서는 **사후 탐지**가 유일한 관문이다.

        걸린 출력은 통째로 버린다. 도구를 부른 응답 안의 epo_search /
        epo_fetch_document 를 우리가 대신 실행하지 않는다 — 그 응답은 이미
        우리가 보지 못한 외부 자료를 읽고 만들어진 것이라, 우리가 실행 기록을
        만들어 줄 수 있는 계획이 아니다.
        """
        names: list[str] = []
        for name in getattr(outcome, "tool_uses", ()) or ():
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        for call in getattr(outcome, "tool_calls", ()) or ():
            if not isinstance(call, dict):
                continue
            text = str(call.get("name") or "").strip()
            if text and text not in names:
                names.append(text)
        if not names:
            return None

        isolation = self._isolation_level(outcome)
        provider_id = str(getattr(self.provider, "id", "") or "")
        return {
            "provider": provider_id,
            "lane": self.lane_id,
            "policy": "no_tools",
            "tools": names,
            "isolation": isolation,
            "detected_at": _utcnow(),
            "detail": (
                f"EPO 검색 계획 턴은 도구 없는 실행인데 {provider_id or '이 Provider'} "
                f"가 도구를 {len(names)}개 호출했습니다({', '.join(names)}). "
                "이 응답의 action 은 하나도 실행하지 않고 폐기했습니다. "
                + (
                    "이 Provider 는 도구를 끌 수단이 없어 ARIA 가 사후에 "
                    "탐지했습니다."
                    if isolation == ISOLATION_POST_HOC
                    else "이 Provider 는 도구를 끄고 실행했는데도 호출이 "
                    "관측됐습니다."
                    if isolation == ISOLATION_ENFORCED
                    else "이 Provider 의 도구 통제 수준을 확인할 수 없습니다."
                )
            ),
        }

    # ------------------------------------------------------ 검색 전략 기록

    def _searched(self, run: EpoSearchRun) -> bool:
        """검색 라운드를 이미 한 번이라도 소모했는가.

        시도 횟수(round)와 다른 축이다. 형식 오류로 거절된 응답은 검색을 하지
        않았으므로, 그 뒤에 오는 분석은 여전히 **검색 전의** 판단이다.
        """
        return any(record.counts_as_round for record in run.rounds)

    def _needs_claim_analysis(self, response, run: EpoSearchRun) -> bool:
        """이 응답을 실행하기 전에 청구항 분석을 먼저 받아야 하는가.

        검색 action 이 하나라도 들어 있는 응답에만 요구한다. finish 만 있는
        응답은 실행할 검색이 없으므로 막을 이유도 없다.
        """
        if run.claim_analysis:
            return False
        return any(
            isinstance(action, epo_actions.EpoSearch)
            for action in (response.actions or ())
        )

    def _record_retired(self, response, run: EpoSearchRun) -> None:
        """모델이 요청했지만 더 이상 실행하지 않는 action 을 기록한다.

        조용히 버리지 않는다. 모델이 상세조회를 계속 요청한다면 그것은 프롬프트가
        아직 옛 계약을 말하고 있다는 신호이고, 기록에 남아야 다음에 고칠 수 있다.
        """
        for name in getattr(response, "retired_actions", None) or ():
            run.exclude(
                kind="action",
                value=str(name),
                reason_code=EXCLUDED_RETIRED_ACTION,
                detail=(
                    "검색 단계에서는 문헌 상세조회를 하지 않습니다. 문헌 본문은 "
                    "후보를 합친 뒤 공식 검증 단계에서만 받습니다."
                ),
            )

    def _absorb_analysis(self, response, run: EpoSearchRun, round_no: int) -> None:
        """첫 응답의 claim_analysis 를 모은다. **shortlist 는 여기서 받지 않는다.**

        나눠 둔 이유는 하나다. 이 함수는 계약 검사(claim_analysis 유무·빈 action)
        **전에** 불린다 — 같은 응답이 분석과 검색을 함께 보냈을 때 그 분석으로
        계약을 만족시켜야 하기 때문이다. 그 자리에서 shortlist 까지 받으면,
        거절된 응답이 고른 후보가 살아남아 나중 검색이 같은 번호를 데려온 순간
        되살아난다. 거절은 "그 출력을 쓰지 않는다"는 뜻이므로 그래서는 안 된다.
        shortlist 는 응답이 계약을 통과한 뒤 :meth:`_absorb_shortlist` 가 받는다.

        claim_analysis 는 **검색 전에 온 첫 것만** 남긴다. 판정 기준은 라운드
        번호가 아니라 "검색을 이미 했는가"다. 형식 오류로 한 번 거절당한 뒤
        2차 시도에서 온 분석은 여전히 검색 전의 판단이므로 받아들이고, 검색이
        나간 뒤에 온 분석은 검색 전략이 아니라 결과 해설이므로 받지 않는다.
        둘을 같은 칸에 두면 "이 검색어를 왜 골랐는가"에 답할 수 없게 된다.
        """
        analysis = getattr(response, "claim_analysis", None)
        if analysis is not None and not analysis.empty:
            if run.claim_analysis:
                if round_no != run.claim_analysis.get("round"):
                    run.notes.append(
                        f"round {round_no}: 청구항 분석이 다시 왔지만 먼저 받은 "
                        "분석을 유지했습니다(검색 전략은 검색 전의 판단입니다)."
                    )
            elif self._searched(run):
                # 검색이 이미 나갔다. 이 분석은 검색어의 근거가 될 수 없으므로
                # 검색 전략 칸에 넣지 않는다. 버리지도 않는다 — 왔다는 사실은
                # 메모로 남긴다.
                run.notes.append(
                    f"round {round_no}: 검색이 이미 나간 뒤에 도착한 청구항 "
                    "분석이라 검색 전략으로 저장하지 않았습니다."
                )
            else:
                payload = analysis.to_dict()
                payload["round"] = round_no
                run.claim_analysis = payload

    def _absorb_shortlist(self, response, round_no: int) -> None:
        """**채택된** 응답의 shortlist 를 받는다. 대조는 루프가 끝난 뒤에 한다."""
        for item in getattr(response, "shortlist", ()) or ():
            self._pending_shortlist.append((round_no, TURN_SEARCH, item))

    def _discard_shortlist(
        self, response, run: EpoSearchRun, round_no: int, *, why: str
    ) -> None:
        """거절된 응답의 shortlist 를 버린다. 버렸다는 사실은 남긴다.

        조용히 버리면 "모델이 고르지 않았다"와 "우리가 안 받았다"가 같은 기록이
        된다. 다시 올리고 싶으면 모델은 계약을 지킨 응답에서 다시 적으면 된다.
        """
        for item in getattr(response, "shortlist", ()) or ():
            number = str(getattr(item, "doc_number", "") or "").strip()
            run.exclude(
                kind="shortlist",
                value=number,
                reason_code=EXCLUDED_REJECTED_RESPONSE,
                detail=(
                    f"round {round_no}: {why} 이 응답의 shortlist 는 채택하지 "
                    f"않았습니다('{number}'). 뒤 라운드가 같은 번호를 찾아도 "
                    "이 선택은 되살아나지 않습니다."
                ),
            )

    def _settle_shortlist(self, run: EpoSearchRun) -> None:
        """shortlist 를 보존된 후보와 대조하고 상한까지만 남긴다.

        빠뜨린 것은 전부 excluded 에 사유와 함께 적는다. 상한 때문에 잘린 것과
        검색 결과에 없어서 버린 것은 다른 사건이므로 사유 코드를 나눈다.
        """
        limit = max(0, int(self.budget.shortlist_limit or 0))
        seen: set[str] = set()
        for round_no, turn, item in self._pending_shortlist:
            where = (
                "최종 선택 턴" if turn == TURN_SELECTION else f"round {round_no}"
            )
            number = str(getattr(item, "doc_number", "") or "").strip()
            key = _compact_number(number)
            if not key or key not in {
                _compact_number(name) for name in run.candidates
            }:
                # 검색 결과에 없는 번호다. 모델이 기억으로 적었거나 지어냈다.
                run.exclude(
                    kind="shortlist",
                    value=number,
                    reason_code=EXCLUDED_UNKNOWN_DOC_NUMBER,
                    detail=(
                        f"{where} shortlist 의 '{number}' 는 이 레인이 보존한 "
                        "검색 결과에 없는 번호라 최종 후보에서 뺐습니다."
                    ),
                )
                continue
            if key in seen:
                continue
            if len(seen) >= limit:
                run.exclude(
                    kind="shortlist",
                    value=number,
                    reason_code=EXCLUDED_SHORTLIST_LIMIT,
                    detail=(
                        f"shortlist 상한({limit}건)을 넘어 '{number}' 를 최종 "
                        "후보에서 뺐습니다. 후보 자체는 레인 기록에 남아 있습니다."
                    ),
                )
                continue
            seen.add(key)
            payload = item.to_dict()
            payload["round"] = round_no
            payload["turn"] = turn
            # 모델이 적은 번호 대신 **보존된 응답의 표기**를 정본으로 쓴다.
            payload["doc_number"] = next(
                (name for name in run.candidates if _compact_number(name) == key),
                number,
            )
            run.shortlist.append(payload)

    # ------------------------------------------------- 최종 선택 턴

    async def _selection_turn(self, run: EpoSearchRun) -> None:
        """검색하지 않는 마지막 한 턴. shortlist 와 finish 만 받는다.

        왜 필요한가
        -----------
        검색 루프는 마지막 라운드의 **결과를 모델에게 보여 주지 못한 채** 끝난다.
        모델이 한 응답에 [검색, finish] 를 보내면 그 검색이 데려온 문헌을 모델은
        한 번도 보지 못하고, 예산 상한으로 끝난 경우도 마찬가지다. 그래서 가장
        나중에(= 가장 좁혀서) 찾은 문헌이 늘 shortlist 평가를 받지 못한다.

        무엇을 하지 않는가
        ------------------
        OPS 를 부르지 않는다. 이 턴의 검색·조회 action 은 실행하지 않고 사유만
        남긴다. 검색 예산도 라운드 수도 소모하지 않으므로, 사용량은 검색
        라운드와 **따로** 기록한다.
        """
        if run.termination_reason not in SELECTION_TURN_TERMINATIONS:
            return
        if not run.candidates or run.cancelled:
            return
        if self.is_cancelled():
            # 취소된 실행을 모델 호출 한 번으로 연장하지 않는다.
            return
        if self.channel.expired():
            run.selection = {
                "attempted": False,
                "reason": (
                    f"EPO 채널 제한시간({self.channel.deadline_seconds:.0f}초)을 "
                    "넘겨 최종 선택 턴을 돌리지 않았습니다."
                ),
            }
            return

        started_at = _utcnow()
        system_prompt = epo_prompts.selection_prompt(self.budget)
        payload = {
            "phase": TURN_SELECTION,
            "search_allowed": False,
            "shortlist_limit": self.budget.shortlist_limit,
            "search_calls_used": run.search_calls,
            "detail_fetches_used": run.detail_fetches,
            "termination_reason": run.termination_reason,
            "already_shortlisted": [
                str(getattr(item, "doc_number", "") or "")
                for _round, _turn, item in self._pending_shortlist
            ],
            "results": self._results_payload(run, None),
            "claim_text": self.claim_text,
        }
        user_message = epo_prompts.render_selection(payload)
        self._write(0, "selection.in", f"{system_prompt}\n\n---\n\n{user_message}")

        await self.emit(
            "epo_progress",
            {"phase": TURN_SELECTION, "candidates": len(run.candidates)},
        )
        try:
            outcome = await self._ask_model(system_prompt, user_message, 0)
        except Exception as exc:  # noqa: BLE001 - 이 턴의 실패가 검색을 지우지 않는다
            run.selection = {
                "attempted": True,
                "status": "provider_error",
                "reason": f"{type(exc).__name__}: {exc}",
                "started_at": started_at,
                "completed_at": _utcnow(),
            }
            return

        text = getattr(outcome, "result_text", "") or ""
        self._write(0, "selection.out", text)
        record = {
            "attempted": True,
            "status": "ok",
            "reason": "",
            "started_at": started_at,
            "completed_at": _utcnow(),
            "input_chars": len(system_prompt) + len(user_message),
            "input_sha256": _sha256(system_prompt + "\n\x00\n" + user_message),
            "output_chars": len(text),
            "output_sha256": _sha256(text),
            "candidates_reviewed": len(run.candidates),
            "shortlist_added": 0,
            "rejected_actions": 0,
            "tool_uses": [],
            # 이 턴도 토큰을 쓴다. 응답을 거절하더라도 쓴 것은 쓴 것이므로
            # 아래 어느 경로로 빠져나가든 사용량은 남는다.
            "provider_usage": _provider_usage(getattr(outcome, "usage", None)),
        }
        run.selection = record

        # 도구 검사는 여기서도 먼저 한다. 이 턴도 NO_TOOLS 다.
        violation = self._tool_violation(outcome)
        if violation is not None:
            violation["phase"] = TURN_SELECTION
            run.tool_violations.append(violation)
            record["status"] = TERM_UNAUTHORIZED_TOOL_USE
            record["reason"] = violation["detail"]
            record["tool_uses"] = list(violation["tools"])
            return

        if self._provider_problem(outcome) is not None:
            record["status"] = "provider_error"
            record["reason"] = "모델이 쓸 수 있는 응답을 돌려주지 않았습니다."
            return
        if self.is_cancelled():
            record["status"] = "cancelled"
            record["reason"] = "사용자가 실행을 취소했습니다."
            return

        try:
            response = epo_actions.parse_response(text)
        except epo_actions.ActionError as exc:
            # 여기서 되묻지 않는다. 이 턴은 보너스이지 계약이 아니고, 재시도로
            # 모델 호출을 늘리면 "검색하지 않는 턴"이 조용히 비싸진다.
            record["status"] = "parse_error"
            record["reason"] = str(exc)
            return

        self._record_retired(response, run)
        for action in response.actions or ():
            if isinstance(action, epo_actions.EpoSearch):
                record["rejected_actions"] += 1
                run.exclude(
                    kind=TURN_SELECTION,
                    value=type(action).__name__,
                    reason_code=EXCLUDED_SEARCH_IN_SELECTION,
                    detail=(
                        "최종 선택 턴은 검색·조회를 허용하지 않습니다. 이 "
                        "action 은 실행하지 않았습니다."
                    ),
                )

        added = 0
        for item in getattr(response, "shortlist", ()) or ():
            self._pending_shortlist.append((0, TURN_SELECTION, item))
            added += 1
        record["shortlist_added"] = added
        if getattr(response, "claim_analysis", None) is not None:
            # 검색이 끝난 뒤의 분석은 검색 전략이 아니다. _absorb_analysis 를
            # 부르지 않는 이유이며, 왔다는 사실만 남긴다.
            run.notes.append(
                "최종 선택 턴이 청구항 분석을 보냈지만 검색 전략으로 저장하지 "
                "않았습니다."
            )

    def _provider_problem(self, outcome) -> tuple[str, str] | None:
        """모델 호출 자체가 실패했는가."""
        if getattr(outcome, "cancelled", False):
            return TERM_CANCELLED, "사용자가 실행을 취소했습니다."
        if getattr(outcome, "timed_out", False):
            return TERM_TIMEOUT, "모델 호출이 제한 시간을 넘겼습니다."
        if not (outcome.result_text or "").strip():
            return TERM_PROVIDER_ERROR, "모델이 아무것도 돌려주지 않았습니다."
        return None

    async def _execute(
        self, actions, run: EpoSearchRun, record: RoundRecord
    ) -> tuple[str, str] | None:
        """이번 라운드의 action 을 실행한다. 끝내야 하면 (사유, 설명)."""
        for action in actions:
            if self.is_cancelled():
                record.status = "cancelled"
                return TERM_CANCELLED, "사용자가 실행을 취소했습니다."

            if isinstance(action, epo_actions.Finish):
                return TERM_LLM_FINISHED, (action.notes or "모델이 검색을 끝냈습니다.")

            if isinstance(action, epo_actions.EpoSearch):
                stop = await self._do_search(action, run, record)
                if stop is not None:
                    return stop
                continue

            record.errors.append(f"알 수 없는 action 입니다: {action!r}")
        return None

    async def _do_search(self, action, run: EpoSearchRun, record: RoundRecord):
        if self.channel.expired():
            return TERM_TIMEOUT, (
                f"EPO 채널 제한시간({self.channel.deadline_seconds:.0f}초)을 "
                "넘겼습니다."
            )
        if record.search_calls >= self.budget.max_search_calls_per_round:
            # 라운드 상한은 루프를 끝내지 않는다. 이번 라운드의 나머지 검색만
            # 거절하고 사유를 모델에게 돌려준다.
            record.errors.append(
                f"한 라운드의 검색 호출 상한({self.budget.max_search_calls_per_round}"
                "회)을 넘어 이 검색은 실행하지 않았습니다."
            )
            return None

        # 분류코드 형식 변환 기록. 모델이 "G08B 13/196" 을 적고 OPS 에는
        # "G08B13/196" 이 나간다면, 두 값이 **모두** 기록에 남아야 한다.
        normalized: list = []
        try:
            node = epo_actions.to_cql_node(action.query)
            cql = epo_cql.build(node, normalized=normalized)
        except epo_cql.CqlError as exc:
            # 검색식이 잘못됐다. 호출은 나가지 않았으므로 예산은 쓰지 않지만,
            # 무한히 되돌려 주지 않도록 잘못된 응답으로 센다.
            run.invalid_responses += 1
            record.errors.append(f"검색식을 만들 수 없습니다: {exc}")
            if run.invalid_responses >= self.budget.max_invalid_responses:
                return TERM_INVALID_RESPONSE_LIMIT, (
                    f"잘못된 질의가 {run.invalid_responses}번 반복되어 루프를 "
                    "끝냅니다."
                )
            return None

        # 채널 전체 예산에서 한 자리를 차지한다. 레인마다 세면 레인 수만큼
        # 예산이 늘어난다.
        if not self.channel.take_search():
            return TERM_SEARCH_CALL_LIMIT, (
                f"OPS 검색 호출 상한({self.channel.max_search_calls}회, 작업 "
                "전체)에 도달했습니다."
            )
        run.search_calls += 1
        record.search_calls += 1
        run.queries.append({
            "round": record.round,
            "cql": cql,
            "normalized_classifications": normalized,
        })
        await self.emit(
            "epo_progress",
            {
                "phase": "search",
                "round": record.round,
                "call": run.search_calls,
                "max_search_calls": self.budget.max_search_calls,
            },
        )

        # 결과 건수 상한은 설정값이다. 모델이 더 큰 수를 적으면 깎되, 조용히
        # 깎지 않는다 — 모델은 자기가 적은 수만큼 받았다고 믿고 다음 라운드의
        # 계획을 세운다.
        wanted = int(action.max_results or 0)
        allowed = max(1, int(self.budget.max_results_per_query or 1))
        if wanted > allowed:
            run.exclude(
                kind="search_results",
                value=str(wanted),
                reason_code=EXCLUDED_RESULT_LIMIT,
                detail=(
                    f"round {record.round}: 모델이 결과 {wanted}건을 요청했지만 "
                    f"검색 결과 상한({allowed}건)까지만 받았습니다."
                ),
            )
        try:
            response = await asyncio.to_thread(
                self.backend.search_structured, node, max_results=min(wanted, allowed)
            )
        except BaseException as exc:  # noqa: BLE001 - 사유별로 나눠 아래에서 판정
            return self._call_failure(exc, record)

        if self.is_cancelled():
            # 호출 **후** 확인. 받은 응답은 이미 보존되고 사용량도 반영됐다.
            record.status = "cancelled"
            return TERM_CANCELLED, "사용자가 실행을 취소했습니다."

        self._absorb(response, run, record.round)
        return None

    def _call_failure(self, exc: BaseException, record: RoundRecord):
        """OPS 호출 실패를 종료 사유로 옮긴다. 0건과 섞지 않는다."""
        record.errors.append(str(exc))
        if isinstance(exc, epo_client.OpsError) and (
            exc.status or exc.fault_code
        ):
            record.faults.append({
                "status": exc.status,
                "fault_code": exc.fault_code,
                "fault_message": exc.fault_message,
            })
        if isinstance(exc, epo_client.OpsCancelled):
            record.status = "cancelled"
            return TERM_CANCELLED, "사용자가 실행을 취소했습니다."
        if isinstance(exc, epo_quota.Throttled):
            return TERM_THROTTLED, str(exc)
        if isinstance(exc, epo_quota.QuotaExceeded):
            return TERM_QUOTA_EXCEEDED, str(exc)
        if isinstance(exc, (epo_client.OpsAuthError, PatentSearchNotConfigured)):
            return TERM_AUTH_FAILED, str(exc)
        if isinstance(exc, epo_client.OpsBudgetExceeded):
            return TERM_TIMEOUT, str(exc)
        if isinstance(exc, (PatentSearchError, epo_quota.QuotaError)):
            return TERM_PROVIDER_ERROR, str(exc)
        raise exc

    def _absorb(self, response, run: EpoSearchRun, round_no: int) -> None:
        """응답의 후보를 누적한다. 같은 문헌은 아티팩트만 늘린다."""
        for record in response.records:
            key = record.doc_number
            if not key:
                continue
            existing = run.candidates.get(key)
            if existing is None:
                existing = CandidateRecord(
                    doc_number=key,
                    title=record.title,
                    source_url=record.source_url,
                    first_seen_round=round_no,
                )
                run.candidates[key] = existing
            if response.raw_artifact_id not in existing.artifact_ids:
                existing.artifact_ids.append(response.raw_artifact_id)
            for name, value in (record.fields or {}).items():
                existing.fields[name] = value.value
                if value.evidence is not None:
                    existing.evidence[name] = {
                        "artifact_id": value.evidence.artifact_id,
                        "field_path": value.evidence.field_path,
                        "profile_id": value.evidence.profile_id,
                    }
            if not existing.title and record.title:
                existing.title = record.title
        for note in getattr(response, "notes", ()) or ():
            run.notes.append(note)

    # --------------------------------------------------------- 입력 만들기

    def _render(
        self, round_no: int, run: EpoSearchRun, results, pending_error: str
    ) -> str:
        payload = {
            "round": round_no,
            # 남은 예산을 숫자로 보여 준다. 모델이 몇 번 더 부를 수 있는지
            # 모르면 마지막 라운드에 넓은 질의를 던지고 잘린다.
            "search_calls_used": run.search_calls,
            "search_calls_left": max(
                0, self.budget.max_search_calls - run.search_calls
            ),
            "detail_fetches_used": run.detail_fetches,
            "detail_fetches_left": max(
                0, self.budget.max_detail_fetches - run.detail_fetches
            ),
            "budget": self.budget.to_dict(),
            "claim_text": self.claim_text,
        }
        if self.spec_text:
            payload["spec_text"] = self.spec_text
        if pending_error:
            payload["previous_error"] = pending_error
        if results:
            payload["results"] = results
        return epo_prompts.render_round(payload)

    def _keep_rank(
        self, candidate: CandidateRecord, latest_round: int, shortlisted: set
    ) -> tuple[int, str]:
        """상한에 걸렸을 때 이 후보를 얼마나 먼저 지킬 것인가. 작을수록 먼저."""
        if latest_round and candidate.first_seen_round >= latest_round:
            return 0, KEEP_LAST_ROUND
        if _compact_number(candidate.doc_number) not in shortlisted:
            return 1, KEEP_NOT_SHORTLISTED
        return 2, KEEP_CANDIDATE_ORDER

    def _results_payload(
        self, run: EpoSearchRun, record: RoundRecord | None
    ) -> list:
        """모델에게 보여 줄 후보 요약. 예산을 넘으면 줄이고 기록한다.

        record 가 None 이면 최종 선택 턴이다. 그 턴은 라운드가 아니므로 메모의
        말머리를 라운드 번호로 적지 않는다.

        **무엇을 뺄지는 배열 순서가 아니라 순위로 정한다.** 뒤에서부터 자르면
        가장 나중에 찾은 후보가 언제나 먼저 없어지는데, 그 후보야말로 모델이
        아직 한 번도 보지 못한 것이다. 최종 선택 턴은 바로 그것을 보여 주려고
        있는 턴이므로, 거기서 그 후보가 빠지면 이 턴을 만든 이유가 사라진다.
        순위는 KEEP_RANKING 이고, 같은 순위 안에서는 후보 목록 순서를 지킨다.
        """
        limit = max(0, int(self.budget.max_round_result_chars or 0))
        where = f"round {record.round}" if record is not None else "최종 선택 턴"
        latest_round = max(
            (item.first_seen_round for item in run.candidates.values()), default=0
        )
        shortlisted = {
            _compact_number(getattr(item, "doc_number", "") or "")
            for _round, _turn, item in self._pending_shortlist
        }
        shortlisted.discard("")

        ranked: list[tuple[int, int, dict, str, str]] = []
        for position, candidate in enumerate(run.candidates.values()):
            abstract = ""
            for name, text in candidate.fields.items():
                if name.startswith("abstract"):
                    abstract = text[:400]
                    break
            row = {
                "doc_number": candidate.doc_number,
                "title": candidate.title,
                "publication_date": candidate.fields.get("publication_date", ""),
                "applicants": candidate.fields.get("applicants", "")[:200],
                "ipc": candidate.fields.get("ipc", "")[:200],
                "abstract_excerpt": abstract,
                "has_claims": any(
                    name.startswith("claims") for name in candidate.fields
                ),
                "first_seen_round": candidate.first_seen_round,
            }
            rank, reason = self._keep_rank(candidate, latest_round, shortlisted)
            ranked.append((rank, position, row, candidate.doc_number, reason))

        ranked.sort(key=lambda item: (item[0], item[1]))
        dropped: list[tuple[str, str]] = []
        while ranked and len(
            json.dumps([item[2] for item in ranked], ensure_ascii=False)
        ) > limit:
            # 뒤에 있는 것이 가장 낮은 순위다. 이것부터 뺀다.
            _rank, _position, _row, number, reason = ranked.pop()
            dropped.append((number, reason))

        for number, reason in dropped:
            run.exclude(
                kind="round_results",
                value=number,
                reason_code=EXCLUDED_RESULT_PAYLOAD_LIMIT,
                detail=(
                    f"{where}: 결과 payload 가 문자 상한({limit}자)을 넘어 "
                    f"'{number}' 를 모델 입력에서 뺐습니다(이 후보의 보존 "
                    f"순위: {_KEEP_LABELS[reason]}). 후보 자체는 레인 기록에 "
                    "남아 있습니다."
                ),
            )
        if dropped:
            run.notes.append(
                f"{where}: 결과 payload 가 예산을 넘어 후보 {len(dropped)}건을 "
                "모델 입력에서 뺐습니다(후보 자체는 기록에 남아 있습니다). 뺀 "
                "공개번호와 사유는 excluded 에 있습니다."
            )
        # 모델에게는 후보 목록 순서 그대로 보여 준다. 순위는 무엇을 남길지만
        # 정하고, 남은 것의 배열까지 흔들지 않는다.
        ranked.sort(key=lambda item: item[1])
        return [item[2] for item in ranked]

    def _write(self, round_no: int, suffix: str, text: str) -> None:
        round_dir = self.work_dir / "rounds"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / f"epo-round-{round_no:02d}.{suffix}.txt").write_text(
            text, encoding="utf-8"
        )

    def _finish(self, run: EpoSearchRun) -> EpoSearchRun:
        if not run.termination_reason:
            run.termination_reason = TERM_ROUND_LIMIT
        # shortlist 는 마지막에 한 번만 정리한다. 마지막 라운드의 검색이 데려온
        # 후보를 가리킬 수 있으므로 라운드마다 대조하면 그 후보가 "검색 결과에
        # 없는 번호"로 잘못 걸린다.
        self._settle_shortlist(run)
        run.tool_isolation = self._isolation_level()
        usage = getattr(self.backend, "usage", None)
        run.usage = usage() if callable(usage) else {}
        run.usage["rounds_used"] = sum(
            1 for record in run.rounds if record.counts_as_round
        )
        run.usage["model_calls"] = len(run.rounds)
        run.usage["search_calls"] = run.search_calls
        run.usage["max_search_calls"] = self.budget.max_search_calls
        run.usage["invalid_responses"] = run.invalid_responses
        run.usage["lane_id"] = self.lane_id
        run.usage["channel_budget"] = self.channel.to_dict()
        run.usage["termination_reason"] = run.termination_reason
        run.usage["shortlist_limit"] = self.budget.shortlist_limit
        run.usage["max_results_per_query"] = self.budget.max_results_per_query
        # 최종 선택 턴은 검색 라운드가 아니다. 사용량을 섞으면 "검색을 몇 번
        # 돌렸나"가 실행마다 하나씩 부풀어 보인다.
        run.usage["selection_turn"] = {
            "attempted": bool(run.selection.get("attempted")),
            "status": str(run.selection.get("status") or ""),
            "model_calls": 1 if run.selection.get("attempted") else 0,
            "search_calls": 0,
            "shortlist_added": int(run.selection.get("shortlist_added") or 0),
            "rejected_actions": int(run.selection.get("rejected_actions") or 0),
            # Provider 가 알려준 실제 사용량. 검색 라운드의 것과 섞지 않는다.
            # 알려주지 않는 Provider 에서는 빈 dict 다 — 0 으로 적으면 "안
            # 썼다"로 읽힌다.
            "provider_usage": dict(run.selection.get("provider_usage") or {}),
        }
        run.usage["tool_isolation"] = run.tool_isolation
        run.usage["provider"] = str(getattr(self.provider, "id", "") or "")
        run.usage["unauthorized_tool_uses"] = sum(
            len(item.get("tools") or []) for item in run.tool_violations
        )
        return run
