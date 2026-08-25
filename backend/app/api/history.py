"""실행 이력 API."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import PATHS
from ..db import get_db
from ..enums import JobKind
from ..models import Attachment, ExecutionJob
from ..patent_search import retention
from ..patent_search.artifacts import ArtifactStore
from ..schemas import HistoryItem, JobOut
from .jobs import _job_out

router = APIRouter(prefix="/api/history", tags=["history"])


def _clear_directory_contents(root: Path) -> None:
    """ARIA가 소유한 저장 폴더는 남기고 그 안의 기록만 모두 지운다."""
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child)


def _child_map(session: Session) -> dict[str, list[str]]:
    """source_job_id → 자식 id 목록.

    이력 테이블 전체를 한 번만 읽는다. 로컬 도구라 행 수가 작고, 목록의 각
    항목마다 계보를 다시 질의하면 N+1 이 된다.
    """
    children: dict[str, list[str]] = {}
    for job_id, source_id in session.query(
        ExecutionJob.id, ExecutionJob.source_job_id
    ).all():
        if source_id:
            children.setdefault(source_id, []).append(job_id)
    return children


def _descendants(children: dict[str, list[str]], root_id: str) -> list[str]:
    """root 에서 이어진 후속 실행 id 를 너비 우선으로 모은다 (root 제외).

    seen 으로 순환을 막는다. 정상 경로에서는 생길 수 없지만, DB 를 직접 손대
    거나 손상된 파일을 열었을 때 무한 루프로 서버가 멈추는 것보다 낫다.
    """
    found: list[str] = []
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        nxt: list[str] = []
        for parent in frontier:
            for child in children.get(parent, ()):
                if child in seen:
                    continue
                seen.add(child)
                found.append(child)
                nxt.append(child)
        frontier = nxt
    return found


def _history_item(row: ExecutionJob, descendant_count: int = 0) -> HistoryItem:
    return HistoryItem(
        id=row.id,
        status=row.status,
        error_code=row.error_code,
        job_kind=row.job_kind or JobKind.PATENT_ANALYSIS,
        prompt_name=row.prompt_name,
        prompt_version=row.prompt_version,
        provider=row.provider,
        model=row.model,
        created_at=row.created_at,
        duration_ms=row.duration_ms,
        attachment_count=len(row.attachments),
        source_job_id=row.source_job_id,
        source_job_label=row.source_job_label or "",
        relation_type=row.relation_type,
        has_citation_mapping=bool((row.citation_mapping or {}).get("items")),
        descendant_count=descendant_count,
    )


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
    children = _child_map(session)
    return [_history_item(r, len(_descendants(children, r.id))) for r in rows]


@router.delete("")
def delete_all_history(session: Session = Depends(get_db)) -> dict:
    """모든 실행 이력과 ARIA 실행 저장소의 파일을 함께 지운다.

    DB 행과 연결되지 않은 미사용 업로드 폴더나 이전 버전의 산출물도 남기지
    않는다. 데이터 폴더 자체와 설정·프롬프트·애플리케이션 로그는 보존한다.
    """
    jobs = session.query(ExecutionJob).all()
    deleted = _purge(session, jobs) if jobs else 0

    # 아직 작업에 귀속되지 않은 업로드 메타데이터도 실행 폴더와 함께 정리한다.
    session.query(Attachment).filter(Attachment.job_id.is_(None)).delete(
        synchronize_session=False
    )
    session.commit()

    _clear_directory_contents(PATHS.runs_dir.resolve())
    _clear_directory_contents(PATHS.artifacts_dir.resolve())
    # 증거도 남기지 않는다. 여기까지 왔으면 참조하는 작업이 하나도 없다.
    _clear_directory_contents(PATHS.evidence_dir.resolve())
    return {"deleted": deleted}


@router.get("/{job_id}", response_model=JobOut)
def get_history_item(job_id: str, session: Session = Depends(get_db)) -> JobOut:
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")
    return _job_out(job)


@router.get("/{job_id}/thread", response_model=list[HistoryItem])
def get_thread(job_id: str, session: Session = Depends(get_db)) -> list[HistoryItem]:
    """이 실행과 그로부터 이어진 후속 실행 전부. 일괄 삭제 전 확인용이다.

    위로 거슬러 올라가지 않는다. 자식을 지우면서 부모까지 지우는 동작은
    사용자가 기대하지 않는다.
    """
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")

    children = _child_map(session)
    ordered_ids = _descendants(children, job_id)
    rows = {
        row.id: row
        for row in session.query(ExecutionJob)
        .filter(ExecutionJob.id.in_(ordered_ids))
        .all()
    } if ordered_ids else {}

    items = [_history_item(job, len(ordered_ids))]
    for child_id in ordered_ids:
        row = rows.get(child_id)
        if row is not None:
            items.append(_history_item(row, len(_descendants(children, child_id))))
    return items


def _purge(session: Session, jobs: list[ExecutionJob]) -> int:
    """작업 행과 작업 폴더를 지운다.

    폴더는 runs 디렉터리 안에 있고, 다른 실행이 같은 폴더를 work_dir 로
    쓰고 있지 않을 때만 지운다. 후속 실행은 자료를 자기 폴더로 복제해 두므로
    보통은 겹치지 않지만, batch_id 가 곧 폴더 이름이 되는 기존 경로가 남아
    있어 확인 없이 지우면 남의 자료를 지울 수 있다.
    """
    work_dirs = [job.work_dir for job in jobs if job.work_dir]
    for job in jobs:
        session.delete(job)
    session.commit()

    runs_root = PATHS.runs_dir.resolve()
    for raw in work_dirs:
        path = Path(raw).resolve()
        if not path.is_dir() or runs_root not in path.parents:
            continue
        still_used = (
            session.query(ExecutionJob.id)
            .filter(ExecutionJob.work_dir == raw)
            .first()
        )
        if still_used is not None:
            continue
        shutil.rmtree(path, ignore_errors=True)

    # 작업이 사라지면 참조도 CASCADE 로 사라진다. 이제 아무도 참조하지 않는
    # 증거 아티팩트를 지운다. 증거는 작업에 딸린 것이지 영구 보관물이 아니다.
    retention.collect_unreferenced(
        session, ArtifactStore(PATHS.evidence_dir.resolve())
    )
    return len(jobs)


@router.delete("/{job_id}", status_code=204, response_class=Response, response_model=None)
def delete_history_item(job_id: str, session: Session = Depends(get_db)) -> None:
    """이 실행 하나만 지운다.

    이어받은 후속 실행은 그대로 둔다. 후속 실행은 첨부와 이전 보고서를 자기
    폴더/컬럼에 복사해 두므로 원본 없이도 온전하다. source_job_id 는 끊긴
    참조로 남고, 화면에는 source_job_label 로 "(삭제됨)" 표시가 나간다.
    """
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")
    _purge(session, [job])


@router.delete("/{job_id}/thread")
def delete_thread(job_id: str, session: Session = Depends(get_db)) -> dict:
    """이 실행과 그로부터 이어진 후속 실행을 함께 지운다."""
    job = session.get(ExecutionJob, job_id)
    if job is None:
        raise HTTPException(404, "실행 이력을 찾을 수 없습니다.")

    ids = _descendants(_child_map(session), job_id)
    targets = [job]
    if ids:
        targets += (
            session.query(ExecutionJob).filter(ExecutionJob.id.in_(ids)).all()
        )
    return {"deleted": _purge(session, targets)}
