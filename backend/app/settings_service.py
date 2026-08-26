"""AppSetting 읽기/쓰기.

기본값은 config.DEFAULTS 에 있고, DB 에는 사용자가 바꾼 것만 저장한다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import patent_search
from .config import DEFAULTS
from .models import AppSetting
from .providers.registry import TOOL_UNCONTROLLABLE_PROVIDERS

# 사용자가 UI 에서 바꿀 수 있는 키. 이 목록에 없는 키는 PUT 으로 못 바꾼다.
EDITABLE_KEYS = frozenset(
    {
        "max_file_size_bytes",
        "max_total_upload_bytes",
        "max_files_per_job",
        "max_inline_chars",
        "default_timeout_seconds",
        "max_concurrency_per_provider",
        "runtime_context",
        "runtime_context_enabled",
        "default_prompt_id",
        "default_provider",
        "provider_paths",
        "default_models",
        "keep_raw_output",
        "fail_on_tool_use",
        "max_search_tool_calls",
        "kiwee_integration_enabled",
    }
)

_PROVIDER_IDS = frozenset({"agy", "claude", "codex"})


def _normalize_provider_id(value: str) -> str:
    """v0.1 에서 agy 를 gemini 로 저장했던 설정을 읽기 호환한다."""
    return "agy" if value == "gemini" else value


def _normalize_provider_map(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized = {
        _normalize_provider_id(str(key)): item for key, item in value.items()
    }
    return normalized


_INT_KEYS = frozenset(
    {
        "max_file_size_bytes",
        "max_total_upload_bytes",
        "max_files_per_job",
        "max_inline_chars",
        "default_timeout_seconds",
        "max_concurrency_per_provider",
        "max_search_tool_calls",
    }
)

_LIMITS = {
    "max_file_size_bytes": (1024, 500 * 1024 * 1024),
    "max_total_upload_bytes": (1024, 2 * 1024 * 1024 * 1024),
    "max_files_per_job": (1, 200),
    "max_inline_chars": (1000, 5_000_000),
    "default_timeout_seconds": (10, 86_400),
    "max_concurrency_per_provider": (1, 8),
    "max_search_tool_calls": (1, 200),
}

# 0 이나 null 을 "제한 없음"으로 받는 키. 다른 한도와 달리 이 값은 안전 장치가
# 아니라 사용자가 스스로 걸어 두는 상한이라, 끄는 것을 허용한다. 끈다고 해서
# 무제한으로 보내지는 것은 아니다 — Provider 전송 한도(Provider.max_input_bytes)
# 와 모델 컨텍스트 한도는 그대로 남고, 그 둘은 사용자가 끌 수 없다.
_UNLIMITED_KEYS = frozenset({"max_inline_chars"})


def inline_char_budget(source: Any) -> int | None:
    """ARIA 자체 글자 수 한도. None 이면 제한 없음.

    설정 전체(dict)를 넘겨도 되고 그 키의 값만 넘겨도 된다. 0·null·정수 아닌
    값의 해석을 한 군데로 모은다 — 호출부마다 `or 800_000` 같은 기본값을 적어
    두면 "제한 없음"이 조용히 다른 숫자로 바뀐다.
    """
    raw = source.get("max_inline_chars") if isinstance(source, dict) else source
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def get_all(session: Session) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in session.query(AppSetting).all():
        # 폐기한 설정 키의 옛 행이 DB 에 남아 있을 수 있다. 지우지는 않고
        # 응답에서만 뺀다 — 사용자 데이터를 조용히 삭제하지 않는다.
        if row.key not in DEFAULTS:
            continue
        values[row.key] = row.value
    # 빈 값을 특정 Provider 로 채우지 않는다. 제한된 안전성 Provider 가 자동으로
    # 선택되면 사용자가 위험을 확인하지 않은 채 실행하게 된다.
    raw_default = str(values.get("default_provider") or "").strip()
    values["default_provider"] = (
        _normalize_provider_id(raw_default) if raw_default else ""
    )
    values["provider_paths"] = _normalize_provider_map(values.get("provider_paths"))
    values["default_models"] = _normalize_provider_map(values.get("default_models"))
    return values


def get(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    if row is None:
        return DEFAULTS.get(key)
    value = row.value
    if key == "default_provider":
        text = str(value).strip()
        return _normalize_provider_id(text) if text else ""
    if key in ("provider_paths", "default_models"):
        return _normalize_provider_map(value)
    return value


def _coerce(key: str, value: Any) -> Any:
    if key in _INT_KEYS:
        if key in _UNLIMITED_KEYS and (
            value is None or (isinstance(value, str) and not value.strip())
        ):
            return 0
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 는 정수여야 합니다.") from exc
        if key in _UNLIMITED_KEYS and number <= 0:
            # 0 = 제한 없음. 음수도 같은 뜻으로 받아 0 으로 정규화한다.
            return 0
        low, high = _LIMITS[key]
        if not low <= number <= high:
            if key in _UNLIMITED_KEYS:
                raise ValueError(
                    f"{key} 는 0(제한 없음)이거나 {low} 이상 {high} 이하여야 합니다."
                )
            raise ValueError(f"{key} 는 {low} 이상 {high} 이하여야 합니다.")
        return number
    if key in (
        "runtime_context_enabled",
        "keep_raw_output",
        "fail_on_tool_use",
        "kiwee_integration_enabled",
    ):
        return bool(value)
    if key == "runtime_context":
        return str(value)
    if key == "default_prompt_id":
        return str(value).strip()
    if key == "default_provider":
        text = str(value).strip()
        if not text:
            # 빈 값 = 기본 Provider 지정 안 함. 실행 시 직접 선택해야 한다.
            return ""
        provider_id = _normalize_provider_id(text)
        if provider_id not in _PROVIDER_IDS:
            raise ValueError(
                "default_provider 는 agy, claude, codex 중 하나이거나 "
                "빈 값이어야 합니다."
            )
        return provider_id
    if key in ("provider_paths", "default_models"):
        if not isinstance(value, dict):
            raise ValueError(f"{key} 는 객체여야 합니다.")
        return {
            _normalize_provider_id(str(k)): str(v)
            for k, v in value.items()
            if str(v).strip()
        }
    return value


def update(session: Session, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - EDITABLE_KEYS
    if unknown:
        raise ValueError(f"변경할 수 없는 설정입니다: {', '.join(sorted(unknown))}")

    for key, raw in changes.items():
        value = _coerce(key, raw)
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    session.flush()
    return get_all(session)


def warnings_for(values: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    if int(values.get("max_concurrency_per_provider", 1)) > 1:
        notes.append(
            "Provider 동시 실행이 2 이상입니다. 메모리 사용량이 늘고 계정 사용량 "
            "제한에 더 빨리 도달할 수 있습니다."
        )
    # 예전에는 "켜 둔 Provider 가 있는가"를 물었다. 사전 동의 관문을 걷어낸
    # 뒤로는 그 질문이 성립하지 않으므로, 지금 실제로 실행에 쓰이는 도구를 본다.
    selected = str(values.get("default_provider") or "")
    if selected in TOOL_UNCONTROLLABLE_PROVIDERS:
        notes.append(
            f"기본 실행 도구({selected})는 셸·파일 도구를 끄는 수단이 없습니다. "
            "ARIA 는 도구 호출을 탐지해 실패로 기록할 뿐 호출 자체를 막지 못하므로, "
            "신뢰할 수 없는 출처의 문서 분석에는 권장하지 않습니다."
        )
    if not values.get("runtime_context_enabled", True):
        notes.append(
            "런타임 컨텍스트가 비활성화되어 있습니다. 첨부 문서 안의 지시문이 "
            "실행 지시로 해석될 위험이 커집니다."
        )
    # 연동을 켜도 지금은 실제 검색이 안 된다는 사실을 화면에 정직하게 남긴다.
    # "URL 이 보인다"와 "공식 API 다"는 다른 문제이므로, 접속 구현은 공급자
    # 승인 뒤로 미뤄져 있다.
    kiwee = patent_search.describe(values)
    if kiwee.enabled and not kiwee.configured:
        notes.append(f"{kiwee.display_name} 연동: {kiwee.detail}")
    return notes
