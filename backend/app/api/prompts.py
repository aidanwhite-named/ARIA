"""File-backed Prompt Library API.

Current prompt bodies come only from files in the configured ``prompt``
directory. The database is not consulted by this API.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ..prompt_store import (
    KIND_ANALYSIS,
    KIND_SEARCH,
    PROMPT_STORE,
    RESERVED_PROMPT_IDS,
    InvalidPromptFile,
    PromptFile,
    PromptNotFound,
    PromptStoreError,
)
from ..search_prompt import SearchPromptError, validate_strategy_body
from ..schemas import (
    PromptCatalogOut,
    PromptCreate,
    PromptImportRequest,
    PromptOut,
    PromptUpdate,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _catalog_item(prompt: PromptFile) -> PromptCatalogOut:
    # 예약은 "지울 수 없다"만 뜻한다. 종류는 파일이 스스로 밝히며, 검색 전략
    # 프롬프트는 사용자가 얼마든지 더 만들 수 있다.
    reserved = prompt.id in RESERVED_PROMPT_IDS
    base = PromptOut.model_validate(prompt)
    return PromptCatalogOut(**base.model_dump(), editable=True, deletable=not reserved)


def _raise_http(exc: PromptStoreError) -> None:
    if isinstance(exc, PromptNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, InvalidPromptFile):
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=list[PromptOut])
def list_prompts(
    search: str = Query(default=""),
    tag: str = Query(default=""),
    kind: str = Query(default=KIND_ANALYSIS),
) -> list[PromptFile]:
    """실행 화면의 프롬프트 선택 목록.

    기본은 분석 프롬프트다. 이 목록은 구성대비 분석의 분석 기준을 고르는 데
    쓰이므로, 검색 전략 프롬프트가 섞이면 검색 계약을 만족하지 않는 본문이
    분석 실행에 선택될 수 있다. 검색 화면은 ``kind=search`` 로 부른다.
    """
    try:
        return PROMPT_STORE.list(
            search=search,
            tag=tag,
            kind=kind,
            include_reserved=kind == KIND_SEARCH,
        )
    except PromptStoreError as exc:
        _raise_http(exc)


@router.get("/catalog", response_model=list[PromptCatalogOut])
def list_prompt_catalog(
    search: str = Query(default=""), tag: str = Query(default="")
) -> list[PromptCatalogOut]:
    """두 작업 모드의 프롬프트를 관리 화면에 함께 보여 준다.

    일반 ``/api/prompts`` 목록은 분석 실행의 선택지이므로 예약된 검색
    프롬프트를 계속 제외한다. 두 목록을 섞으면 검색 프롬프트가 구성대비 분석의
    기본값으로 선택될 수 있다.
    """
    try:
        rows = PROMPT_STORE.list(
            search=search, tag=tag, include_reserved=True
        )
    except PromptStoreError as exc:
        _raise_http(exc)
    items = [_catalog_item(row) for row in rows]
    return sorted(items, key=lambda item: (item.kind != "analysis", item.name))


@router.post("", response_model=PromptOut, status_code=201)
def create_prompt(payload: PromptCreate) -> PromptFile:
    values = payload.model_dump()
    if values.get("kind") == KIND_SEARCH:
        # 검색 전략 프롬프트에는 요구하는 표시가 없다(데이터 구간은 ARIA 가
        # 붙인다). 다만 옛 방식으로 placeholder 를 직접 든 본문을 붙여 넣었다면
        # 그 계약은 만족해야 한다 — 반쯤 옮겨 적은 본문으로 실행하면 청구항이
        # 경계 밖에 놓인다.
        try:
            validate_strategy_body(str(values.get("body") or ""))
        except SearchPromptError as exc:
            raise HTTPException(422, str(exc)) from exc
    try:
        return PROMPT_STORE.create(**values)
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
                # 종류를 함께 내보낸다. 빠뜨리면 다시 들여올 때 검색 전략
                # 프롬프트가 분석 프롬프트로 되살아난다.
                "kind": row.kind,
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
        current = PROMPT_STORE.get(prompt_id)
    except PromptStoreError as exc:
        _raise_http(exc)
    if current.kind == KIND_SEARCH and "body" in changes:
        try:
            validate_strategy_body(str(changes["body"]), prompt_id=prompt_id)
        except SearchPromptError as exc:
            raise HTTPException(422, str(exc)) from exc
    try:
        return PROMPT_STORE.update(prompt_id, changes)
    except PromptStoreError as exc:
        _raise_http(exc)


@router.put("/reserved/{prompt_id}", response_model=PromptCatalogOut)
def update_reserved_prompt(
    prompt_id: str, payload: PromptUpdate
) -> PromptCatalogOut:
    """배포본 검색 전략 프롬프트를 조립 계약 검증 후 갱신한다."""
    if prompt_id not in RESERVED_PROMPT_IDS:
        raise HTTPException(404, "예약된 프롬프트를 찾을 수 없습니다.")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        current = PROMPT_STORE.get_reserved(prompt_id)
        validate_strategy_body(
            str(changes.get("body", current.body)), prompt_id=prompt_id
        )
        return _catalog_item(PROMPT_STORE.update_reserved(prompt_id, changes))
    except SearchPromptError as exc:
        raise HTTPException(422, str(exc)) from exc
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
