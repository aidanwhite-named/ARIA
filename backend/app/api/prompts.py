"""File-backed Prompt Library API.

Current prompt bodies come only from files in the configured ``prompt``
directory. The database is not consulted by this API.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..prompt_store import (
    PROMPT_STORE,
    InvalidPromptFile,
    PromptFile,
    PromptFileVersion,
    PromptNotFound,
    PromptStoreError,
)
from ..schemas import (
    PromptCreate,
    PromptImportRequest,
    PromptOut,
    PromptUpdate,
    PromptVersionOut,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _raise_http(exc: PromptStoreError) -> None:
    if isinstance(exc, PromptNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, InvalidPromptFile):
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=list[PromptOut])
def list_prompts(
    search: str = Query(default=""), tag: str = Query(default="")
) -> list[PromptFile]:
    try:
        return PROMPT_STORE.list(search=search, tag=tag)
    except PromptStoreError as exc:
        _raise_http(exc)


@router.post("", response_model=PromptOut, status_code=201)
def create_prompt(payload: PromptCreate) -> PromptFile:
    try:
        return PROMPT_STORE.create(**payload.model_dump())
    except PromptStoreError as exc:
        _raise_http(exc)


@router.get("/export")
def export_prompts() -> dict:
    try:
        rows = PROMPT_STORE.list()
    except PromptStoreError as exc:
        _raise_http(exc)
    return {
        "version": 1,
        "source": "prompt-directory",
        "prompts": [
            {
                "name": row.name,
                "description": row.description,
                "body": row.body,
                "output_mode": row.output_mode,
                "tags": row.tags,
                "accepted_file_types": row.accepted_file_types,
            }
            for row in rows
        ],
    }


@router.post("/import")
def import_prompts(payload: PromptImportRequest) -> dict:
    created = 0
    updated = 0
    try:
        by_name = {item.name: item for item in PROMPT_STORE.list()}
        for item in payload.prompts:
            existing = by_name.get(item.name)
            if existing is not None and payload.replace_existing:
                changed = PROMPT_STORE.update(existing.id, item.model_dump())
                by_name[changed.name] = changed
                updated += 1
                continue
            if existing is not None:
                continue
            prompt = PROMPT_STORE.create(**item.model_dump())
            by_name[prompt.name] = prompt
            created += 1
    except PromptStoreError as exc:
        _raise_http(exc)
    return {"created": created, "updated": updated}


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: str) -> PromptFile:
    try:
        return PROMPT_STORE.get(prompt_id)
    except PromptStoreError as exc:
        _raise_http(exc)


@router.put("/{prompt_id}", response_model=PromptOut)
def update_prompt(prompt_id: str, payload: PromptUpdate) -> PromptFile:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        return PROMPT_STORE.update(prompt_id, changes)
    except PromptStoreError as exc:
        _raise_http(exc)


@router.delete(
    "/{prompt_id}", status_code=204, response_class=Response, response_model=None
)
def delete_prompt(prompt_id: str) -> None:
    try:
        PROMPT_STORE.delete(prompt_id)
    except PromptStoreError as exc:
        _raise_http(exc)


@router.get("/{prompt_id}/versions", response_model=list[PromptVersionOut])
def list_versions(prompt_id: str) -> list[PromptFileVersion]:
    try:
        return PROMPT_STORE.versions(prompt_id)
    except PromptStoreError as exc:
        _raise_http(exc)
