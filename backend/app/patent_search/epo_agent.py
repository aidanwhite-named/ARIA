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
TERM_NO_NEW_CANDIDATES = "no_new_candidates"
TERM_TIMEOUT = "timeout"
TERM_THROTTLED = "throttled"
TERM_QUOTA_EXCEEDED = "quota_exceeded"
TERM_CANCELLED = "cancelled"
TERM_AUTH_FAILED = "authentication_failed"
TERM_PROVIDER_ERROR = "provider_error"
TERM_INVALID_RESPONSE_LIMIT = "invalid_response_limit"

TERMINATION_REASONS = (
    TERM_LLM_FINISHED,
    TERM_ROUND_LIMIT,
    TERM_SEARCH_CALL_LIMIT,
    TERM_DETAIL_FETCH_LIMIT,
    TERM_NO_NEW_CANDIDATES,
    TERM_TIMEOUT,
    TERM_THROTTLED,
    TERM_QUOTA_EXCEEDED,
    TERM_CANCELLED,
    TERM_AUTH_FAILED,
    TERM_PROVIDER_ERROR,
    TERM_INVALID_RESPONSE_LIMIT,
)


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

    max_rounds: int = 2
    max_search_calls: int = 6
    max_search_calls_per_round: int = 3
    max_detail_fetches: int = 12
    # 형식 오류·잘못된 질의를 몇 번까지 되돌려 줄 것인가. 이 상한이 없으면
    # 모델이 같은 실수를 반복하는 동안 라운드가 계속 소모된다.
    max_invalid_responses: int = 3
    # 한 라운드 결과 payload 의 최대 문자 수. 넘으면 후보를 줄이고 그 사실을
    # 기록한다. 조용히 자르면 모델은 전부 봤다고 믿는다.
    max_round_result_chars: int = 40_000

    def to_dict(self) -> dict:
        return {
            "max_rounds": self.max_rounds,
            "max_search_calls": self.max_search_calls,
            "max_search_calls_per_round": self.max_search_calls_per_round,
            "max_detail_fetches": self.max_detail_fetches,
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
        }


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


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

    # ------------------------------------------------------------ 실행

    async def run(self) -> EpoSearchRun:
        run = EpoSearchRun()
        # 재시도 대기 중에도 취소를 본다. OpsClient 는 자기 sleep 을 쓰므로
        # 여기서 갈아 끼운다.
        self._install_cancellable_sleep()

        pending_error = ""
        results_payload: list[dict] = []

        # for 루프를 쓰지 않는다. continue 가 라운드를 소모해 버리기 때문이다 —
        # 형식 오류 두 번이면 검색을 한 번도 못 해 보고 끝난다.
        rounds_used = 0
        attempt = 0
        while rounds_used < self.budget.max_rounds:
            if self._stop_for_cancel(run):
                return self._finish(run)

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
                    "max_rounds": self.budget.max_rounds,
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
                return self._finish(run)

            stopped = self._provider_problem(outcome)
            if stopped is not None:
                reason, detail = stopped
                record.status = reason
                record.errors.append(detail)
                run.rounds.append(record)
                run.termination_reason = reason
                run.termination_detail = detail
                run.cancelled = reason == TERM_CANCELLED
                return self._finish(run)

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
                    return self._finish(run)
                pending_error = (
                    f"이전 응답을 action 으로 읽지 못했습니다: {exc} "
                    "JSON 객체 하나만, 설명 없이 돌려주십시오."
                )
                results_payload = []
                # 형식 오류는 라운드를 소모하지 않는다. 대신 위의 상한이 센다.
                continue

            pending_error = ""
            record.actions = len(response.actions)
            if response.strategy:
                run.notes.append(f"round {round_no}: {response.strategy}")

            if not response.actions:
                record.status = "no_actions"
                record.errors.append("action 이 비어 있습니다.")
                run.rounds.append(record)
                run.invalid_responses += 1
                if run.invalid_responses >= self.budget.max_invalid_responses:
                    run.termination_reason = TERM_INVALID_RESPONSE_LIMIT
                    run.termination_detail = "빈 응답이 반복되어 루프를 끝냅니다."
                    return self._finish(run)
                pending_error = (
                    "action 이 비어 있습니다. 검색을 더 할 것이 없으면 "
                    f'{{"action":"{epo_actions.ACTION_FINISH}"}} 를 돌려주십시오.'
                )
                results_payload = []
                continue

            rounds_used += 1
            record.counts_as_round = True
            before = set(run.candidates)
            outcome_reason = await self._execute(response.actions, run, record)
            record.new_candidates = len(set(run.candidates) - before)
            record.status = record.status if record.status != "running" else "ok"
            run.rounds.append(record)

            if outcome_reason is not None:
                run.termination_reason = outcome_reason[0]
                run.termination_detail = outcome_reason[1]
                run.cancelled = outcome_reason[0] == TERM_CANCELLED
                return self._finish(run)

            # 검색 예산을 다 썼으면 그것이 멈추는 이유다. "새 후보가 없어서"로
            # 적으면 더 찾을 수 있었는데 안 찾은 것처럼 읽힌다.
            if self.channel.searches_used >= self.channel.max_search_calls:
                run.termination_reason = TERM_SEARCH_CALL_LIMIT
                run.termination_detail = (
                    f"OPS 검색 호출 상한({self.channel.max_search_calls}회, 작업 "
                    "전체)을 다 썼습니다."
                )
                return self._finish(run)
            if self.channel.expired():
                run.termination_reason = TERM_TIMEOUT
                run.termination_detail = (
                    f"EPO 채널 제한시간({self.channel.deadline_seconds:.0f}초)을 "
                    "넘겼습니다."
                )
                return self._finish(run)

            # 새 후보가 없으면 더 돌 이유가 없다. 남은 예산을 태우지 않는다.
            if rounds_used > 1 and record.new_candidates == 0 and record.search_calls:
                run.termination_reason = TERM_NO_NEW_CANDIDATES
                run.termination_detail = (
                    f"{round_no}라운드에서 새 공개번호가 나오지 않아 조기 "
                    "종료했습니다."
                )
                return self._finish(run)

            results_payload = self._results_payload(run, record)

        run.termination_reason = TERM_ROUND_LIMIT
        run.termination_detail = (
            f"검색 라운드 상한({self.budget.max_rounds})에 도달했습니다."
        )
        return self._finish(run)

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

            if isinstance(action, epo_actions.EpoFetchDocument):
                stop = await self._do_fetch(action, run, record)
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

        try:
            node = epo_actions.to_cql_node(action.query)
            cql = epo_cql.build(node)
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
        run.queries.append({"round": record.round, "cql": cql})
        await self.emit(
            "epo_progress",
            {
                "phase": "search",
                "round": record.round,
                "call": run.search_calls,
                "max_search_calls": self.budget.max_search_calls,
            },
        )

        try:
            response = await asyncio.to_thread(
                self.backend.search_structured, node, max_results=action.max_results
            )
        except BaseException as exc:  # noqa: BLE001 - 사유별로 나눠 아래에서 판정
            return self._call_failure(exc, record)

        if self.is_cancelled():
            # 호출 **후** 확인. 받은 응답은 이미 보존되고 사용량도 반영됐다.
            record.status = "cancelled"
            return TERM_CANCELLED, "사용자가 실행을 취소했습니다."

        self._absorb(response, run, record.round)
        return None

    async def _do_fetch(self, action, run: EpoSearchRun, record: RoundRecord):
        if self.channel.expired():
            return TERM_TIMEOUT, (
                f"EPO 채널 제한시간({self.channel.deadline_seconds:.0f}초)을 "
                "넘겼습니다."
            )
        if not self.channel.take_detail():
            return TERM_DETAIL_FETCH_LIMIT, (
                f"상세 조회 상한({self.channel.max_detail_fetches}건, 작업 "
                "전체)에 도달했습니다."
            )
        try:
            response = await asyncio.to_thread(
                self.backend.fetch_document, action.doc_number, action.constituent
            )
        except BaseException as exc:  # noqa: BLE001
            return self._call_failure(exc, record)

        run.detail_fetches += 1
        record.detail_fetches += 1
        if self.is_cancelled():
            record.status = "cancelled"
            return TERM_CANCELLED, "사용자가 실행을 취소했습니다."
        self._absorb(response, run, record.round)
        return None

    def _call_failure(self, exc: BaseException, record: RoundRecord):
        """OPS 호출 실패를 종료 사유로 옮긴다. 0건과 섞지 않는다."""
        record.errors.append(str(exc))
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
            "max_rounds": self.budget.max_rounds,
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

    def _results_payload(self, run: EpoSearchRun, record: RoundRecord) -> list:
        """다음 라운드에 보여 줄 후보 요약. 예산을 넘으면 줄이고 기록한다."""
        rows = []
        for candidate in run.candidates.values():
            abstract = ""
            for name, text in candidate.fields.items():
                if name.startswith("abstract"):
                    abstract = text[:400]
                    break
            rows.append(
                {
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
            )
        dropped = 0
        while rows and len(json.dumps(rows, ensure_ascii=False)) > (
            self.budget.max_round_result_chars
        ):
            rows.pop()
            dropped += 1
        if dropped:
            run.notes.append(
                f"round {record.round}: 결과 payload 가 예산을 넘어 후보 "
                f"{dropped}건을 다음 라운드 입력에서 뺐습니다(후보 자체는 "
                "기록에 남아 있습니다)."
            )
        return rows

    def _write(self, round_no: int, suffix: str, text: str) -> None:
        round_dir = self.work_dir / "rounds"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / f"epo-round-{round_no:02d}.{suffix}.txt").write_text(
            text, encoding="utf-8"
        )

    def _finish(self, run: EpoSearchRun) -> EpoSearchRun:
        if not run.termination_reason:
            run.termination_reason = TERM_ROUND_LIMIT
        usage = getattr(self.backend, "usage", None)
        run.usage = usage() if callable(usage) else {}
        run.usage["rounds_used"] = sum(
            1 for record in run.rounds if record.counts_as_round
        )
        run.usage["model_calls"] = len(run.rounds)
        run.usage["max_rounds"] = self.budget.max_rounds
        run.usage["search_calls"] = run.search_calls
        run.usage["max_search_calls"] = self.budget.max_search_calls
        run.usage["invalid_responses"] = run.invalid_responses
        run.usage["lane_id"] = self.lane_id
        run.usage["channel_budget"] = self.channel.to_dict()
        run.usage["termination_reason"] = run.termination_reason
        return run
