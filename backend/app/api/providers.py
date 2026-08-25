"""Provider 상태 API.

기본 probe 는 모델을 호출하지 않으므로 사용량이 발생하지 않는다.
실제 호출 테스트(smoke-test)는 사용자가 명시적으로 눌렀을 때만 실행한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import settings_service
from ..db import get_db
from ..models import ProviderSnapshot
from ..providers.registry import (
    PROVIDER_ORDER,
    build_provider,
    probe_all,
    probe_one,
    to_dict,
)
from ..providers.login import (
    HELPER_WINDOW_LOGOUT_PROVIDERS,
    LOGIN_INTENT,
    LOGIN_MANAGER,
    LOGOUT_INTENT,
    LoginError,
    logout_provider,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


def _persist(session: Session, data: dict) -> None:
    session.add(
        ProviderSnapshot(
            provider=data["provider"],
            installed=data["installed"],
            executable_path=data["executable_path"],
            executable_kind=data["executable_kind"],
            executable_ok=data["executable_ok"],
            version=data["version"],
            auth_state=data["auth_state"],
            capabilities=data["capabilities"],
            notes=data["notes"],
        )
    )
    session.commit()


@router.get("")
async def list_providers(session: Session = Depends(get_db)) -> dict:
    overrides = settings_service.get(session, "provider_paths") or {}
    results = await probe_all(overrides)
    return {"providers": [to_dict(r) for r in results]}


@router.post("/probe")
async def reprobe(session: Session = Depends(get_db)) -> dict:
    overrides = settings_service.get(session, "provider_paths") or {}
    results = await probe_all(overrides, force=True)
    payload = [to_dict(r) for r in results]
    for item in payload:
        _persist(session, item)
    return {"providers": payload}


@router.post("/{provider_id}/login", status_code=status.HTTP_202_ACCEPTED)
async def start_login(
    provider_id: str,
    body: object = Body(default=None),
    session: Session = Depends(get_db),
) -> dict:
    """CLI가 소유하는 브라우저/도우미 로그인 세션을 시작한다."""
    if provider_id not in PROVIDER_ORDER:
        raise HTTPException(404, "알 수 없는 Provider 입니다.")
    if body is None:
        payload: dict = {}
    elif isinstance(body, dict):
        payload = body
    else:
        raise HTTPException(400, "로그인 요청 형식이 올바르지 않습니다.")
    if set(payload) - {"method"}:
        # FastAPI/Pydantic의 기본 422 응답은 거부한 입력값을 그대로 되돌려줄 수
        # 있다. credential처럼 보이는 값을 반사하지 않도록 직접 검증한다.
        raise HTTPException(400, "로그인 요청에는 method만 사용할 수 있습니다.")
    method = payload.get("method")
    if method is not None and not isinstance(method, str):
        raise HTTPException(400, "로그인 방식이 올바르지 않습니다.")
    overrides = settings_service.get(session, "provider_paths") or {}
    try:
        return await LOGIN_MANAGER.start(
            provider_id,
            method=method,
            executable_override=overrides.get(provider_id) or None,
        )
    except LoginError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/{provider_id}/login/{session_id}")
async def login_status(provider_id: str, session_id: str) -> dict:
    result = await LOGIN_MANAGER.get(provider_id, session_id, intent=LOGIN_INTENT)
    if result is None:
        raise HTTPException(404, "로그인 세션을 찾을 수 없습니다.")
    return result


@router.delete("/{provider_id}/login/{session_id}")
async def cancel_login(provider_id: str, session_id: str) -> dict:
    result = await LOGIN_MANAGER.cancel(provider_id, session_id, intent=LOGIN_INTENT)
    if result is None:
        raise HTTPException(404, "로그인 세션을 찾을 수 없습니다.")
    return result


@router.post("/{provider_id}/logout")
async def logout(provider_id: str, session: Session = Depends(get_db)) -> dict:
    """CLI가 저장한 현재 계정 로그인을 해제한다.

    claude/codex 는 CLI 의 logout 명령으로 즉시 끝나므로 `mode: immediate` 결과를
    돌려준다. agy 는 전용 logout 명령이 없어 도우미 창 세션을 시작하고, 프런트가
    /logout/{session_id} 로 상태를 폴링한다.
    """
    if provider_id not in PROVIDER_ORDER:
        raise HTTPException(404, "알 수 없는 Provider 입니다.")
    overrides = settings_service.get(session, "provider_paths") or {}
    override = overrides.get(provider_id) or None
    try:
        if provider_id in HELPER_WINDOW_LOGOUT_PROVIDERS:
            return await LOGIN_MANAGER.start_logout(
                provider_id, executable_override=override
            )
        return await logout_provider(provider_id, executable_override=override)
    except LoginError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/{provider_id}/logout/{session_id}")
async def logout_status(provider_id: str, session_id: str) -> dict:
    result = await LOGIN_MANAGER.get(provider_id, session_id, intent=LOGOUT_INTENT)
    if result is None:
        raise HTTPException(404, "로그아웃 세션을 찾을 수 없습니다.")
    return result


@router.delete("/{provider_id}/logout/{session_id}")
async def cancel_logout(provider_id: str, session_id: str) -> dict:
    result = await LOGIN_MANAGER.cancel(provider_id, session_id, intent=LOGOUT_INTENT)
    if result is None:
        raise HTTPException(404, "로그아웃 세션을 찾을 수 없습니다.")
    return result


@router.get("/{provider_id}")
async def get_provider(provider_id: str, session: Session = Depends(get_db)) -> dict:
    overrides = settings_service.get(session, "provider_paths") or {}
    result = await probe_one(provider_id, overrides)
    if result is None:
        raise HTTPException(404, "알 수 없는 Provider 입니다.")
    return to_dict(result)


@router.post("/{provider_id}/smoke-test")
async def smoke_test(provider_id: str, session: Session = Depends(get_db)) -> dict:
    """실제 모델을 호출한다. 사용량이 발생할 수 있다."""
    overrides = settings_service.get(session, "provider_paths") or {}
    provider = build_provider(provider_id, overrides)
    if provider is None:
        raise HTTPException(404, "알 수 없는 Provider 입니다.")

    try:
        outcome = await provider.smoke_test()
    except NotImplementedError:
        raise HTTPException(
            400, f"{provider_id} 는 실제 호출 테스트를 지원하지 않습니다."
        ) from None

    return {
        "provider": provider_id,
        "ok": not outcome.is_error and bool(outcome.result_text.strip()),
        "result_text": outcome.result_text[:2000],
        "is_error": outcome.is_error,
        "auth_required": outcome.auth_required,
        "rate_limited": outcome.rate_limited,
        "exit_code": outcome.exit_code,
        "terminal_reason": outcome.terminal_reason,
        "error_message": outcome.error_message,
        "cli_path": outcome.cli_path,
        "cli_version": outcome.cli_version,
        "stderr_tail": (outcome.raw_stderr or "")[-2000:],
    }
