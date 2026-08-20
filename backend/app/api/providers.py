"""Provider 상태 API.

기본 probe 는 모델을 호출하지 않으므로 사용량이 발생하지 않는다.
실제 호출 테스트(smoke-test)는 사용자가 명시적으로 눌렀을 때만 실행한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import settings_service
from ..db import get_db
from ..models import ProviderSnapshot
from ..providers.registry import (
    apply_optin,
    build_provider,
    is_allowed,
    probe_all,
    probe_one,
    to_dict,
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
    enabled = settings_service.get(session, "enabled_experimental_providers")
    results = apply_optin(await probe_all(overrides), enabled)
    return {"providers": [to_dict(r) for r in results]}


@router.post("/probe")
async def reprobe(session: Session = Depends(get_db)) -> dict:
    overrides = settings_service.get(session, "provider_paths") or {}
    enabled = settings_service.get(session, "enabled_experimental_providers")
    results = apply_optin(await probe_all(overrides, force=True), enabled)
    payload = [to_dict(r) for r in results]
    for item in payload:
        _persist(session, item)
    return {"providers": payload}


@router.get("/{provider_id}")
async def get_provider(provider_id: str, session: Session = Depends(get_db)) -> dict:
    overrides = settings_service.get(session, "provider_paths") or {}
    enabled = settings_service.get(session, "enabled_experimental_providers")
    result = await probe_one(provider_id, overrides)
    if result is None:
        raise HTTPException(404, "알 수 없는 Provider 입니다.")
    apply_optin([result], enabled)
    return to_dict(result)


@router.post("/{provider_id}/smoke-test")
async def smoke_test(provider_id: str, session: Session = Depends(get_db)) -> dict:
    """실제 모델을 호출한다. 사용량이 발생할 수 있다."""
    overrides = settings_service.get(session, "provider_paths") or {}
    enabled = settings_service.get(session, "enabled_experimental_providers")
    if not is_allowed(provider_id, enabled):
        raise HTTPException(
            403,
            f"{provider_id} 는 실험적 Provider 입니다. Settings 에서 위험을 "
            "확인하고 명시적으로 활성화한 뒤 사용하십시오.",
        )
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
