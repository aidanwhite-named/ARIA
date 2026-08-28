"""AppSetting 읽기/쓰기.

기본값은 config.DEFAULTS 에 있고, DB 에는 사용자가 바꾼 것만 저장한다.
"""

from __future__ import annotations

import threading
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
        "epo_http_budget_seconds",
        "epo_hourly_quota_bytes",
        "epo_max_detail_fetches",
        "epo_max_search_calls",
        "epo_channel_timeout_seconds",
        # epo_quota_state 는 일부러 없다. ARIA 가 관측해 적는 값이라
        # 사용자가 PUT 으로 고칠 수 있으면 사용량을 0 으로 되돌릴 수 있다.
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
        "epo_http_budget_seconds",
        "epo_hourly_quota_bytes",
        "epo_max_detail_fetches",
        "epo_max_search_calls",
        "epo_channel_timeout_seconds",
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
    # OPS HTTP 대기 시간의 총합. 600초를 넘겨 잡을 이유가 없다 — 그보다 오래
    # 걸리는 것은 느린 것이 아니라 고장난 것이다.
    "epo_http_budget_seconds": (10, 600),
    # 0 = 관측만. 켤 때의 하한을 1MB 로 둔다. 그보다 작으면 첫 검색에서 바로
    # 막혀서 "설정했더니 아무것도 안 된다"가 된다.
    # 상한은 주간 계약량과 같은 값으로 맞춘다. 시간당 상한이 주간 한도보다
    # 클 수 있으면 그 설정은 아무것도 막지 못한다.
    "epo_hourly_quota_bytes": (1000 * 1000, patent_search.WEEKLY_QUOTA_BYTES),
    "epo_max_detail_fetches": (1, 50),
    "epo_max_search_calls": (1, 20),
    "epo_channel_timeout_seconds": (30, 900),
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
        # 0 = 시간당 사용량을 관측·표시만 하고 차단하지 않음(기본값). 주간
        # 한도는 계약값이라 여기 없다 — 사용자가 끌 수 있으면 안 된다.
        "epo_hourly_quota_bytes",
    }
)


# 외부 데이터 소스의 자격증명. Provider(AI 실행 도구)의 API Key 와는 다른
# 축이다 — 그쪽은 각 CLI 의 로그인 세션을 쓰므로 ARIA 가 받지 않는다. EPO OPS
# 는 CLI 가 없고 OAuth client_credentials 뿐이라 저장 외에 방법이 없다.
_CREDENTIAL_KEYS = frozenset({"epo_consumer_key", "epo_consumer_secret"})

# 사용량 경고를 띄우는 비율. epo_quota.WARN_RATIO 와 같은 값을 화면 문구에
# 쓰기 위한 백분율 표현이다.
WARN_PERCENT = int(patent_search.QUOTA_WARN_RATIO * 100)

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


# 사용량 누적을 직렬화한다. 두 실행이 같은 값을 읽고 각자 쓰면 한쪽 사용량이
# 사라지는데, 사용량이 사라지는 방향의 결함은 한도를 무력화한다.
#
# 프로세스 안에서만 보장한다. ARIA 는 로컬 단일 프로세스로 도는 앱이라 이것으로
# 충분하고, 여러 프로세스가 같은 DB 를 쓰는 배치는 지원 대상이 아니다. 그
# 전제가 깨지면 SQLite 수준의 즉시 잠금이 필요하다.
_EPO_QUOTA_LOCK = threading.Lock()

EPO_QUOTA_KEY = "epo_quota_state"


def merge_epo_quota(delta: dict) -> dict:
    """관측한 증분을 저장된 사용량에 **더한다**. 덮어쓰지 않는다.

    QuotaLedger.drain() 이 준 증분만 받는다. 현재 상태를 통째로 써 넣으면 두
    실행이 겹칠 때 나중 쪽이 앞쪽 사용량을 지운다.

    **자기 트랜잭션을 연다.** 호출자의 세션을 쓰지 않는 데는 두 가지 이유가
    있다.

      1. 사용량은 호출자의 트랜잭션에 속하지 않는다. 실행이 실패해서 롤백되어도
         바이트는 이미 나갔고 EPO 는 그만큼 과금한다. 같이 롤백되면 우리
         숫자만 줄어들어 한도가 느슨해진다.
      2. 잠금이 커밋까지 덮어야 한다. 잠금 안에서 flush 만 하고 커밋을 호출자에게
         맡기면, 잠금을 놓은 뒤 커밋 전에 다른 스레드가 읽어 옛 값 위에 쓴다.
         실측으로 재현했다 — 8스레드 × 25회에서 정확히 절반이 사라졌다.

    EDITABLE_KEYS 를 거치지 않는 유일한 쓰기다. 사용자가 PUT 으로 고칠 수
    있으면 사용량을 0 으로 되돌려 한도를 무력화할 수 있으므로, 이 키는 편집
    목록 밖에 두고 ARIA 의 관측 경로만 여기로 쓴다.
    """
    if not isinstance(delta, dict):
        return {}
    week = str(delta.get("week") or "")
    added_bytes = max(0, int(delta.get("local_bytes") or 0))
    added_requests = max(0, int(delta.get("requests") or 0))

    from .db import session_scope

    with _EPO_QUOTA_LOCK, session_scope() as session:
        row = session.get(AppSetting, EPO_QUOTA_KEY)
        stored = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        # 주가 바뀌었으면 이전 주 누적은 이월하지 않는다.
        if str(stored.get("week") or "") != week:
            stored = {"week": week, "local_bytes": 0, "requests": 0}

        merged = {
            "week": week,
            "local_bytes": max(0, int(stored.get("local_bytes") or 0)) + added_bytes,
            "requests": max(0, int(stored.get("requests") or 0)) + added_requests,
            # OPS 가 알려준 값은 누적이 아니라 관측이다. 그쪽이 이미 누적치이므로
            # 더하면 두 배가 된다. 대신 큰 쪽을 남긴다 — 주중에 줄어들 값이
            # 아니고, 작은 값으로 덮이면 한도가 느슨해진다.
            "ops_weekly_bytes": _max_or_none(
                stored.get("ops_weekly_bytes"), delta.get("ops_weekly_bytes")
            ),
            # 시간당은 한 시간마다 0 으로 돌아가므로 최신 관측을 그대로 쓴다.
            "ops_hourly_bytes": (
                delta.get("ops_hourly_bytes")
                if delta.get("ops_hourly_bytes") is not None
                else stored.get("ops_hourly_bytes")
            ),
            "throttle": delta.get("throttle") or stored.get("throttle") or {},
            "observed_at": delta.get("observed_at") or stored.get("observed_at") or "",
        }
        if row is None:
            session.add(AppSetting(key=EPO_QUOTA_KEY, value=merged))
        else:
            row.value = merged
        session.flush()
        # session_scope 가 이 블록을 벗어나며 커밋한다. 잠금은 그 뒤에 풀린다.
    return merged


def _max_or_none(left, right):
    values = [int(v) for v in (left, right) if v is not None]
    return max(values) if values else None


# 프로세스 전역 사용량 원장. **쿼터가 하나이므로 원장도 하나다.**
#
# 백엔드마다 원장을 따로 두면 각자 자기가 처음 읽은 스냅샷 위에서만 판정한다.
# 실측으로 재현했다 — 전역 잔량이 100바이트인데 두 백엔드가 모두 8MB 요청
# 검사를 통과했다. 바이트가 사라지지 않는 것과 한도가 지켜지는 것은 다른
# 문제다.
#
# DB 에 예약을 적는 방식(2단계 예약 프로토콜)은 쓰지 않는다. 프로세스가 예약과
# 정산 사이에 죽으면 쓰지도 않은 8MB 가 저장소에 굳고, 그것을 걷어내려면 만료·
# 회수 장치가 또 필요하다. ARIA 는 로컬 단일 프로세스 앱이므로, 예약은 메모리에
# 두고 **실제로 쓴 바이트만** 저장한다. 죽으면 예약은 프로세스와 함께 사라지고
# 저장소에는 진짜 사용량만 남는다 — 스스로 복구되는 쪽이다.
_EPO_LEDGER = None


def epo_ledger(session: Session):
    """프로세스 전역 원장을 돌려준다. 저장된 값과 맞춘 뒤 준다.

    잠금 순서를 지킨다. 저장소 잠금(_EPO_QUOTA_LOCK)은 **싱글턴 자리를 정하는
    동안만** 쥐고, 원장 잠금을 잡는 동기화는 그 밖에서 한다. 안쪽에서 하면
    이렇게 된다.

        이쪽:   저장소 잠금 → (동기화) → 원장 잠금
        저쪽:   원장 잠금 → (저장 콜백) → 저장소 잠금

    두 방향이 동시에 일어나면 서로를 기다리며 멈춘다(AB-BA 교착).
    """
    global _EPO_LEDGER
    values = get_all(session)
    stored = patent_search.QuotaState.from_dict(values.get(EPO_QUOTA_KEY))
    hourly = int(values.get("epo_hourly_quota_bytes") or 0)

    with _EPO_QUOTA_LOCK:
        ledger = _EPO_LEDGER

    if ledger is None:
        # 생성도 잠금 밖에서 한다. 생성자가 원장 잠금을 건드리므로, 저장소
        # 잠금을 쥔 채 만들면 그 자체가 역전 경로가 된다.
        candidate = patent_search.QuotaLedger(state=stored, hourly_limit=hourly)
        candidate.on_change = lambda _state: persist_epo_quota(candidate)
        with _EPO_QUOTA_LOCK:
            if _EPO_LEDGER is None:
                _EPO_LEDGER = candidate
            ledger = _EPO_LEDGER

    # 여기서부터는 저장소 잠금을 쥐고 있지 않다. 원장이 스스로 잠근다.
    ledger.hourly_limit = hourly
    ledger.sync_from_stored(stored)
    return ledger


def reset_epo_ledger() -> None:
    """전역 원장을 버린다. 테스트에서만 쓴다."""
    global _EPO_LEDGER, _EPO_PERSIST_ERROR
    with _EPO_QUOTA_LOCK:
        _EPO_LEDGER = None
        _EPO_PERSIST_ERROR = ""


# peek → merge → ack 한 벌을 직렬화한다.
#
# 저장 콜백은 원장 잠금 **밖에서** 돈다(교착을 피하려고 그렇게 했다). 그래서
# 두 콜백이 겹칠 수 있고, 겹치면 같은 바이트를 두 번 저장한다.
#
#     A: peek → 증분 1
#     B: peek → 증분 2   (A 가 아직 ack 하지 않아 A 것까지 다시 보인다)
#     둘 다 저장 → DB 3, 실제 2
#
# 실측으로 재현했다. 덜 세는 게 아니라 **더 세는** 결함이라 조용하다 — 한도를
# 일찍 소진시켜 멀쩡한 검색을 차단하고, pending 이 0 이라 화면에도 안 보인다.
#
# 잠금 순서: 이 잠금은 **항상 가장 바깥**이다.
#
#     _EPO_PERSIST_LOCK → (peek: 원장 잠금) → (merge: 저장소 잠금) → (ack: 원장 잠금)
#
# 원장 잠금과 저장소 잠금을 겹쳐 쥐지 않으므로 예전 AB-BA 교착은 돌아오지
# 않는다. 이 성질은 "저장 콜백은 원장 잠금 밖에서 부른다"에 기대고 있고,
# test_persist_callback_runs_outside_the_ledger_lock 이 그것을 지킨다.
_EPO_PERSIST_LOCK = threading.Lock()

# 마지막 저장 실패. 화면과 경고가 이것을 보고 알린다.
_EPO_PERSIST_ERROR = ""


def epo_persist_error() -> str:
    return _EPO_PERSIST_ERROR


def persist_epo_quota(ledger) -> None:
    """아직 저장되지 않은 증분을 저장하고, **성공했을 때만** 눈금을 옮긴다.

    peek → 저장 → ack 순서다. 저장 전에 눈금을 옮기면 저장이 실패했을 때 그
    증분을 다시 낼 수 없다(실측: 123바이트가 사라졌다).

    저장이 실패해도 **예외를 올리지 않는다.** 이 함수는 응답을 받은 직후
    HTTP 전송 한가운데서 불린다. 여기서 터지면 사용자는 "EPO 에 접속하지
    못했습니다" 대신 DB 오류를 보게 되고, 진짜 원인이 가려진다.

    대신 두 가지로 안전을 지킨다.

      - 증분은 ledger 안에 pending 으로 남는다. 다음 저장이 성공하면 함께
        올라가므로 사라지지 않는다.
      - 그동안에도 한도는 지켜진다. pending 은 ledger.state.local_bytes 에
        이미 들어 있어 check() 가 그것을 포함해 판정한다.
      - 실패 사실은 화면 경고와 사용량 스냅샷에 드러난다. 조용히 넘기지 않는다.
    """
    global _EPO_PERSIST_ERROR
    # peek 과 ack 사이에 다른 저장이 끼어들면 같은 증분을 두 번 적는다.
    # 셋을 한 잠금 안에 둔다. 원장 잠금은 peek/ack 안에서 잠깐씩만 잡히고
    # 여기서는 쥐고 있지 않으므로 저장소 잠금과 겹치지 않는다.
    with _EPO_PERSIST_LOCK:
        delta = ledger.peek_delta()
        nothing_new = (
            not delta.get("local_bytes")
            and not delta.get("requests")
            and delta.get("ops_weekly_bytes") is None
            and delta.get("ops_hourly_bytes") is None
        )
        if nothing_new:
            return
        try:
            merge_epo_quota(delta)
        except Exception as exc:  # noqa: BLE001 - 어떤 저장 오류든 같은 처리다
            _EPO_PERSIST_ERROR = f"{type(exc).__name__}: {exc}"
            return
        ledger.ack(delta)
        _EPO_PERSIST_ERROR = ""


def epo_backend_for(session: Session):
    """설정에서 EPO 백엔드를 만들고 전역 원장·사용량 저장을 묶어 준다.

    검색 가능한 EPO 백엔드를 만드는 **유일한 지원 경로**다. 직접
    EpoOpsBackend() 를 만들면 사용량이 그 객체 메모리에만 쌓이다 사라지고,
    한도도 자기 스냅샷으로만 본다 — 실제로 둘 다 그렇게 되어 있었다.

    응답 하나마다 즉시 누적한다. 실행이 끝날 때 한 번 저장하면, 중간에 끊긴
    실행이 쓴 사용량이 통째로 사라진다.
    """
    values = get_all(session)
    backend = patent_search.EpoOpsBackend()
    backend.configure(values)
    backend.use_ledger(epo_ledger(session))
    return backend


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
    if key == "model_context_tokens":
        if not isinstance(value, dict):
            raise ValueError("model_context_tokens 는 객체여야 합니다.")
        normalized: dict[str, int] = {}
        low, high = _LIMITS["unknown_model_context_tokens"]
        for raw_key, raw_value in value.items():
            model_key = str(raw_key).strip()
            if not model_key:
                raise ValueError("model_context_tokens 의 모델 이름은 비어 있을 수 없습니다.")
            if isinstance(raw_value, bool):
                raise ValueError(
                    f"model_context_tokens[{model_key}] 는 정수여야 합니다."
                )
            try:
                context_tokens = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"model_context_tokens[{model_key}] 는 정수여야 합니다."
                ) from exc
            if not low <= context_tokens <= high:
                raise ValueError(
                    f"model_context_tokens[{model_key}] 는 {low} 이상 {high} "
                    "이하여야 합니다."
                )
            normalized[model_key] = context_tokens
        return normalized
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


def _validate_model_token_budgets(values: dict[str, Any]) -> None:
    """컨텍스트보다 예약이 크거나 같은 설정을 저장하지 않는다.

    이 조합을 입력 예산 0으로만 정규화하면 호출부가 0을 「예산 없음」으로 읽어
    전체 입력을 보내는 안전장치 역전이 생긴다. 저장 단계와 실행 단계가 모두
    방어하지만, 사용자가 가장 빨리 원인을 알 수 있는 곳은 설정 저장이다.
    """

    reserve = int(values.get("model_output_reserve_tokens") or 0)
    fallback = int(values.get("unknown_model_context_tokens") or 0)
    if reserve >= fallback:
        raise ValueError(
            "model_output_reserve_tokens 는 unknown_model_context_tokens 보다 "
            "작아야 합니다. 같거나 크면 모델 입력 예산이 0이 됩니다."
        )

    overrides = values.get("model_context_tokens") or {}
    if not isinstance(overrides, dict):
        raise ValueError("model_context_tokens 는 객체여야 합니다.")
    invalid = [
        str(model)
        for model, context in overrides.items()
        if reserve >= int(context)
    ]
    if invalid:
        raise ValueError(
            "model_output_reserve_tokens 는 다음 모델의 컨텍스트 토큰보다 "
            f"작아야 합니다: {', '.join(sorted(invalid))}."
        )


def update(session: Session, changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - EDITABLE_KEYS
    if unknown:
        raise ValueError(f"변경할 수 없는 설정입니다: {', '.join(sorted(unknown))}")

    coerced = {key: _coerce(key, raw) for key, raw in changes.items()}
    model_budget_keys = {
        "model_context_tokens",
        "model_output_reserve_tokens",
        "unknown_model_context_tokens",
    }
    if model_budget_keys.intersection(coerced):
        prospective = get_all(session)
        prospective.update(coerced)
        _validate_model_token_budgets(prospective)

    for key, value in coerced.items():
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
    notes.extend(_epo_quota_notes(values))
    return notes


def epo_quota_snapshot(values: dict[str, Any]) -> dict:
    """화면에 그릴 사용량. 관측값과 한도를 한 번에 준다.

    values 의 epo_quota_state 는 날것이라 화면이 직접 쓰기 어렵다. 남은 양과
    경고 여부를 여기서 한 번만 계산해서, 화면과 경고 문구가 같은 숫자를 보게
    한다.
    """
    ledger = patent_search.QuotaLedger(
        state=patent_search.QuotaState.from_dict(values.get("epo_quota_state")),
        hourly_limit=int(values.get("epo_hourly_quota_bytes") or 0),
    )
    snapshot = ledger.snapshot()
    # 살아 있는 전역 원장이 있으면 그쪽이 최신이다. 저장에 실패해 아직 DB 에
    # 못 올라간 증분도 여기서 보인다.
    live = _EPO_LEDGER
    if live is not None:
        snapshot = live.snapshot()
    snapshot["persist_error"] = _EPO_PERSIST_ERROR
    return snapshot


def _epo_quota_notes(values: dict[str, Any]) -> list[str]:
    """사용량 경고. 한도에 닿기 **전에** 말해야 쓸모가 있다.

    OPS 는 요청 수가 아니라 데이터량으로 과금되므로, 사용자가 "몇 번 돌렸나"로
    남은 양을 짐작할 수 없다. 그래서 숫자를 직접 보여 준다.
    """
    if not values.get(patent_search.EPO_SETTING_ENABLED, False):
        return []
    ledger = patent_search.QuotaLedger(
        state=patent_search.QuotaState.from_dict(values.get("epo_quota_state")),
        hourly_limit=int(values.get("epo_hourly_quota_bytes") or 0),
    )
    notes: list[str] = []
    if _EPO_PERSIST_ERROR:
        notes.append(
            "EPO 사용량을 저장하지 못했습니다: "
            f"{_EPO_PERSIST_ERROR}. 사용량은 메모리에 남아 있고 한도는 계속 "
            "지켜지지만, 프로그램을 다시 시작하면 그만큼이 사라집니다."
        )
    if ledger.state.throttle.dangerous:
        notes.append(
            "EPO OPS 가 과부하 상태를 보고했습니다"
            f"({ledger.state.throttle.raw}). 지금은 EPO 채널이 중단됩니다."
        )
    used = ledger.state.effective_weekly_bytes
    if ledger.weekly_limit and used >= ledger.weekly_limit:
        notes.append(
            f"이번 주 EPO OPS 사용량이 한도에 도달했습니다"
            f"({used / 1024 / 1024:,.0f} MB / "
            f"{ledger.weekly_limit / 1024 / 1024 / 1024:.0f} GB). "
            "다음 주까지 EPO 검색을 사용할 수 없습니다."
        )
    elif ledger.warn:
        notes.append(
            f"이번 주 EPO OPS 사용량이 한도의 {WARN_PERCENT}% 를 넘었습니다"
            f"({used / 1024 / 1024:,.0f} MB / "
            f"{ledger.weekly_limit / 1024 / 1024 / 1024:.0f} GB)."
        )
    return notes
