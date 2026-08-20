"""업로드, 작업 생성, 스트리밍, 취소, 결과 다운로드."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import citation_mapping, settings_service
from ..config import PATHS
from ..db import get_db
from ..enums import AttachmentRole, JobStatus, RelationType, derive_quality
from ..execution.bus import BUS
from ..execution.runner import RUNNER, row_to_ingested
from ..ingestion.security import UnsafeFilename
from ..ingestion.service import (
    AttachmentCloneError,
    IngestionLimits,
    clone_attachment,
    ingest_many,
)
from ..models import Attachment, ExecutionJob
from ..prompt_store import PROMPT_STORE, InvalidPromptFile, PromptNotFound
from ..providers.registry import build_provider, cached, is_allowed, probe_one
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
    roles: str = Form(default=""),
    session: Session = Depends(get_db),
) -> UploadResponse:
    """파일을 실행별 격리 폴더에 저장하고 전달 가능 여부를 미리 알려준다.

    batch_id 가 그대로 작업 폴더 이름이 된다. 작업 생성 시 파일을 옮기지
    않으므로 경로가 바뀌지 않는다.
    """
    if not files:
        raise HTTPException(400, "업로드된 파일이 없습니다.")

    if roles:
        try:
            parsed_roles = json.loads(roles)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "첨부 역할 정보가 올바른 JSON 이 아닙니다.") from exc
        if not isinstance(parsed_roles, list) or len(parsed_roles) != len(files):
            raise HTTPException(400, "첨부 역할 수와 파일 수가 일치하지 않습니다.")
    else:
        parsed_roles = [AttachmentRole.SUPPLEMENTAL] * len(files)

    allowed_roles = {role.value for role in AttachmentRole}
    if any(
        not isinstance(role, str) or role not in allowed_roles for role in parsed_roles
    ):
        raise HTTPException(400, "알 수 없는 첨부 역할이 포함되어 있습니다.")

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

    payloads: list[tuple[str, bytes, bool, str]] = []
    consumed = 0
    for upload, role in zip(files, parsed_roles, strict=True):
        try:
            data, consumed = await _read_limited(upload, limits, consumed)
        except UnsafeFilename as exc:
            raise HTTPException(400, str(exc)) from exc
        payloads.append((upload.filename or "", data, True, role))

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
                role=item.role,
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
                role=f.role,
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
        claim_text=job.claim_text or "",
        source_job_id=job.source_job_id,
        source_job_label=job.source_job_label or "",
        relation_type=job.relation_type,
        followup_instruction=job.followup_instruction or "",
        prior_claim_text=job.prior_claim_text or "",
        prior_report=job.prior_report or "",
        citation_mapping=job.citation_mapping,
        prior_citation_mapping=job.prior_citation_mapping,
        prompt_capabilities=list(job.prompt_capabilities or []),
        citation_mapping_error=job.citation_mapping_error,
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
                "role": a.role,
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


def source_label(job: ExecutionJob) -> str:
    """원본 실행이 삭제된 뒤에도 화면에 남길 표시용 라벨."""
    stamp = job.created_at.strftime("%Y-%m-%d %H:%M") if job.created_at else ""
    version = f" v{job.prompt_version}" if job.prompt_version else ""
    return f"{stamp} · {job.prompt_name}{version}".strip().strip("·").strip()


def _clone_parent_attachments(
    session: Session,
    source_job: ExecutionJob,
    job: ExecutionJob,
    work_dir: Path,
) -> list:
    """원본 실행의 첨부를 자식 작업 폴더로 복제하고 새 행을 만든다.

    도중에 실패하면 이미 쓴 복제본을 지운다. DB 는 예외로 롤백되지만 파일은
    남으므로, 부분 복제 상태의 폴더를 만들지 않는다.

    복제본 목록을 돌려준다. 문헌 매핑을 새 attachment_id 에 다시 묶어야 한다.
    """
    written: list[Path] = []
    cloned: list = []
    try:
        for row in source_job.attachments:
            new_id = str(uuid.uuid4())
            cloned_file = clone_attachment(row_to_ingested(row), work_dir, new_id)
            written.append(Path(cloned_file.stored_path))
            if cloned_file.normalized_text_path:
                written.append(Path(cloned_file.normalized_text_path))
            session.add(
                Attachment(
                    id=cloned_file.attachment_id,
                    job_id=job.id,
                    upload_batch=None,
                    original_filename=cloned_file.original_filename,
                    internal_filename=cloned_file.internal_filename,
                    mime_type=cloned_file.mime_type,
                    size_bytes=cloned_file.size_bytes,
                    sha256=cloned_file.sha256,
                    required=cloned_file.required,
                    role=cloned_file.role,
                    stored_path=cloned_file.stored_path,
                    normalized_text_path=cloned_file.normalized_text_path,
                    page_count=cloned_file.page_count,
                    char_count=cloned_file.char_count,
                    extraction_method=cloned_file.extraction_method,
                    ocr_used=cloned_file.ocr_used,
                    delivery_mode=cloned_file.delivery_mode,
                    read_ok=cloned_file.read_ok,
                    error=cloned_file.error,
                )
            )
            cloned.append(cloned_file)
    except AttachmentCloneError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return cloned


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(payload: JobCreate, session: Session = Depends(get_db)) -> JobOut:
    values = settings_service.get_all(session)
    configured_prompt_id = str(values.get("default_prompt_id") or "")
    prompt_id = payload.prompt_id or configured_prompt_id
    prompt = None
    if prompt_id:
        try:
            prompt = PROMPT_STORE.get(prompt_id)
        except PromptNotFound:
            # An explicit API override must be valid. A stale configured default
            # (for example an old database UUID) falls back to the prompt folder.
            if payload.prompt_id:
                raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
        except InvalidPromptFile as exc:
            raise HTTPException(422, str(exc)) from exc
    if prompt is None:
        try:
            prompt = next((item for item in PROMPT_STORE.list() if item.enabled), None)
        except InvalidPromptFile as exc:
            raise HTTPException(422, str(exc)) from exc
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    if not prompt.enabled:
        raise HTTPException(400, "비활성화된 프롬프트입니다.")
    provider_id = payload.provider or str(values.get("default_provider") or "")
    if not provider_id:
        # 자동 선택하지 않는다. 실험적 Provider 가 기본값으로 끼어들면
        # 사용자가 위험을 확인하지 않은 채 실행하게 된다.
        raise HTTPException(
            400,
            "사용할 Provider 가 지정되지 않았습니다. Settings 에서 기본 "
            "Provider 를 선택한 뒤 다시 실행하십시오.",
        )
    provider_paths = values.get("provider_paths") or {}
    if build_provider(provider_id, provider_paths) is None:
        raise HTTPException(400, f"알 수 없거나 제거된 Provider 입니다: {provider_id}")
    if not is_allowed(provider_id, values.get("enabled_experimental_providers")):
        raise HTTPException(
            403,
            f"{provider_id} 는 실험적 Provider 입니다. 도구를 끌 수 없어 "
            "신뢰할 수 없는 문서 분석에 안전하지 않습니다. Settings 에서 위험을 "
            "확인하고 명시적으로 활성화한 뒤 사용하십시오.",
        )
    default_models = values.get("default_models") or {}
    selected_model = payload.model or default_models.get(provider_id) or None
    if selected_model and provider_id in {"agy", "claude", "codex"}:
        provider_info = cached(provider_id) or await probe_one(provider_id, provider_paths)
        available_models = (
            provider_info.capabilities.get("models", []) if provider_info else []
        )
        if available_models and selected_model not in available_models:
            raise HTTPException(
                400,
                f"{provider_id} 에서 사용할 수 없는 모델입니다: {selected_model}",
            )

    # --- 후속 분석 계보 -------------------------------------------------
    # source_job_id 와 relation_type 은 항상 함께 온다. 하나만 오면 어느 쪽을
    # 의도한 것인지 알 수 없으므로 추측하지 않고 거절한다.
    source_job: ExecutionJob | None = None
    relation = payload.relation_type
    if bool(payload.source_job_id) != bool(relation):
        raise HTTPException(
            400, "source_job_id 와 relation_type 은 함께 지정해야 합니다."
        )
    if payload.source_job_id:
        source_job = session.get(ExecutionJob, payload.source_job_id)
        if source_job is None:
            raise HTTPException(404, "이어받을 원본 실행을 찾을 수 없습니다.")
        if relation == RelationType.CONTINUED and not (
            source_job.result_text or ""
        ).strip():
            raise HTTPException(
                400,
                "원본 실행에 이어받을 보고서가 없습니다. 자료만 재사용하려면 "
                "새로 분석을 선택하십시오.",
            )
        if relation == RelationType.MAPPED and not (
            source_job.citation_mapping or {}
        ).get("items"):
            # 조용히 보고서 전체 전달로 되돌아가지 않는다. 그렇게 하면 사용자가
            # 모르는 사이에 이전 유사도와 발췌문이 다시 모델 앞에 놓인다.
            detail = (
                source_job.citation_mapping_error
                or "원본 실행에 검증된 문헌 매핑이 없습니다."
            )
            raise HTTPException(400, f"번호를 이어받을 수 없습니다: {detail}")

    work_dir = (
        PATHS.run_dir(payload.batch_id) if payload.batch_id else None
    )

    relation_kind = RelationType(relation) if relation else None
    carries_claims = relation_kind is not None and relation_kind.inherits_mapping
    job = ExecutionJob(
        prompt_id=prompt.id,
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_snapshot=prompt.body,
        output_mode=prompt.output_mode,
        claim_text=payload.claim_text or "",
        source_job_id=source_job.id if source_job else None,
        source_job_label=source_label(source_job) if source_job else "",
        relation_type=relation,
        followup_instruction=payload.followup_instruction or "",
        # 원본이 나중에 지워져도 이 실행의 입력은 바뀌면 안 된다. prompt_snapshot
        # 과 같은 이유로 생성 시점에 복사해 둔다.
        prior_claim_text=(source_job.claim_text or "") if carries_claims else "",
        prior_report=(
            (source_job.result_text or "")
            if relation_kind is not None and relation_kind.inherits_report
            else ""
        ),
        prompt_capabilities=list(prompt.capabilities),
        provider=provider_id,
        model=selected_model,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.flush()

    if work_dir is None:
        work_dir = PATHS.run_dir(job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    job.work_dir = str(work_dir)

    # 업로드 batch 를 먼저 처리한다. 여기서 거절당할 수 있는데, 복제를 먼저 하면
    # 롤백된 작업의 파일 사본만 폴더에 남는다.
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

    if source_job is not None:
        # 두 실행이 같은 폴더를 쓰면 복제본이 원본의 증거 파일을 덮어쓴다.
        # 지금은 위의 batch 재사용 검사에 먼저 걸려 도달하지 않지만, 그 규칙이
        # 완화되면 곧바로 도달한다. 파일을 잃는 쪽이라 방어를 남겨 둔다.
        if source_job.work_dir and Path(source_job.work_dir) == work_dir:
            raise HTTPException(
                400, "원본 실행과 같은 작업 폴더를 쓸 수 없습니다. 자료는 복제됩니다."
            )
        try:
            cloned = _clone_parent_attachments(session, source_job, job, work_dir)
        except AttachmentCloneError as exc:
            raise HTTPException(409, f"원본 자료를 복제하지 못했습니다: {exc}") from exc

        # 복제하면 attachment_id 가 바뀐다. 고정 매핑을 이 실행의 자료에 sha256
        # 으로 다시 묶는다. 한 항목이라도 짝이 없으면 번호가 어긋나므로 실패시킨다.
        if relation_kind.inherits_mapping and source_job.citation_mapping:
            try:
                job.prior_citation_mapping = citation_mapping.rebind(
                    source_job.citation_mapping, cloned
                )
            except citation_mapping.MappingError as exc:
                raise HTTPException(
                    409, f"문헌 매핑을 이 실행의 자료에 묶지 못했습니다: {exc}"
                ) from exc

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
