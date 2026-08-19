"""작업 실행 오케스트레이션.

큐 → Provider 세마포어 → 프롬프트 조립 → 실행 → 판정 → 저장.

Provider 당 동시 실행은 기본 1이다. 로컬 CLI 는 계정 단위 사용량 제한을
공유하므로 병렬로 올려봐야 대기만 늘어나는 경우가 많다.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .. import settings_service
from ..config import PATHS
from ..db import session_scope
from ..enums import ErrorCode, JobStatus
from ..evaluation.evaluator import evaluate
from ..ingestion.service import IngestedFile, preprocessing_versions
from ..models import Attachment, ExecutionEvent, ExecutionJob, ResultArtifact
from ..prompt_assembly import InputTooLarge, assemble
from ..providers.base import ExecutionRequest
from ..providers.registry import build_provider
from . import process as proc
from .bus import BUS

# UI 표시용 델타는 DB 에 남기지 않는다. 최종 결과 텍스트만 저장한다.
_NON_PERSISTED = frozenset({"result_stream"})


def row_to_ingested(row: Attachment) -> IngestedFile:
    return IngestedFile(
        attachment_id=row.id,
        original_filename=row.original_filename,
        internal_filename=row.internal_filename,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        required=row.required,
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRunner:
    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._providers: dict[str, object] = {}
        self._seq: dict[str, int] = {}

    def _semaphore(self, provider_id: str, limit: int) -> asyncio.Semaphore:
        existing = self._semaphores.get(provider_id)
        if existing is None or getattr(existing, "_aria_limit", None) != limit:
            semaphore = asyncio.Semaphore(limit)
            semaphore._aria_limit = limit  # type: ignore[attr-defined]
            self._semaphores[provider_id] = semaphore
            return semaphore
        return existing

    async def submit(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))

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
            master_prompt = job.prompt_snapshot
            claim_text = job.claim_text
            output_mode = job.output_mode
            work_dir = Path(job.work_dir) if job.work_dir else PATHS.run_dir(job_id)
            attachments = [row_to_ingested(a) for a in job.attachments]
            values = settings_service.get_all(session)

        limit = int(values.get("max_concurrency_per_provider", 1))
        timeout = int(values.get("default_timeout_seconds", 900))
        max_chars = int(values.get("max_inline_chars", 300_000))
        runtime_context = str(values.get("runtime_context", ""))
        runtime_enabled = bool(values.get("runtime_context_enabled", True))
        keep_raw = bool(values.get("keep_raw_output", True))
        fail_on_tool_use = bool(values.get("fail_on_tool_use", True))
        overrides = values.get("provider_paths") or {}

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
            try:
                assembled = assemble(
                    master_prompt=master_prompt,
                    attachments=attachments,
                    runtime_context=runtime_context,
                    runtime_context_enabled=runtime_enabled,
                    max_chars=max_chars,
                    claim_text=claim_text,
                )
            except InputTooLarge as exc:
                await self._fail(job_id, ErrorCode.INPUT_TOO_LARGE, str(exc))
                return

            prompt_path = work_dir / "final_prompt.txt"
            prompt_path.write_text(
                f"===== SYSTEM PROMPT =====\n{assembled.system_prompt}\n\n"
                f"===== USER MESSAGE =====\n{assembled.user_message}",
                encoding="utf-8",
            )

            with session_scope() as session:
                job = session.get(ExecutionJob, job_id)
                if job is not None:
                    job.system_prompt_snapshot = assembled.system_prompt
                    job.final_prompt_path = str(prompt_path)
                    job.final_prompt_sha256 = assembled.sha256
                    job.final_prompt_chars = assembled.total_chars
                    job.attachment_manifest = assembled.manifest

            await self._emit(
                job_id,
                "prompt_ready",
                {
                    "chars": assembled.total_chars,
                    "sha256": assembled.sha256,
                    "attachments": len(attachments),
                },
            )

            # --- 실행 -----------------------------------------------------
            request = ExecutionRequest(
                job_id=job_id,
                work_dir=work_dir,
                system_prompt=assembled.system_prompt,
                user_message=assembled.user_message,
                model=model,
                timeout_seconds=timeout,
            )

            async def emit(event_type: str, payload: dict) -> None:
                await self._emit(job_id, event_type, payload)

            await self._emit(
                job_id, "stage", {"stage": "executing", "message": "Provider 실행 중"}
            )
            outcome = await provider.execute(request, emit)
            self._providers.pop(job_id, None)

            await self._emit(
                job_id, "stage", {"stage": "verifying", "message": "결과 검증 중"}
            )
            verdict = evaluate(
                outcome, attachments, output_mode, fail_on_tool_use=fail_on_tool_use
            )

            # --- 저장 -----------------------------------------------------
            completed = _utcnow()
            artifacts: list[tuple[str, Path]] = []

            if outcome.result_text.strip():
                result_path = work_dir / "result.md"
                result_path.write_text(outcome.result_text, encoding="utf-8")
                artifacts.append(("result", result_path))

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
                job.warnings = verdict.warnings
                job.errors = verdict.errors
                job.permission_denials = outcome.permission_denials
                job.usage = outcome.usage
                job.result_text = outcome.result_text
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

            for warning in verdict.warnings:
                await self._emit(job_id, "warning", {"message": warning})
            for error in verdict.errors:
                await self._emit(job_id, "error", {"message": error})

            await self._emit(
                job_id,
                "status",
                {"status": verdict.status, "error_code": verdict.error_code},
            )
            await self._emit(job_id, "done", {"status": verdict.status})
            await BUS.close(job_id)

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
