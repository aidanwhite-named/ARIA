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
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import (
    analysis_manifest,
    citation_mapping,
    job_assembly,
    retrieval,
    search_channels,
    search_dates,
    search_manifest,
    search_prompt,
    search_report,
    search_verification,
    settings_service,
)
from ..config import PATHS
from ..db import session_scope
from ..enums import DeliveryPlan, ErrorCode, JobKind, JobStatus, RetrievalMode
from ..evaluation.evaluator import Verdict, evaluate
from ..ingestion.service import IngestedFile, preprocessing_versions
from ..models import Attachment, ExecutionEvent, ExecutionJob, ResultArtifact
from .. import prompt_store
from ..prompt_assembly import InputTooLarge
from ..patent_search import retention as evidence_retention
from .. import analysis_completeness, report_symbols
from ..providers.base import (
    NO_TOOLS,
    ExecutionRequest,
    ToolPolicy,
)
from ..providers.registry import build_provider
from . import process as proc
from .bus import BUS

# UI 표시용 델타는 DB 에 남기지 않는다. 최종 결과 텍스트만 저장한다.
# UI 표시용 진행 신호는 DB 에 남기지 않는다. 최종 결과 텍스트만 저장한다.
_NON_PERSISTED = frozenset({"result_progress"})

# 조립은 job_assembly 가 한다. runner 와 preflight 가 같은 함수를 부르지 않으면
# 화면이 안내한 크기와 실제로 나가는 크기가 어긋난다. 기존 import 경로를 쓰는
# 코드가 있으므로 이름만 여기 남긴다.
_SEARCH_CONTEXT_BY_POLICY = job_assembly.SEARCH_CONTEXT_BY_POLICY
search_spec = job_assembly.search_spec


def _search_mcp_servers(work_dir: Path, cutoff: str, max_calls: int) -> dict:
    """Per-run MCP config.  No credentials are placed in CLI arguments."""
    backend_root = Path(__file__).resolve().parents[2]
    return {
        "aria-search": {
            "command": sys.executable,
            "args": ["-m", "app.search_mcp_server"],
            "env": {
                "PYTHONPATH": str(backend_root),
                "ARIA_SEARCH_WORK_DIR": str(work_dir.resolve()),
                "ARIA_DATA_DIR": str(PATHS.data_dir.resolve()),
                "ARIA_SEARCH_CUTOFF": cutoff or "",
                "ARIA_SEARCH_MAX_TOOL_CALLS": str(max(1, int(max_calls))),
            },
        }
    }


def _evidence_artifact_ids(value) -> set[str]:
    """매니페스트가 실제로 참조하는 내용주소 증거 ID를 모은다."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("artifact_id", "raw_artifact_id") and isinstance(item, str) and item:
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _progress_should_count(counted: set, call_id: str) -> bool:
    """Count each observed call once across start and completion events."""
    if not call_id:
        return True
    if call_id in counted:
        return False
    counted.add(call_id)
    return True


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
            # 선택적 검색 기준일. 빈 문자열이면 **날짜 조건이 없다**는 뜻이고,
            # 여기서 오늘 날짜로 채우지 않는다.
            search_cutoff = search_dates.normalize_cutoff(job.search_cutoff_date)
            search_depth = job.search_depth or "standard"
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
        if job_kind is JobKind.SIMILARITY_SEARCH:
            search_budget, timeout = search_channels.execution_limits(values, search_depth)

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
                    selected_policy,
                    max_tool_calls=max(1, search_budget),
                    mcp_tools=(),
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
                    search_cutoff=search_cutoff,
                    search_tool_status=search_channels.availability(values, provider_id),
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

            except job_assembly.SpecUnreadable as exc:
                await self._fail(
                    job_id,
                    ErrorCode.ATTACHMENT_ERROR,
                    "출원발명 문서의 본문을 읽지 못했습니다: "
                    f"{exc.filename}. 명세서를 반영하지 못한 채로 검색하지 "
                    "않습니다.",
                )
                return
            except (InputTooLarge, job_assembly.ModelInputTooLarge, job_assembly.TransportInputTooLarge) as exc:
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
                retrieval_budget = assembly.evidence_budget or retrieval_budget
                await self._emit(
                    job_id,
                    "stage",
                    {
                        "stage": "indexing",
                        "message": "인용발명 문헌을 페이지·문단 단위로 로컬 색인 중",
                    },
                )

                async def retrieval_emit(event_type: str, payload: dict) -> None:
                    # 로컬 검색 라운드의 모델 출력은 검색 계획과 action JSON 이며
                    # 보고서 본문이 아니다. 실측(job d39dc2cc): 최종 보고서
                    # 스트림 앞부분이 통째로 검색 라운드의 JSON 이었다. 라운드
                    # 진행은 retrieval_progress 가 따로 알리므로 이 델타는
                    # 밖으로 내보내지 않는다.
                    if event_type == "result_stream":
                        return
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
                    except (InputTooLarge, job_assembly.ModelInputTooLarge, job_assembly.TransportInputTooLarge) as exc:
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
            prompt_text = (
                f"===== SYSTEM PROMPT =====\n{assembled.system_prompt}\n\n"
                f"===== USER MESSAGE =====\n{assembled.user_message}"
            )
            prompt_path.write_text(prompt_text, encoding="utf-8")
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
            # 호출의 시작·완료 이벤트를 같은 ID로 중복 집계하지 않는다.
            search_state = {
                "searches": 0,
                "fetches": 0,
                "reads": 0,
                "counted": set(),
            }

            # 모델 출력을 실시간으로 화면에 붙이지 않는다.
            #
            # 붙이면 완성 전의 원문이 그대로 보고서 자리에 흐른다 — 기계 판독
            # 블록(구성별 분석·문헌 매핑)도 그 안에 있다. 실측(job d39dc2cc):
            # 최종 스트림 5,748자 중 1,521자가 두 감사 블록이었다. 화면에
            # 필요한 것은 "얼마나 받았는가"이고, 보고서는 블록을 걷어낸 최종
            # 결과 하나로 충분하다. 원문은 stdout.log 에 그대로 남는다.
            received = 0

            async def emit(event_type: str, payload: dict) -> None:
                nonlocal received
                payload = dict(payload)
                if event_type == "result_stream":
                    received += len(str(payload.get("delta") or ""))
                    await self._emit(job_id, "result_progress", {"chars": received})
                    return
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
                    str(payload.get("id") or ""),
                ):
                    return
                summary = payload.get("input") or {}
                origin_label = "에이전트"
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
                            "searches": search_state["searches"],
                            "fetches": search_state["fetches"],
                            "reads": search_state["reads"],
                            "message": (
                                f"{origin_label} 페이지 본문 확인 "
                                f"{search_state['reads']}회째"
                            ),
                        },
                    )


            if await self._reject_if_over_byte_budget(
                job_id, provider, assembled.system_prompt, assembled.user_message
            ):
                return
            mcp_servers = {}
            tool_availability = {}
            if job_kind is JobKind.SIMILARITY_SEARCH:
                tool_availability = search_channels.availability(values, provider_id)
                if provider_id in ("claude", "codex"):
                    mcp_servers = _search_mcp_servers(work_dir, search_cutoff, search_budget)
                available_names = search_channels.available_mcp_names(tool_availability) if mcp_servers else ()
                tool_policy = replace(
                    tool_policy, mcp_tools=tuple(available_names),
                    required_tools=(),
                    max_search_calls=0, max_url_lookup_calls=0,
                    max_content_read_calls=0,
                )
            request = ExecutionRequest(
                job_id=job_id, work_dir=work_dir,
                system_prompt=assembled.system_prompt,
                user_message=assembled.user_message, model=model,
                reasoning_effort=reasoning_effort, timeout_seconds=timeout,
                tool_policy=tool_policy, mcp_servers=mcp_servers,
            )
            await self._emit(
                job_id, "stage", {"stage": "executing", "message": "Provider 실행 중"}
            )
            outcome = await provider.execute(request, emit)
            verdict = evaluate(outcome, attachments, fail_on_tool_use=fail_on_tool_use)
            if job_id in self._cancel_requested:
                verdict = Verdict(JobStatus.CANCELLED, ErrorCode.CANCELLED, list(verdict.errors))
            await self._emit(
                job_id, "stage", {"stage": "verifying", "message": "식별자·근거 사실 검증 중"}
            )
            manifest = None
            manifest_error = None
            model_narrative = ""
            if job_kind is JobKind.SIMILARITY_SEARCH:
                model_narrative = outcome.result_text
                reported = None
                notes = []
                journal = search_manifest.read_tool_journal(work_dir)
                observed = search_manifest.observed(outcome.tool_calls, outcome.tool_uses)
                try:
                    if verdict.status != JobStatus.SUCCEEDED:
                        raise search_manifest.SearchLogError(
                            "실행이 정상 완료되지 않아 최종 후보로 확정하지 않았습니다."
                        )
                    if not search_manifest.has_retrieval_attempt(outcome.tool_calls, outcome.tool_uses, journal):
                        verdict = Verdict(JobStatus.FAILED, ErrorCode.SEARCH_NOT_PERFORMED, ["실제 검색 도구 호출이 없습니다."])
                        raise search_manifest.SearchLogError("실제 검색 도구 호출이 없습니다.")
                    reported, notes = search_manifest.parse(outcome.result_text, observed)
                    reported = search_verification.verify(reported, observed, journal)
                except search_manifest.SearchLogError as exc:
                    manifest_error = str(exc)
                date_filter = search_dates.filter_candidates(reported, search_cutoff)
                manifest = search_manifest.build(
                    claim_text=claim_text, provider=provider_id, model=model,
                    prompt_id=prompt_id, prompt_name=prompt_name,
                    prompt_sha256=search_prompt_sha,
                    runtime_context_sha256=search_runtime_context_sha,
                    spec_document=spec_document, search_focus=search_focus,
                    started_at=started.isoformat(), completed_at=_utcnow().isoformat(),
                    tool_calls=outcome.tool_calls, tool_uses=outcome.tool_uses,
                    observed_section=observed, tool_journal=journal,
                    tool_availability=tool_availability,
                    reported=reported, notes=notes, error=manifest_error,
                    date_filter=date_filter, max_tool_calls_total=search_budget,
                    timeout_seconds=timeout, usage=outcome.usage, search_depth=search_depth,
                    raw_output=model_narrative,
                    claim_boundary_neutralized=claim_boundary_neutralized,
                    spec_boundary_neutralized=spec_boundary_neutralized,
                    focus_boundary_neutralized=focus_boundary_neutralized,
                    template_mode=search_prompt_mode,
                    strategy_boundary_neutralized=strategy_boundary_neutralized,
                    tool_policy_name=tool_policy.name, allowed_tools=tool_policy.allowed_tools,
                    mcp_tools=tool_policy.mcp_tools,
                    advertised_tools_enforced=tool_policy.enforce_advertised_allowlist,
                )
                if reported is None:
                    outcome.result_text = ""
                    if verdict.status == JobStatus.SUCCEEDED:
                        verdict = Verdict(
                            JobStatus.FAILED, ErrorCode.INVALID_OUTPUT,
                            [*verdict.errors, manifest_error or "최종 JSON이 없습니다."],
                        )
                else:
                    outcome.result_text = search_report.render(manifest)
            self._providers.pop(job_id, None)

            # 두 블록의 출력 규칙은 ARIA 가 분석 프롬프트 뒤에 직접 붙인다
            # (analysis_protocol). 그러니 읽는 쪽도 프롬프트의 capabilities 선언에
            # 매달리지 않는다 — 사용자가 프롬프트를 자기 것으로 바꿔도 선언을 잊었다는
            # 이유로 유사도 표와 번호 유지가 조용히 꺼지면 안 된다. 검색 실행은
            # 규칙을 받지 않으므로 여기서도 제외한다.
            expects_blocks = job_kind is not JobKind.SIMILARITY_SEARCH

            component_result: dict | None = None
            component_error: str | None = None
            if expects_blocks:
                try:
                    component_result = analysis_manifest.parse(outcome.result_text)
                except analysis_manifest.ComponentAnalysisError as exc:
                    component_error = str(exc)
                outcome.result_text = analysis_manifest.strip_block(outcome.result_text)

            mapping: dict | None = None
            mapping_error: str | None = None
            if expects_blocks:
                try:
                    mapping = citation_mapping.parse(
                        outcome.result_text, assembled.aliases
                    )
                except citation_mapping.MappingError as exc:
                    mapping_error = str(exc)
                # 사람이 받아 갈 보고서에는 프로토콜 블록을 남기지 않는다.
                # 원문은 stdout.log 에 그대로 있다.
                outcome.result_text = citation_mapping.strip_block(outcome.result_text)

            if expects_blocks:
                # 등급 심볼. 프롬프트가 정의한 등급표에서만 읽으며 수치·등급명은
                # 건드리지 않는다. 본문 하나를 고치므로 화면·복사·다운로드가
                # 저절로 같아진다.
                outcome.result_text = report_symbols.apply(
                    outcome.result_text, master_prompt
                )
                # 프로세스 성공·블록 파싱 성공과 분석 완전성을 나눠서 확인한다.
                completeness = analysis_completeness.check(
                    retrieval_manifest=retrieval_manifest,
                    analysis_manifest=component_result,
                    analysis_error=component_error,
                    process_succeeded=verdict.status == JobStatus.SUCCEEDED,
                )
                notice = analysis_completeness.render(completeness)
                if notice and outcome.result_text.strip():
                    outcome.result_text = outcome.result_text.rstrip() + notice

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
