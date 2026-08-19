"""Settings API.

API Key 입력란은 만들지 않는다. 각 CLI 에 저장된 로그인 세션만 사용한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import settings_service
from ..config import DEFAULT_RUNTIME_CONTEXT, PATHS
from ..db import get_db
from ..providers.env import describe_filtering
from ..providers.registry import invalidate
from ..schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _payload(session: Session) -> SettingsOut:
    values = settings_service.get_all(session)
    return SettingsOut(
        values=values,
        warnings=settings_service.warnings_for(values),
        data_dir=str(PATHS.data_dir),
        runs_dir=str(PATHS.runs_dir),
        env_filtering=describe_filtering(),
    )


@router.get("", response_model=SettingsOut)
def get_settings(session: Session = Depends(get_db)) -> SettingsOut:
    return _payload(session)


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate, session: Session = Depends(get_db)
) -> SettingsOut:
    try:
        settings_service.update(session, payload.values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    # 실행 파일 경로가 바뀌었을 수 있으므로 probe 캐시를 버린다.
    invalidate()
    return _payload(session)


@router.post("/runtime-context/reset", response_model=SettingsOut)
def reset_runtime_context(session: Session = Depends(get_db)) -> SettingsOut:
    settings_service.update(session, {"runtime_context": DEFAULT_RUNTIME_CONTEXT})
    session.commit()
    return _payload(session)
