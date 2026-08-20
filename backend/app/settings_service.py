"""AppSetting 읽기/쓰기.

기본값은 config.DEFAULTS 에 있고, DB 에는 사용자가 바꾼 것만 저장한다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .config import DEFAULTS
from .models import AppSetting

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
        "enabled_experimental_providers",
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
    }
)

_LIMITS = {
    "max_file_size_bytes": (1024, 500 * 1024 * 1024),
    "max_total_upload_bytes": (1024, 2 * 1024 * 1024 * 1024),
    "max_files_per_job": (1, 200),
    "max_inline_chars": (1000, 5_000_000),
    "default_timeout_seconds": (10, 86_400),
    "max_concurrency_per_provider": (1, 8),
}


def get_all(session: Session) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in session.query(AppSetting).all():
        values[row.key] = row.value
    # 빈 값을 특정 Provider 로 채우지 않는다. 실험적 Provider 가 자동으로
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
    if key == "enabled_experimental_providers":
        if not isinstance(value, list):
            raise ValueError("enabled_experimental_providers 는 배열이어야 합니다.")
        return [str(v) for v in value]
    if key in ("provider_paths", "default_models"):
        return _normalize_provider_map(value)
    return value


def _coerce(key: str, value: Any) -> Any:
    if key in _INT_KEYS:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 는 정수여야 합니다.") from exc
        low, high = _LIMITS[key]
        if not low <= number <= high:
            raise ValueError(f"{key} 는 {low} 이상 {high} 이하여야 합니다.")
        return number
    if key in ("runtime_context_enabled", "keep_raw_output", "fail_on_tool_use"):
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
    enabled = values.get("enabled_experimental_providers") or []
    if enabled:
        notes.append(
            f"실험적 Provider 가 활성화되어 있습니다: {', '.join(enabled)}. "
            "도구를 끌 수 없어 신뢰할 수 없는 문서 분석에는 권장하지 않습니다."
        )
    if not values.get("runtime_context_enabled", True):
        notes.append(
            "런타임 컨텍스트가 비활성화되어 있습니다. 첨부 문서 안의 지시문이 "
            "실행 지시로 해석될 위험이 커집니다."
        )
    return notes
