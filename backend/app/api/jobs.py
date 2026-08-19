"""업로드, 작업 생성, 스트리밍, 취소, 결과 다운로드."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import settings_service
from ..config import PATHS
from ..db import get_db
from ..enums import JobStatus, derive_quality
from ..execution.bus import BUS
from ..execution.runner import RUNNER
from ..ingestion.security import UnsafeFilename
from ..ingestion.service import IngestionLimits, ingest_many
from ..models import Attachment, ExecutionJob, PromptTemplate
from ..providers.registry import is_allowed
from ..schemas import AttachmentAnalysis, JobCreate, JobOut, UploadResponse

router = APIRouter(prefix="/api", tags=["jobs"])


_UPLOAD_CHUNK = 1024 * 1024


async def _read_limited(
    upload: UploadFile, limits: IngestionLimits, consumed: int
) -> tuple[bytes, int]:
    """한도를 넘는 순간 읽기를 멈춘다.

    전부 읽고 나서 크기를 확인하면 설정한 한도가 메모리를 보호하지
    못한다. 실수로 대용량 파일을 고르면 한도와 무관하게 전부 메모리에
    올라온다.
    """
    buffer = bytearray()
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limits.max_file_size_bytes:
            raise UnsafeFilename(
                f"파일이 너무 큽니다: {upload.filename!r} "
                f"(제한 {limits.max_file_size_bytes:,} bytes)"
            )
        if consumed + len(buffer) > limits.max_total_upload_bytes:
            raise UnsafeFilename(
                "총 업로드 크기가 제한을 넘었습니다 "
                f"(제한 {limits.max_total_upload_bytes:,} bytes)"
            )
    return bytes(buffer), consumed + len(buffer)


def _limits(session: Session) -> IngestionLimits:
    values = settings_service.get_all(session)
    return IngestionLimits(
        max_file_size_bytes=int(values["max_file_size_bytes"]),
        max_total_upload_bytes=int(values["max_total_upload_bytes"]),
        max_files=int(values["max_files_per_job"]),
    )


@router.post("/uploads", response_model=UploadResponse)
async def upload_files(
    files: list[UploadFile] = File(default_factory=list),
    session: Session = Depends(get_db),
) -> UploadResponse:
    """파일을 실행별 격리 폴더에 저장하고 전달 가능 여부를 미리 알려준다.

    batch_id 가 그대로 작업 폴더 이름이 된다. 작업 생성 시 파일을 옮기지
    않으므로 경로가 바뀌지 않는다.
    """
    if not files:
        raise HTTPException(400, "업로드된 파일이 없습니다.")

    batch_id = str(uuid.uuid4())
    work_dir = PATHS.run_dir(batch_id)
    work_dir.mkdir(parents=True, exist_ok=True)

    limits = _limits(session)
    # 개수 초과면 한 바이트도 읽지 않고 거절한다.
    if len(files) > limits.max_files:
        raise HTTPException(
            400,
            f"파일 개수가 제한을 넘었습니다: {len(files)} (최대 {limits.max_files})",
        )

    payloads: list[tuple[str, bytes, bool]] = []
    consumed = 0
    for upload in files:
        try:
            data, consumed = await _read_limited(upload, limits, consumed)
        except UnsafeFilename as exc:
            raise HTTPException(400, str(exc)) from exc
        payloads.append((upload.filename or "", data, True))

    try:
        result = ingest_many(payloads, work_dir, limits)
    except UnsafeFilename as exc:
        raise HTTPException(400, str(exc)) from exc

    for item in result.files:
        session.add(
            Attachment(
                id=item.attachment_id,
                job_id=None,
                upload_batch=batch_id,
                original_filename=item.original_filename,
                internal_filename=item.internal_filename,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                required=True,
                stored_path=item.stored_path,
                normalized_text_path=item.normalized_text_path,
                page_count=item.page_count,
                char_count=item.char_count,
                extraction_method=item.extraction_method,
                ocr_used=item.ocr_used,
                delivery_mode=item.delivery_mode,
                read_ok=item.read_ok,
                error=item.error,
            )
        )
    session.commit()

    return UploadResponse(
        batch_id=batch_id,
        files=[
            AttachmentAnalysis(
                attachment_id=f.attachment_id,
                original_filename=f.original_filename,
                mime_type=f.mime_type,
                size_bytes=f.size_bytes,
                sha256=f.sha256,
                page_count=f.page_count,
                char_count=f.char_count,
                extraction_method=f.extraction_method,
                delivery_mode=f.delivery_mode,
                read_ok=f.read_ok,
                error=f.error,
            )
            for f in result.files
        ],
        rejected=result.rejected,
        total_chars=result.total_chars,
        max_inline_chars=int(settings_service.get(session, "max_inline_chars")),
    )


def _job_out(job: ExecutionJob) -> JobOut:
    return JobOut(
        id=job.id,
        status=job.status,
        result_quality=derive_quality(job.status, job.warnings or []),
        error_code=job.error_code,
        prompt_id=job.prompt_id,
        prompt_name=job.prompt_name,
        prompt_version=job.prompt_version,
        prompt_snapshot=job.prompt_snapshot,
        output_mode=job.output_mode,
        user_input=job.user_input,
        provider=job.provider,
        model=job.model,
        cli_path=job.cli_path,
        cli_version=job.cli_version,
        cli_args=job.cli_args or [],
        system_prompt_snapshot=job.system_prompt_snapshot or "",
        final_prompt_sha256=job.final_prompt_sha256,
        final_prompt_chars=job.final_prompt_chars or 0,
        terminal_reason=job.terminal_reason,
        exit_code=job.exit_code,
        warnings=job.warnings or [],
        errors=job.errors or [],
        permission_denials=job.permission_denials or [],
        usage=job.usage,
        result_text=job.result_text,
        attachments=[
            {
                "attachment_id": a.id,
                "original_filename": a.original_filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
                "sha256": a.sha256,
                "required": a.required,
                "page_count": a.page_count,
                "char_count": a.char_count,
                "extraction_method": a.extraction_method,
                "delivery_mode": a.delivery_mode,
                "read_ok": a.read_ok,
                "error": a.error,
            }
            for a in job.attachments
        ],
        preprocessing_versions=job.preprocessing_versions or {},
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration_ms=job.duration_ms,
    )


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(payload: JobCreate, session: Session = Depends(get_db)) -> JobOut:
    prompt = session.get(PromptTemplate, payload.prompt_id)
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    if not prompt.enabled:
        raise HTTPException(400, "비활성화된 프롬프트입니다.")

    enabled_experimental = settings_service.get(
        session, "enabled_experimental_providers"
    )
    if not is_allowed(payload.provider, enabled_experimental):
        raise HTTPException(
            403,
            f"{payload.provider} 는 실험적 Provider 입니다. 도구를 끌 수 없어 "
            "신뢰할 수 없는 문서 분석에 안전하지 않습니다. Settings 에서 위험을 "
            "확인하고 명시적으로 활성화한 뒤 사용하십시오.",
        )

    work_dir = (
        PATHS.run_dir(payload.batch_id) if payload.batch_id else None
    )

    job = ExecutionJob(
        prompt_id=prompt.id,
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_snapshot=prompt.body,
        output_mode=prompt.output_mode,
        user_input=payload.user_input or "",
        provider=payload.provider,
        model=payload.model or prompt.default_model,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.flush()

    if work_dir is None:
        work_dir = PATHS.run_dir(job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    job.work_dir = str(work_dir)

    if payload.batch_id:
        rows = (
            session.query(Attachment)
            .filter(Attachment.upload_batch == payload.batch_id)
            .all()
        )
        if not rows:
            raise HTTPException(400, "업로드 batch 를 찾을 수 없습니다.")
        for row in rows:
            if row.job_id is not None:
                raise HTTPException(400, "이미 다른 작업에 사용된 업로드입니다.")
            row.job_id = job.id
            row.required = bool(payload.required_map.get(row.id, True))

    session.commit()
    session.refresh(job)

    await RUNNER.submit(job.id)
    return _job_out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(get_db)) -> JobOut:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, session: Session = Depends(get_db)) -> dict:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        return {"cancelled": False, "reason": "이미 종료된 작업입니다."}
    cancelled = await RUNNER.cancel(job_id)
    return {"cancelled": cancelled}


@router.get("/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """SSE. 단방향이므로 WebSocket 대신 이걸 쓴다. 취소는 별도 POST."""
    queue, backlog = await BUS.subscribe(job_id, after=after)

    async def generator():
        try:
            for event in backlog:
                yield f"id: {event.seq}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except (asyncio.TimeoutError, TimeoutError):
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    yield 'data: {"type":"stream_end"}\n\n'
                    break
                yield f"id: {event.seq}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        finally:
            await BUS.unsubscribe(job_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}/result")
def get_result(
    job_id: str, fmt: str = "md", session: Session = Depends(get_db)
) -> PlainTextResponse:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")

    if fmt == "json":
        return PlainTextResponse(
            json.dumps(
                _job_out(job).model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="aria-{job_id[:8]}.json"'
            },
        )

    text = job.result_text or ""
    extension = "txt" if fmt == "txt" else "md"
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8"
        if extension == "md"
        else "text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="aria-{job_id[:8]}.{extension}"'
        },
    )


@router.get("/jobs/{job_id}/final-prompt")
def get_final_prompt(job_id: str, session: Session = Depends(get_db)) -> PlainTextResponse:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    if not job.final_prompt_path:
        raise HTTPException(404, "저장된 최종 프롬프트가 없습니다.")
    path = Path(job.final_prompt_path)
    if not path.exists():
        raise HTTPException(404, "최종 프롬프트 파일이 삭제되었습니다.")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")


@router.get("/jobs/{job_id}/raw")
def get_raw(
    job_id: str, which: str = "stdout", session: Session = Depends(get_db)
) -> PlainTextResponse:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    target = job.raw_stdout_path if which == "stdout" else job.raw_stderr_path
    if not target:
        return PlainTextResponse("", media_type="text/plain")
    path = Path(target)
    if not path.exists():
        return PlainTextResponse("", media_type="text/plain")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain")
