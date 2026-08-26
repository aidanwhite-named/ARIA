"""Backend-orchestrated agent 검색 루프.

AI 에게 셸이나 파일 도구를 주지 않는다. 대신 이렇게 돈다.

    ARIA → (청구항 + 문헌 목록 + 예산)      → AI
    AI   → (구조화된 action JSON)            → ARIA
    ARIA → (인덱스 조회 결과, 원문 구간)     → AI
    ...
    AI   → finalize_evidence                 → ARIA (검증 후 근거 패키지)

모든 LLM 호출은 기존 Provider 추상화의 NO_TOOLS 정책으로 나간다. Codex/Claude/
agy 의 셸·파일 도구 호출에 기대지 않는다 — 그쪽은 실행마다 무엇을 읽었는지가
달라지고, ARIA 는 그 사실을 확인할 수 없다.

라운드마다 새 프로세스를 띄우고 상태를 메시지로 다시 넣는다. CLI 세션
(`--resume`)을 쓰지 않는 이유는 README 와 같다 — 모델이 실제로 무엇을 받았는지가
CLI 내부 상태에 숨으면 재현성이 깨진다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..enums import ErrorCode
from ..providers.base import NO_TOOLS, ExecutionRequest
from . import search as search_module
from .actions import (
    ACTION_FINALIZE,
    ALL_DOCUMENTS,
    ActionError,
    FinalizeEvidence,
    GetDocumentStatus,
    ReadPage,
    ReadPages,
    ReadParagraph,
    SearchDocument,
    SearchExact,
    SearchNumbersAndSymbols,
    parse_response,
)
from .prompts import AGENT_SYSTEM_PROMPT, render_round
from .search import IndexedDocument

# 예산 기본값. 사용자가 설정에서 바꿀 수 있고, preflight 와 실행이 **같은
# 계산**을 쓴다(retrieval.budget_from_settings).
DEFAULT_MAX_ROUNDS = 6
DEFAULT_MAX_PAGE_READS = 40
# 근거 패키지 상한. 한글 1자는 UTF-8 3 bytes 이므로 최악의 경우 120,000 bytes 다.
# agy 의 전송 한도(180,000 bytes)에서 Master Prompt(약 19 KB)와 청구항을 빼고도
# 남는 크기로 잡았다. 여기를 올리면 preflight 가 계산하는 최댓값이 그만큼 커져서
# 전송 한도가 작은 Provider 에서 실행이 막힐 수 있다.
DEFAULT_EVIDENCE_CHARS = 40_000
DEFAULT_HITS_PER_DOCUMENT = 6

# 한 라운드에서 모델에게 돌려주는 검색 결과 본문의 총 상한. 라운드 예산과 다른
# 축이다 — 검색을 적게 하고도 페이지를 통째로 받아 가면 컨텍스트가 터진다.
MAX_ROUND_RESULT_CHARS = 24_000

# 검색 결과 한 줄에서 모델에게 보여주는 본문 길이. 근거 패키지에는 청크
# 전체가 들어가므로 여기서 자른 것이 최종 보고서에 영향을 주지 않는다.
SNIPPET_CHARS = 900

# 이 구성에 대해 ARIA 가 관측한 서로 다른 검색어가 이보다 적으면, 모델이
# not_found 를 주장해도 확정하지 않는다. 모델의 자기 보고가 아니라 실제 실행된
# 검색을 센다.
MIN_EXPANSION_TERMS = 3


@dataclass(frozen=True)
class RetrievalBudget:
    """검색 라운드·페이지 읽기·반환 문자 수의 상한."""

    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_page_reads: int = DEFAULT_MAX_PAGE_READS
    max_evidence_chars: int = DEFAULT_EVIDENCE_CHARS
    hits_per_document: int = DEFAULT_HITS_PER_DOCUMENT
    max_round_result_chars: int = MAX_ROUND_RESULT_CHARS

    def to_dict(self) -> dict:
        return {
            "max_rounds": self.max_rounds,
            "max_page_reads": self.max_page_reads,
            "max_evidence_chars": self.max_evidence_chars,
            "hits_per_document": self.hits_per_document,
            "max_round_result_chars": self.max_round_result_chars,
        }


@dataclass
class RoundRecord:
    """LLM 호출 한 번의 감사 기록."""

    round: int
    started_at: str
    completed_at: str
    status: str
    input_sha256: str = ""
    output_sha256: str = ""
    input_chars: int = 0
    output_chars: int = 0
    actions: int = 0
    error: str = ""
    usage: dict | None = None

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "actions": self.actions,
            "error": self.error,
            "usage": self.usage,
        }


@dataclass
class DocumentSearchRecord:
    """구성 하나 × 문헌 하나의 검색 실행 기록.

    결과가 0건인 검색도 남긴다. "찾지 못했다"와 "찾아보지 않았다"를 구분하는
    유일한 근거이기 때문이다. 이 기록이 없으면, AI 가 D1 만 검색하고 D2 를
    건드리지도 않은 채 "검토 범위에서 미발견"을 받을 수 있다.
    """

    attachment_id: str
    alias: str
    queries: list[str] = field(default_factory=list)
    channels_used: list[str] = field(default_factory=list)
    failed_channels: list[str] = field(default_factory=list)
    hits: int = 0
    # 찾았지만 라운드 반환 예산 때문에 AI 에게 보여주지 못한 후보 수.
    # AI 가 보지 못한 후보는 판단에 쓰이지 않았으므로 검토 범위가 줄어든 것이다.
    omitted: int = 0

    def to_dict(self) -> dict:
        return {
            "attachment": self.alias,
            "attachment_id": self.attachment_id,
            "queries": list(self.queries),
            "channels_used": list(self.channels_used),
            "channels_failed": list(self.failed_channels),
            "hits": self.hits,
            "omitted": self.omitted,
        }


@dataclass
class ComponentState:
    """구성 하나에 대해 ARIA 가 직접 관측한 것."""

    id: str
    label: str
    feature: str
    queries: list[str] = field(default_factory=list)
    channels_used: list[str] = field(default_factory=list)
    failed_channels: list[str] = field(default_factory=list)
    hit_chunks: dict[str, dict] = field(default_factory=dict)
    reviewed_pages: dict[str, set] = field(default_factory=dict)
    # 문헌별 검색 실행 기록. 키는 attachment_id.
    searched: dict[str, DocumentSearchRecord] = field(default_factory=dict)

    def record_query(self, value: str) -> None:
        text = str(value).strip()
        if text and text not in self.queries:
            self.queries.append(text)

    def record_page(self, attachment_id: str, page: int) -> None:
        self.reviewed_pages.setdefault(attachment_id, set()).add(int(page))

    def record_search(
        self,
        *,
        attachment_id: str,
        alias: str,
        queries: list[str],
        channels_used: list[str],
        failed_channels: list[str],
        hits: int,
        omitted: int = 0,
    ) -> None:
        record = self.searched.get(attachment_id)
        if record is None:
            record = DocumentSearchRecord(attachment_id=attachment_id, alias=alias)
            self.searched[attachment_id] = record
        for query in queries:
            text = str(query).strip()
            if text and text not in record.queries:
                record.queries.append(text)
        for channel in channels_used:
            if channel not in record.channels_used:
                record.channels_used.append(channel)
        for channel in failed_channels:
            if channel not in record.failed_channels:
                record.failed_channels.append(channel)
        record.hits += hits
        record.omitted += omitted


@dataclass
class RetrievalRun:
    """루프 전체의 결과."""

    rounds: list[RoundRecord] = field(default_factory=list)
    components: list[ComponentState] = field(default_factory=list)
    finalize: FinalizeEvidence | None = None
    # ARIA 가 이번 실행에서 **실제로 AI 에게 돌려준** 청크. (attachment_id, chunk_id)
    #
    # 근거 패키지에 들어갈 수 있는 것은 이 집합 안의 구간뿐이다. 없으면 AI 가
    # 본 적 없는 청크를 지목해도 원문이 실재하기만 하면 matched 가 된다 —
    # chunk_id 형식(P0012-003)은 action 스키마에 그대로 노출돼 있어 추측이
    # 쉽다. "AI 는 원문을 지어낼 수 없다"는 주장이 성립하려면 텍스트뿐 아니라
    # **무엇을 보았는가**까지 ARIA 가 쥐고 있어야 한다.
    exposed_chunks: set = field(default_factory=set)
    pages_read: int = 0
    # 이미 읽은 페이지를 다시 요청한 횟수. 막지는 않는다 — 앞뒤 문맥을 넓히다
    # 보면 겹치는 것이 정상이다. 다만 예산을 반복 요청에 쓰고 있는지는 기록에
    # 남아야 한다.
    repeat_page_reads: int = 0
    cancelled: bool = False
    timed_out: bool = False
    error: str = ""
    error_code: str = ""
    usage: dict = field(default_factory=dict)
    action_errors: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    budget_exhausted: bool = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_size(value) -> int:
    """이 값이 다음 라운드 프롬프트에서 차지할 문자 수.

    본문 텍스트만 세지 않는다. 파일명, 채널별 실행 기록, 청크 메타데이터,
    문헌 상태 목록도 전부 같은 JSON 에 실려 모델에게 간다. 본문만 세면
    get_document_status 처럼 본문이 없는 action 이 소비량 0 으로 잡히고,
    문헌 20개짜리 상태 조회 하나가 예산의 두 배를 만들어 낸다(실측 8,318자).

    render_round 와 같은 직렬화(indent=2)를 쓴다. 중첩 깊이 때문에 실제
    프롬프트에서는 들여쓰기가 조금 더 붙지만, 그만큼 이 값이 보수적으로
    작게 잡히지 않도록 한 단계 들여쓴 상태로 잰다.
    """
    return len(json.dumps(value, ensure_ascii=False, indent=2))


def _compact_channels(channels: list[dict]) -> list[dict]:
    """모델에게 돌려줄 채널 실행 기록.

    검색어는 action 수준에 이미 한 번 실려 있다. 문헌마다 채널마다 같은 목록을
    되풀이하면 그 반복만으로 라운드 예산을 넘긴다 — 문헌 20개 × 채널 5개면
    같은 검색어가 100번 실린다.

    감사 기록(component.searched, retrieval_manifest)에는 전체 정보가 그대로
    남는다. 여기서 줄이는 것은 모델에게 보내는 사본뿐이다.
    """
    compact: list[dict] = []
    for entry in channels:
        if not (entry.get("requested") or entry.get("executed")):
            continue
        row = {
            "channel": entry["channel"],
            "executed": bool(entry.get("executed")),
            "hits": entry.get("hits", 0),
        }
        if entry.get("skipped_reason"):
            row["skipped"] = entry["skipped_reason"]
        if entry.get("error"):
            row["error"] = entry["error"]
        compact.append(row)
    return compact


async def _noop_emit(_event_type: str, _payload: dict) -> None:
    """emit 을 넘기지 않은 호출부(테스트, 스크립트)용 자리."""
    return None


class TraceWriter:
    """retrieval_trace.jsonl. 한 줄에 사건 하나."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def write(self, event_type: str, payload: dict, *, round_no: int = 0) -> None:
        line = json.dumps(
            {
                "ts": _utcnow(),
                "round": round_no,
                "type": event_type,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class RetrievalAgent:
    """청구항 하나에 대한 로컬 검색 루프."""

    def __init__(
        self,
        *,
        job_id: str,
        provider,
        model: str | None,
        timeout_seconds: int,
        work_dir: Path,
        corpus: list[IndexedDocument],
        claim_text: str,
        budget: RetrievalBudget,
        trace: TraceWriter,
        emit=None,
        is_cancelled=None,
        semantic_encoder=None,
    ) -> None:
        self.job_id = job_id
        self.provider = provider
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.work_dir = work_dir
        self.corpus = corpus
        self.claim_text = claim_text
        self.budget = budget
        self.trace = trace
        # Provider.execute 는 항상 호출 가능한 emit 을 기대한다. None 을 그대로
        # 넘기면 Provider 안에서 터진다.
        self.emit = emit or _noop_emit
        self.is_cancelled = is_cancelled or (lambda: False)
        self.semantic_encoder = semantic_encoder
        self._by_alias = {document.alias: document for document in corpus}
        self._components: dict[str, ComponentState] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------- 유틸리티

    async def _emit(self, event_type: str, payload: dict) -> None:
        await self.emit(event_type, payload)

    def _component(self, component_id: str) -> ComponentState | None:
        return self._components.get(str(component_id or "").strip())

    def _documents_for(self, attachment: str) -> tuple[list[IndexedDocument], str]:
        """action 의 attachment 값을 실제 문헌 목록으로 바꾼다.

        존재하지 않거나 이번 분석에 포함되지 않은 자료 번호는 목록이 아니라
        오류로 돌려준다. 조용히 전체 검색으로 넓히면 모델은 자기가 지정한
        문헌을 봤다고 믿게 된다.
        """
        value = str(attachment or "").strip()
        if not value or value == ALL_DOCUMENTS:
            return list(self.corpus), ""
        document = self._by_alias.get(value)
        if document is None:
            known = ", ".join(sorted(self._by_alias)) or "(없음)"
            return [], (
                f"알 수 없는 자료 번호입니다: {value}. 이번 분석에 포함된 자료는 "
                f"{known} 입니다."
            )
        return [document], ""

    # --------------------------------------------------------------- 실행

    async def run(self) -> RetrievalRun:
        run = RetrievalRun()
        pending_error = ""
        results_payload: list[dict] = []

        for round_no in range(1, self.budget.max_rounds + 1):
            if self.is_cancelled():
                run.cancelled = True
                run.error_code = ErrorCode.CANCELLED
                run.error = "사용자가 실행을 취소했습니다."
                return run

            payload = self._round_payload(round_no, results_payload, pending_error)
            user_message = render_round(payload)
            round_dir = self.work_dir / "rounds"
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / f"round-{round_no:02d}.in.txt").write_text(
                f"===== SYSTEM PROMPT =====\n{AGENT_SYSTEM_PROMPT}\n\n"
                f"===== USER MESSAGE =====\n{user_message}",
                encoding="utf-8",
            )

            record = RoundRecord(
                round=round_no,
                started_at=_utcnow(),
                completed_at="",
                status="running",
                input_sha256=_sha256(AGENT_SYSTEM_PROMPT + "\n\x00\n" + user_message),
                input_chars=len(AGENT_SYSTEM_PROMPT) + len(user_message),
            )
            self.trace.write(
                "llm_input",
                {
                    "sha256": record.input_sha256,
                    "chars": record.input_chars,
                    "documents": [document.alias for document in self.corpus],
                },
                round_no=round_no,
            )
            await self._emit(
                "retrieval_progress",
                {
                    "phase": "round",
                    "round": round_no,
                    "max_rounds": self.budget.max_rounds,
                    "pages_read": run.pages_read,
                    "message": (
                        f"로컬 검색 {round_no}/{self.budget.max_rounds} 라운드 — "
                        "AI 검색 요청 대기 중"
                    ),
                },
            )

            # 검색 라운드에도 Provider 전송 한도가 그대로 걸린다. 인용발명
            # 본문을 빼도 청구항과 검색 결과만으로 한도를 넘을 수 있고, 그때
            # 자르는 주체는 모델이 아니라 CLI 다. 넘겨 보내면 뒷부분이 조용히
            # 사라진 채로 검색이 돈다.
            provider_budget = getattr(self.provider, "max_input_bytes", None)
            payload_bytes = len(AGENT_SYSTEM_PROMPT.encode("utf-8")) + len(
                user_message.encode("utf-8")
            )
            if provider_budget and payload_bytes > provider_budget:
                record.status = "input_too_large"
                record.error = (
                    f"로컬 검색 {round_no}라운드의 입력이 "
                    f"{payload_bytes:,} bytes 로 이 Provider 의 전송 한도 "
                    f"{provider_budget:,} bytes 를 넘습니다. 인용발명 본문을 "
                    "빼고도 넘는 크기이므로(청구항·검색 결과만으로도 초과) "
                    "ARIA 는 자르지 않고 중단합니다. 청구항을 나눠 실행하거나 "
                    "전송 한도가 더 큰 Provider 를 선택하십시오."
                )
                record.completed_at = _utcnow()
                run.rounds.append(record)
                run.error_code = ErrorCode.INPUT_TOO_LARGE
                run.error = record.error
                self.trace.write(
                    "input_too_large",
                    {"bytes": payload_bytes, "budget": provider_budget},
                    round_no=round_no,
                )
                return run

            request = ExecutionRequest(
                job_id=self.job_id,
                work_dir=round_dir,
                system_prompt=AGENT_SYSTEM_PROMPT,
                user_message=user_message,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                tool_policy=NO_TOOLS,
            )
            outcome = await self.provider.execute(request, self.emit)
            record.completed_at = _utcnow()
            record.usage = outcome.usage
            record.output_chars = len(outcome.result_text or "")
            record.output_sha256 = _sha256(outcome.result_text or "")
            (round_dir / f"round-{round_no:02d}.out.txt").write_text(
                outcome.result_text or "", encoding="utf-8"
            )
            self.trace.write(
                "llm_output",
                {"sha256": record.output_sha256, "chars": record.output_chars},
                round_no=round_no,
            )

            aborted = self._abort_reason(outcome)
            if aborted is not None:
                record.status, record.error = aborted[0], aborted[1]
                run.rounds.append(record)
                run.error_code = aborted[2]
                run.error = aborted[1]
                run.cancelled = outcome.cancelled
                run.timed_out = outcome.timed_out
                return run

            try:
                response = parse_response(outcome.result_text or "")
            except ActionError as exc:
                record.status = "parse_error"
                record.error = str(exc)
                run.rounds.append(record)
                # 형식 오류는 셸로 우회하지 않고 구조화된 오류로 돌려준다.
                pending_error = (
                    f"이전 응답을 action 으로 읽지 못했습니다: {exc} "
                    "JSON 객체 하나만, 설명 없이 돌려주십시오."
                )
                results_payload = []
                self.trace.write(
                    "parse_error", {"reason": str(exc)}, round_no=round_no
                )
                continue

            pending_error = ""
            self._declare_components(response, run)
            record.actions = len(response.actions)
            if response.notes:
                run.notes.append(f"round {round_no}: {response.notes}")

            # 구성 분해가 없으면 이번 실행에는 대비할 대상이 없다. 빈 구성으로
            # 끝내면 근거가 하나도 없는 패키지가 최종 분석에 그대로 들어가고,
            # 사용자는 검색이 돌았는데 아무것도 못 찾은 것으로 읽는다.
            if not self._order:
                record.status = "no_components"
                record.error = "첫 응답에 청구항 구성 분해(components)가 없습니다."
                run.rounds.append(record)
                pending_error = (
                    "components 가 비어 있습니다. 첫 응답에는 청구항을 구성요소로 "
                    "분해해서 components 에 최소 1개 이상 넣어야 합니다. 그 뒤에야 "
                    "ARIA 가 구성마다 id 를 붙여 검색 기록을 남길 수 있습니다."
                )
                results_payload = []
                self.trace.write(
                    "no_components", {"reason": record.error}, round_no=round_no
                )
                continue

            record.status = "ok"
            run.rounds.append(record)

            finalize = next(
                (item for item in response.actions if item.action == ACTION_FINALIZE),
                None,
            )
            if finalize is not None:
                problem = self._finalize_problem(finalize)
                if problem:
                    # 마무리 요청을 받아 주지 않는다. 구성이 빠진 채로 확정하면
                    # 그 구성은 근거도 상태 사유도 없이 조용히 사라진다.
                    run.action_errors.append(
                        {
                            "round": round_no,
                            "action": ACTION_FINALIZE,
                            "reason": problem,
                        }
                    )
                    self.trace.write(
                        "finalize_rejected", {"reason": problem}, round_no=round_no
                    )
                    pending_error = problem
                    results_payload = []
                    continue
                run.finalize = finalize
                self.trace.write(
                    "finalize",
                    {"components": len(finalize.components)},
                    round_no=round_no,
                )
                return run

            results_payload = await self._execute_actions(
                response.actions, run, round_no
            )

        run.budget_exhausted = True
        run.notes.append(
            f"검색 라운드 상한({self.budget.max_rounds})에 도달해 루프를 끝냈습니다. "
            "모인 근거만으로 패키지를 만듭니다."
        )
        if not self._order:
            run.error_code = ErrorCode.RETRIEVAL_FAILED
            run.error = (
                "AI 가 청구항 구성 분해를 끝내 돌려주지 않아 근거 패키지를 만들 "
                "수 없습니다. 구성이 없으면 무엇을 검토했고 무엇을 못 했는지 "
                "말할 수 없으므로, 빈 패키지로 분석을 진행하지 않습니다."
            )
        return run

    def _finalize_problem(self, finalize: FinalizeEvidence) -> str:
        """마무리 요청이 선언된 구성 전부를 정확히 한 번씩 덮는가.

        빠진 구성은 근거도 상태 사유도 없이 사라지고, 중복된 구성은 뒤엣것이
        앞엣것을 덮어쓴다. 어느 쪽이든 보고서에서는 구분되지 않으므로 받아
        주지 않는다.
        """
        declared = list(self._order)
        seen: list[str] = []
        duplicated: list[str] = []
        unknown: list[str] = []
        for item in finalize.components:
            component_id = str(item.component_id or "").strip()
            if component_id not in declared:
                if component_id not in unknown:
                    unknown.append(component_id or "(빈 값)")
                continue
            if component_id in seen:
                if component_id not in duplicated:
                    duplicated.append(component_id)
                continue
            seen.append(component_id)
        missing = [component_id for component_id in declared if component_id not in seen]

        problems: list[str] = []
        if missing:
            problems.append(f"빠진 구성: {', '.join(missing)}")
        if duplicated:
            problems.append(f"중복된 구성: {', '.join(duplicated)}")
        if unknown:
            problems.append(f"알 수 없는 구성 id: {', '.join(unknown)}")
        if not problems:
            return ""
        return (
            "finalize_evidence 는 선언된 구성 "
            f"{len(declared)}개를 정확히 한 번씩 포함해야 합니다 — "
            + " / ".join(problems)
            + ". 근거를 찾지 못한 구성도 빼지 말고 evidence 를 비운 채 "
            "status_claim 과 note 를 적어 포함하십시오."
        )

    # ----------------------------------------------------------- 내부 단계

    def _abort_reason(self, outcome) -> tuple[str, str, str] | None:
        """이 라운드가 실행 자체로 실패했는가. (status, message, error_code)"""
        if outcome.cancelled:
            return ("cancelled", "사용자가 실행을 취소했습니다.", ErrorCode.CANCELLED)
        if outcome.timed_out:
            return (
                "timed_out",
                "로컬 검색 라운드가 시간 제한을 넘겼습니다.",
                ErrorCode.TIMED_OUT,
            )
        if outcome.auth_required:
            return (
                "auth_required",
                "Provider 인증이 필요합니다.",
                ErrorCode.AUTH_REQUIRED,
            )
        if outcome.rate_limited:
            return (
                "rate_limited",
                "Provider 사용량 제한에 도달했습니다.",
                ErrorCode.RATE_LIMITED,
            )
        # 도구를 끈 실행이다. 한 번이라도 도구를 불렀으면 계약이 깨진 것이고,
        # 결과가 멀쩡해 보여도 실패로 본다.
        if outcome.tool_uses or NO_TOOLS.unexpected_calls(outcome.tool_calls):
            names = ", ".join(dict.fromkeys(outcome.tool_uses)) or "알 수 없음"
            return (
                "tool_policy_violation",
                f"도구를 끈 로컬 검색 실행에서 도구 호출이 관측되었습니다: {names}",
                ErrorCode.TOOL_POLICY_VIOLATION,
            )
        if outcome.is_error:
            return (
                "provider_error",
                outcome.error_message or "Provider 실행이 실패했습니다.",
                ErrorCode.PROCESS_ERROR,
            )
        return None

    def _declare_components(self, response, run: RetrievalRun) -> None:
        """첫 라운드의 구성 분해를 받아 id 를 붙인다.

        모델에게 긴 식별자를 옮겨 적게 하지 않는다. citation_mapping 이 별칭을
        쓰는 것과 같은 이유다.
        """
        if self._order or not response.components:
            return
        for index, item in enumerate(response.components[:200], start=1):
            component_id = f"R{index:03d}"
            state = ComponentState(
                id=component_id,
                label=item.label or f"구성 {index}",
                feature=item.feature,
            )
            self._components[component_id] = state
            self._order.append(component_id)
        run.components = [self._components[key] for key in self._order]
        self.trace.write(
            "components",
            {"items": [{"id": key, "label": self._components[key].label} for key in self._order]},
        )

    def _round_payload(
        self, round_no: int, results: list[dict], pending_error: str
    ) -> dict:
        payload = {
            "round": round_no,
            "claim_text": self.claim_text,
            "budget": {
                **self.budget.to_dict(),
                "rounds_remaining": self.budget.max_rounds - round_no + 1,
            },
            "documents": [
                {
                    "attachment": document.alias,
                    "filename": document.filename,
                    "pdf_pages": document.page_count,
                    "extraction_status": document.report.get("status"),
                    "pages_needing_visual_review": len(
                        document.report.get("visual_review_required_pages") or []
                    ),
                    "pages_empty_or_low_text": len(
                        document.report.get("empty_or_low_text_pages") or []
                    ),
                    "pages_extraction_failed": len(
                        document.report.get("extraction_failed_pages") or []
                    ),
                }
                for document in self.corpus
            ],
        }
        if self._order:
            payload["components"] = [
                {
                    "id": key,
                    "label": self._components[key].label,
                    "feature": self._components[key].feature,
                    "queries_used": self._components[key].queries[-20:],
                    "candidates_found": len(self._components[key].hit_chunks),
                }
                for key in self._order
            ]
        else:
            payload["instruction"] = (
                "첫 라운드입니다. components 에 청구항 구성 분해를 넣고, "
                "각 구성에 대한 검색 action 을 함께 돌려주십시오."
            )
        if pending_error:
            payload["previous_error"] = pending_error
        if results:
            payload["results"] = results
        return payload

    async def _execute_actions(
        self, items: list, run: RetrievalRun, round_no: int
    ) -> list[dict]:
        """action 을 실행하고 다음 라운드에 넣을 결과를 만든다.

        반환 문자 예산은 action 사이에서만이 아니라 **action 안에서도** 걸린다.
        action 단위로만 재면 문헌 20개 × 후보 6건 × 900자짜리 전체 검색 하나가
        예산의 네 배를 만들어 내고, 그 결과가 그대로 다음 라운드 프롬프트에
        실린다. 그러면 Provider 전송 한도에 걸려 검색 비용을 다 쓰고 나서
        실행이 실패한다.
        """
        results: list[dict] = []
        budget_left = self.budget.max_round_result_chars

        for position, item in enumerate(items):
            if self.is_cancelled():
                run.cancelled = True
                break
            if budget_left <= 0:
                run.action_errors.append(
                    {
                        "round": round_no,
                        "index": position,
                        "action": getattr(item, "action", "?"),
                        "reason": (
                            "이번 라운드의 반환 문자 예산을 모두 썼습니다. 남은 "
                            "action 은 실행되지 않았습니다."
                        ),
                    }
                )
                run.budget_exhausted = True
                break

            entry, _reported = await self._execute_one(
                item, run, round_no, budget_left
            )
            if entry is None:
                continue
            # 소비량은 handler 의 자기 보고가 아니라 **실제 직렬화 크기**로
            # 센다. handler 가 넘겨받은 예산은 "이만큼까지만 만들어라"이고,
            # 여기서 재는 것은 "실제로 얼마가 나갔나"다. 둘을 같은 값으로
            # 두면 세지 않는 필드가 생기는 순간 예산이 조용히 뚫린다.
            entry_size = json_size(entry)
            if entry_size > budget_left:
                # 오류 응답처럼 본문이 없는 경로도 남은 예산보다 클 수 있다.
                # 그때는 초과한 JSON 을 다음 라운드에 싣지 않고 감사 기록에만
                # 남긴다. 검색·읽기 handler 는 실제로 반환한 청크만 노출 상태에
                # 넣으므로, 이 경로에서 보지 않은 근거가 승인되지는 않는다.
                run.action_errors.append(
                    {
                        "round": round_no,
                        "index": position,
                        "action": getattr(item, "action", "?"),
                        "reason": (
                            "action 결과 자체가 이번 라운드의 남은 반환 문자 "
                            "예산보다 커서 다음 라운드에 싣지 못했습니다."
                        ),
                    }
                )
                run.budget_exhausted = True
                break
            budget_left -= entry_size
            results.append(entry)
        return results

    async def _execute_one(
        self, item, run: RetrievalRun, round_no: int, budget_left: int
    ) -> tuple[dict | None, int]:
        action_name = item.action
        attachment = getattr(item, "attachment", ALL_DOCUMENTS)
        documents, error = self._documents_for(attachment)
        if error:
            failure = {
                "round": round_no,
                "action": action_name,
                "attachment": attachment,
                "reason": error,
            }
            run.action_errors.append(failure)
            self.trace.write("action_error", failure, round_no=round_no)
            return ({"action": action_name, "error": error}, 0)

        if isinstance(item, GetDocumentStatus):
            return self._document_status(documents, action_name, budget_left)

        if isinstance(item, (ReadPage, ReadPages, ReadParagraph)):
            return await self._read(item, documents, run, round_no, budget_left)

        if isinstance(item, (SearchDocument, SearchExact, SearchNumbersAndSymbols)):
            return await self._search(item, documents, run, round_no, budget_left)

        return None, 0

    def _document_status(
        self, documents: list[IndexedDocument], action_name: str, budget_left: int
    ) -> tuple[dict, int]:
        """문헌 상태 조회. 본문이 없어도 반환 JSON 은 크다.

        문헌마다 페이지 목록이 여럿 실리므로 20개짜리 조회 하나가 예산의 두 배가
        된다(실측 8,318자). 본문이 없다는 이유로 소비량을 0 으로 두면 이 action
        만 반복해서 라운드 예산을 통째로 우회할 수 있다.
        """
        entry: dict = {"action": action_name, "documents": []}
        spent = json_size(entry)
        omitted: list[str] = []

        for document in documents:
            row = {
                "attachment": document.alias,
                "filename": document.filename,
                "pdf_sha256": document.sha256,
                **{
                    key: document.report.get(key)
                    for key in (
                        "source_page_count",
                        "processed_page_count",
                        "page_count_mismatch",
                        "ok_pages",
                        "empty_or_low_text_pages",
                        "extraction_failed_pages",
                        "visual_review_required_pages",
                        "extraction_divergence_pages",
                        "chunk_count",
                        "status",
                    )
                },
                "note": (
                    "OCR 은 수행하지 않았습니다. 위 목록의 페이지는 내용이 "
                    "없다는 뜻이 아니라 텍스트를 얻지 못했다는 뜻입니다."
                ),
            }
            cost = json_size(row)
            if spent + cost > budget_left:
                omitted.append(document.alias)
                continue
            entry["documents"].append(row)
            spent += cost

        def finish() -> dict:
            if omitted:
                entry["omitted_by_budget"] = list(omitted)
                entry["hint"] = (
                    "이번 라운드의 반환 문자 예산이 부족해 위 문헌의 상태를 "
                    "돌려주지 못했습니다. 다음 라운드에 나눠 요청하십시오."
                )
            return entry

        # 생략 안내 자체도 자리를 차지한다. 안내를 붙인 뒤 다시 재서, 그래도
        # 넘으면 문헌을 더 뺀다. 붙이기 전 크기를 믿으면 예산을 조금씩 넘긴다.
        finish()
        while json_size(entry) > budget_left and entry["documents"]:
            dropped = entry["documents"].pop()
            omitted.append(dropped["attachment"])
            finish()
        return entry, json_size(entry)

    async def _search(
        self,
        item,
        documents: list[IndexedDocument],
        run: RetrievalRun,
        round_no: int,
        budget_left: int,
    ) -> tuple[dict, int]:
        component = self._component(getattr(item, "component_id", ""))
        queries: list[str] = []
        phrases: list[str] = []
        literals: list[str] = []
        if isinstance(item, SearchDocument):
            queries = item.queries
        elif isinstance(item, SearchExact):
            phrases = item.phrases
        else:
            literals = item.terms

        results = search_module.search_corpus(
            documents,
            queries=queries,
            phrases=phrases,
            literals=literals,
            per_document_limit=min(item.limit, self.budget.hits_per_document),
            semantic_encoder=self.semantic_encoder,
        )

        # 완성된 action envelope 를 포함한 JSON 전체를 직접 재면서 후보를 넣는다.
        # 문헌 row 크기만 더하면 component_id와 생략 안내가 마지막에 붙어 상한을
        # 다시 넘는다. 실제로 4,000자 예산에서 최종 entry 가 5,813자가 됐었다.
        payload_documents: list[dict] = []
        omitted_aliases = [result.document.alias for result in results]
        preview_limit = 40
        include_hint = True
        omission_hint = (
            "일부 문헌은 이번 라운드의 반환 문자 예산이 부족해 결과를 싣지 "
            "못했습니다. 검색 자체는 실행됐습니다. 다음 라운드에 문헌을 "
            "나눠 요청하십시오."
        )

        def make_payload(document_rows: list[dict], aliases: list[str]) -> dict:
            payload: dict = {
                "action": item.action,
                "component_id": getattr(item, "component_id", ""),
                "documents": document_rows,
            }
            if aliases:
                payload["omitted_document_count"] = len(aliases)
                if preview_limit:
                    payload["omitted_documents"] = aliases[:preview_limit]
                if include_hint:
                    payload["hint"] = omission_hint
            return payload

        # 생략 안내 자체가 예산보다 큰 극단적인 경우에는 목록과 문장을 줄인다.
        # 개수는 끝까지 남으므로 AI 는 검색 결과가 완전하지 않다는 사실을 안다.
        while json_size(make_payload([], omitted_aliases)) > budget_left:
            if include_hint:
                include_hint = False
            elif preview_limit > 8:
                preview_limit = 8
            elif preview_limit:
                preview_limit = 0
            else:
                break

        base_cost = json_size(make_payload([], omitted_aliases))
        available = max(0, budget_left - base_cost)

        for position, result in enumerate(results):
            executed = [
                entry["channel"] for entry in result.channels if entry.get("executed")
            ]
            failed = [
                entry["channel"]
                for entry in result.channels
                if entry.get("requested") and not entry.get("executed")
            ]
            prefix_limit = base_cost + (
                available * (position + 1) // max(1, len(results))
            )
            document_entry: dict = {
                "attachment": result.document.alias,
                "filename": result.document.filename,
                "channels": _compact_channels(result.channels),
                "hits": [],
            }
            if result.hits:
                document_entry["omitted_by_budget"] = len(result.hits)

            candidate_aliases = [
                alias for alias in omitted_aliases if alias != result.document.alias
            ]
            trial = make_payload(
                [*payload_documents, document_entry], candidate_aliases
            )
            accepted = json_size(trial) <= prefix_limit
            if accepted:
                omitted_aliases = candidate_aliases
                payload_documents.append(document_entry)
                for hit in result.hits:
                    snippet = hit.row.text[: SNIPPET_CHARS]
                    row = {
                        **hit.to_dict(include_text=False),
                        "text": snippet,
                        "truncated": len(hit.row.text) > SNIPPET_CHARS,
                    }
                    document_entry["hits"].append(row)
                    omitted = len(result.hits) - len(document_entry["hits"])
                    if omitted:
                        document_entry["omitted_by_budget"] = omitted
                    else:
                        document_entry.pop("omitted_by_budget", None)
                    if json_size(make_payload(payload_documents, omitted_aliases)) > prefix_limit:
                        document_entry["hits"].pop()
                        omitted = len(result.hits) - len(document_entry["hits"])
                        document_entry["omitted_by_budget"] = omitted
                        break

            returned_rows = document_entry["hits"] if accepted else []
            omitted = len(result.hits) - len(returned_rows)
            if not accepted or omitted:
                run.budget_exhausted = True

            # 예산 조정이 끝난 다음, 실제 payload 에 남은 행만 노출로 기록한다.
            # 잠깐 넣었다 뺀 행을 먼저 기록하면 AI 가 못 본 청크가 근거가 된다.
            for row in returned_rows:
                attachment_id = str(row.get("attachment_id") or "")
                chunk_id = str(row.get("chunk_id") or "")
                if not attachment_id or not chunk_id:
                    continue
                run.exposed_chunks.add((attachment_id, chunk_id))
                if component is not None:
                    component.hit_chunks[f"{attachment_id}:{chunk_id}"] = {
                        "attachment_id": attachment_id,
                        "alias": row.get("alias") or result.document.alias,
                        "chunk_id": chunk_id,
                        "channels": list(row.get("channels") or []),
                    }
                    if row.get("page_number"):
                        component.record_page(attachment_id, int(row["page_number"]))

            if component is not None:
                for channel in executed:
                    if channel not in component.channels_used:
                        component.channels_used.append(channel)
                for channel in failed:
                    if channel not in component.failed_channels:
                        component.failed_channels.append(channel)
                component.record_search(
                    attachment_id=result.document.attachment_id,
                    alias=result.document.alias,
                    queries=[*queries, *phrases, *literals],
                    channels_used=executed,
                    failed_channels=failed,
                    hits=len(returned_rows),
                    omitted=omitted,
                )

        if component is not None:
            for value in (*queries, *phrases, *literals):
                component.record_query(value)

        self.trace.write(
            "search",
            {
                "action": item.action,
                "component_id": getattr(item, "component_id", ""),
                "attachment": getattr(item, "attachment", ALL_DOCUMENTS),
                "queries": [*queries, *phrases, *literals],
                "hits": sum(len(entry["hits"]) for entry in payload_documents),
            },
            round_no=round_no,
        )
        await self._emit(
            "retrieval_progress",
            {
                "phase": "search",
                "round": round_no,
                "queries": [*queries, *phrases, *literals][:5],
                "message": (
                    "로컬 인덱스 검색: "
                    + ", ".join([*queries, *phrases, *literals][:3])[:120]
                ),
            },
        )
        entry = make_payload(payload_documents, omitted_aliases)
        return entry, json_size(entry)

    async def _read(
        self,
        item,
        documents: list[IndexedDocument],
        run: RetrievalRun,
        round_no: int,
        budget_left: int,
    ) -> tuple[dict, int]:
        document = documents[0]
        index = document.index
        component = self._component(getattr(item, "component_id", ""))

        if isinstance(item, ReadParagraph):
            rows = index.paragraph_rows(item.paragraph)
            if not rows:
                reason = (
                    f"{document.alias} 에서 문단번호 {item.paragraph} 를 찾지 "
                    "못했습니다. 이 문헌에 그 번호가 없거나 문단번호가 없는 "
                    "형식입니다."
                )
                run.action_errors.append(
                    {"round": round_no, "action": item.action, "reason": reason}
                )
                return {"action": item.action, "error": reason}, 0
            pages = sorted({row.page_number for row in rows})
        elif isinstance(item, ReadPage):
            pages = [item.page]
        else:
            pages = list(item.pages)

        valid: list[int] = []
        rejected: list[dict] = []
        for page in pages:
            if page < 1 or page > document.page_count:
                rejected.append(
                    {
                        "page": page,
                        "reason": (
                            f"{document.alias} 는 {document.page_count}페이지입니다. "
                            f"{page}페이지는 범위 밖입니다."
                        ),
                    }
                )
                continue
            valid.append(page)

        # 스냅샷을 뜬다. 아래에서 record_page 가 같은 집합을 갱신하므로, 참조를
        # 그대로 쓰면 이번 요청의 뒤쪽 페이지가 "이미 읽음"으로 잘못 표시된다.
        already = (
            set(component.reviewed_pages.get(document.attachment_id, ()))
            if component is not None
            else set()
        )
        page_budget = max(0, self.budget.max_page_reads - run.pages_read)
        eligible = valid[:page_budget]
        skipped_budget = valid[page_budget:]
        skipped_chars = list(eligible)
        served: list[dict] = []
        served_rows: dict[int, list] = {}
        include_hint = True
        preview_limit = 40
        starting_pages_read = run.pages_read

        def make_payload(page_rows: list[dict], omitted_pages: list[int]) -> dict:
            payload: dict = {
                "action": item.action,
                "attachment": document.alias,
                "pages": page_rows,
                "pages_read_total": starting_pages_read + len(page_rows),
                "page_read_budget": self.budget.max_page_reads,
            }
            if rejected:
                payload["rejected"] = rejected
            if skipped_budget:
                payload["budget_exhausted_pages"] = skipped_budget[:preview_limit]
                payload["budget_exhausted_page_count"] = len(skipped_budget)
            if omitted_pages:
                payload["skipped_by_result_budget"] = omitted_pages[:preview_limit]
                payload["skipped_by_result_budget_count"] = len(omitted_pages)
                if include_hint:
                    payload["hint"] = (
                        "위 페이지는 이번 라운드의 반환 문자 예산보다 커서 "
                        "돌려주지 못했습니다. 본문을 잘라서 주지 않습니다. "
                        "검색 결과의 chunk_id를 사용하거나 더 좁은 구간을 "
                        "요청하십시오."
                    )
            return payload

        # 경고 골격도 실제 JSON 예산에 포함한다. 아주 작은 테스트 예산에서는
        # 설명과 페이지 번호 미리보기를 줄이되 생략 개수는 남긴다.
        while json_size(make_payload([], skipped_chars)) > budget_left:
            if include_hint:
                include_hint = False
            elif preview_limit > 8:
                preview_limit = 8
            elif preview_limit:
                preview_limit = 0
            else:
                break

        for page in eligible:
            rows = index.page_rows(page)
            status = index.page_status(page) or {}
            body = "\n\n".join(row.text for row in rows)
            page_entry = {
                "pdf_page": page,
                "printed_page": status.get("printed_page"),
                "extraction_status": status.get("status"),
                "extraction_error": status.get("extraction_error"),
                "already_read": page in already,
                "chunks": [
                    {
                        "chunk_id": row.chunk_id,
                        "paragraph": row.paragraph or None,
                        "section": row.section or None,
                    }
                    for row in rows
                ],
                "text": body,
            }
            candidate_skipped = [value for value in skipped_chars if value != page]
            trial = make_payload([*served, page_entry], candidate_skipped)
            if json_size(trial) > budget_left:
                run.budget_exhausted = True
                continue
            served.append(page_entry)
            served_rows[page] = rows
            skipped_chars = candidate_skipped

        # 선택이 끝난 뒤 실제로 반환되는 페이지에 대해서만 관측 상태를 갱신한다.
        for page_entry in served:
            page = int(page_entry["pdf_page"])
            if page in already:
                run.repeat_page_reads += 1
            for row in served_rows.get(page, []):
                run.exposed_chunks.add((document.attachment_id, row.chunk_id))
            run.pages_read += 1
            if component is not None:
                component.record_page(document.attachment_id, page)

        if skipped_budget or skipped_chars:
            run.budget_exhausted = True

        self.trace.write(
            "read",
            {
                "action": item.action,
                "attachment": document.alias,
                "pages": [entry["pdf_page"] for entry in served],
                "repeat_pages": [
                    entry["pdf_page"] for entry in served if entry["already_read"]
                ],
                "rejected": rejected,
                "skipped_budget": skipped_budget,
                "skipped_by_result_budget": skipped_chars,
            },
            round_no=round_no,
        )
        await self._emit(
            "retrieval_progress",
            {
                "phase": "read",
                "round": round_no,
                "pages_read": run.pages_read,
                "message": (
                    f"{document.alias} 페이지 읽기 — 누적 {run.pages_read}"
                    f"/{self.budget.max_page_reads}쪽"
                ),
            },
        )

        payload = make_payload(served, skipped_chars)
        return payload, json_size(payload)
