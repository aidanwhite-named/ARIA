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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import (
    analysis_manifest,
    citation_mapping,
    job_assembly,
    retrieval,
    search_manifest,
    search_prompt,
    search_report,
    settings_service,
)
from ..config import PATHS
from ..db import session_scope
from ..enums import DeliveryPlan, ErrorCode, JobKind, JobStatus, RetrievalMode
from ..evaluation.evaluator import Verdict, evaluate
from ..ingestion.service import IngestedFile, preprocessing_versions
from ..models import Attachment, ExecutionEvent, ExecutionJob, ResultArtifact
from ..prompt_assembly import InputTooLarge
from ..providers.base import NO_TOOLS, ExecutionOutcome, ExecutionRequest, ToolPolicy
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
            claim_text = job.claim_text
            followup_instruction = job.followup_instruction or ""
            # 생성 시점에 복사해 둔 값이다. 원본 실행을 여기서 다시 읽지 않는다.
            prior_claim_text = job.prior_claim_text or ""
            prior_report = job.prior_report or ""
            prior_mapping = job.prior_citation_mapping
            search_focus = job.search_focus
            capabilities = list(job.prompt_capabilities or [])
            prompt_version = job.prompt_version
            output_mode = job.output_mode
            work_dir = Path(job.work_dir) if job.work_dir else PATHS.run_dir(job_id)
            # 「분석에 포함」을 푼 자료는 여기서 빠진다. preflight 가 크기를
            # 잴 때 부르는 것과 같은 함수이므로, 화면이 안내한 숫자와 실제로
            # 나가는 숫자가 어긋나지 않는다.
            attachments = job_assembly.included_attachments(
                [row_to_ingested(a) for a in job.attachments]
            )
            values = settings_service.get_all(session)

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
                    followup_instruction=followup_instruction,
                    prior_claim_text=prior_claim_text,
                    prior_report=prior_report,
                    prior_citation_mapping=prior_mapping,
                    tool_policy_name=tool_policy.name,
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
                    claim_boundary_neutralized = assembly.claim_boundary_neutralized
                    spec_boundary_neutralized = assembly.spec_boundary_neutralized
                    focus_boundary_neutralized = assembly.focus_boundary_neutralized

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
            search_state = {"searches": 0, "fetches": 0, "reads": 0}

            def make_emit(search_origin: str | None = None):
                async def emit(event_type: str, payload: dict) -> None:
                    payload = dict(payload)
                    if search_origin:
                        payload["search_origin"] = search_origin
                    await self._emit(job_id, event_type, payload)
                    if job_kind is not JobKind.SIMILARITY_SEARCH:
                        return
                    if event_type != "tool_use":
                        return
                    name = str(payload.get("name") or "")
                    summary = payload.get("input") or {}
                    origin_label = (
                        "청구항 단독"
                        if search_origin == search_manifest.ORIGIN_CLAIM_ONLY
                        else "명세서 확장"
                    )
                    if name in search_manifest.SEARCH_TOOL_NAMES:
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
                    elif name in search_manifest.FETCH_TOOL_NAMES:
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

            search_lane_outcomes: list[tuple[str, ExecutionOutcome]] = []
            search_lane_records: list[dict] = []
            lane_verdicts: list[Verdict] = []

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
                    )
                    lane_request = ExecutionRequest(
                        job_id=job_id,
                        work_dir=lane_dir,
                        system_prompt=lane_assembled.system_prompt,
                        user_message=lane_assembled.user_message,
                        model=model,
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

            self._providers.pop(job_id, None)

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
                manifest = search_manifest.build(
                    claim_text=claim_text,
                    prompt_id=search_prompt.SEARCH_PROMPT_ID,
                    prompt_version=prompt_version,
                    prompt_sha256=search_prompt_sha,
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
