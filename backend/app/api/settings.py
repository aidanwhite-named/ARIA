"""Settings API.

AI 실행 도구(Provider)의 API Key 입력란은 만들지 않는다. 각 CLI 에 저장된
로그인 세션만 사용한다.

예외는 **외부 데이터 소스**의 자격증명이다(EPO OPS). 그쪽은 CLI 도 로그인
세션도 없고 OAuth client_credentials 뿐이라 ARIA 가 보관하는 것 외에 방법이
없다. 대신 두 가지를 지킨다.

  - 저장은 하되 응답으로 돌려주지 않는다(settings_service.redact_for_api).
    화면은 "설정됨/미설정"만 본다.
  - 자격증명을 쓰는 외부 호출은 사용자가 버튼을 눌렀을 때의 확인 한 번뿐이다.
    실행(runner) 경로는 이 자격증명을 쓰지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import patent_search, settings_service
from ..config import DEFAULT_RUNTIME_CONTEXT, PATHS
from ..db import get_db
from ..providers import agy_permissions
from ..providers.env import describe_filtering
from ..providers.registry import invalidate
from ..schemas import CredentialCheckOut, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _payload(session: Session) -> SettingsOut:
    values = settings_service.get_all(session)
    # 경고 문구는 가리기 **전** 값으로 만든다. 가린 값으로 만들면 자격증명을
    # 넣어 둔 사용자에게도 "설정되지 않았습니다"가 뜬다.
    warnings = settings_service.warnings_for(values)
    return SettingsOut(
        values=settings_service.redact_for_api(values),
        warnings=warnings,
        data_dir=str(PATHS.data_dir),
        runs_dir=str(PATHS.runs_dir),
        env_filtering=describe_filtering(),
        secrets_set=settings_service.secrets_set(values),
        epo_quota=settings_service.epo_quota_snapshot(values),
        # 읽기만 한다. 이 화면에서 파일을 고치지 않는다 — 적용은 agy Provider
        # 검사 경로 한 곳에서만 일어나고, 여기는 그 결과를 보여 준다.
        agy_permissions=agy_permissions.read_state().to_dict(),
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


@router.post("/epo/check", response_model=CredentialCheckOut)
def check_epo_credentials(session: Session = Depends(get_db)) -> CredentialCheckOut:
    """저장된 EPO OPS 자격증명으로 토큰 발급을 한 번 시도한다.

    ARIA 가 외부로 나가는 유일한 설정 화면 동작이다. 사용자가 버튼을 눌렀을
    때만 실행되고, 특허 데이터는 요청하지 않으며, 받은 토큰은 저장하지 않는다.
    자격증명은 요청 본문이 아니라 저장된 값에서 읽는다 — 본문으로 받으면 비밀이
    프록시 로그와 브라우저 기록에 한 번 더 남는다.
    """
    values = settings_service.get_all(session)
    if not values.get(patent_search.EPO_SETTING_ENABLED, False):
        raise HTTPException(400, "EPO OPS 연동이 꺼져 있습니다.")
    result = patent_search.check_credentials(
        str(values.get(patent_search.EPO_SETTING_CONSUMER_KEY) or ""),
        str(values.get(patent_search.EPO_SETTING_CONSUMER_SECRET) or ""),
    )
    return CredentialCheckOut(
        ok=result.ok,
        detail=result.detail,
        http_status=result.http_status,
        expires_in=result.expires_in,
    )


@router.post("/agy-permissions/apply", response_model=SettingsOut)
def apply_agy_permissions(session: Session = Depends(get_db)) -> SettingsOut:
    """권장 논문 출처를 agy 의 허용 목록에 다시 병합한다.

    ARIA 가 이 파일을 자동으로 고치는 것은 설치당 한 번뿐이다(앱 시작 시의
    일회성 마이그레이션). 그 뒤에 다시 넣는 유일한 방법이 이 버튼이다 —
    사용자가 지운 호스트를 프로그램이 되살리지 않기 위해서다.

    병합만 한다. 기존 항목을 덮어쓰지 않고, 와일드카드를 만들지 않으며,
    파일이 손상돼 있으면 손대지 않고 400 으로 알린다.
    """
    from ..providers.agy_permissions import AgyPermissionsError

    try:
        settings_service.apply_agy_allowlist(session, forced=True)
    except AgyPermissionsError as exc:
        raise HTTPException(400, str(exc)) from None
    session.commit()
    return _payload(session)


@router.post("/runtime-context/reset", response_model=SettingsOut)
def reset_runtime_context(session: Session = Depends(get_db)) -> SettingsOut:
    settings_service.update(session, {"runtime_context": DEFAULT_RUNTIME_CONTEXT})
    session.commit()
    return _payload(session)
