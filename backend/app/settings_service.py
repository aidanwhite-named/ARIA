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
        "retrieval_mode",
        "retrieval_max_rounds",
        "retrieval_max_page_reads",
        "retrieval_evidence_chars",
        "retrieval_hits_per_document",
        "retrieval_semantic_enabled",
        "kiwee_integration_enabled",
        "epo_integration_enabled",
        "epo_consumer_key",
        "epo_consumer_secret",
            # 근거 패키지의 페이지 확장.
        "retrieval_neighbor_pages",
        # 모델 컨텍스트 기반 입력 예산. Provider 전송 하드 한도는 여기에 없다 —
        # 사용자가 끌 수 없는 값이기 때문이다.
        "model_context_tokens",
        "model_output_reserve_tokens",
        "unknown_model_context_tokens",
        "embedding_cache_max_mb",
        # 사건 규모 품질 기준.
        "delivery_scale_documents",
        "delivery_scale_pages",
        "delivery_scale_claim_elements",
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
        "retrieval_max_rounds",
        "retrieval_max_page_reads",
        "retrieval_evidence_chars",
        "retrieval_hits_per_document",
        "retrieval_neighbor_pages",
        "model_output_reserve_tokens",
        "unknown_model_context_tokens",
        "embedding_cache_max_mb",
        "delivery_scale_documents",
        "delivery_scale_pages",
        "delivery_scale_claim_elements",
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
    "retrieval_max_rounds": (1, 30),
    "retrieval_max_page_reads": (1, 500),
    "retrieval_evidence_chars": (2_000, 400_000),
    "retrieval_hits_per_document": (1, 20),
    "retrieval_neighbor_pages": (0, 5),
    "model_output_reserve_tokens": (1_000, 500_000),
    "unknown_model_context_tokens": (10_000, 5_000_000),
    "embedding_cache_max_mb": (16, 100_000),
    "delivery_scale_documents": (1, 200),
    "delivery_scale_pages": (1, 100_000),
    "delivery_scale_claim_elements": (1, 200),
}

# 인용발명 문헌 전달 방식. enums.RetrievalMode 와 같은 값이며, 여기서 import
# 하지 않는 이유는 settings_service 가 enums 에 의존하지 않기 때문이다.
_RETRIEVAL_MODES = ("auto", "full", "retrieval")

# 폐기한 전달 방식 값 → 지금의 어느 값으로 읽을 것인가.
#
# focused 는 「페이지 단위로 담아라」였고, 그 동작은 지금 retrieval 의 근거
# 패키지 안에 들어가 있다(retrieval.pages). auto 로 되돌리면 사용자가 명시적으로
# 좁혀 두었던 설정이 조용히 넓어지므로 retrieval 로 옮긴다.
_RETIRED_RETRIEVAL_MODES = {"focused": "retrieval"}

# 0 이나 null 을 "제한 없음"으로 받는 키. 다른 한도와 달리 이 값은 안전 장치가
# 아니라 사용자가 스스로 걸어 두는 상한이라, 끄는 것을 허용한다. 끈다고 해서
# 무제한으로 보내지는 것은 아니다 — Provider 전송 한도(Provider.max_input_bytes)
# 와 모델 컨텍스트 한도는 그대로 남고, 그 둘은 사용자가 끌 수 없다.
_UNLIMITED_KEYS = frozenset(
    {
        "max_inline_chars",
        # 사건 규모 품질 기준은 전부 0 = 쓰지 않음을 받는다. 이 값들은 전송
        # 한도가 아니라 "이 정도면 좁혀 읽는 편이 낫다"는 판단이므로, 끄는
        # 선택이 있어야 한다. 화면도 「0 = 사용 안 함」이라고 안내한다 — 둘이
        # 어긋나면 사용자가 안내대로 0 을 넣었을 때 저장이 거절된다.
        "delivery_scale_documents",
        "delivery_scale_pages",
        "delivery_scale_claim_elements",
        # 캐시 정리도 끌 수 있어야 한다. 0 = 정리하지 않음.
        "embedding_cache_max_mb",
    }
)


# 외부 데이터 소스의 자격증명. Provider(AI 실행 도구)의 API Key 와는 다른
# 축이다 — 그쪽은 각 CLI 의 로그인 세션을 쓰므로 ARIA 가 받지 않는다. EPO OPS
# 는 CLI 가 없고 OAuth client_credentials 뿐이라 저장 외에 방법이 없다.
_CREDENTIAL_KEYS = frozenset({"epo_consumer_key", "epo_consumer_secret"})

# 응답에서 값 자체를 내보내지 않는 키. 화면에는 "설정됨/미설정"만 준다.
SECRET_KEYS = frozenset({"epo_consumer_secret"})

# OPS 자격증명은 base64 로 안전하게 실릴 수 있는 짧은 문자열이다. 상한을 두는
# 이유는 실수로 파일 내용이나 로그를 통째로 붙여 넣는 것을 막기 위해서다.
_CREDENTIAL_MAX_LEN = 256


def _coerce_credential(key: str, value: Any) -> str:
    """자격증명 문자열을 정리한다. 빈 값은 '지움'이라는 정상 입력이다."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if len(text) > _CREDENTIAL_MAX_LEN:
        raise ValueError(f"{key} 는 {_CREDENTIAL_MAX_LEN}자 이하여야 합니다.")
    # 복사·붙여넣기로 딸려 들어온 공백·줄바꿈은 Basic 인증 헤더를 조용히
    # 망가뜨린다. 잘라내지 말고 거절해서 사용자가 알아채게 한다.
    if any(ch.isspace() for ch in text):
        raise ValueError(f"{key} 에 공백이나 줄바꿈이 들어 있습니다.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError(f"{key} 에 사용할 수 없는 문자가 들어 있습니다.")
    return text


def secrets_set(values: dict[str, Any]) -> dict[str, bool]:
    """비밀 값이 저장되어 있는가. 화면이 상태를 그릴 유일한 근거."""
    return {key: bool(str(values.get(key) or "").strip()) for key in sorted(SECRET_KEYS)}


def redact_for_api(values: dict[str, Any]) -> dict[str, Any]:
    """API 응답에서 비밀 값을 지운다.

    저장은 하되 돌려주지는 않는다. 앞 몇 글자를 남기는 절충도 하지 않는다 —
    부분 노출은 보안상 이득이 없고, 화면이 그 조각을 편집 초안으로 되쓰면
    사용자가 저장을 누르는 순간 잘린 값이 진짜 값을 덮어쓴다.
    """
    redacted = dict(values)
    for key in SECRET_KEYS:
        if key in redacted:
            redacted[key] = ""
    return redacted


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
        "retrieval_semantic_enabled",
        "kiwee_integration_enabled",
        "epo_integration_enabled",
    ):
        return bool(value)
    if key in _CREDENTIAL_KEYS:
        return _coerce_credential(key, value)
    if key == "retrieval_mode":
        text = str(value).strip().lower()
        # 폐기한 값은 뜻이 가장 가까운 쪽으로 옮긴다. 거절하면 그 값이 저장된
        # 기존 설정에서 화면이 열리지 않는다.
        text = _RETIRED_RETRIEVAL_MODES.get(text, text)
        if text not in _RETRIEVAL_MODES:
            raise ValueError(
                "retrieval_mode 는 "
                + ", ".join(_RETRIEVAL_MODES)
                + " 중 하나여야 합니다."
            )
        return text
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
    mode = str(values.get("retrieval_mode") or "auto")
    if mode == "full":
        notes.append(
            "인용발명 전달 방식이 「전체 인라인 고정」입니다. Provider 전송 "
            "한도를 넘는 문헌은 로컬 검색으로 넘어가지 않고 INPUT_TOO_LARGE 로 "
            "거절됩니다."
        )
    elif mode == "retrieval":
        notes.append(
            "인용발명 전달 방식이 「로컬 검색 고정」입니다. 작은 문헌도 전체 "
            "본문 대신 근거 패키지만 전달되므로, 검색어에 걸리지 않은 구간은 "
            "최종 분석 모델이 보지 못합니다."
        )
    if values.get("retrieval_semantic_enabled"):
        notes.append(
            "의미 검색이 켜져 있습니다. sentence-transformers 와 모델 캐시가 "
            "없으면 키워드 검색만으로 진행하며, 그 사실이 보고서와 실행 기록에 "
            "남습니다."
        )
    # 연동을 켜도 지금은 실제 검색이 안 된다는 사실을 화면에 정직하게 남긴다.
    # "URL 이 보인다"와 "공식 API 다"는 다른 문제이므로, 접속 구현은 공급자
    # 승인 뒤로 미뤄져 있다.
    for status in patent_search.describe_all(values):
        if status.enabled and not status.configured:
            notes.append(f"{status.display_name} 연동: {status.detail}")
    return notes
