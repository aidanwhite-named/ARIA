"""실행 이력 API."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import PATHS
from ..db import get_db
from ..enums import derive_quality
from ..models import ExecutionJob
from ..schemas import HistoryItem, JobOut
from .jobs import _job_out

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("", response_model=list[HistoryItem])
def list_history(
    session: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    provider: str = Query(default=""),
    status: str = Query(default=""),
) -> list[HistoryItem]:
    query = session.query(ExecutionJob)
    if provider:
        query = query.filter(ExecutionJob.provider == provider)
    if status:
        query = query.filter(ExecutionJob.status == status)
    rows = (
        query.order_by(ExecutionJob.created_at.desc()).offset(offset).limit(limit).all()
    )
    return [
        HistoryItem(
            id=r.id,
            status=r.status,
            result_quality=derive_quality(r.status, r.warnings or []),
            error_code=r.error_code,
            prompt_name=r.prompt_name,
            prompt_version=r.prompt_version,
            provider=r.provider,
            model=r.model,
            created_at=r.created_at,
            duration_ms=r.duration_ms,
            attachment_count=len(r.attachments),
            warning_count=len(r.warnings or []),
        )
        for r in rows
    ]


@router.get("/{job_id}", response_model=JobOut)
def get_history_item(job_id: str, session: Session = Depends(get_db)) -> JobOut:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")
    return _job_out(job)


@router.delete("/{job_id}", status_code=204, response_class=Response, response_model=None)
def delete_history_item(job_id: str, session: Session = Depends(get_db)) -> None:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")

    work_dir = job.work_dir
    session.delete(job)
    session.commit()

    # 작업 폴더는 runs 디렉터리 안에 있을 때만 지운다.
    if work_dir:
        path = Path(work_dir).resolve()
        runs_root = PATHS.runs_dir.resolve()
        if path.is_dir() and runs_root in path.parents:
            shutil.rmtree(path, ignore_errors=True)
