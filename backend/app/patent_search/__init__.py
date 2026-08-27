"""특허 검색 연동 모듈 (Provider·벤더 중립).

ARIA 본체는 이 패키지의 인터페이스(base)와 팩토리(get_backend/describe)만
의존한다. Kiwee 구현 세부는 kiwee_backend 에 격리한다. 나중에 Kiwee 를 새로
만들거나 다른 특허 DB 를 붙일 때는 PatentSearchBackend 를 구현한 새 백엔드를
_REGISTRY 에 등록하고 활성 backend_id 만 바꾸면 된다 — 본체는 그대로다.

지금 단계 원칙
--------------
- 기본값은 꺼짐(config.DEFAULTS['kiwee_integration_enabled'] = False).
- 꺼져 있으면 get_backend 는 None 을 준다. 실행 경로는 예전과 정확히 같다
  (fail-closed).
- 켜져 있어도 백엔드 search() 는 네트워크를 열지 않는다
  (PatentSearchNotConfigured). 외부 접속은 공급자 승인·API 계약·NK 동등성
  검증 뒤에만 별도로 구현한다.
- runner 실행 경로는 아직 건드리지 않는다. search_manifest 에는 채널 허용
  목록 분리만 반영했다(모델 보고=web 고정, patent_db=ARIA 생산자 전용).
  동작은 이전과 같고, 경계를 이름으로 못 박은 것뿐이다.
- 증거 등급은 이 모듈이 계산한다. 발췌 단위이며, 보존 아티팩트에서 원본을
  다시 읽어 해시를 재계산하고 신뢰 파서로 필드를 재추출한 뒤 대조한다.
  어댑터가 준 값은 판정에 쓰지 않는다.
- 원문 등급(raw)에는 관문이 둘이다. 중앙 정책(policy.RAW_DISABLED 가 기본)과
  소스 프로필의 raw_capable 이 **둘 다** 참이어야 한다. 지금 raw_capable
  프로필은 하나도 등록되어 있지 않으므로 정책을 켜도 원문 등급은 안 나온다.
- 출처(source_kind, is_translation)는 어댑터가 아니라 등록된 소스 프로필에서
  나온다. FieldValue 에는 출처를 주장할 필드가 없다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .artifacts import (
    ArtifactCorrupted,
    ArtifactError,
    ArtifactIdInvalid,
    ArtifactMissing,
    ArtifactStore,
    compute_id,
)
from .base import (
    ORIGINAL_SOURCE_KINDS,
    SOURCE_KINDS,
    SOURCE_OFFICIAL_XML,
    BackendStatus,
    EvidenceRef,
    FieldValue,
    PatentRecord,
    PatentSearchBackend,
    PatentSearchDisabled,
    PatentSearchError,
    PatentSearchNotConfigured,
    PatentSearchQuery,
    PatentSearchResponse,
)
from .epo_backend import (
    SETTING_CONSUMER_KEY as EPO_SETTING_CONSUMER_KEY,
    SETTING_CONSUMER_SECRET as EPO_SETTING_CONSUMER_SECRET,
    SETTING_ENABLED as EPO_SETTING_ENABLED,
    CredentialCheck,
    EpoOpsBackend,
    check_credentials,
)
from .kiwee_backend import KiweePatentSearchBackend
from .parsers import (
    PROFILE_GENERIC_JSON,
    ExtractedField,
    SourceProfile,
    raw_capable_profiles,
    register_profile,
)
from .policy import RAW_DISABLED, RAW_ENABLED, EvidencePolicy
from .provenance import (
    MATCH_EXACT,
    MATCH_KINDS,
    MATCH_NONE,
    MATCH_NORMALIZED,
    ExcerptVerification,
    summarize,
    verify_excerpt,
    verify_record_excerpt,
)

# 백엔드마다 켜고 끄는 설정 키. 하나로 묶으면 EPO 를 켜는 순간 Kiwee 도 켜진다.
_ENABLE_KEYS: dict[str, str] = {
    "kiwee": "kiwee_integration_enabled",
    "epo": EPO_SETTING_ENABLED,
}

# 설정 키. 이름의 단일 출처. 백엔드가 하나뿐이던 시절의 이름이라 Kiwee 를
# 가리킨다. 새 코드는 _ENABLE_KEYS 를 쓴다.
SETTING_KEY = _ENABLE_KEYS["kiwee"]

# 기본 백엔드. 인자 없이 부르는 호출부가 가리키는 대상이다.
DEFAULT_BACKEND_ID = "kiwee"

# 화면이 상태를 보여 줄 백엔드 전체. 등록 순서가 표시 순서다.
BACKEND_IDS = ("kiwee", "epo")

_REGISTRY: dict[str, Callable[[], PatentSearchBackend]] = {
    "kiwee": KiweePatentSearchBackend,
    "epo": EpoOpsBackend,
}

__all__ = [
    "SETTING_KEY",
    "DEFAULT_BACKEND_ID",
    "BACKEND_IDS",
    "EPO_SETTING_ENABLED",
    "EPO_SETTING_CONSUMER_KEY",
    "EPO_SETTING_CONSUMER_SECRET",
    "CredentialCheck",
    "EpoOpsBackend",
    "check_credentials",
    "describe_all",
    "BackendStatus",
    "PatentRecord",
    "PatentSearchBackend",
    "PatentSearchDisabled",
    "PatentSearchError",
    "PatentSearchNotConfigured",
    "PatentSearchQuery",
    "PatentSearchResponse",
    "is_enabled",
    "get_backend",
    "describe",
    "register_backend",
    "ArtifactCorrupted",
    "ArtifactError",
    "ArtifactIdInvalid",
    "ArtifactMissing",
    "ArtifactStore",
    "compute_id",
    "EvidenceRef",
    "FieldValue",
    "ORIGINAL_SOURCE_KINDS",
    "SOURCE_KINDS",
    "ExcerptVerification",
    "MATCH_EXACT",
    "MATCH_KINDS",
    "MATCH_NONE",
    "MATCH_NORMALIZED",
    "EvidencePolicy",
    "RAW_DISABLED",
    "RAW_ENABLED",
    "ExtractedField",
    "SourceProfile",
    "PROFILE_GENERIC_JSON",
    "SOURCE_OFFICIAL_XML",
    "raw_capable_profiles",
    "register_profile",
    "summarize",
    "verify_excerpt",
    "verify_record_excerpt",
]


def register_backend(
    backend_id: str,
    factory: Callable[[], PatentSearchBackend],
    enable_key: str | None = None,
) -> None:
    """새 백엔드를 등록한다. 나중에 Kiwee 를 새로 만들 때의 진입점.

    enable_key 를 주지 않으면 기본 백엔드의 토글을 함께 쓴다(옛 호출부 호환).
    독립적으로 켜고 꺼야 하는 백엔드는 반드시 자기 키를 넘겨야 한다.
    """
    _REGISTRY[backend_id] = factory
    _ENABLE_KEYS[backend_id] = enable_key or SETTING_KEY


def is_enabled(
    values: Mapping[str, Any], backend_id: str = DEFAULT_BACKEND_ID
) -> bool:
    """그 백엔드의 설정 토글 상태. values 는 settings_service.get_all 결과."""
    key = _ENABLE_KEYS.get(backend_id)
    if key is None:
        return False
    return bool(values.get(key, False))


def get_backend(
    values: Mapping[str, Any], backend_id: str = DEFAULT_BACKEND_ID
) -> PatentSearchBackend | None:
    """활성 백엔드. 연동이 꺼져 있거나 알 수 없는 백엔드면 None.

    None 을 돌려주는 것은 '연동 안 함'이라는 정상 상태다. 호출부는 None 이면
    예전 경로(웹 검색만)를 그대로 쓴다.
    """
    if not is_enabled(values, backend_id):
        return None
    factory = _REGISTRY.get(backend_id)
    if factory is None:
        return None
    backend = factory()
    backend.configure(values)
    return backend


def describe(
    values: Mapping[str, Any], backend_id: str = DEFAULT_BACKEND_ID
) -> BackendStatus:
    """백엔드 상태를 네트워크 없이 보고한다. Settings 경고 문구의 출처."""
    enabled = is_enabled(values, backend_id)
    factory = _REGISTRY.get(backend_id)
    if factory is None:
        return BackendStatus(
            backend_id=backend_id,
            display_name=backend_id,
            enabled=enabled,
            configured=False,
            detail="등록되지 않은 백엔드입니다.",
        )
    backend = factory()
    backend.configure(values)
    if not enabled:
        return BackendStatus(
            backend_id=backend.id,
            display_name=backend.display_name,
            enabled=False,
            configured=False,
            detail="연동이 꺼져 있습니다.",
        )
    return backend.status()


def describe_all(values: Mapping[str, Any]) -> tuple[BackendStatus, ...]:
    """등록된 백엔드 전체의 상태. 화면과 경고 문구가 같은 것을 본다."""
    return tuple(describe(values, backend_id) for backend_id in BACKEND_IDS)
