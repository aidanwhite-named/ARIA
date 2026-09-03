"""작업 실행 오케스트레이션.

큐 → Provider 세마포어 → 프롬프트 조립 → 실행 → 판정 → 저장.

Provider 당 동시 실행은 기본 1이다. 로컬 CLI 는 계정 단위 사용량 제한을
공유하므로 병렬로 올려봐야 대기만 늘어나는 경우가 많다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import (
    analysis_manifest,
    citation_mapping,
    job_assembly,
    patent_search,
    retrieval,
    search_channels,
    search_manifest,
    search_plan,
    search_prompt,
    search_report,
    search_verification,
    settings_service,
)
from ..config import DEFAULTS as SETTING_DEFAULTS, PATHS
from ..db import session_scope
from ..enums import DeliveryPlan, ErrorCode, JobKind, JobStatus, RetrievalMode
from ..evaluation.evaluator import Verdict, evaluate
from ..ingestion.service import IngestedFile, preprocessing_versions
from ..models import Attachment, ExecutionEvent, ExecutionJob, ResultArtifact
from .. import prompt_store
from ..prompt_assembly import InputTooLarge
from ..patent_search import retention as evidence_retention
from ..providers.base import (
    NO_TOOLS,
    ExecutionOutcome,
    ExecutionRequest,
    ToolPolicy,
)
from ..providers.registry import build_provider
from . import process as proc
from .bus import BUS

# UI 표시용 델타는 DB 에 남기지 않는다. 최종 결과 텍스트만 저장한다.
_NON_PERSISTED = frozenset({"result_stream"})

# 조립은 job_assembly 가 한다. runner 와 preflight 가 같은 함수를 부르지 않으면
# 화면이 안내한 크기와 실제로 나가는 크기가 어긋난다. 기존 import 경로를 쓰는
# 코드가 있으므로 이름만 여기 남긴다.
_SEARCH_CONTEXT_BY_POLICY = job_assembly.SEARCH_CONTEXT_BY_POLICY
search_spec = job_assembly.search_spec


def _evidence_artifact_ids(value) -> set[str]:
    """매니페스트가 실제로 참조하는 내용주소 증거 ID를 모은다."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "artifact_id" and isinstance(item, str) and item:
                found.add(item)
            elif key == "artifact_ids" and isinstance(item, list):
                found.update(str(part) for part in item if str(part))
            else:
                found.update(_evidence_artifact_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_artifact_ids(item))
    return found


def row_to_ingested(row: Attachment) -> IngestedFile:
    return IngestedFile(
        attachment_id=row.id,
        original_filename=row.original_filename,
        internal_filename=row.internal_filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        required=row.required,
        included=row.included,
        stored_path=row.stored_path,
        role=row.role,
        normalized_text_path=row.normalized_text_path,
        page_count=row.page_count,
        char_count=row.char_count,
        extraction_method=row.extraction_method,
        ocr_used=row.ocr_used,
        delivery_mode=row.delivery_mode,
        read_ok=row.read_ok,
        error=row.error,
    )


def render_search_focus(focus: dict | None) -> str:
    """검증된 선택 구성만 검색 프롬프트의 데이터 경계 안에 넣는다.

    원 분석 보고서와 인용 발췌문은 넣지 않는다. 구성 문구와 차이점도 모델 출력에서
    온 비신뢰 데이터이므로 search_prompt.render 가 경계 문자열을 다시 중화한다.
    """
    if not focus:
        return ""
    payload = {
        "threshold": focus.get("threshold", analysis_manifest.DEFAULT_THRESHOLD),
        "strategy": "combined_then_individual",
        "components": [
            {
                "id": item.get("id"),
                "claim": item.get("claim"),
                "symbol": item.get("symbol"),
                "feature": item.get("feature"),
                "similarity": item.get("similarity"),
                "status": item.get("status"),
                "difference": item.get("difference"),
            }
            for item in (focus.get("components") or [])
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _positive(value, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _setting(values: dict, key: str) -> int:
    """설정값 하나. 비어 있으면 **설정 기본값**으로 돌아간다.

    fallback 숫자를 호출부에 적지 않는 이유는 하나다. 코드에 박은 숫자와
    config.DEFAULTS 가 어긋나면, 설정이 누락된 경로에서만 옛 숫자가 살아난다.
    그 경로는 화면에서 값을 줄여도 줄지 않고, 아무도 그 사실을 모른다.
    """
    return _positive(values.get(key), int(SETTING_DEFAULTS[key]))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _search_lane_budgets(total: int, spec_provided: bool) -> dict[str, int]:
    """작업 전체 상한을 독립 실행에 나눈다.

    설정의 max_search_tool_calls 는 작업 한 건 전체 상한이다. 명세서를 넣었다고
    상한을 두 배로 늘리면 사용자가 정한 비용·안전 경계가 깨진다.
    """
    total = max(1, int(total))
    if not spec_provided:
        return {search_manifest.ORIGIN_CLAIM_ONLY: total}
    if total < 2:
        return {}
    claim_only = (total + 1) // 2
    return {
        search_manifest.ORIGIN_CLAIM_ONLY: claim_only,
        search_manifest.ORIGIN_SPEC_ASSISTED: total - claim_only,
    }


PROGRESS_SEARCH = "search"
PROGRESS_URL_LOOKUP = "url_lookup"
PROGRESS_FETCH = "fetch"


def _progress_counts_as(event_type: str, payload: dict) -> str:
    """실시간 진행 표시에서 이 이벤트를 무엇으로 셀지. "" 이면 세지 않는다.

    도구 이름만 보고 세면 안 된다. Codex 의 web_search 는 도구 하나로 검색과
    URL 조회를 겸하고, 종류는 완료 이벤트에서야 정해진다. 시작 이벤트에
    kind_pending 이 붙어 있으면 여기서 세지 않고 완료를 기다린다 — 그러지
    않으면 URL 조회가 "검색어 없는 검색 N회째" 로 화면에 찍힌다.
    """
    if event_type not in ("tool_use", "tool_use_resolved"):
        return ""
    if event_type == "tool_use" and payload.get("kind_pending"):
        return ""
    summary = payload.get("input") or {}
    kind = summary.get("input_kind") if isinstance(summary, dict) else None
    if kind == search_manifest.INPUT_KIND_URL:
        return PROGRESS_URL_LOOKUP
    if kind == search_manifest.INPUT_KIND_QUERY:
        return PROGRESS_SEARCH
    if event_type == "tool_use_resolved":
        # 종류를 표시하는 Provider 가 이번엔 표시하지 못했다는 뜻이다(완료
        # 이벤트에 query 가 비었거나 형태가 바뀌었다). 이름으로 되돌리면 URL
        # 조회가 다시 검색으로 잡힌다 — 애초에 이름 기반 가정을 버리려고 이
        # 이벤트를 만들었다. 모르면 세지 않는다.
        return ""
    name = str(payload.get("name") or "")
    # 종류를 표시하지 않는 Provider 는 도구 이름이 곧 종류다.
    if name in search_manifest.SEARCH_TOOL_NAMES:
        return PROGRESS_SEARCH
    if name in search_manifest.FETCH_TOOL_NAMES:
        return PROGRESS_FETCH
    return ""


def _progress_should_count(counted: set, search_origin: str, call_id: str) -> bool:
    """같은 호출을 두 번 세지 않는다. 세어야 하면 True 를 주고 표시까지 한다.

    키에 레인을 넣는다. 두 레인은 각자 별도의 CLI 프로세스이고 호출 ID 는 그
    프로세스 안에서만 고유하다. ID 만 쓰면 명세서 보조 검색의 첫 호출이 청구항
    단독 검색의 같은 ID 와 겹쳐 통째로 누락된다.
    """
    if not call_id:
        return True
    key = (search_origin, call_id)
    if key in counted:
        return False
    counted.add(key)
    return True


# 문헌 식별자가 들어 있는 질의. 특허 공개번호(US8773539, KR1020210086877) 와
# DOI 를 잡는다.
_IDENTIFIER_QUERY = re.compile(r"\b[A-Z]{2}\d{6,}\b|\b10\.\d{4,9}/")


def _literature_queries(observed: dict | None, *, limit: int) -> list[dict]:
    """ARIA 가 다시 물을 검색어를 고른다.

    ARIA 가 **관측한** 검색어만 쓴다. 모델이 보고서에 적은 검색어가 아니다 —
    둘은 어긋날 수 있고, 어긋났을 때 신뢰할 수 있는 쪽은 관측이다.

    같은 질의를 정규화한 뒤 중복을 없앤다. 모델은 같은 뜻의 검색어를 따옴표만
    바꿔 여러 번 부르는 일이 잦고, 그것을 그대로 다시 물으면 예산만 쓴다.
    """
    by_origin = ((observed or {}).get("search_queries_by_origin") or {})
    ordered: list[tuple[str, str]] = []
    if isinstance(by_origin, dict) and by_origin:
        for origin, queries in by_origin.items():
            for query in queries or []:
                ordered.append((str(origin), str(query)))
    else:
        for query in (observed or {}).get("search_queries") or []:
            ordered.append((search_manifest.ORIGIN_CLAIM_ONLY, str(query)))

    chosen: dict[str, dict] = {}
    for origin, query in ordered:
        normalized = patent_search.plain_query(query)
        # 낱말이 둘 이하로 남는 질의는 다시 묻지 않는다.
        if len(normalized.split()) < 3:
            continue
        # 문헌번호·DOI 확인용 질의도 보내지 않는다. 관측된 실행에서는 16개 중
        # 5개가 이런 질의였다("US8773539" "Google Patents" 같은 것). 이미 아는
        # 번호의 서지를 다시 확인하는 일이라 **개념 검색이 아니고**, 그 확인은
        # 아래 공식 서지 확보 단계가 DOI 로 직접 한다.
        if _IDENTIFIER_QUERY.search(normalized):
            continue
        key = normalized.lower()
        row = chosen.get(key)
        if row is None:
            if len(chosen) >= max(0, int(limit or 0)):
                continue
            chosen[key] = {"query": query, "search_origins": [origin]}
            continue
        if origin not in row["search_origins"]:
            row["search_origins"].append(origin)
    return list(chosen.values())


@dataclass
class _EpoStage:
    """EPO 공식 조회 구간의 결과.

    ``completed`` 가 거짓이면 이 채널로는 아무것도 받지 못했다는 뜻이고, 그 사유는
    ``section`` 에 담겨 있다. 실패가 아니다 — EPO 가 꺼져 있거나 그 문헌이 OPS 에
    없는 것과, 문헌이 존재하지 않는 것은 다른 말이다.
    """

    reported: dict | None
    completed: bool
    section: dict | None = None
    bundles: dict = field(default_factory=dict)
    dropped: list = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    started_at: str = ""
    backend: object = None



def _merge_search_outcomes(
    lane_outcomes: list[tuple[str, ExecutionOutcome]], tool_policy: ToolPolicy
) -> ExecutionOutcome:
    """두 Provider 실행의 원시 기록을 저장·감사용 한 객체로 합친다."""
    combined = ExecutionOutcome(tool_policy=tool_policy)
    if not lane_outcomes:
        return combined

    first = lane_outcomes[0][1]
    combined.cli_path = first.cli_path
    combined.cli_version = first.cli_version
    combined.cli_args = list(first.cli_args)
    combined.exit_code = 0
    combined.terminal_reason = "completed"
    usage_by_lane: dict[str, dict | None] = {}
    narratives: list[str] = []
    stdout: list[str] = []
    stderr: list[str] = []

    for origin, outcome in lane_outcomes:
        narratives.append(f"===== {origin} =====\n{outcome.result_text}")
        if outcome.raw_stdout:
            stdout.append(f"===== {origin} =====\n{outcome.raw_stdout}")
        if outcome.raw_stderr:
            stderr.append(f"===== {origin} =====\n{outcome.raw_stderr}")
        usage_by_lane[origin] = outcome.usage
        combined.permission_denials.extend(outcome.permission_denials)
        combined.errors.extend(outcome.errors)
        combined.tools_advertised.extend(
            name for name in outcome.tools_advertised if name not in combined.tools_advertised
        )
        combined.tool_uses.extend(outcome.tool_uses)
        for call in outcome.tool_calls:
            tagged = dict(call)
            tagged["search_origin"] = origin
            combined.tool_calls.append(tagged)

        combined.is_error = combined.is_error or outcome.is_error
        combined.cancelled = combined.cancelled or outcome.cancelled
        combined.timed_out = combined.timed_out or outcome.timed_out
        combined.auth_required = combined.auth_required or outcome.auth_required
        combined.rate_limited = combined.rate_limited or outcome.rate_limited
        combined.tool_budget_exceeded = (
            combined.tool_budget_exceeded or outcome.tool_budget_exceeded
        )
        combined.content_read_budget_exceeded = (
            combined.content_read_budget_exceeded
            or outcome.content_read_budget_exceeded
        )
        combined.tools_uncontrollable = (
            combined.tools_uncontrollable or outcome.tools_uncontrollable
        )
        combined.tools_must_be_disabled = (
            combined.tools_must_be_disabled or outcome.tools_must_be_disabled
        )
        if outcome.error_message and not combined.error_message:
            combined.error_message = outcome.error_message
        if outcome.exit_code not in (None, 0) and combined.exit_code == 0:
            combined.exit_code = outcome.exit_code
        if outcome.terminal_reason not in (None, "completed"):
            combined.terminal_reason = outcome.terminal_reason

    combined.result_text = "\n\n".join(narratives)
    combined.raw_stdout = "\n\n".join(stdout)
    combined.raw_stderr = "\n\n".join(stderr)
    combined.usage = {"search_lanes": usage_by_lane}
    return combined


class JobRunner:
    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._providers: dict[str, object] = {}
        self._seq: dict[str, int] = {}
        self._cancel_requested: set[str] = set()

    def _semaphore(self, provider_id: str, limit: int) -> asyncio.Semaphore:
        existing = self._semaphores.get(provider_id)
        if existing is None or getattr(existing, "_aria_limit", None) != limit:
            semaphore = asyncio.Semaphore(limit)
            semaphore._aria_limit = limit  # type: ignore[attr-defined]
            self._semaphores[provider_id] = semaphore
            return semaphore
        return existing

    async def submit(self, job_id: str) -> None:
        self._cancel_requested.discard(job_id)
        task = asyncio.create_task(self._run(job_id))
        self._tasks[job_id] = task

        def cleanup(_task) -> None:
            self._tasks.pop(job_id, None)
            self._cancel_requested.discard(job_id)

        task.add_done_callback(cleanup)

    async def cancel(self, job_id: str) -> bool:
        provider = self._providers.get(job_id)
        cancelled = False
        if provider is not None:
            with contextlib.suppress(Exception):
                cancelled = await provider.cancel(job_id)  # type: ignore[attr-defined]
        if not cancelled:
            cancelled = await proc.cancel_job(job_id)

        if not cancelled:
            # 아직 큐에서 대기 중인 작업.
            task = self._tasks.get(job_id)
            if task is not None and not task.done():
                task.cancel()
                cancelled = True
                with session_scope() as session:
                    job = session.get(ExecutionJob, job_id)
                    if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                        job.status = JobStatus.CANCELLED
                        job.error_code = ErrorCode.CANCELLED
                        job.completed_at = _utcnow()
                await BUS.publish(job_id, "status", {"status": JobStatus.CANCELLED})
                await BUS.close(job_id)
        if cancelled:
            # 독립 검색 두 호출 사이의 아주 짧은 구간에는 실행 중인 프로세스가
            # 없을 수 있다. 취소 의도를 메모리에 남겨 다음 호출이 시작되지 않게
            # 한다.
            self._cancel_requested.add(job_id)
        return cancelled

    # ------------------------------------------------------------------ 실행

    async def _emit(self, job_id: str, event_type: str, payload: dict) -> None:
        event = await BUS.publish(job_id, event_type, payload)
        if event_type in _NON_PERSISTED:
            return
        with contextlib.suppress(Exception), session_scope() as session:
            session.add(
                ExecutionEvent(
                    job_id=job_id,
                    seq=event.seq,
                    type=event_type,
                    payload=payload,
                )
            )

    async def _reject_if_over_byte_budget(
        self, job_id: str, provider, system_prompt: str, user_message: str
    ) -> bool:
        """Provider 가 자료 전체를 손실 없이 전달할 수 있는 한도를 넘으면 막는다.

        이것은 사용자 입력 제한이 아니라 전달 경로의 한계다. 이 크기를 넘겨
        보내면 Provider 가 뒷부분을 잘라 앞부분만 모델에 넘기고도(agy 실측)
        종료 코드 0 으로 끝나, 절반이 빠진 분석이 '성공'으로 남는다.

        그래서 ARIA 의 글자 수 한도(max_inline_chars)를 꺼도 이 검사는 남는다.
        글자 수는 사용자가 스스로 거는 상한이지만 이 한도는 모델이 자료를 전부
        보았는지를 좌우한다. 모델 컨텍스트가 크다거나 Provider 에 자동 압축이
        있다는 이유로 완화해서는 안 된다 — 자르는 주체가 모델이 아니라 CLI 다.

        한도를 넘겨 실패시켰으면 True 를 돌려주고, 호출부는 즉시 반환한다.
        """
        budget = getattr(provider, "max_input_bytes", None)
        if budget is None:
            return False
        # 크기는 Provider 에게 묻는다. 여기서 두 문자열을 더하면 감싸기·이스케이프
        # 이후의 크기를 알 수 없다 — agy 는 stream-json 한 줄로 직렬화한 뒤 그것을
        # 자르므로, 개행이 많은 문서일수록 합산값이 실제보다 작다.
        measure = getattr(provider, "payload_bytes", None)
        if callable(measure):
            payload_bytes = measure(system_prompt, user_message)
        else:
            payload_bytes = len(system_prompt.encode("utf-8")) + len(
                user_message.encode("utf-8")
            )
        if payload_bytes <= budget:
            return False
        label = getattr(provider, "display_name", "") or getattr(provider, "id", "")
        await self._fail(
            job_id,
            ErrorCode.INPUT_TOO_LARGE,
            f"이번 입력은 {label} 가 자료 전체를 손실 없이 전달할 수 있는 한도를 "
            f"넘습니다 ({payload_bytes:,} bytes > {budget:,} bytes). 사용자 입력 "
            f"제한이 아니라 {label} 가 모델에 넘기기 전에 뒷부분을 잘라 버리는 "
            "지점입니다. ARIA 는 문서를 임의로 자르거나 요약하지 않으므로 "
            "Provider 를 호출하기 전에 막았고, 토큰은 소모되지 않았습니다. "
            "문헌을 나눠 여러 번 실행하거나, 입력 전송 한도가 더 큰 Provider 를 "
            "선택하십시오.",
        )
        return True

    async def _run_epo_channel(
        self,
        *,
        job_id: str,
        values: dict,
        policy: search_channels.ChannelPolicy,
        provider,
        model,
        timeout: int,
        work_dir: Path,
        claim_text: str,
        spec_text: str,
        emit,
    ) -> tuple[dict, list]:
        """EPO 채널을 돈다. 웹 결과에는 어떤 경우에도 영향을 주지 않는다.

        꺼져 있거나 자격증명이 없으면 **모델도 OPS 도 부르지 않고** 빈 기록을
        돌려준다. 그때 이 함수가 하는 일은 사유를 적는 것뿐이다.

        레인 하나가 실패해도 다른 레인은 계속한다. EPO 는 보조 채널이고, 한
        레인의 실패로 나머지를 버리면 초기 보정에 쓸 자료가 사라진다.

        (감사 기록, 살아 있는 EpoSearchRun 목록) 을 돌려준다. 뒤엣것은 이
        실행 안에서만 쓴다 — 레인이 이미 받은 청구항·초록 본문이 들어 있어서
        공식 검증 단계가 같은 자료를 다시 내려받지 않게 해 준다. 직렬화된
        기록에는 그 본문이 없다(매니페스트를 특허 본문의 사본으로 만들지 않는다).
        """
        from ..patent_search import epo_agent

        if not policy.runs(search_channels.CHANNEL_EPO):
            return search_manifest.empty_epo_section(
                enabled=False,
                reason=policy.reason(search_channels.CHANNEL_EPO)
                or "EPO OPS 연동이 꺼져 있습니다.",
            ), []

        with session_scope() as session:
            backend = settings_service.epo_backend_for(session)
        if not backend.has_credentials:
            return search_manifest.empty_epo_section(
                enabled=True,
                reason="Consumer Key/Secret 가 설정되지 않아 EPO 채널을 건너뜁니다.",
            ), []

        # 채널 예산은 **작업당**이다(명세: "작업당 OPS 검색 요청 최대 6회",
        # "EPO 채널 전체 제한시간 180초"). 레인마다 만들면 레인 수만큼 예산이
        # 늘어난다.
        # fallback 숫자를 여기 적지 않는다. 설정이 누락된 경로에서 코드에 박힌
        # 옛 숫자로 돌아가면, 화면에서 줄인 값이 그 경로에서만 조용히 무시된다.
        channel = epo_agent.ChannelBudget(
            max_search_calls=_setting(values, "epo_max_search_calls"),
            max_detail_fetches=_setting(values, "epo_max_detail_fetches"),
            deadline_seconds=float(_setting(values, "epo_channel_timeout_seconds")),
        )
        channel.start()

        # 레인 예산. 채널 예산(위)과 다른 축이며, 상한은 전부 설정에서 온다 —
        # 코드에 박아 두면 화면에서 줄여도 실제로는 줄지 않는다.
        lane_budget = epo_agent.EpoAgentBudget(
            max_search_calls=channel.max_search_calls,
            max_detail_fetches=channel.max_detail_fetches,
            max_results_per_query=_setting(values, "epo_max_results_per_query"),
            shortlist_limit=_setting(values, "epo_shortlist_limit"),
        )

        origins = [search_manifest.ORIGIN_CLAIM_ONLY]
        if spec_text.strip():
            origins.append(search_manifest.ORIGIN_SPEC_ASSISTED)

        lanes: list[dict] = []
        runs: list = []
        section_error = ""
        for origin in origins:
            lane = search_manifest.lane_id(search_manifest.LANE_CHANNEL_EPO, origin)
            if job_id in self._cancel_requested:
                lanes.append(
                    search_manifest.epo_lane_record(
                        origin=origin, run=None, status="cancelled",
                        error="사용자가 실행을 취소했습니다.",
                    )
                )
                continue

            await emit(
                job_id,
                "stage",
                {
                    "stage": "executing",
                    "search_origin": origin,
                    "lane": lane,
                    "message": (
                        "EPO 특허 DB 검색 중"
                        if origin == search_manifest.ORIGIN_CLAIM_ONLY
                        else "EPO 명세서 보조 확장 검색 중"
                    ),
                },
            )

            async def lane_emit(event_type: str, payload: dict, _lane=lane) -> None:
                await emit(job_id, event_type, {**payload, "lane": _lane})

            agent = epo_agent.EpoSearchAgent(
                job_id=job_id,
                provider=provider,
                model=model,
                timeout_seconds=timeout,
                work_dir=work_dir / lane.replace(":", "-"),
                claim_text=claim_text,
                spec_text=(
                    spec_text
                    if origin == search_manifest.ORIGIN_SPEC_ASSISTED
                    else ""
                ),
                backend=backend,
                budget=lane_budget,
                channel=channel,
                lane_id=lane,
                emit=lane_emit,
                is_cancelled=lambda: job_id in self._cancel_requested,
            )
            try:
                run = await agent.run()
            except Exception as exc:  # noqa: BLE001 - 레인 하나의 실패로 끝내지 않는다
                lanes.append(
                    search_manifest.epo_lane_record(
                        origin=origin, run=None, status="failed", error=str(exc)
                    )
                )
                section_error = section_error or str(exc)
                continue
            runs.append(run)
            # 예외가 없었다는 이유만으로 ok 를 적지 않는다. 종료 사유가
            # 무엇이었는지가 이 레인의 상태다.
            lanes.append(
                search_manifest.epo_lane_record(
                    origin=origin,
                    run=run,
                    status=epo_agent.lane_status(run),
                    error=(
                        run.termination_detail
                        if run.termination_reason in epo_agent.FAILED_TERMINATIONS
                        else ""
                    ),
                )
            )
            if run.termination_reason in epo_agent.FAILED_TERMINATIONS:
                section_error = section_error or (
                    run.termination_detail
                    or f"EPO 검색이 {run.termination_reason} 로 끝났습니다."
                )

        # 사용량은 실행 뒤에 반드시 저장한다. 여기서 빠뜨리면 나간 바이트가
        # 메모리에만 남는다.
        try:
            settings_service.persist_epo_quota(backend.ledger)
        except Exception:  # pragma: no cover - 저장 실패는 화면 경고로 드러난다
            pass

        return {
            "enabled": True,
            "backend_id": search_manifest.EPO_BACKEND_ID,
            "reason": "",
            "channel_budget": channel.to_dict(),
            # 레인 예산도 남긴다. 채널 예산만 적으면 "결과를 몇 건까지 받았나",
            # "shortlist 를 몇 건까지 올렸나" 를 나중에 알 수 없다.
            "lane_budget": lane_budget.to_dict(),
            "lanes": lanes,
            "usage": backend.usage(),
            "error": section_error,
        }, runs

    def _kiwee_channel_record(
        self, policy: search_channels.ChannelPolicy, values: dict
    ) -> dict:
        """Kiwee 채널의 기록을 만든다. **네트워크를 열지 않는다.**

        정책이 이 채널을 돌지 않기로 했으면 사유만 적는다. 정책이 돌기로 했더라도
        백엔드가 스스로 미구성이라고 답하면 그 사유를 적는다 — 어느 쪽이든 검색을
        흉내 내지 않고, 후보를 만들지 않고, 요청을 보내지 않는다.

        골격을 만든 목적이 '연동 지점을 모듈로 고정'하는 것이므로, 실행 경로에서도
        그 지점이 눈에 보여야 한다. 채널이 아예 없는 것과 채널이 있는데 아직
        구현되지 않은 것은 다른 상태다.
        """
        if not policy.runs(search_channels.CHANNEL_KIWEE):
            section = search_manifest.empty_kiwee_section(
                enabled=bool(values.get("kiwee_integration_enabled", False)),
                reason=policy.reason(search_channels.CHANNEL_KIWEE),
            )
            section["skip_kind"] = (
                policy.skip_kind(search_channels.CHANNEL_KIWEE)
                or section["skip_kind"]
            )
            return section

        # 여기 오는 경우는 지금 없다(UNIMPLEMENTED). 나중에 구현이 붙었을 때를
        # 위해 백엔드의 자기 신고를 그대로 옮긴다.
        status = patent_search.describe(values, "kiwee")
        return search_manifest.empty_kiwee_section(
            enabled=True,
            reason=str(getattr(status, "detail", "") or "실행하지 않았습니다."),
        )

    async def _run_literature_channel(
        self,
        *,
        job_id: str,
        values: dict,
        policy: search_channels.ChannelPolicy,
        observed: dict | None,
        claim_text: str,
        plan: search_plan.SearchPlan | None = None,
    ) -> tuple[dict, object]:
        """ARIA 가 직접 서지 DB 에 물어 **식별된** 논문 후보를 만든다.

        왜 필요한가
        -----------
        웹 검색 채널은 논문을 식별하지 못한다. agy 의 search_web 은 결과 목록이
        아니라 줄글 요약과 익명 각주를 돌려주고(2026-09-01 실측: 검색 16회에
        각주 84개, 전부 제목도 DOI 도 없는 리다이렉트 주소), 그 리다이렉트는 이
        PC 의 네트워크가 차단한다. 그래서 모델이 후보로 적을 수 있는 것은 요약문이
        우연히 제목을 써 준 문헌뿐이었고, 84개 중 2건만 후보가 됐다.

        여기서는 각주를 해석하려 하지 않는다. **모델이 실제로 사용한 검색어**를
        가져와 ARIA 가 Crossref 와 Europe PMC 에 직접 묻는다. 두 곳 다 제목과
        DOI 가 붙은 결과를 주므로, 받은 문헌은 그대로 후보가 된다.

        모델의 검색어를 쓰는 이유
        -------------------------
        청구항에서 검색어를 다시 뽑으면 두 번째 검색기가 되고, 그 품질을 우리가
        보증해야 한다. 모델이 이미 만든 검색어를 쓰면 이 단계가 하는 일은 하나로
        좁혀진다 — **같은 질문을 식별 가능한 곳에 다시 묻는 것.**
        """
        section = search_manifest.empty_literature_section()
        if not policy.runs(search_channels.CHANNEL_LITERATURE):
            return search_manifest.empty_literature_section(
                reason=policy.reason(search_channels.CHANNEL_LITERATURE)
                or (
                    "비특허문헌(Crossref·Europe PMC) 연동이 꺼져 있어 ARIA "
                    "서지 검색을 하지 않았습니다."
                )
            ), None

        limit = _positive(values.get("literature_max_queries"), 6)
        queries = _literature_queries(observed, limit=limit)
        query_source = "observed"
        if not queries and plan is not None:
            # 웹 레인이 실패했거나 검색어를 한 건도 관측하지 못한 실행. 예전에는
            # 여기서 채널을 통째로 건너뛰었고, 그 결과 웹 채널 하나의 실패가 논문
            # 채널까지 함께 없앴다. 채널 격리를 지키려면 대체 입력이 있어야 한다.
            #
            # 대체 입력은 모델의 문장이 아니라 ARIA 의 내부 검색 계획이다. 계획은
            # 청구항과 사용자 전략 본문에서 기계적으로 뽑은 것이므로, 이 경로가
            # 모델 출력에 의존하지 않는다는 성질이 유지된다.
            queries = [
                {"query": text, "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY]}
                for text in plan.query_texts()[:limit]
            ]
            query_source = "plan"
        if not queries:
            return search_manifest.empty_literature_section(
                enabled=True,
                reason=(
                    "모델이 실행한 검색어를 관측하지 못했고 내부 검색 계획에서도 "
                    "질의를 만들지 못해 ARIA 서지 검색을 하지 않았습니다."
                ),
            ), None

        backend = patent_search.LiteratureBackend()
        backend.configure(values)
        rows = _positive(values.get("literature_max_results_per_query"), 10)
        section = search_manifest.empty_literature_section(enabled=True)
        section["limits"] = {
            "max_queries": len(queries),
            "max_results_per_query": rows,
        }
        # 이 채널이 무엇을 물었는지의 출처. 모델이 실제로 쓴 검색어인지, 모델
        # 출력을 얻지 못해 ARIA 의 내부 계획으로 대체했는지는 다른 사실이다.
        section["query_source"] = query_source

        await self._emit(
            job_id,
            "stage",
            {
                "stage": "searching",
                "message": (
                    f"모델이 쓴 검색어 {len(queries)}개로 ARIA 가 Crossref·"
                    "Europe PMC 를 직접 조회하는 중"
                ),
            },
        )

        by_doi: dict = {}
        for entry in queries:
            record = {
                "query": entry["query"],
                "normalized": patent_search.plain_query(entry["query"]),
                "search_origins": entry["search_origins"],
                "found": 0,
                "error": "",
                "notes": [],
            }
            if job_id in self._cancel_requested:
                record["error"] = "사용자가 실행을 취소했습니다."
                section["queries"].append(record)
                break
            try:
                response = await asyncio.to_thread(
                    backend.search,
                    patent_search.PatentSearchQuery(
                        text=entry["query"], max_results=rows
                    ),
                )
            except Exception as exc:  # 서지 채널 고장으로 웹 결과를 잃지 않는다
                record["error"] = f"{type(exc).__name__}: {exc}"
                section["queries"].append(record)
                continue
            record["notes"] = list(response.notes)
            record["found"] = len(response.records)
            # 두 서지 DB 가 모두 실패했으면 이 질의는 실패다. 예외가 올라오지
            # 않았다는 이유로 성공이라고 적으면, 전부 죽은 실행이 "결과 0건"
            # 으로 보인다 — 그 둘은 사용자가 할 일이 다르다.
            record["failed_sources"] = list(response.failed_sources)
            if response.failed_sources and not response.records:
                record["error"] = "; ".join(
                    note for note in response.notes if note
                ) or ("서지 DB 조회에 모두 실패했습니다: "
                      + ", ".join(response.failed_sources))
            section["queries"].append(record)

            for item in response.records:
                doi = str(item.doc_number or "").lower()
                if not doi:
                    continue
                row = by_doi.get(doi)
                if row is None:
                    fields = item.fields or {}
                    row = {
                        "doi": doi,
                        "doc_number": doi,
                        "title": item.title,
                        "authors": (fields.get("authors").value
                                    if "authors" in fields else ""),
                        "container": (fields.get("container").value
                                      if "container" in fields else ""),
                        "url": item.source_url,
                        "artifact_ids": [],
                        "evidence_fields": sorted(fields),
                        "sources": [],
                        "queries": [],
                        "search_origins": [],
                    }
                    by_doi[doi] = row
                for value in (item.fields or {}).values():
                    ref = value.evidence
                    if ref is None or not ref.artifact_id:
                        continue
                    if ref.artifact_id not in row["artifact_ids"]:
                        row["artifact_ids"].append(ref.artifact_id)
                    source = (
                        "crossref"
                        if ref.profile_id == patent_search.PROFILE_CROSSREF_JSON
                        else "europepmc"
                    )
                    if source not in row["sources"]:
                        row["sources"].append(source)
                if entry["query"] not in row["queries"]:
                    row["queries"].append(entry["query"])
                for origin in entry["search_origins"]:
                    if origin not in row["search_origins"]:
                        row["search_origins"].append(origin)

        # --- 후보표에 올릴 것을 고른다 -----------------------------------
        #
        # 받은 것을 전부 올리지 않는다. 한 실행에서 60건 넘게 오는데(2026-09-01
        # 실측: 질의 6개에 62건), 그것을 그대로 후보표에 넣으면 모델이 검토한
        # 후보와 검색 결과 목록이 같은 위계로 읽힌다. EPO 채널에 shortlist 상한을
        # 둔 것과 같은 이유다.
        #
        # 무엇을 위로 올릴 것인가에 ARIA 는 관련성 판단을 만들지 않는다. 대신
        # **교차 확인**을 신호로 쓴다.
        #
        #   1. 서로 다른 질의 여러 개가 같은 문헌을 데려왔는가
        #   2. 두 서지 DB 가 모두 그 문헌을 데려왔는가
        #
        # 둘 다 "이 문헌이 이 주제에 반복해서 걸린다"는 관측이지 우리의 의견이
        # 아니다. 같은 순위 안에서는 먼저 나온 순서를 지킨다(안정 정렬).
        discovered = list(by_doi.values())
        for position, row in enumerate(discovered):
            row["query_count"] = len(row["queries"])
            row["source_count"] = len(row["sources"])
            row["first_seen_position"] = position
        ranked = sorted(
            discovered,
            key=lambda row: (
                -row["query_count"],
                -row["source_count"],
                row["first_seen_position"],
            ),
        )
        shortlist = _positive(values.get("literature_shortlist_limit"), 10)
        promoted = ranked[:shortlist]
        promoted_keys = {row["doi"] for row in promoted}
        for row in discovered:
            row["promoted"] = row["doi"] in promoted_keys
        section["limits"]["shortlist_limit"] = shortlist
        # 후보표에 올린 것과 받은 것 전부를 나눠 남긴다. 상한에 걸려 빠진 문헌이
        # 기록에서 사라지면 "서지 검색이 못 찾았다"와 "찾았는데 안 올렸다"가
        # 같은 말이 된다.
        section["candidates"] = promoted
        section["discovered"] = [
            {
                "doi": row["doi"],
                "title": row["title"],
                "sources": row["sources"],
                "query_count": row["query_count"],
                "promoted": row["promoted"],
            }
            for row in ranked
        ]
        section["usage"] = backend.usage()
        return section, backend

    async def _epo_official_bundles(
        self,
        *,
        job_id: str,
        values: dict,
        reported: dict | None,
        fetch_budget: int | None,
        epo_runs: list | None,
    ) -> "_EpoStage":
        """EPO 공식 응답을 확보하는 구간. 2차 분류는 호출부가 돈다.

        _run_official_verification 에서 갈라져 나왔다. 이유는 하나다 — 논문
        후보의 근거(Crossref·Europe PMC)는 EPO 와 **무관하게** 확보되므로, EPO 가
        꺼져 있거나 자격증명이 없다고 해서 2차 분류까지 건너뛰면 안 된다. 예전에는
        이 구간의 조기 반환이 곧 단계 전체의 종료였다.

        조기 종료는 실패가 아니라 "이 채널로는 받지 못했다"이며, 그 사유는
        completed=False 인 _EpoStage 의 section 에 그대로 담겨 호출부로 간다.
        """
        if not patent_search.is_enabled(values, "epo"):
            detail = (
                "EPO OPS 연동이 꺼져 있어 공식 문헌 대조를 하지 않았습니다. "
                "각 후보는 1차 분류 그대로 남습니다."
            )
            reported = search_verification.annotate_not_attempted(
                reported, reason_code="epo_disabled", detail=detail
            )
            return _EpoStage(reported, False, search_verification.section(
                attempted=False,
                reason=detail,
            ))

        with session_scope() as session:
            backend = settings_service.epo_backend_for(session)
        if not backend.has_credentials:
            detail = (
                "EPO Consumer Key/Secret가 없어 공식 문헌을 확보하지 "
                "못했습니다. 각 후보는 1차 분류 그대로 남습니다."
            )
            reported = search_verification.annotate_not_attempted(
                reported, reason_code="epo_credentials_missing", detail=detail
            )
            return _EpoStage(reported, False, search_verification.section(
                attempted=False,
                reason=detail,
            ))

        if fetch_budget is None:
            fetch_budget = _setting(values, "epo_max_detail_fetches")
        fetch_budget = max(0, int(fetch_budget))
        # 검증 대상 수와 조회 예산은 다른 축이다. 후보 하나에 청구항·초록·서지
        # 세 번을 부를 수 있으므로, 조회 예산만으로 "몇 명을 검증할 것인가"를
        # 정하면 상한이 실행마다 달라진다.
        target_limit = _setting(values, "epo_verification_targets")
        limits = {
            "verification_targets": target_limit,
            "detail_fetches": fetch_budget,
            "configured_detail_fetches": _setting(values, "epo_max_detail_fetches"),
        }
        # EPO 검색 레인이 이미 받아 둔 자료. 있으면 그것으로 시작하고 모자란
        # 구성요소만 더 받는다. 무엇이 모자란지는 여기서 세어 둔다 — 묶음이
        # 있다는 것과 추가 조회가 없다는 것은 다른 말이다.
        prefetched = search_verification.reuse_bundles(epo_runs or [])
        reuse = search_verification.reuse_plan(prefetched)
        if fetch_budget == 0 and not prefetched:
            detail = "EPO 상세 조회 예산을 앞선 검색 채널에서 모두 사용했습니다."
            reported = search_verification.annotate_not_attempted(
                reported, reason_code="epo_budget_exhausted", detail=detail
            )
            return _EpoStage(reported, False, search_verification.section(
                attempted=False, reason=detail, limits=limits
            ))
        dropped: list[dict] = []
        selection_order: list[dict] = []
        found = search_verification.targets(
            reported,
            limit=target_limit,
            dropped=dropped,
            # 이미 받아 둔 아티팩트가 있는 후보를 먼저 고른다. 배열 순서로
            # 자르면 웹 후보가 앞자리를 다 차지해 EPO 후보가 통째로 빠진다.
            # 계획에는 후보마다 예상 추가 조회 횟수가 들어 있다.
            reuse=reuse,
            order=selection_order,
        )
        if not found:
            detail = "EPO OPS로 조회할 수 있는 특허번호 후보가 없습니다."
            reported = search_verification.annotate_not_attempted(
                reported, reason_code="no_epo_compatible_identifier", detail=detail
            )
            return _EpoStage(reported, False, search_verification.section(
                attempted=False,
                reason=detail,
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ))

        started_at = _utcnow().isoformat()
        await self._emit(
            job_id,
            "stage",
            {
                "stage": "verifying",
                "message": (
                    f"후보 {len(found)}건의 공식 초록·청구항을 EPO에서 "
                    "확인하는 중"
                ),
            },
        )
        try:
            bundles = await asyncio.to_thread(
                search_verification.fetch_official,
                found,
                backend,
                max_fetches=fetch_budget,
                is_cancelled=lambda: job_id in self._cancel_requested,
                prefetched=prefetched,
            )
        except Exception as exc:  # 공식 채널 고장으로 1차 검색을 잃지 않는다
            detail = f"공식 문헌 조회 단계 오류: {type(exc).__name__}: {exc}"
            reported = search_verification.annotate_not_attempted(
                reported,
                reason_code="official_fetch_stage_failed",
                detail=detail,
            )
            return _EpoStage(reported, False, search_verification.section(
                attempted=True,
                reason="공식 문헌 조회 단계가 실패해 후보를 잠정 분류로 남겼습니다.",
                classification_error=detail,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                target_count=len(found),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ))
        finally:
            # 성공·실패와 무관하게 실제 OPS 사용량을 저장한다.
            with contextlib.suppress(Exception):
                settings_service.persist_epo_quota(backend.ledger)

        return _EpoStage(
            reported,
            True,
            None,
            bundles,
            dropped,
            limits,
            selection_order,
            started_at,
            backend,
        )


    async def _run_official_verification(
        self,
        *,
        job_id: str,
        values: dict,
        provider,
        model,
        reasoning_effort: str,
        timeout: int,
        work_dir: Path,
        claim_text: str,
        reported: dict | None,
        fetch_budget: int | None = None,
        epo_runs: list | None = None,
        literature_bundles: dict | None = None,
        literature_dropped: list | None = None,
    ) -> tuple[dict | None, dict, ExecutionOutcome | None, list[str]]:
        """후보를 공식 문헌으로 확인하고 도구 없는 2차 분류를 돈다.

        **Provider 이름으로 갈라지지 않는다.** 예전에는 Codex 실행에서만 돌았다.
        Codex 는 web_search 의 URL 조회 성공을 스트림에 내지 않아 웹 게이트를
        통과할 수 없고, 이 경로가 유일한 산출 통로였기 때문이다. 그런데 그것은
        이 단계를 '보완'으로 만드는 이유이지 '한 Provider 전용'으로 만드는
        이유가 아니다. 페이지를 열었다는 관측은 그 페이지에 그 문장이 있었다는
        확인이 아니므로(search_manifest._mapping_row 주석), agy·Claude 후보에도
        같은 대조를 적용한다.

        2차 분류는 **검색에 쓴 것과 같은 Provider·모델·추론강도**로 돈다.
        중간에 Provider 를 갈아타면 사용자가 고르지 않은 CLI 를 호출하게 되고
        한 실행의 사용량이 두 계정으로 쪼개진다.

        승격되지 못한 후보를 강등하지는 않는다. 공식 조회 실패는 문헌 부재가
        아니다 — search_verification._keep_provisional 주석을 보라.
        """
        if reported is None:
            return reported, search_verification.section(
                attempted=False,
                reason="1차 후보 목록을 읽지 못해 공식 문헌 검증을 시작하지 않았습니다.",
            ), None, []

        literature_bundles = dict(literature_bundles or {})
        stage = await self._epo_official_bundles(
            job_id=job_id,
            values=values,
            reported=reported,
            fetch_budget=fetch_budget,
            epo_runs=epo_runs,
        )
        reported = stage.reported
        if not stage.completed and not literature_bundles:
            # 예전과 정확히 같은 경로다. 논문 근거가 하나도 없으면 EPO 가 돌지
            # 못한 것이 곧 이 단계의 결과다.
            return reported, stage.section, None, []

        bundles = dict(stage.bundles)
        # 논문 근거를 같은 묶음에 넣는다. 2차 분류 턴은 특허와 논문을 구분하지
        # 않는다 — 청구항과 겨루는 문헌이라는 점에서 같고, 근거의 출처는 각
        # 묶음의 backend_id 와 아티팩트 참조가 들고 있다.
        bundles.update(literature_bundles)
        dropped = list(stage.dropped)
        # 후보에게 사유를 적을 때만 논문 채널의 상한 제외를 함께 본다. EPO 검증
        # 구간의 excluded_candidates 에 섞으면 특허 상한이 논문 후보를 잘라 낸
        # 것처럼 읽힌다 — 두 상한은 다른 설정이고 다른 예산이다.
        annotation_dropped = dropped + [
            row for row in (literature_dropped or []) if isinstance(row, dict)
        ]
        limits = dict(stage.limits)
        selection_order = list(stage.order)
        started_at = stage.started_at or _utcnow().isoformat()
        backend = stage.backend
        # EPO 가 돌지 못한 실행에서도 아티팩트 저장소는 있어야 한다. 두 채널이
        # 같은 디렉터리를 쓰므로 어느 쪽 저장소든 같은 바이트를 읽는다.
        artifact_store = (
            backend.artifact_store
            if backend is not None
            else patent_search.LiteratureBackend().artifact_store
        )
        if not stage.completed:
            notes_prefix = [
                "EPO 공식 조회는 돌지 못했지만 확보한 논문 서지가 있어 2차 분류를 "
                "이어 갔습니다: " + str((stage.section or {}).get("reason") or "")
            ]
        else:
            notes_prefix = []

        reported = search_verification.annotate_bundles(
            reported, bundles, annotation_dropped
        )

        verified = [bundle for bundle in bundles.values() if bundle.verified]
        if job_id in self._cancel_requested:
            return reported, search_verification.section(
                attempted=True,
                reason="사용자가 공식 문헌 검증 중 실행을 취소했습니다.",
                bundles=bundles,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), None, notes_prefix
        if not verified:
            return reported, search_verification.section(
                attempted=True,
                reason="공식 응답에서 2차 분류에 사용할 본문을 확보하지 못했습니다.",
                bundles=bundles,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), None, notes_prefix

        system_prompt = search_verification.classification_system_prompt()
        user_message = search_verification.classification_message(claim_text, bundles)
        prompt_sha = hashlib.sha256(
            (system_prompt + "\n\x00\n" + user_message).encode("utf-8")
        ).hexdigest()
        verify_dir = work_dir / "official-verification"
        verify_dir.mkdir(parents=True, exist_ok=True)
        (verify_dir / "final_prompt.txt").write_text(
            "===== SYSTEM PROMPT =====\n"
            + system_prompt
            + "\n\n===== USER MESSAGE =====\n"
            + user_message,
            encoding="utf-8",
        )

        # 검증 단계가 크다는 이유로 1차 후보까지 실패시키지는 않는다. 전송 상한을
        # 넘으면 사유를 남기고 잠정 분류로 보고한다.
        byte_budget = getattr(provider, "max_input_bytes", None)
        if byte_budget is not None:
            measure = getattr(provider, "payload_bytes", None)
            payload_bytes = (
                measure(system_prompt, user_message)
                if callable(measure)
                else len(system_prompt.encode("utf-8"))
                + len(user_message.encode("utf-8"))
            )
            if payload_bytes > byte_budget:
                detail = (
                    "2차 공식 근거 프롬프트가 Provider 전송 상한을 넘었습니다 "
                    f"({payload_bytes:,} > {byte_budget:,} bytes)."
                )
                reported = search_verification.annotate_classification_failure(
                    reported, bundles, detail=detail, dropped=annotation_dropped
                )
                return reported, search_verification.section(
                    attempted=True,
                    reason=detail,
                    bundles=bundles,
                    classification_error=detail,
                    prompt_sha256=prompt_sha,
                    started_at=started_at,
                    completed_at=_utcnow().isoformat(),
                    dropped=dropped,
                    limits=limits,
                    order=selection_order,
                ), None, notes_prefix

        await self._emit(
            job_id,
            "stage",
            {
                "stage": "verifying",
                "message": (
                    f"확보한 공식 문헌 {len(verified)}건을 "
                    f"{getattr(provider, 'display_name', '') or '모델'}이 "
                    "A/B로 분류하는 중"
                ),
            },
        )

        async def classify_emit(event_type: str, payload: dict) -> None:
            await self._emit(
                job_id,
                event_type,
                {**dict(payload), "phase": "official_classification"},
            )

        request = ExecutionRequest(
            job_id=job_id,
            work_dir=verify_dir,
            system_prompt=system_prompt,
            user_message=user_message,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout,
            tool_policy=NO_TOOLS,
        )
        try:
            classification_outcome = await provider.execute(request, classify_emit)
        except Exception as exc:  # 1차 검색 결과는 보존하는 fail-soft 경로
            detail = f"2차 분류 실행 오류: {type(exc).__name__}: {exc}"
            reported = search_verification.annotate_classification_failure(
                reported, bundles, detail=detail, dropped=annotation_dropped
            )
            return reported, search_verification.section(
                attempted=True,
                reason="공식 문헌은 확보했지만 2차 분류를 완료하지 못했습니다.",
                bundles=bundles,
                classification_error=detail,
                prompt_sha256=prompt_sha,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), None, notes_prefix
        classification_verdict = evaluate(
            classification_outcome, [], fail_on_tool_use=True
        )
        if classification_verdict.status != JobStatus.SUCCEEDED:
            detail = " / ".join(classification_verdict.errors) or (
                classification_outcome.error_message or "2차 분류 실행이 실패했습니다."
            )
            reported = search_verification.annotate_classification_failure(
                reported, bundles, detail=detail, dropped=annotation_dropped
            )
            return reported, search_verification.section(
                attempted=True,
                reason="공식 문헌은 확보했지만 2차 분류를 완료하지 못했습니다.",
                bundles=bundles,
                classification_error=detail,
                prompt_sha256=prompt_sha,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), classification_outcome, notes_prefix

        try:
            payload = search_verification.parse_classification(
                classification_outcome.result_text
            )
            updated, notes = search_verification.apply_classification(
                reported, payload, bundles, artifact_store, dropped=annotation_dropped
            )
        except search_verification.ClassificationError as exc:
            detail = str(exc)
            reported = search_verification.annotate_classification_failure(
                reported, bundles, detail=detail, dropped=annotation_dropped
            )
            return reported, search_verification.section(
                attempted=True,
                reason="2차 분류 출력을 구조화하지 못해 잠정 분류로 남겼습니다.",
                bundles=bundles,
                classification_error=detail,
                prompt_sha256=prompt_sha,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), classification_outcome, notes_prefix
        except Exception as exc:  # 증거 대조 오류도 1차 후보를 없애지 않는다
            detail = f"공식 근거 대조 오류: {type(exc).__name__}: {exc}"
            reported = search_verification.annotate_classification_failure(
                reported, bundles, detail=detail, dropped=annotation_dropped
            )
            return reported, search_verification.section(
                attempted=True,
                reason="공식 근거를 대조하지 못해 후보를 잠정 분류로 남겼습니다.",
                bundles=bundles,
                classification_error=detail,
                prompt_sha256=prompt_sha,
                started_at=started_at,
                completed_at=_utcnow().isoformat(),
                dropped=dropped,
                limits=limits,
                order=selection_order,
            ), classification_outcome, notes_prefix

        return updated, search_verification.section(
            attempted=True,
            reason="",
            bundles=bundles,
            prompt_sha256=prompt_sha,
            started_at=started_at,
            completed_at=_utcnow().isoformat(),
            dropped=dropped,
            limits=limits,
            order=selection_order,
        ), classification_outcome, notes_prefix + notes

    async def _run(self, job_id: str) -> None:
        try:
            await self._run_inner(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 예상 못 한 오류도 작업 상태로 남긴다
            await self._fail(
                job_id,
                ErrorCode.PROCESS_ERROR,
                f"실행 중 처리하지 못한 오류: {type(exc).__name__}: {exc}",
            )

    async def _run_inner(self, job_id: str) -> None:
        with session_scope() as session:
            job = session.get(ExecutionJob, job_id)
            if job is None:
                return
            provider_id = job.provider
            model = job.model
            job_kind = JobKind(job.job_kind or JobKind.PATENT_ANALYSIS)
            master_prompt = job.prompt_snapshot
            # 실행 시점에 파일을 다시 읽지 않는다. 작업 생성 때 고른 프롬프트의
            # 신원과 본문 스냅샷으로 돈다 — 큐에서 기다리는 사이 사용자가 그
            # 전략을 고쳐도 이 실행의 계약은 흔들리지 않아야 한다.
            prompt_id = job.prompt_id or ""
            prompt_name = job.prompt_name or ""
            claim_text = job.claim_text
            followup_instruction = job.followup_instruction or ""
            # 생성 시점에 복사해 둔 값이다. 원본 실행을 여기서 다시 읽지 않는다.
            prior_claim_text = job.prior_claim_text or ""
            prior_report = job.prior_report or ""
            prior_mapping = job.prior_citation_mapping
            search_focus = job.search_focus
            capabilities = list(job.prompt_capabilities or [])
            output_mode = job.output_mode
            work_dir = Path(job.work_dir) if job.work_dir else PATHS.run_dir(job_id)
            # 「분석에 포함」을 푼 자료는 여기서 빠진다. preflight 가 크기를
            # 잴 때 부르는 것과 같은 함수이므로, 화면이 안내한 숫자와 실제로
            # 나가는 숫자가 어긋나지 않는다.
            attachments = job_assembly.included_attachments(
                [row_to_ingested(a) for a in job.attachments]
            )
            values = settings_service.get_all(session)
            # 고르지 않았으면 빈 문자열이고, 그때는 Provider 가 CLI 에 아무
            # 것도 넘기지 않는다. 여기서 기본값을 채우지 않는 것이 요점이다.
            reasoning_effort = str(
                (values.get("reasoning_effort") or {}).get(provider_id or "", "")
            ).strip()

        limit = int(values.get("max_concurrency_per_provider", 1))
        timeout = int(values.get("default_timeout_seconds", 900))
        # None = 제한 없음(기본값). Provider 전송 한도와 모델 컨텍스트 한도는
        # 이것과 별개로 아래에서 그대로 걸린다.
        max_chars = settings_service.inline_char_budget(values)
        runtime_context = str(values.get("runtime_context", ""))
        runtime_enabled = bool(values.get("runtime_context_enabled", True))
        keep_raw = bool(values.get("keep_raw_output", True))
        fail_on_tool_use = bool(values.get("fail_on_tool_use", True))
        overrides = values.get("provider_paths") or {}
        # 로컬 검색 설정. preflight 가 크기를 잴 때 쓰는 것과 **같은 함수**로
        # 예산을 만든다. 두 곳이 각자 기본값을 적어 두면 화면이 안내한 상한과
        # 실행이 강제하는 상한이 어긋난다.
        retrieval_mode = str(values.get("retrieval_mode") or "auto")
        retrieval_budget = retrieval.budget_from_settings(values)
        semantic_enabled = bool(values.get("retrieval_semantic_enabled", False))
        embedding_cache_max_bytes = (
            max(0, int(values.get("embedding_cache_max_mb") or 0)) * 1024 * 1024
        )
        delivery_policy = job_assembly.delivery_policy_from_settings(values)

        # Provider 를 만든 뒤 그 Provider 가 선언한 검색 정책으로 교체한다.
        tool_policy: ToolPolicy = NO_TOOLS
        search_budget = int(values.get("max_search_tool_calls", 40))

        # --- 채널 실행 정책 --------------------------------------------------
        #
        # 프롬프트 본문을 보지 않는다. 어떤 채널이 도는지는 작업 종류와 설정이
        # 정하며, 프롬프트에 "검색하지 마라"라고 적혀 있어도 이 판정은 바뀌지
        # 않는다. 반대로 프롬프트가 채널을 켜지도 못한다.
        channel_policy = search_channels.resolve(values)

        await self._emit(job_id, "stage", {"stage": "queued", "message": "실행 대기 중"})

        semaphore = self._semaphore(provider_id, limit)
        async with semaphore:
            provider = build_provider(provider_id, overrides)
            if provider is None:
                await self._fail(
                    job_id,
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    f"알 수 없는 Provider 입니다: {provider_id}",
                )
                return

            if job_kind is JobKind.SIMILARITY_SEARCH:
                selected_policy = provider.search_tool_policy
                if (
                    selected_policy is None
                    or not provider.supports_tool_policy(selected_policy)
                ):
                    await self._fail(
                        job_id,
                        ErrorCode.PROVIDER_UNAVAILABLE,
                        f"{provider_id} 는 유사 문헌 웹 검색 정책을 지원하지 않습니다.",
                    )
                    return
                tool_policy = replace(
                    selected_policy, max_tool_calls=max(1, search_budget)
                )

            if job_kind is JobKind.PATENT_ANALYSIS and not attachments:
                # 작업 생성에서 이미 막지만, 큐에서 기다리는 사이에 자료가
                # 사라졌거나 예전 클라이언트가 만든 작업일 수 있다. 대비할
                # 문헌이 없는 실행은 사용량만 쓰고 끝나므로 여기서도 막는다.
                await self._fail(
                    job_id,
                    ErrorCode.ATTACHMENT_ERROR,
                    job_assembly.NO_INCLUDED_MATERIAL,
                )
                return

            self._providers[job_id] = provider
            work_dir.mkdir(parents=True, exist_ok=True)

            started = _utcnow()
            with session_scope() as session:
                job = session.get(ExecutionJob, job_id)
                if job is None:
                    return
                if job.status == JobStatus.CANCELLED:
                    return
                job.status = JobStatus.RUNNING
                job.started_at = started
                job.preprocessing_versions = preprocessing_versions()
            await self._emit(job_id, "status", {"status": JobStatus.RUNNING})
            await self._emit(
                job_id, "stage", {"stage": "preprocessing", "message": "프롬프트 조립 중"}
            )

            # --- 프롬프트 조립 -------------------------------------------
            search_prompt_sha = ""
            search_runtime_context_sha = ""
            search_prompt_mode = ""
            strategy_boundary_neutralized = False
            claim_boundary_neutralized = False
            spec_boundary_neutralized = False
            focus_boundary_neutralized = False
            spec_document: dict | None = None
            search_assemblies: dict[str, object] = {}
            lane_budgets: dict[str, int] = {}
            content_budget = 0
            content_lane_budgets: dict[str, int] = {}
            try:
                assembly = job_assembly.assemble_job(
                    job_kind=job_kind,
                    master_prompt=master_prompt,
                    attachments=attachments,
                    runtime_context=runtime_context,
                    runtime_context_enabled=runtime_enabled,
                    max_chars=max_chars,
                    claim_text=claim_text,
                    focus_text=render_search_focus(search_focus),
                    search_prompt_id=prompt_id or search_prompt.SEARCH_PROMPT_ID,
                    followup_instruction=followup_instruction,
                    prior_claim_text=prior_claim_text,
                    prior_report=prior_report,
                    prior_citation_mapping=prior_mapping,
                    tool_policy_name=tool_policy.name,
                    agy_allowed_hosts=job_assembly.allowed_hosts_for(
                        tool_policy.name
                    ),
                    retrieval_mode=retrieval_mode,
                    provider_byte_budget=getattr(provider, "max_input_bytes", None),
                    retrieval_budget=retrieval_budget,
                    provider_id=provider_id,
                    model=model or "",
                    provider_measure=getattr(provider, "payload_bytes", None),
                    claim_element_count=job_assembly.claim_element_count(claim_text),
                    **delivery_policy,
                )
                assembled = assembly.representative
                if job_kind is JobKind.SIMILARITY_SEARCH:
                    search_assemblies = dict(assembly.lanes)
                    spec_document = assembly.spec_document
                    search_prompt_sha = assembly.search_prompt_sha
                    search_runtime_context_sha = assembly.search_runtime_context_sha
                    claim_boundary_neutralized = assembly.claim_boundary_neutralized
                    spec_boundary_neutralized = assembly.spec_boundary_neutralized
                    focus_boundary_neutralized = assembly.focus_boundary_neutralized
                    search_prompt_mode = assembly.search_prompt_mode
                    strategy_boundary_neutralized = (
                        assembly.strategy_boundary_neutralized
                    )

                    lane_budgets = _search_lane_budgets(
                        search_budget, spec_document is not None
                    )
                    # 본문 읽기 상한은 사용자 설정이 아니라 정책 상수다. 검색
                    # 횟수와 달리 비용·범위를 정하는 값이 아니라, 한 문헌을 몇
                    # 조각으로 나눠 읽는지에 딸린 값이기 때문이다.
                    content_budget = int(
                        getattr(tool_policy, "max_content_read_calls", 0) or 0
                    )
                    content_lane_budgets = (
                        _search_lane_budgets(
                            content_budget, spec_document is not None
                        )
                        if content_budget
                        else {}
                    )
                    if content_budget and not content_lane_budgets:
                        # 나눌 수 없을 만큼 작은 상한. 0 은 '상한 없음'이라
                        # 여기서 쓰면 정반대가 되므로 레인마다 1회로 둔다.
                        content_lane_budgets = {
                            origin: 1 for origin in search_assemblies
                        }
                    # 검색 호출과 URL 조회 상한도 같은 방식으로 레인에 나눈다.
                    # 나누지 않으면 두 레인이 한 예산을 놓고 경쟁해서 먼저 도는
                    # 레인이 다 써 버린다.
                    search_call_budget = int(
                        getattr(tool_policy, "max_search_calls", 0) or 0
                    )
                    search_call_lane_budgets = (
                        _search_lane_budgets(
                            search_call_budget, spec_document is not None
                        )
                        if search_call_budget
                        else {}
                    )
                    if search_call_budget and not search_call_lane_budgets:
                        search_call_lane_budgets = {
                            origin: 1 for origin in search_assemblies
                        }
                    url_lookup_budget = int(
                        getattr(tool_policy, "max_url_lookup_calls", 0) or 0
                    )
                    url_lookup_lane_budgets = (
                        _search_lane_budgets(
                            url_lookup_budget, spec_document is not None
                        )
                        if url_lookup_budget
                        else {}
                    )
                    if url_lookup_budget and not url_lookup_lane_budgets:
                        url_lookup_lane_budgets = {
                            origin: 1 for origin in search_assemblies
                        }
                    if spec_document is not None and not lane_budgets:
                        await self._fail(
                            job_id,
                            ErrorCode.SEARCH_BUDGET_EXCEEDED,
                            "출원발명 문서를 사용한 검색은 청구항 단독·명세서 보조 "
                            "두 독립 실행이 필요합니다. 검색 1회당 최대 도구 호출 "
                            "수를 2 이상으로 설정하십시오.",
                        )
                        return
            except job_assembly.SpecUnreadable as exc:
                await self._fail(
                    job_id,
                    ErrorCode.ATTACHMENT_ERROR,
                    "출원발명 문서의 본문을 읽지 못했습니다: "
                    f"{exc.filename}. 명세서를 반영하지 못한 채로 검색하지 "
                    "않습니다.",
                )
                return
            except (InputTooLarge, job_assembly.ModelInputTooLarge) as exc:
                await self._fail(job_id, ErrorCode.INPUT_TOO_LARGE, str(exc))
                return
            except search_prompt.SearchPromptError as exc:
                await self._fail(job_id, ErrorCode.SEARCH_PROMPT_ERROR, str(exc))
                return

            # --- 로컬 검색 (retrieval) -----------------------------------
            # 여기까지의 assembly 는 "어떻게 전달할 것인가"만 정한 것이다.
            # 로컬 검색으로 정해졌으면 실제 근거 패키지를 만든 뒤 **같은 조립
            # 함수**로 최종 프롬프트를 다시 만든다. preflight 가 잰 크기는 예산
            # 상한이고, 여기서 만드는 실제 패키지는 그 상한을 넘지 못한다.
            delivery_plan = assembly.delivery_plan
            # 왜 이 폭을 골랐는가는 **여기서 정해진다.** 아래에서 근거 묶음을
            # 넣어 다시 조립할 때는 이미 정해진 폭을 고정 모드로 넘기므로, 그때
            # 나오는 사유는 "사용자가 고정했다"가 되어 버린다. 원래 판정을 들고
            # 가서 최종 기록에 되돌린다 — 화면이 안내한 문장과 실행이 남긴
            # 문장이 달라지면 같은 실행을 두 가지로 설명하게 된다.
            delivery_decision = assembly.decision
            retrieval_manifest: dict | None = None
            retrieval_error: str | None = None
            retrieval_artifacts: list[tuple[str, Path]] = []
            retrieval_usage: dict = {}

            if (
                job_kind is JobKind.PATENT_ANALYSIS
                and delivery_plan == DeliveryPlan.LOCAL_RETRIEVAL
            ):
                await self._emit(
                    job_id,
                    "stage",
                    {
                        "stage": "indexing",
                        "message": "인용발명 문헌을 페이지·문단 단위로 로컬 색인 중",
                    },
                )

                async def retrieval_emit(event_type: str, payload: dict) -> None:
                    await self._emit(job_id, event_type, payload)

                found = await retrieval.run_retrieval(
                    job_id=job_id,
                    provider=provider,
                    model=model,
                    timeout_seconds=timeout,
                    work_dir=work_dir,
                    attachments=attachments,
                    claim_text=claim_text,
                    budget=retrieval_budget,
                    semantic_enabled=semantic_enabled,
                    embedding_cache_max_bytes=embedding_cache_max_bytes,
                    emit=retrieval_emit,
                    is_cancelled=lambda: job_id in self._cancel_requested,
                )
                retrieval_manifest = found.manifest or None
                retrieval_artifacts = list(found.artifacts)
                retrieval_usage = found.usage
                try:
                    if not found.ok:
                        retrieval_error = found.error or "로컬 검색이 실패했습니다."
                        self._save_retrieval(
                            job_id,
                            delivery_plan,
                            retrieval_manifest,
                            retrieval_error,
                            retrieval_artifacts,
                        )
                        if found.cancelled:
                            await self._cancelled(job_id)
                            return
                        await self._fail(
                            job_id,
                            found.error_code or ErrorCode.RETRIEVAL_FAILED,
                            retrieval_error,
                        )
                        return

                    try:
                        assembly = job_assembly.assemble_job(
                            job_kind=job_kind,
                            master_prompt=master_prompt,
                            attachments=attachments,
                            runtime_context=runtime_context,
                            runtime_context_enabled=runtime_enabled,
                            max_chars=max_chars,
                            claim_text=claim_text,
                            followup_instruction=followup_instruction,
                            prior_claim_text=prior_claim_text,
                            prior_report=prior_report,
                            prior_citation_mapping=prior_mapping,
                            retrieval_mode=RetrievalMode.RETRIEVAL,
                            provider_byte_budget=getattr(
                                provider, "max_input_bytes", None
                            ),
                            retrieval_budget=retrieval_budget,
                            evidence_bundle=found.bundle,
                            provider_id=provider_id,
                            model=model or "",
                            provider_measure=getattr(provider, "payload_bytes", None),
                            **delivery_policy,
                        )
                        assembled = assembly.representative
                    except (InputTooLarge, job_assembly.ModelInputTooLarge) as exc:
                        self._save_retrieval(
                            job_id,
                            delivery_plan,
                            retrieval_manifest,
                            str(exc),
                            retrieval_artifacts,
                        )
                        await self._fail(job_id, ErrorCode.INPUT_TOO_LARGE, str(exc))
                        return
                finally:
                    retrieval.close_documents(found.documents)

                await self._emit(
                    job_id,
                    "retrieval_ready",
                    {
                        "rounds": len((retrieval_manifest or {}).get("rounds") or []),
                        "pages_read": (retrieval_manifest or {}).get("pages_read", 0),
                        "evidence_chars": (found.bundle or {}).get(
                            "evidence_chars", 0
                        ),
                    },
                )

            # 전달 기록은 **최종 조립본**으로 만든다. 로컬 검색이 돌았으면 위에서
            # 다시 조립했으므로, 그 전에 만들면 자리표 크기가 실제로 나간 크기로
            # 기록된다.
            if delivery_decision is not None:
                assembly.decision = delivery_decision
                assembly.full_inline_bytes = delivery_decision.full_inline_bytes
                assembly.full_inline_chars = delivery_decision.full_inline_chars
            delivery_manifest = assembly.delivery_manifest(provider)

            prompt_path = work_dir / "final_prompt.txt"
            if job_kind is JobKind.SIMILARITY_SEARCH and len(search_assemblies) > 1:
                prompt_parts: list[str] = []
                for origin, lane_assembled in search_assemblies.items():
                    lane_dir = work_dir / origin
                    lane_dir.mkdir(parents=True, exist_ok=True)
                    lane_prompt = (
                        f"===== SYSTEM PROMPT =====\n{lane_assembled.system_prompt}\n\n"
                        f"===== USER MESSAGE =====\n{lane_assembled.user_message}"
                    )
                    (lane_dir / "final_prompt.txt").write_text(
                        lane_prompt, encoding="utf-8"
                    )
                    prompt_parts.append(
                        f"===== SEARCH LANE: {origin} =====\n{lane_prompt}"
                    )
                prompt_text = "\n\n".join(prompt_parts)
            else:
                prompt_text = (
                    f"===== SYSTEM PROMPT =====\n{assembled.system_prompt}\n\n"
                    f"===== USER MESSAGE =====\n{assembled.user_message}"
                )
            prompt_path.write_text(prompt_text, encoding="utf-8")
            if job_kind is JobKind.SIMILARITY_SEARCH and len(search_assemblies) > 1:
                lane_identity = "\n".join(
                    f"{origin}:{lane_assembled.sha256}"
                    for origin, lane_assembled in search_assemblies.items()
                )
                final_prompt_sha = hashlib.sha256(
                    lane_identity.encode("utf-8")
                ).hexdigest()
                final_prompt_chars = sum(
                    lane_assembled.total_chars
                    for lane_assembled in search_assemblies.values()
                )
            else:
                # 기존 분석·단일 검색 필드의 의미를 바꾸지 않는다.
                final_prompt_sha = assembled.sha256
                final_prompt_chars = assembled.total_chars

            with session_scope() as session:
                job = session.get(ExecutionJob, job_id)
                if job is not None:
                    job.system_prompt_snapshot = assembled.system_prompt
                    job.final_prompt_path = str(prompt_path)
                    job.final_prompt_sha256 = final_prompt_sha
                    job.final_prompt_chars = final_prompt_chars
                    job.attachment_manifest = assembled.manifest

            await self._emit(
                job_id,
                "prompt_ready",
                {
                    "chars": final_prompt_chars,
                    "sha256": final_prompt_sha,
                    "attachments": len(attachments),
                },
            )

            # --- 실행 -----------------------------------------------------
            # 검색 작업은 도구 호출이 곧 진행 상황이다. 화면이 "무엇을 검색하고
            # 어디를 열어 보는 중"인지 보여줄 수 있도록 관측한 호출을 단계로
            # 옮긴다. 보고서를 기다리는 동안 아무 일도 없어 보이면 안 된다.
            # counted 는 이미 센 호출의 (레인, ID). 같은 호출이 시작·완료 두 번
            # 오므로 이것이 없으면 두 번 세거나, 종류를 알기 전에 세게 된다.
            # 레인을 키에 넣는 이유: 두 레인은 각자 별도의 CLI 프로세스이고 호출
            # ID 는 프로세스 안에서만 고유하다. ID 만 쓰면 명세서 보조 검색의
            # 첫 호출이 청구항 단독 검색의 같은 ID 와 겹쳐 통째로 누락된다.
            search_state = {
                "searches": 0,
                "fetches": 0,
                "reads": 0,
                "counted": set(),
            }

            def make_emit(search_origin: str | None = None):
                async def emit(event_type: str, payload: dict) -> None:
                    payload = dict(payload)
                    if search_origin:
                        payload["search_origin"] = search_origin
                    await self._emit(job_id, event_type, payload)
                    if job_kind is not JobKind.SIMILARITY_SEARCH:
                        return
                    if event_type not in ("tool_use", "tool_use_resolved"):
                        return
                    counts_as = _progress_counts_as(event_type, payload)
                    name = str(payload.get("name") or "")
                    if not counts_as and name not in (
                        tool_policy.content_read_tools or ()
                    ):
                        return
                    if not _progress_should_count(
                        search_state["counted"],
                        search_origin,
                        str(payload.get("id") or ""),
                    ):
                        return
                    summary = payload.get("input") or {}
                    origin_label = (
                        "청구항 단독"
                        if search_origin == search_manifest.ORIGIN_CLAIM_ONLY
                        else "명세서 확장"
                    )
                    if counts_as == PROGRESS_URL_LOOKUP:
                        # 검색도 아니고 페이지 열람도 아니다. 성공 여부를 알 수
                        # 없으므로 "시도" 로만 알린다.
                        search_state["url_lookups"] = (
                            search_state.get("url_lookups", 0) + 1
                        )
                        await self._emit(
                            job_id,
                            "search_progress",
                            {
                                "phase": "url_lookup",
                                "search_origin": search_origin,
                                "searches": search_state["searches"],
                                "fetches": search_state["fetches"],
                                "url_lookups": search_state["url_lookups"],
                                "message": (
                                    f"{origin_label} URL 조회 "
                                    f"{search_state['url_lookups']}건째"
                                    " (열람 성공 여부는 확인되지 않음): "
                                    f"{str(summary.get('url', ''))[:120]}"
                                ),
                            },
                        )
                    elif counts_as == PROGRESS_SEARCH:
                        search_state["searches"] += 1
                        await self._emit(
                            job_id,
                            "search_progress",
                            {
                                "phase": "search",
                                "search_origin": search_origin,
                                "searches": search_state["searches"],
                                "fetches": search_state["fetches"],
                                "query": summary.get("query", ""),
                                "message": (
                                    f"{origin_label} 검색 "
                                    f"{search_state['searches']}회째: "
                                    f"{str(summary.get('query', ''))[:120]}"
                                ),
                            },
                        )
                    elif counts_as == PROGRESS_FETCH:
                        search_state["fetches"] += 1
                        await self._emit(
                            job_id,
                            "search_progress",
                            {
                                "phase": "fetch",
                                "search_origin": search_origin,
                                "searches": search_state["searches"],
                                "fetches": search_state["fetches"],
                                "url": summary.get("url", ""),
                                "message": (
                                    f"{origin_label} 원문 페이지 확인 "
                                    f"{search_state['fetches']}건째: "
                                    f"{str(summary.get('url', ''))[:120]}"
                                ),
                            },
                        )
                    elif name in (tool_policy.content_read_tools or ()):
                        # 본문을 나눠 읽는 구간. 검색도 열람도 늘지 않으므로
                        # 표시하지 않으면 화면이 멈춘 것처럼 보인다.
                        search_state["reads"] += 1
                        await self._emit(
                            job_id,
                            "search_progress",
                            {
                                "phase": "read",
                                "search_origin": search_origin,
                                "searches": search_state["searches"],
                                "fetches": search_state["fetches"],
                                "reads": search_state["reads"],
                                "message": (
                                    f"{origin_label} 페이지 본문 확인 "
                                    f"{search_state['reads']}회째"
                                ),
                            },
                        )

                return emit

            # --- 내부 검색 계획 ------------------------------------------
            #
            # 검색을 시작하기 전에 ARIA 가 자기 스키마로 만든다. 사용자 전략
            # 프롬프트와 청구항이 입력이고, 이 스키마는 그 프롬프트에 노출되지
            # 않는다. 계획한 것과 실제로 실행된 검색어는 감사 기록에서 서로 다른
            # 자리에 남는다(plan vs observed).
            plan: search_plan.SearchPlan | None = None
            if job_kind is JobKind.SIMILARITY_SEARCH:
                plan = search_plan.build(
                    claim_text=claim_text,
                    strategy_body=master_prompt,
                    strategy_prompt_id=prompt_id,
                    strategy_prompt_sha256=search_prompt_sha,
                    search_focus=search_focus,
                    spec_provided=spec_document is not None,
                )
                await self._emit(
                    job_id,
                    "search_plan_ready",
                    {
                        "terms": len(plan.terms),
                        "queries": len(plan.queries),
                        "components": len(plan.components),
                        "classifications": list(plan.classifications),
                    },
                )

            search_lane_outcomes: list[tuple[str, ExecutionOutcome]] = []
            search_lane_records: list[dict] = []
            lane_verdicts: list[Verdict] = []
            epo_section: dict = search_manifest.empty_epo_section()
            # 살아 있는 EPO 레인 결과. 직렬화된 기록에는 본문이 없으므로,
            # 공식 검증 단계가 같은 자료를 다시 받지 않으려면 이것이 필요하다.
            epo_runs: list = []

            if job_kind is JobKind.SIMILARITY_SEARCH:
                for origin, lane_assembled in search_assemblies.items():
                    lane_dir = work_dir / origin
                    lane_dir.mkdir(parents=True, exist_ok=True)
                    if await self._reject_if_over_byte_budget(
                        job_id,
                        provider,
                        lane_assembled.system_prompt,
                        lane_assembled.user_message,
                    ):
                        return
                    lane_policy = replace(
                        tool_policy,
                        max_tool_calls=lane_budgets[origin],
                        max_content_read_calls=content_lane_budgets.get(origin, 0),
                        max_search_calls=search_call_lane_budgets.get(origin, 0),
                        max_url_lookup_calls=url_lookup_lane_budgets.get(origin, 0),
                    )
                    lane_request = ExecutionRequest(
                        job_id=job_id,
                        work_dir=lane_dir,
                        system_prompt=lane_assembled.system_prompt,
                        user_message=lane_assembled.user_message,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        timeout_seconds=timeout,
                        tool_policy=lane_policy,
                    )
                    if job_id in self._cancel_requested:
                        lane_outcome = ExecutionOutcome(
                            cancelled=True,
                            terminal_reason="cancelled",
                            tool_policy=lane_policy,
                        )
                        lane_verdict = evaluate(
                            lane_outcome,
                            attachments,
                            fail_on_tool_use=fail_on_tool_use,
                        )
                        search_lane_outcomes.append((origin, lane_outcome))
                        lane_verdicts.append(lane_verdict)
                        search_lane_records.append(
                            {
                                "id": origin,
                                "spec_in_context": (
                                    origin == search_manifest.ORIGIN_SPEC_ASSISTED
                                ),
                                "prompt_sha256": lane_assembled.sha256,
                                "max_tool_calls": lane_budgets[origin],
                                "max_content_read_calls": (
                                    content_lane_budgets.get(origin, 0)
                                ),
                                "started_at": _utcnow().isoformat(),
                                "completed_at": _utcnow().isoformat(),
                                "status": JobStatus.CANCELLED.value,
                                "error_code": ErrorCode.CANCELLED.value,
                            }
                        )
                        break
                    label = (
                        "미대응 구성 조합→개별 검색 중"
                        if search_focus
                        else (
                            "청구항 단독 검색 중"
                            if origin == search_manifest.ORIGIN_CLAIM_ONLY
                            else "명세서 보조 확장 검색 중"
                        )
                    )
                    await self._emit(
                        job_id,
                        "stage",
                        {
                            "stage": "executing",
                            "search_origin": origin,
                            "message": label,
                        },
                    )
                    lane_started = _utcnow()
                    lane_outcome = await provider.execute(
                        lane_request, make_emit(origin)
                    )
                    lane_completed = _utcnow()
                    lane_verdict = evaluate(
                        lane_outcome, attachments, fail_on_tool_use=fail_on_tool_use
                    )
                    search_lane_outcomes.append((origin, lane_outcome))
                    lane_verdicts.append(lane_verdict)
                    search_lane_records.append(
                        {
                            "id": origin,
                            "spec_in_context": (
                                origin == search_manifest.ORIGIN_SPEC_ASSISTED
                            ),
                            "prompt_sha256": lane_assembled.sha256,
                            "max_tool_calls": lane_budgets[origin],
                            "max_content_read_calls": (
                                content_lane_budgets.get(origin, 0)
                            ),
                            "started_at": lane_started.isoformat(),
                            "completed_at": lane_completed.isoformat(),
                            "status": getattr(
                                lane_verdict.status, "value", str(lane_verdict.status)
                            ),
                            "error_code": (
                                getattr(
                                    lane_verdict.error_code,
                                    "value",
                                    str(lane_verdict.error_code),
                                )
                                if lane_verdict.error_code is not None
                                else None
                            ),
                        }
                    )
                    # 기본 검색이 실패했으면 명세서 검색으로 가려서 성공시키지
                    # 않는다. 기본 후보 집합이 없는 결과는 합집합이 아니다.
                    if lane_verdict.status != JobStatus.SUCCEEDED:
                        break

                # --- EPO 채널 ------------------------------------------
                #
                # 웹 레인이 끝난 **뒤에** 돈다. 순서를 이렇게 두면 EPO 가 어떤
                # 식으로 실패해도 웹 결과는 이미 확정되어 있다. 두 채널은
                # 논리적으로 격리되어야 하므로 EPO 는 웹의 후보도 검색어도
                # 보지 않고, 같은 청구항만 입력으로 받는다.
                epo_section, epo_runs = await self._run_epo_channel(
                    job_id=job_id,
                    values=values,
                    policy=channel_policy,
                    provider=provider,
                    model=model,
                    timeout=timeout,
                    work_dir=work_dir,
                    claim_text=claim_text,
                    spec_text=getattr(assembly, "spec_text", ""),
                    emit=self._emit,
                )

                outcome = _merge_search_outcomes(search_lane_outcomes, tool_policy)
                failed_lane = next(
                    (
                        lane_verdict
                        for lane_verdict in lane_verdicts
                        if lane_verdict.status != JobStatus.SUCCEEDED
                    ),
                    None,
                )
                verdict = failed_lane or evaluate(
                    outcome, attachments, fail_on_tool_use=fail_on_tool_use
                )
                # EPO 는 보조 채널이라 서버·인증·쿼터 실패가 웹 결과를
                # 무효화하지는 않는다. 하지만 사용자가 누른 **작업 취소**는
                # 별개다. EPO 레인이 취소를 확인한 뒤에도 웹의 성공 verdict 를
                # 그대로 두면, 사용자가 멈춘 작업이 마지막에 SUCCEEDED 로
                # 덮어써진다.
                #
                # 여기서 바로 return 하지 않는 이유는 이미 끝난 웹 결과와 EPO
                # 부분 실행 기록을 아래 manifest 에 보존하기 위해서다. 최종 상태만
                # CANCELLED 로 확정하고 정상적인 저장 경로를 끝까지 지난다.
                if job_id in self._cancel_requested:
                    verdict = Verdict(
                        JobStatus.CANCELLED,
                        ErrorCode.CANCELLED,
                        list(verdict.errors),
                    )
            else:
                if await self._reject_if_over_byte_budget(
                    job_id,
                    provider,
                    assembled.system_prompt,
                    assembled.user_message,
                ):
                    return
                request = ExecutionRequest(
                    job_id=job_id,
                    work_dir=work_dir,
                    system_prompt=assembled.system_prompt,
                    user_message=assembled.user_message,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    timeout_seconds=timeout,
                    tool_policy=tool_policy,
                )
                await self._emit(
                    job_id,
                    "stage",
                    {"stage": "executing", "message": "Provider 실행 중"},
                )
                outcome = await provider.execute(request, make_emit())
                verdict = evaluate(
                    outcome, attachments, fail_on_tool_use=fail_on_tool_use
                )

            await self._emit(
                job_id, "stage", {"stage": "verifying", "message": "결과 검증 중"}
            )

            # --- 문헌 매핑 -------------------------------------------------
            # 프롬프트가 선언했을 때만 기대한다. 읽지 못해도 실행은 실패시키지
            # 않는다. 매핑은 후속 기능이지 분석 요건이 아니다. 실패한 사유는
            # citation_mapping_error 에 남겨서, 후속 버튼이 왜 잠겼는지 화면이
            # 설명할 수 있게 한다.
            # --- 검색 감사 기록과 보고서 생성 ------------------------------
            # 모델이 쓴 산문을 사용자 보고서로 쓰지 않는다. 그 안에서 WebFetch
            # 요약이 원문 인용처럼 표시돼도 ARIA 는 알아볼 수 없기 때문이다.
            # 대신 검증된 구조화 필드에서 보고서를 직접 만든다. 그래서 감사
            # 블록은 이 작업의 선택 기능이 아니라 필수 출력이다.
            #
            # 모델의 원문 출력은 버리지 않는다. model_report.md 와 stdout.log 에
            # 그대로 남으므로 감사와 재검토가 가능하다.
            manifest: dict | None = None
            manifest_error: str | None = None
            model_narrative = ""
            verification_narrative = ""
            verification_outcome: ExecutionOutcome | None = None
            verification_section: dict = search_verification.section(
                attempted=False, reason="유사문헌 검색 작업이 아닙니다."
            )
            if job_kind is JobKind.SIMILARITY_SEARCH:
                model_narrative = outcome.result_text
                reported: dict | None = None
                notes: list[str] = []
                lane_observed_sections: list[dict] = []
                lane_reports: list[dict | None] = []
                manifest_errors: list[str] = []
                for origin, lane_outcome in search_lane_outcomes:
                    # 후보의 URL 은 같은 독립 실행에서 성공한 WebFetch 와만
                    # 대조한다. 다른 경로가 연 URL 로 증거 등급을 올리지 않는다.
                    lane_observed = search_manifest.observed(
                        lane_outcome.tool_calls,
                        lane_outcome.tool_uses,
                        search_origin=origin,
                    )
                    lane_observed_sections.append(lane_observed)
                    try:
                        lane_report, lane_notes = search_manifest.parse(
                            lane_outcome.result_text,
                            lane_observed,
                            spec_provided=(
                                origin == search_manifest.ORIGIN_SPEC_ASSISTED
                            ),
                            search_origin=origin,
                        )
                        lane_reports.append(lane_report)
                        label = (
                            "청구항 단독"
                            if origin == search_manifest.ORIGIN_CLAIM_ONLY
                            else "명세서 확장"
                        )
                        notes.extend(f"[{label}] {note}" for note in lane_notes)
                    except search_manifest.SearchLogError as exc:
                        lane_reports.append(None)
                        manifest_errors.append(f"{origin}: {exc}")

                missing_lanes = [
                    origin
                    for origin in search_assemblies
                    if origin not in {row["id"] for row in search_lane_records}
                ]
                if missing_lanes:
                    manifest_errors.append(
                        "실행되지 않은 검색 경로: " + ", ".join(missing_lanes)
                    )

                observed = search_manifest.merge_observed(*lane_observed_sections)
                if not manifest_errors and all(
                    lane_report is not None for lane_report in lane_reports
                ):
                    # 순서는 항상 claim_only, spec_assisted 이다. 병합 함수가
                    # 기본 검색의 분류를 유지하고 후보 집합만 확장한다.
                    reported = search_manifest.merge_reported(*lane_reports)
                else:
                    manifest_error = " / ".join(manifest_errors)
                    # 권한 거부로 빈 응답이 온 실행에서는 "감사 블록이 없다"가
                    # 별개의 원인이 아니라 그 거부의 후속 증상이다. 둘을 나란히
                    # 적으면 사용자는 고칠 곳을 두 군데로 읽는데, 실제로 고칠
                    # 곳은 허용 목록 하나뿐이다. 원래 파서 메시지는 버리지 않고
                    # 정규화 메모로 내린다 — 증상도 기록이지만 원인은 아니다.
                    if verdict.error_code is ErrorCode.SEARCH_PERMISSION_DENIED:
                        notes.append(
                            f"웹 채널 감사 블록 파싱 결과: {manifest_error}"
                        )
                        manifest_error = (
                            "웹페이지 읽기 권한이 거부되어 이 채널이 빈 응답으로 "
                            "끝났습니다. 감사 블록이 없는 것은 그 후속 증상입니다."
                        )

                # --- EPO 독립 검색 후보를 주 대응표에 연결 ------------------
                #
                # 여기서 합치는 것은 **후보 목록**뿐이다. epo.lanes 원본은 손대지
                # 않고 검색 감사용으로 남는다. 같은 공개번호를 두 채널이 찾았으면
                # 후보를 하나로 두고 발견 경로를 둘 다 남긴다.
                #
                # 이 시점에는 어떤 EPO 후보도 A/B 를 받지 않는다. 등급은 바로
                # 아래 공식 검증이 보존 응답에 구성 대응을 대조한 뒤에만 붙는다.
                #
                # 웹 보고를 읽지 못했어도(reported is None) 완료된 EPO 검색이
                # 있으면 빈 골격을 만들어 EPO 후보만으로 이어 간다. 두 채널은
                # 격리되어 있고, 한쪽의 출력 형식 오류가 다른 쪽이 실제로 받아
                # 보존한 공식 응답을 무효로 만들지 않는다. 웹의 실패 상태는
                # manifest_error 와 reported.web_report_error 양쪽에 남는다.
                web_report_missing = reported is None
                epo_notes: list[str] = []
                reported, epo_notes = search_manifest.merge_epo_discoveries(
                    reported,
                    epo_section,
                    web_report_error=manifest_error or "",
                )
                notes.extend(epo_notes)

                # --- ARIA 서지 검색 -----------------------------------------
                #
                # 웹 채널이 논문을 식별하지 못하는 실행에서 유일하게 제목과 DOI 가
                # 붙은 후보를 만드는 경로다. 모델이 실제로 쓴 검색어를 그대로
                # 가져가므로 새 검색 전략을 만들지 않는다.
                literature_section, literature_backend = (
                    await self._run_literature_channel(
                        job_id=job_id,
                        values=values,
                        policy=channel_policy,
                        observed=observed,
                        claim_text=claim_text,
                        plan=plan,
                    )
                )
                reported, literature_notes = (
                    search_manifest.merge_literature_discoveries(
                        reported,
                        literature_section,
                        web_report_error=manifest_error or "",
                    )
                )
                notes.extend(literature_notes)
                epo_only_salvage = web_report_missing and reported is not None

                # --- 논문 후보의 공식 서지 확보 -----------------------------
                #
                # EPO 조회와 **다른 예산**을 쓴다. 한 예산에 섞으면 특허 후보가
                # 많은 실행에서 논문 조회가 조용히 0건이 되고, 그 0건이 "논문이
                # 없다"로 읽힌다.
                literature_bundles: dict = {}
                literature_dropped: list = []
                literature_order: list = []
                literature_found: list = []
                if literature_backend is not None and reported is not None:
                    # 확보 목표와 시도 상한은 다른 축이다. 목표는 "대조 가능한
                    # 문헌을 몇 건 확보할 것인가"이고, shortlist 상한은 그것을
                    # 채우려고 **최대 몇 명까지 불러 볼 수 있는가**이다. 초록을
                    # 등록하지 않는 발행사가 흔해서 둘을 같은 수로 두면 목표가
                    # 실패 건수만큼 조용히 깎인다.
                    literature_goal = _positive(
                        values.get("literature_verification_targets"), 8
                    )
                    literature_shortlist = _positive(
                        values.get("literature_shortlist_limit"), 10
                    )
                    literature_found = search_verification.literature_targets(
                        reported,
                        limit=literature_goal,
                        shortlist_limit=literature_shortlist,
                        dropped=literature_dropped,
                        order=literature_order,
                    )
                    # 후보 하나에 초록·서지 두 번이 상한이다. shortlist 가 상한을
                    # 함께 묶으므로 이월이 예산을 늘리지 않는다.
                    literature_fetch_budget = 2 * len(literature_found)
                    if literature_found:
                        reserve = sum(
                            1
                            for target in literature_found
                            if target.selection_role
                            == search_verification.ROLE_BACKFILL
                        )
                        await self._emit(
                            job_id,
                            "stage",
                            {
                                "stage": "verifying",
                                "message": (
                                    f"논문 후보 {len(literature_found) - reserve}건의 "
                                    "공식 초록을 Crossref·Europe PMC 에서 확인하는 중"
                                    + (
                                        f" (조회 실패 시 예비 {reserve}건으로 이월)"
                                        if reserve
                                        else ""
                                    )
                                ),
                            },
                        )
                        try:
                            literature_bundles = await asyncio.to_thread(
                                search_verification.fetch_literature,
                                literature_found,
                                literature_backend,
                                max_fetches=literature_fetch_budget,
                                verification_targets=literature_goal,
                                is_cancelled=(
                                    lambda: job_id in self._cancel_requested
                                ),
                            )
                        except Exception as exc:
                            # 서지 확보 실패로 EPO 검증까지 잃지 않는다.
                            notes.append(
                                "논문 공식 서지 확보 단계 오류: "
                                f"{type(exc).__name__}: {exc}"
                            )
                    literature_summary = (
                        search_verification.literature_verification_summary(
                            literature_found,
                            literature_bundles,
                            verification_targets=literature_goal,
                            shortlist_limit=literature_shortlist,
                            max_fetches=literature_fetch_budget,
                            order=literature_order,
                        )
                    )
                    literature_section["verification"] = {
                        **literature_summary,
                        # 옛 기록을 읽는 코드를 위해 남긴다. 다만 이 값이 "고른
                        # 수"인지 "부른 수"인지 모호했던 것이 이번 문제의 절반이라,
                        # 새 코드는 selected/attempted 를 쓴다.
                        "target_count": literature_summary["selected"],
                        "excluded_candidates": literature_dropped,
                        "selection_order": literature_order,
                    }
                    literature_section["usage"] = literature_backend.usage()

                # 웹 게이트를 느슨하게 하지 않고, 후보 번호로 공식 문헌을 확보한
                # 뒤 도구 없는 별도 턴에서 분류한다. Provider 를 가리지 않는다 —
                # Codex 는 이 경로가 유일한 산출 통로이고, agy·Claude 는 페이지를
                # 열었다는 관측을 문장 대조로 한 단계 올리는 경로다. 앞선 EPO 검색
                # 레인이 사용한 상세 조회량을 빼서 작업 전체 상한을 공유한다.
                configured_fetches = _positive(
                    values.get("epo_max_detail_fetches"),
                    int(SETTING_DEFAULTS["epo_max_detail_fetches"]),
                )
                epo_fetches = int(
                    ((epo_section or {}).get("usage") or {}).get(
                        "detail_fetches", 0
                    )
                    or 0
                )
                remaining_fetches = max(0, configured_fetches - epo_fetches)
                (
                    reported,
                    verification_section,
                    verification_outcome,
                    verification_notes,
                ) = await self._run_official_verification(
                    job_id=job_id,
                    values=values,
                    provider=provider,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    timeout=timeout,
                    work_dir=work_dir,
                    claim_text=claim_text,
                    reported=reported,
                    fetch_budget=remaining_fetches,
                    epo_runs=epo_runs,
                    literature_bundles=literature_bundles,
                    literature_dropped=literature_dropped,
                )
                notes.extend(verification_notes)
                if verification_outcome is not None:
                    verification_narrative = verification_outcome.result_text
                    merged_usage = dict(outcome.usage or {})
                    merged_usage["official_classification"] = dict(
                        verification_outcome.usage or {}
                    )
                    outcome.usage = merged_usage
                if job_id in self._cancel_requested:
                    verdict = Verdict(
                        JobStatus.CANCELLED,
                        ErrorCode.CANCELLED,
                        list(verdict.errors),
                    )
                # --- Kiwee 채널 ---------------------------------------
                #
                # 접속·인증이 구현되지 않았다. 검색을 흉내 내지 않고, 네트워크도
                # 열지 않고, 기록에 사유만 남긴다. 이 줄이 없으면 "Kiwee 를 봤나"에
                # 기록이 답할 수 없다.
                kiwee_section = self._kiwee_channel_record(channel_policy, values)

                manifest = search_manifest.build(
                    claim_text=claim_text,
                    # 이 실행이 실제로 고른 검색 전략 프롬프트. 예약 상수를
                    # 적으면 어떤 전략으로 돌았는지가 기록에서 사라진다.
                    prompt_id=prompt_id or search_prompt.SEARCH_PROMPT_ID,
                    prompt_name=prompt_name,
                    prompt_kind=prompt_store.KIND_SEARCH,
                    prompt_template_mode=search_prompt_mode,
                    strategy_boundary_neutralized=strategy_boundary_neutralized,
                    prompt_sha256=search_prompt_sha,
                    runtime_context_sha256=search_runtime_context_sha,
                    reasoning_effort=reasoning_effort,
                    claim_boundary_neutralized=claim_boundary_neutralized,
                    spec_document=spec_document,
                    spec_boundary_neutralized=spec_boundary_neutralized,
                    search_focus=search_focus,
                    focus_boundary_neutralized=focus_boundary_neutralized,
                    started_at=started.isoformat(),
                    completed_at=_utcnow().isoformat(),
                    tool_calls=outcome.tool_calls,
                    tool_uses=outcome.tool_uses,
                    tool_policy_name=tool_policy.name,
                    allowed_tools=tool_policy.allowed_tools,
                    advertised_tools_enforced=(
                        tool_policy.enforce_advertised_allowlist
                    ),
                    observed_section=observed,
                    search_strategy=(
                        "combined_then_individual"
                        if search_focus
                        else (
                            "isolated_union"
                            if spec_document is not None
                            else search_manifest.ORIGIN_CLAIM_ONLY
                        )
                    ),
                    search_lanes=search_lane_records,
                    lanes=[
                        search_manifest.web_lane_record(record)
                        for record in search_lane_records
                    ]
                    + list((epo_section or {}).get("lanes") or []),
                    epo=epo_section,
                    literature=literature_section,
                    kiwee=kiwee_section,
                    verification=verification_section,
                    channel_policy=channel_policy.as_dict(),
                    plan=plan.as_dict() if plan is not None else None,
                    max_tool_calls_total=search_budget,
                    lane_budgets=lane_budgets,
                    max_content_reads_total=content_budget or None,
                    content_read_lane_budgets=content_lane_budgets,
                    reported=reported,
                    notes=notes,
                    error=manifest_error,
                )
                if reported is None:
                    # 검증되지 않은 산문을 대신 내보내지 않는다. 실행 자체는
                    # 끝났지만 사용자에게 줄 수 있는 보고서가 없다.
                    outcome.result_text = ""
                    if verdict.status == JobStatus.SUCCEEDED:
                        verdict = Verdict(
                            JobStatus.FAILED,
                            ErrorCode.INVALID_OUTPUT,
                            [
                                *verdict.errors,
                                f"검색 감사 블록을 읽지 못했습니다: {manifest_error} "
                                "검증되지 않은 모델 출력을 보고서로 내보내지 "
                                "않습니다. 모델 원문은 실행 기록에 있습니다.",
                            ],
                        )
                else:
                    outcome.result_text = search_report.render(manifest)
                    if epo_only_salvage:
                        # 보고서는 나왔지만 웹 채널은 실패했다. 성공으로만 적으면
                        # 그 실패가 기록에서 사라진다 — 이 실행의 후보에는 웹
                        # 검색이 찾은 문헌이 하나도 없다는 사실이 남아야 한다.
                        #
                        # 다만 권한 거부가 원인일 때는 그 사유를 원인으로 다시
                        # 적지 않는다. 이미 verdict.errors 의 첫 줄이 그것을
                        # 말하고 있고, 여기서 또 적으면 원인 하나가 오류 두 개로
                        # 보인다. 이 줄이 더할 것은 "그래서 어떻게 됐는가" 뿐이다.
                        if verdict.error_code is ErrorCode.SEARCH_PERMISSION_DENIED:
                            salvage = (
                                "그 결과 웹 채널은 후보를 하나도 내지 못했습니다. "
                                "완료된 EPO 독립 검색이 있어 EPO 후보만으로 "
                                "보고서를 만들었습니다."
                            )
                        else:
                            salvage = (
                                "웹 채널의 검색 감사 블록을 읽지 못했습니다: "
                                f"{manifest_error} 완료된 EPO 독립 검색이 있어 "
                                "EPO 후보만으로 보고서를 만들었습니다."
                            )
                        verdict = Verdict(
                            verdict.status,
                            verdict.error_code,
                            [*verdict.errors, salvage],
                        )

            # 2차 분류까지 취소 대상으로 남겨 둔다. 이보다 일찍 제거하면 사용자가
            # 검증 중 취소해도 실행 중인 Provider 프로세스에 cancel이 전달되지 않는다.
            self._providers.pop(job_id, None)

            component_result: dict | None = None
            component_error: str | None = None
            if analysis_manifest.CAPABILITY in capabilities:
                try:
                    component_result = analysis_manifest.parse(outcome.result_text)
                except analysis_manifest.ComponentAnalysisError as exc:
                    component_error = str(exc)
                outcome.result_text = analysis_manifest.strip_block(outcome.result_text)

            mapping: dict | None = None
            mapping_error: str | None = None
            if citation_mapping.CAPABILITY in capabilities:
                try:
                    mapping = citation_mapping.parse(
                        outcome.result_text, assembled.aliases
                    )
                except citation_mapping.MappingError as exc:
                    mapping_error = str(exc)
                # 사람이 받아 갈 보고서에는 프로토콜 블록을 남기지 않는다.
                # 원문은 stdout.log 에 그대로 있다.
                outcome.result_text = citation_mapping.strip_block(outcome.result_text)

            # --- 저장 -----------------------------------------------------
            completed = _utcnow()
            artifacts: list[tuple[str, Path]] = list(retrieval_artifacts)
            if retrieval_usage:
                # 로컬 검색 라운드도 사용량을 쓴다. 최종 호출분만 남기면 이
                # 실행이 실제로 얼마를 썼는지가 기록에서 빠진다.
                merged_usage = dict(outcome.usage or {})
                merged_usage["retrieval"] = retrieval_usage
                outcome.usage = merged_usage

            if outcome.result_text.strip():
                result_path = work_dir / "result.md"
                result_path.write_text(outcome.result_text, encoding="utf-8")
                artifacts.append(("result", result_path))

            if model_narrative.strip():
                # 모델이 실제로 쓴 출력. 사용자 보고서가 아니라 감사 자료다.
                # 이 안의 인용문은 원문 대조를 거치지 않았으므로 발췌로 쓰면
                # 안 된다.
                narrative_path = work_dir / "model_report.md"
                narrative_path.write_text(
                    "<!-- ARIA: 모델이 생성한 원문 출력입니다. 검증되지 않았으며 "
                    "여기 있는 인용문은 원문 직접 발췌가 아닙니다. -->\n\n"
                    + model_narrative,
                    encoding="utf-8",
                )
                artifacts.append(("model_report", narrative_path))

            if verification_narrative.strip():
                verification_dir = work_dir / "official-verification"
                verification_dir.mkdir(parents=True, exist_ok=True)
                verification_path = verification_dir / "model_report.md"
                verification_path.write_text(
                    "<!-- ARIA: 공식 문헌을 입력으로 한 2차 AI 분류 원문입니다. "
                    "ARIA가 대조해 채택한 행은 search_manifest.json에 따로 "
                    "표시됩니다. -->\n\n"
                    + verification_narrative,
                    encoding="utf-8",
                )
                artifacts.append(("verification_report", verification_path))

            verification_prompt = work_dir / "official-verification" / "final_prompt.txt"
            if verification_prompt.exists():
                artifacts.append(("verification_prompt", verification_prompt))

            if verification_outcome is not None and keep_raw:
                verification_dir = work_dir / "official-verification"
                verification_dir.mkdir(parents=True, exist_ok=True)
                if verification_outcome.raw_stdout:
                    verification_stdout = verification_dir / "stdout.log"
                    verification_stdout.write_text(
                        verification_outcome.raw_stdout, encoding="utf-8"
                    )
                    artifacts.append(("verification_stdout", verification_stdout))
                if verification_outcome.raw_stderr:
                    verification_stderr = verification_dir / "stderr.log"
                    verification_stderr.write_text(
                        verification_outcome.raw_stderr, encoding="utf-8"
                    )
                    artifacts.append(("verification_stderr", verification_stderr))

            if manifest is not None:
                manifest_path = work_dir / "search_manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                artifacts.append(("search_manifest", manifest_path))

            if component_result is not None:
                component_path = work_dir / "analysis_manifest.json"
                component_path.write_text(
                    json.dumps(component_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                artifacts.append(("analysis_manifest", component_path))

            if keep_raw:
                if outcome.raw_stdout:
                    stdout_path = work_dir / "stdout.log"
                    stdout_path.write_text(outcome.raw_stdout, encoding="utf-8")
                    artifacts.append(("stdout", stdout_path))
                if outcome.raw_stderr:
                    stderr_path = work_dir / "stderr.log"
                    stderr_path.write_text(outcome.raw_stderr, encoding="utf-8")
                    artifacts.append(("stderr", stderr_path))

            with session_scope() as session:
                job = session.get(ExecutionJob, job_id)
                if job is None:
                    return
                job.status = verdict.status
                job.error_code = verdict.error_code
                job.errors = verdict.errors
                job.permission_denials = outcome.permission_denials
                job.usage = outcome.usage
                job.result_text = outcome.result_text
                job.citation_mapping = mapping
                job.citation_mapping_error = mapping_error
                job.analysis_manifest = component_result
                job.analysis_manifest_error = component_error
                job.search_manifest = manifest
                job.search_manifest_error = manifest_error
                job.delivery_plan = delivery_plan
                job.delivery_manifest = delivery_manifest
                job.retrieval_manifest = retrieval_manifest
                job.retrieval_manifest_error = retrieval_error
                job.exit_code = outcome.exit_code
                job.terminal_reason = outcome.terminal_reason
                job.cli_path = outcome.cli_path
                job.cli_version = outcome.cli_version
                job.cli_args = outcome.cli_args
                job.completed_at = completed
                job.duration_ms = int((completed - started).total_seconds() * 1000)
                for artifact_id in _evidence_artifact_ids(manifest):
                    evidence_retention.reference(session, job_id, artifact_id)
                for kind, path in artifacts:
                    if kind == "stdout":
                        job.raw_stdout_path = str(path)
                    elif kind == "stderr":
                        job.raw_stderr_path = str(path)
                    session.add(
                        ResultArtifact(
                            job_id=job_id,
                            kind=kind,
                            path=str(path),
                            size_bytes=path.stat().st_size if path.exists() else 0,
                        )
                    )

            for error in verdict.errors:
                await self._emit(job_id, "error", {"message": error})

            await self._emit(
                job_id,
                "status",
                {"status": verdict.status, "error_code": verdict.error_code},
            )
            await self._emit(job_id, "done", {"status": verdict.status})
            await BUS.close(job_id)

    def _save_retrieval(
        self,
        job_id: str,
        delivery_plan: str,
        manifest: dict | None,
        error: str | None,
        artifacts: list[tuple[str, Path]],
    ) -> None:
        """로컬 검색 감사 기록을 저장한다. 실패한 실행에서도 남긴다.

        실패했다고 기록을 버리면 "무엇을 검색했고 어디서 막혔는지"가 사라진다.
        사용자가 다시 실행할지 문헌을 나눌지 정하려면 그 기록이 필요하다.
        """
        with contextlib.suppress(Exception), session_scope() as session:
            job = session.get(ExecutionJob, job_id)
            if job is None:
                return
            job.delivery_plan = delivery_plan
            job.retrieval_manifest = manifest
            job.retrieval_manifest_error = error
            for kind, path in artifacts:
                session.add(
                    ResultArtifact(
                        job_id=job_id,
                        kind=kind,
                        path=str(path),
                        size_bytes=path.stat().st_size if path.exists() else 0,
                    )
                )

    async def _cancelled(self, job_id: str) -> None:
        """취소로 끝난 다단계 실행을 종료 상태로 확정한다."""
        completed = _utcnow()
        with contextlib.suppress(Exception), session_scope() as session:
            job = session.get(ExecutionJob, job_id)
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.error_code = ErrorCode.CANCELLED
                job.completed_at = completed
                if job.started_at:
                    job.duration_ms = int(
                        (
                            completed - job.started_at.replace(tzinfo=timezone.utc)
                        ).total_seconds()
                        * 1000
                    )
        await self._emit(
            job_id,
            "status",
            {"status": JobStatus.CANCELLED, "error_code": ErrorCode.CANCELLED},
        )
        await self._emit(job_id, "done", {"status": JobStatus.CANCELLED})
        await BUS.close(job_id)
        self._providers.pop(job_id, None)

    async def _fail(self, job_id: str, error_code: str, message: str) -> None:
        completed = _utcnow()
        with contextlib.suppress(Exception), session_scope() as session:
            job = session.get(ExecutionJob, job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.error_code = error_code
                errors = list(job.errors or [])
                errors.append(message)
                job.errors = errors
                job.completed_at = completed
                if job.started_at:
                    job.duration_ms = int(
                        (completed - job.started_at.replace(tzinfo=timezone.utc)).total_seconds()
                        * 1000
                    )
        await self._emit(job_id, "error", {"message": message, "error_code": error_code})
        await self._emit(
            job_id, "status", {"status": JobStatus.FAILED, "error_code": error_code}
        )
        await self._emit(job_id, "done", {"status": JobStatus.FAILED})
        await BUS.close(job_id)
        self._providers.pop(job_id, None)


RUNNER = JobRunner()


def attachments_for(session: Session, job_id: str) -> list[IngestedFile]:
    rows = session.query(Attachment).filter(Attachment.job_id == job_id).all()
    return [row_to_ingested(r) for r in rows]
