"""Prompt Library API.

PromptTemplate 의 body 가 업무 로직의 유일한 출처다. ARIA 는 여기에 어떤
업무 지시도 추가하지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PromptTemplate, PromptVersion
from ..schemas import (
    PromptCreate,
    PromptImportRequest,
    PromptOut,
    PromptUpdate,
    PromptVersionOut,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _snapshot(session: Session, prompt: PromptTemplate) -> None:
    session.add(
        PromptVersion(
            prompt_id=prompt.id,
            version=prompt.version,
            name=prompt.name,
            description=prompt.description or "",
            body=prompt.body,
            output_mode=prompt.output_mode,
        )
    )


@router.get("", response_model=list[PromptOut])
def list_prompts(
    session: Session = Depends(get_db),
    search: str = Query(default=""),
    tag: str = Query(default=""),
    include_archived: bool = Query(default=False),
) -> list[PromptTemplate]:
    query = session.query(PromptTemplate)
    if not include_archived:
        query = query.filter(PromptTemplate.archived.is_(False))
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            or_(
                PromptTemplate.name.ilike(pattern),
                PromptTemplate.description.ilike(pattern),
                PromptTemplate.body.ilike(pattern),
            )
        )
    rows = query.order_by(PromptTemplate.updated_at.desc()).all()
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])]
    return rows


@router.post("", response_model=PromptOut, status_code=201)
def create_prompt(
    payload: PromptCreate, session: Session = Depends(get_db)
) -> PromptTemplate:
    prompt = PromptTemplate(
        name=payload.name,
        description=payload.description,
        body=payload.body,
        output_mode=payload.output_mode,
        default_provider=payload.default_provider,
        default_model=payload.default_model,
        tags=payload.tags,
        accepted_file_types=payload.accepted_file_types,
        version=1,
    )
    session.add(prompt)
    session.flush()
    _snapshot(session, prompt)
    session.commit()
    session.refresh(prompt)
    return prompt


@router.get("/export")
def export_prompts(session: Session = Depends(get_db)) -> dict:
    rows = session.query(PromptTemplate).filter(PromptTemplate.archived.is_(False)).all()
    return {
        "version": 1,
        "prompts": [
            {
                "name": r.name,
                "description": r.description or "",
                "body": r.body,
                "output_mode": r.output_mode,
                "default_provider": r.default_provider,
                "default_model": r.default_model,
                "tags": r.tags or [],
                "accepted_file_types": r.accepted_file_types or [],
            }
            for r in rows
        ],
    }


@router.post("/import")
def import_prompts(
    payload: PromptImportRequest, session: Session = Depends(get_db)
) -> dict:
    created = 0
    updated = 0
    for item in payload.prompts:
        existing = (
            session.query(PromptTemplate)
            .filter(PromptTemplate.name == item.name, PromptTemplate.archived.is_(False))
            .first()
        )
        if existing is not None and payload.replace_existing:
            existing.body = item.body
            existing.description = item.description
            existing.output_mode = item.output_mode
            existing.default_provider = item.default_provider
            existing.default_model = item.default_model
            existing.tags = item.tags
            existing.accepted_file_types = item.accepted_file_types
            existing.version += 1
            session.flush()
            _snapshot(session, existing)
            updated += 1
            continue
        if existing is not None:
            continue
        prompt = PromptTemplate(
            name=item.name,
            description=item.description,
            body=item.body,
            output_mode=item.output_mode,
            default_provider=item.default_provider,
            default_model=item.default_model,
            tags=item.tags,
            accepted_file_types=item.accepted_file_types,
            version=1,
        )
        session.add(prompt)
        session.flush()
        _snapshot(session, prompt)
        created += 1
    session.commit()
    return {"created": created, "updated": updated}


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: str, session: Session = Depends(get_db)) -> PromptTemplate:
    prompt = session.get(PromptTemplate, prompt_id)
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    return prompt


@router.put("/{prompt_id}", response_model=PromptOut)
def update_prompt(
    prompt_id: str, payload: PromptUpdate, session: Session = Depends(get_db)
) -> PromptTemplate:
    prompt = session.get(PromptTemplate, prompt_id)
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")

    changes = payload.model_dump(exclude_unset=True)
    # 본문/이름/출력형식이 바뀌면 새 버전을 남긴다.
    versioned_fields = {"name", "body", "output_mode", "description"}
    should_version = any(
        field in changes and changes[field] != getattr(prompt, field)
        for field in versioned_fields
    )

    for field, value in changes.items():
        setattr(prompt, field, value)

    if should_version:
        prompt.version += 1
        session.flush()
        _snapshot(session, prompt)

    session.commit()
    session.refresh(prompt)
    return prompt


@router.delete("/{prompt_id}", status_code=204, response_class=Response, response_model=None)
def delete_prompt(prompt_id: str, session: Session = Depends(get_db)) -> None:
    prompt = session.get(PromptTemplate, prompt_id)
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    session.delete(prompt)
    session.commit()


@router.post("/{prompt_id}/clone", response_model=PromptOut, status_code=201)
def clone_prompt(prompt_id: str, session: Session = Depends(get_db)) -> PromptTemplate:
    source = session.get(PromptTemplate, prompt_id)
    if source is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    clone = PromptTemplate(
        name=f"{source.name} (복제)",
        description=source.description,
        body=source.body,
        output_mode=source.output_mode,
        default_provider=source.default_provider,
        default_model=source.default_model,
        tags=list(source.tags or []),
        accepted_file_types=list(source.accepted_file_types or []),
        version=1,
    )
    session.add(clone)
    session.flush()
    _snapshot(session, clone)
    session.commit()
    session.refresh(clone)
    return clone


@router.get("/{prompt_id}/versions", response_model=list[PromptVersionOut])
def list_versions(prompt_id: str, session: Session = Depends(get_db)) -> list[PromptVersion]:
    prompt = session.get(PromptTemplate, prompt_id)
    if prompt is None:
        raise HTTPException(404, "프롬프트를 찾을 수 없습니다.")
    return (
        session.query(PromptVersion)
        .filter(PromptVersion.prompt_id == prompt_id)
        .order_by(PromptVersion.version.desc())
        .all()
    )
