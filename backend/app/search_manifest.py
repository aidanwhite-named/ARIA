"""검색 감사 기록 프로토콜.

사람이 읽는 Markdown 보고서와 별개로, 이번 검색이 실제로 무엇을 했는지 기계가
읽을 수 있는 형태로 남긴다. citation_mapping 과 같은 방식이다. 보고서 표를
파싱하지 않고 버전이 붙은 전용 블록만 읽는다.

    [ARIA_SEARCH_LOG_V1]
    {"rounds": [...], "candidates": [...], "access_failures": [...]}
    [/ARIA_SEARCH_LOG_V1]

기록은 두 층으로 나뉜다.

  모델이 보고한 것 : 검색 라운드, 후보 목록, 접근 실패
  ARIA 가 관측한 것 : 실제 도구 호출 이름·시각·검색어·성공 여부, 청구항,
                      검색 프롬프트 해시, 실행 시각

둘을 섞지 않는다. 모델의 자기 보고와 ARIA 가 스트림에서 직접 본 것은 증거
등급이 다르다. `observed` 아래 있는 것만 ARIA 가 보증한다.

증거 등급 강등
--------------
WebFetch 는 페이지 원문을 그대로 주지 않는다. 페이지를 변환한 뒤 별도의 작은
모델이 추출한 결과를 돌려준다. 그러므로 web 채널에서 얻은 후보에는
`raw_original_verified` 를 부여할 근거가 존재하지 않는다. 모델이 그렇게
보고해도 ARIA 가 `webfetch_summary` 로 내리고, 무엇을 내렸는지 기록에 남긴다.

이건 취향이 아니라 이 기능의 안전 요건이다. 요약문이 원문 인용으로 승격되는
순간 보고서는 "이 특허가 이렇게 적혀 있다"는 거짓 진술을 담게 된다.

채널
----
`channel` 은 이 후보를 어느 검색 경로로 얻었는지다. 이번 단계는 web 뿐이지만
필드를 지금 넣어 둔다. 나중에 EPO 나 업로드 문헌이 붙어도 스키마를 갈아엎지
않고 같은 배열에 다른 채널의 후보를 넣을 수 있어야 한다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# 프롬프트가 메타데이터에 선언해야 이 기능이 켜진다.
CAPABILITY = "similarity_search_v1"

MANIFEST_VERSION = 10
_OPEN = "[ARIA_SEARCH_LOG_V1]"
_CLOSE = "[/ARIA_SEARCH_LOG_V1]"

# 구분자는 반드시 독립된 줄이어야 한다. 모델이 보고서 앞에서
# ``[ARIA_SEARCH_LOG_V1] ... [/ARIA_SEARCH_LOG_V1] 형식으로 쓰겠습니다``라고
# 설명할 때 그 인라인 예시까지 블록으로 세면, 실제 JSON 블록 하나를 정상적으로
# 냈는데도 "2개"로 오판한다. 줄 시작/끝을 고정하되 들여쓰기와 코드펜스는
# 허용한다.
_BLOCK = re.compile(
    r"(?:^[ \t]*```[\w-]*[ \t]*\r?\n)?"
    r"^[ \t]*"
    + re.escape(_OPEN)
    + r"[ \t]*\r?\n"
    r"(?P<payload>.*?)"
    r"^[ \t]*"
    + re.escape(_CLOSE)
    + r"[ \t]*(?:\r?\n|$)"
    r"(?:^[ \t]*```[ \t]*(?:\r?\n|$))?",
    re.DOTALL | re.MULTILINE,
)

# 검색 채널. 값 자체는 후보 항목에 남으므로 채널별 집계가 가능하다.
CHANNEL_WEB = "web"
# ARIA 가 특허 DB 백엔드를 직접 호출해 만든 후보. 모델이 보고할 수 없다.
CHANNEL_PATENT_DB = "patent_db"

# 채널 허용 목록은 둘로 나뉜다. 나누지 않으면, patent_db 를 모델 보고에
# 허용하는 순간 모델이 웹에서 찾은 후보에 channel: patent_db 라고 적어도
# 통과한다. 증거 등급은 강등되더라도 채널 라벨은 남으므로 채널별 집계가
# 오염된다. 그래서 '모델이 주장할 수 있는 채널'과 'ARIA 가 붙이는 채널'을
# 처음부터 다른 목록으로 둔다.
#
# 모델 보고에서 허용하는 채널. 여기 없는 값은 web 으로 강제된다.
MODEL_REPORTED_CHANNELS = (CHANNEL_WEB,)
# ARIA 의 신뢰된 생산자만 붙일 수 있는 채널. 모델 보고 경로는 이 목록을
# 절대 참조하지 않는다.
ARIA_PRODUCED_CHANNELS = (CHANNEL_PATENT_DB,)

# 감사 기록에 나타날 수 있는 모든 채널.
KNOWN_CHANNELS = MODEL_REPORTED_CHANNELS + ARIA_PRODUCED_CHANNELS

PROV_SNIPPET = "search_snippet"
PROV_WEBFETCH = "webfetch_summary"
PROV_RAW = "raw_original_verified"
# ARIA 가 OPS 를 직접 불러 받은 응답에서 후보를 만들었다. 모델이 주장할 수 없는
# 값이다 — 이 값이 붙는 유일한 경로는 epo_agent 가 돌린 검색이고, 그 응답은
# 아티팩트로 보존되어 있다. "원문 대조 완료"와 다르다: 응답을 받아 보존했다는
# 뜻이지, 그 안의 문장이 특허 원문의 직접 인용이라는 뜻이 아니다.
PROV_OFFICIAL_RESPONSE = "official_record_response"
MODEL_REPORTED_PROVENANCE = (PROV_SNIPPET, PROV_WEBFETCH, PROV_RAW)
ARIA_PRODUCED_PROVENANCE = (PROV_OFFICIAL_RESPONSE,)
PROVENANCE = MODEL_REPORTED_PROVENANCE + ARIA_PRODUCED_PROVENANCE

EVIDENCE_CANDIDATE = "candidate_only"
EVIDENCE_REVIEWED = "source_page_reviewed"
# ARIA 가 서버에서 공식 문헌(EPO OPS)을 직접 받아 아티팩트로 보존하고, 그
# 아티팩트에 대조가 끝난 후보. 채널과 같은 이유로 목록을 둘로 나눈다 — 모델
# 보고에 이 값을 허용하는 순간, 열어 보지도 않은 후보가 "공식 문헌 확인됨"
# 으로 보고서에 실린다.
EVIDENCE_OFFICIAL = "official_record_verified"
MODEL_REPORTED_EVIDENCE_STATUS = (EVIDENCE_CANDIDATE, EVIDENCE_REVIEWED)
ARIA_PRODUCED_EVIDENCE_STATUS = (EVIDENCE_OFFICIAL,)
EVIDENCE_STATUS = MODEL_REPORTED_EVIDENCE_STATUS + ARIA_PRODUCED_EVIDENCE_STATUS

GROUPS = ("A", "B", "C")

# A/B/C 값과 그 값을 뒷받침하는 근거는 별개의 축이다. ``group`` 하나만
# 저장하면 페이지 본문을 본 분류와 공식 문헌을 대조한 분류가 화면에서 같은
# 것으로 보인다. 또한 과거 매니페스트의 group 을 새 기준의 정식 분류로
# 오인하기 쉽다. 아래 값은 ARIA 가 저장된 관측 필드로 계산하며 모델이 고르지
# 않는다.
CLASSIFICATION_NONE = "none"
CLASSIFICATION_LEGACY = "legacy_unknown"
CLASSIFICATION_SEARCH = "search_result"
CLASSIFICATION_PAGE = "page_observed"
CLASSIFICATION_OFFICIAL = "official_record"
CLASSIFICATION_ORIGINAL = "original_text"
CLASSIFICATION_BASES = (
    CLASSIFICATION_NONE,
    CLASSIFICATION_LEGACY,
    CLASSIFICATION_SEARCH,
    CLASSIFICATION_PAGE,
    CLASSIFICATION_OFFICIAL,
    CLASSIFICATION_ORIGINAL,
)

# 후보별 검증 상태. 검색 전체의 실패 사유만 남기면 일부 후보만 OPS 조회에
# 실패한 실행을 설명할 수 없다. 모든 새 후보는 이 객체를 가지며, 공식 검증
# 단계가 진행될 때 후보마다 상태와 사유가 갱신된다.
VERIFY_NOT_ATTEMPTED = "not_attempted"
VERIFY_FETCH_FAILED = "fetch_failed"
VERIFY_RECORD_FETCHED = "record_fetched"
VERIFY_CLASSIFICATION_FAILED = "classification_failed"
VERIFY_EVIDENCE_MISMATCH = "evidence_mismatch"
VERIFY_PROMOTED = "promoted"
VERIFICATION_STATUSES = (
    VERIFY_NOT_ATTEMPTED,
    VERIFY_FETCH_FAILED,
    VERIFY_RECORD_FETCHED,
    VERIFY_CLASSIFICATION_FAILED,
    VERIFY_EVIDENCE_MISMATCH,
    VERIFY_PROMOTED,
)

# 그룹 정의의 단일 출처.
#
# 이 표는 세 곳에 흩어져 있었다. prompt/search_prompt.md 가 모델에게 뜻을
# 알려주고, search_report 가 Markdown 제목을 찍고, SearchManifestView 가 감사
# 패널 제목을 찍었다. 2026-08-25 10:14 실행에서 실제로 어긋났다 — 모델은
# 프롬프트 정의대로 분류했는데 보고서는 B 와 C 제목을 바꿔 인쇄했다. 라벨만
# 낡은 게 아니라 분류가 뒤집혀 보였고, 부분 대응으로 분류된 후보가 "핵심 특징이
# 강하게 유사"로 표시됐다.
#
# 그래서 정의를 여기 한 곳에 두고 매니페스트에 실어 보낸다. 렌더러는 자기 표를
# 갖지 않고 기록에 적힌 정의를 인쇄한다. 정의가 없는 옛 매니페스트만 렌더러의
# fallback 을 쓴다.
GROUP_SCHEMA_VERSION = 1
GROUP_DEFINITIONS: dict[str, str] = {
    "A": "전체 구조와 핵심 특징이 모두 강하게 유사",
    "B": "전체 구조는 다르지만 핵심 특징 또는 핵심 관계가 강하게 유사",
    "C": "전체 구조는 유사하지만 핵심 대응은 부분적",
}

DOC_TYPES = ("patent", "paper", "other")

# 청구항 구성 대응 정도. 검색 프롬프트가 요구하는 네 단계와 같다.
DEGREE_UNKNOWN = "확인되지 않음"
DEGREES = ("강한 대응", "부분 대응", "관련은 있으나 다름", DEGREE_UNKNOWN)

# 행별 근거의 출처. 후보 단위 provenance 와 다른 축이다.
#
# 후보 단위 provenance 는 "이 문헌을 무엇으로 알게 되었나"이고, 여기 있는 것은
# "이 행의 대응 주장을 무엇을 읽고 썼나"이다. 후보 페이지를 열었어도 특정 행은
# 그 페이지에 근거가 없을 수 있다. 2026-08-25 실행의 후보 1 이 그랬다 — 페이지는
# 실제로 열었지만 레이더·EO/IR 융합 처리 행은 그 페이지에 없는 내용이었다.
SUPPORT_PAGE = "page_text"
SUPPORT_SNIPPET = "snippet"
SUPPORT_NONE = "none"
# ARIA 가 보존한 공식 응답 아티팩트에서 이 행의 support_text 를 실제로 찾아낸
# 경우에만 붙는다. 모델은 이 값을 고르지 못한다 — 2차 분류 턴에서도 마찬가지다.
# 그 턴의 support_source 는 모델이 적는 것이 아니라, ARIA 가 대조 결과로 붙인다.
SUPPORT_OFFICIAL = "official_record"
MODEL_REPORTED_SUPPORT_SOURCES = (SUPPORT_PAGE, SUPPORT_SNIPPET, SUPPORT_NONE)
ARIA_PRODUCED_SUPPORT_SOURCES = (SUPPORT_OFFICIAL,)
SUPPORT_SOURCES = MODEL_REPORTED_SUPPORT_SOURCES + ARIA_PRODUCED_SUPPORT_SOURCES

# 그 근거를 문헌의 어디까지 보고 말하는가.
#
# 범위를 함께 받지 않으면 "이 문헌에 그 구성이 없다"와 "내가 본 요약에 없었다"를
# 구분할 수 없다. 요약만 읽고 부재를 단정하면 대응 할루시네이션이 부재
# 할루시네이션으로 바뀔 뿐이다.
SCOPE_CLAIMS = "claims"
SCOPE_FULL_TEXT = "full_text"
SCOPE_ABSTRACT = "abstract"
SCOPE_UNKNOWN = "unknown"
SUPPORT_SCOPES = (SCOPE_CLAIMS, SCOPE_FULL_TEXT, SCOPE_ABSTRACT, SCOPE_UNKNOWN)

# 이 채널로는 원문 대조를 주장할 수 없다. 여기 있는 채널에서 온
# raw_original_verified 는 무조건 내린다.
#
# 모델 보고 경로에서는 채널이 web 으로 강제되므로 사실상 모든 모델 보고
# 후보가 여기 걸린다. patent_db 후보의 원문 등급은 모델이 아니라 ARIA 가
# patent_search.provenance 로 보존 아티팩트에 대조해 계산한다.
_CANNOT_VERIFY_ORIGINAL = frozenset({CHANNEL_WEB})

MAX_ROUNDS = 2
_MAX_CANDIDATES = 200
_MAX_QUERIES = 60
_MAX_TEXT = 2000
_MAX_MAPPING_ROWS = 60
_MAX_INTERPRETATIONS = 40

ORIGIN_CLAIM_ONLY = "claim_only"
ORIGIN_SPEC_ASSISTED = "spec_assisted"
SEARCH_ORIGINS = (ORIGIN_CLAIM_ONLY, ORIGIN_SPEC_ASSISTED)

# --- 발견 경로 --------------------------------------------------------------
#
# search_origins(claim_only / spec_assisted)와 **다른 축**이다. 저쪽은 "무엇을
# 입력으로 검색했나"이고 이쪽은 "어느 검색 경로가 이 문헌을 데려왔나"이다. 한
# 후보가 두 경로에서 모두 나올 수 있고, 그때 두 값을 다 남긴다 — 한쪽을 지우면
# "EPO 를 켜서 더 얻은 것이 무엇인가"에 답할 수 없다.
#
# 값이 없는 옛 후보는 web 으로 읽는다. v8 까지 후보를 만든 경로는 모델의 웹
# 검색 보고뿐이었다.
DISCOVERY_WEB = "web"
DISCOVERY_EPO = "epo"
# ARIA 가 서지 DB(Crossref·Europe PMC)에 직접 물어 데려온 후보. 웹 검색이 논문을
# **식별**하지 못하는 실행에서 유일하게 제목과 DOI 가 붙은 후보를 만드는 경로다.
DISCOVERY_LITERATURE = "literature"
DISCOVERY_ORIGINS = (DISCOVERY_WEB, DISCOVERY_EPO, DISCOVERY_LITERATURE)


def discovery_origins(candidate: dict) -> list[str]:
    """이 후보를 데려온 경로. 옛 기록은 web 하나로 읽는다."""
    raw = (candidate or {}).get("discovery_origins")
    found = [
        origin
        for origin in DISCOVERY_ORIGINS
        if isinstance(raw, list) and origin in raw
    ]
    return found or [DISCOVERY_WEB]

# --- 레인 ------------------------------------------------------------------
#
# 레인은 (검색 경로 × 검색 기원)이다. 네 개가 전부이고 id 는 고정이다.
#
#     web:claim_only   web:spec_assisted   epo:claim_only   epo:spec_assisted
#
# 여기서 말하는 '채널'은 **검색 경로**다. 후보에 붙는 channel(web / patent_db)과
# 축이 다르다 — 그쪽은 "이 후보를 누가 만들었나(모델 보고냐 ARIA 생산이냐)"이고,
# 이쪽은 "어느 경로로 검색했나"이다. EPO 레인이 만든 후보의 channel 은
# patent_db 이고 backend_id 는 epo 다.
LANE_CHANNEL_WEB = "web"
LANE_CHANNEL_EPO = "epo"
LANE_CHANNELS = (LANE_CHANNEL_WEB, LANE_CHANNEL_EPO)

EPO_BACKEND_ID = "epo"
LITERATURE_BACKEND_ID = "literature"


def lane_id(channel: str, origin: str) -> str:
    """레인 id 를 만드는 유일한 곳. 두 군데서 만들면 반드시 어긋난다."""
    return f"{channel}:{origin}"


LANE_IDS = tuple(
    lane_id(channel, origin)
    for channel in LANE_CHANNELS
    for origin in SEARCH_ORIGINS
)

# 원문을 확보하지 못한 칸에 들어가는 고정 문구. 검증 단계(search_verification)도
# 같은 문구를 쓴다 — 두 곳에서 따로 적으면 한쪽만 바뀌었을 때 보고서에 두 가지
# "미확인" 표기가 섞인다.
UNVERIFIED_EXCERPT = "원문에서 확인되지 않음"
UNVERIFIED_LOCATION = "확인 필요"
_UNVERIFIED_EXCERPT = UNVERIFIED_EXCERPT
_UNVERIFIED_LOCATION = UNVERIFIED_LOCATION

# Provider 별 도구 이름. 감사 스키마는 같은 web 채널로 정규화하지만 실제 호출
# 이름은 tool_calls 에 그대로 보존한다.
# Provider 마다 도구 이름이 다르다. 한 곳에서만 정의해서 runner 의 진행
# 표시와 여기 감사 집계가 같은 목록을 보게 한다 — 갈라지면 검색은 돌았는데
# 횟수가 0으로 보이는 식으로 조용히 어긋난다.
#   Claude : WebSearch / WebFetch
#   agy    : search_web / read_url_content
#   Codex  : web_search (검색과 URL 조회를 겸한다. 종류는 input_kind 로 갈리며,
#            URL 조회의 성공 여부는 스트림에 오지 않아 열람으로 세지 않는다)
SEARCH_TOOL_NAMES = frozenset({"WebSearch", "search_web", "web_search"})
FETCH_TOOL_NAMES = frozenset({"WebFetch", "read_url_content"})

# Codex 의 web_search 는 도구 하나가 검색과 URL 조회를 겸한다. 도구 이름만으로는
# 종류를 알 수 없어서 Provider 가 붙인 input_kind 를 본다. 붙이지 않는
# Provider(Claude/agy)는 도구 이름이 곧 종류이므로 예전 계약대로 검색으로 센다.
INPUT_KIND_QUERY = "query"
INPUT_KIND_URL = "url"


def _call_kind(call: dict) -> str:
    data = call.get("input")
    if not isinstance(data, dict):
        return ""
    kind = data.get("input_kind")
    if kind in (INPUT_KIND_QUERY, INPUT_KIND_URL):
        return str(kind)
    return INPUT_KIND_QUERY if data.get("query") else ""
_SEARCH_TOOL_NAMES = SEARCH_TOOL_NAMES
_FETCH_TOOL_NAMES = FETCH_TOOL_NAMES


class SearchLogError(Exception):
    """블록이 없거나, 형식이 아니거나, 필수 항목이 빠졌다."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_block(text: str) -> str:
    """사람이 읽을 보고서에서 감사 블록을 걷어낸다.

    원문은 stdout.log 와 search_manifest.json 에 남으므로 기록은 잃지 않는다.
    """
    return _BLOCK.sub("", text).rstrip() + ("\n" if text.endswith("\n") else "")


def _text(value, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def one_of(value, allowed: tuple[str, ...], fallback: str) -> str:
    """허용 목록 밖의 값은 조용히 fallback 으로 바꾼다."""
    candidate = _text(value, 60)
    return candidate if candidate in allowed else fallback


_one_of = one_of


def empty_candidate_verification(
    *, reason_code: str = "not_requested", detail: str = ""
) -> dict:
    """새 후보에 붙이는 후보 단위 검증 감사 기록."""
    return {
        "status": VERIFY_NOT_ATTEMPTED,
        "reason_code": _text(reason_code, 120),
        "detail": _text(detail, 1000),
        "backend_id": "",
        "artifact_ids": [],
    }


def classification_view(candidate: dict, manifest_version: int | None = None) -> dict:
    """저장된 후보를 정식/잠정 분류로 안전하게 해석한다.

    과거 스키마에는 ``group_eligible`` 자체가 없었다. 그 값을 ``True`` 로
    간주하면 검색 결과만 보고 붙인 옛 A/B/C가 새 기준의 정식 분류로 승격된다.
    따라서 명시적인 게이트 또는 저장된 검증 흔적이 없으면 항상 잠정으로 본다.
    이 함수는 입력 dict를 수정하지 않으므로 과거 아티팩트도 그대로 보존된다.
    """
    raw_group = candidate.get("group") if candidate.get("group") in GROUPS else None
    raw_provisional = (
        candidate.get("provisional_group")
        if candidate.get("provisional_group") in GROUPS
        else None
    )
    eligibility = candidate.get("group_eligible")

    def has_rows(key: str) -> bool:
        try:
            return int(candidate.get(key) or 0) > 0
        except (TypeError, ValueError):
            return False

    official_supported = has_rows("official_supported_rows")
    official_evidence = candidate.get("official_evidence")
    official_evidence = official_evidence if isinstance(official_evidence, dict) else {}
    official_saved = bool(
        candidate.get("evidence_status") == EVIDENCE_OFFICIAL
        and official_evidence.get("artifact_ids")
        and official_supported
    )
    original_saved = bool(candidate.get("original_verified") and raw_group)
    page_saved = bool(
        candidate.get("identifier_url_matched")
        and candidate.get("page_fetch_succeeded")
        and has_rows("page_supported_rows")
    )
    explicitly_formal = eligibility is True or official_saved or original_saved or page_saved

    if raw_group and explicitly_formal:
        stored_basis = candidate.get("classification_basis")
        if stored_basis in (
            CLASSIFICATION_PAGE,
            CLASSIFICATION_OFFICIAL,
            CLASSIFICATION_ORIGINAL,
        ):
            basis = stored_basis
        elif original_saved:
            basis = CLASSIFICATION_ORIGINAL
        elif official_saved or candidate.get("evidence_status") == EVIDENCE_OFFICIAL:
            basis = CLASSIFICATION_OFFICIAL
        else:
            basis = CLASSIFICATION_PAGE
        return {"group": raw_group, "provisional_group": None, "basis": basis}

    proposed = raw_provisional or raw_group
    if proposed:
        stored_basis = candidate.get("classification_basis")
        if stored_basis in (CLASSIFICATION_SEARCH, CLASSIFICATION_LEGACY):
            basis = stored_basis
        # 명시적으로 게이트에서 탈락했거나 잠정 칸이 있으면 당시 분류가 검색
        # 결과 기반 제안이었다는 구조적 흔적이 남아 있다.
        elif eligibility is False or raw_provisional:
            basis = CLASSIFICATION_SEARCH
        else:
            basis = CLASSIFICATION_LEGACY
        return {"group": None, "provisional_group": proposed, "basis": basis}

    return {
        "group": None,
        "provisional_group": None,
        "basis": CLASSIFICATION_NONE,
    }


def normalize_url(raw) -> str:
    """URL 을 대조 가능한 형태로 정규화한다.

    모델이 보고한 후보 URL 과 ARIA 가 스트림에서 본 WebFetch 인수는 같은
    페이지라도 문자열이 다를 수 있다(대소문자, 기본 포트, 끝 슬래시, 프래그먼트,
    www.). 이 정도만 맞춘다. 질의 문자열은 건드리지 않는다 — 특허 사이트에서
    질의가 문서를 가리키는 경우가 있어서, 지우면 다른 문서를 같다고 판정한다.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text if "//" in text else f"//{text}", scheme="https")
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    port = parsed.port
    if port in (80, 443):
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, parsed.query, ""))


# 후보 전용 페이지로 볼 수 없는 주소.
#
# 2026-08-25 실행의 후보 3 은 url 이 http://www.kipris.or.kr 이었다. 검색 포털
# 첫 화면이다. 그 주소로는 어떤 문헌번호도 확인할 수 없는데 그룹 C 와 구성
# 대응표를 그대로 받았다. 후보 2 는 url 이 아예 "확인 필요" 였다.
_URL_UNKNOWN = frozenset({"", "확인 필요", "unknown", "n/a", "none", "-"})
# 값이 자유 질의문인 파라미터. 이것만 있으면 결과 목록이지 문헌 페이지가 아니다.
_SEARCH_QUERY_KEYS = frozenset({"q", "query", "searchquery", "kw", "keyword", "search"})
_SEARCH_PATH_HINTS = ("/search", "/results", "/result", "/list")


def is_document_url(raw) -> bool:
    """이 주소가 문헌 하나를 가리키는 전용 페이지인가.

    보수적으로 판정한다. 거짓이 나와도 후보가 사라지는 게 아니라 격리될 뿐이므로,
    애매한 주소는 거절하는 쪽이 안전하다.
    """
    text = str(raw or "").strip()
    if text.lower() in _URL_UNKNOWN:
        return False
    try:
        parsed = urlsplit(text if "//" in text else f"//{text}", scheme="https")
    except ValueError:
        return False
    if not (parsed.hostname or ""):
        return False
    path = (parsed.path or "").rstrip("/")
    query = parsed.query or ""
    if not path and not query:
        # 호스트 루트. 포털 첫 화면이다.
        return False
    keys = {key.lower() for key, _ in parse_qsl(query)}
    if keys & _SEARCH_QUERY_KEYS:
        return False
    if not path and not keys:
        return False
    lowered = path.lower()
    if any(hint in lowered for hint in _SEARCH_PATH_HINTS) and not keys:
        return False
    return True


def _number_variants(raw) -> set[str]:
    """문헌번호를 URL 안에서 찾기 위한 표기 변형.

    같은 문헌이라도 사이트마다 국가코드와 종류코드를 붙이거나 뗀다. KIPRIS 는
    URL 에 숫자만 싣고, Google Patents 는 US11268651B2 처럼 통째로 싣는다.
    """
    compact = re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())
    if not compact:
        return set()
    variants = {compact}
    without_country = re.sub(r"^[A-Z]{2}", "", compact)
    if without_country:
        variants.add(without_country)
    for value in list(variants):
        trimmed = re.sub(r"[A-Z]\d?$", "", value)
        if trimmed:
            variants.add(trimmed)
    # 너무 짧은 조각은 우연히 URL 어딘가에 들어 있다. 식별 근거가 못 된다.
    return {value for value in variants if len(value) >= 5}


def identity_in_url(doc_number, doi, url) -> bool:
    """후보의 식별번호가 그 URL 안에 실제로 들어 있는가.

    페이지를 열었다는 사실만으로는 "그 페이지가 이 번호의 문헌"임이 확인되지
    않는다. 2026-08-25 실행의 후보 3 은 실재하는 출원번호에 무관한 명칭과 기술
    내용이 결합돼 있었다. 번호와 주소가 서로를 가리키는지가 최소 확인이다.
    """
    haystack = re.sub(r"[^0-9A-Za-z]", "", str(url or ""))
    if not haystack:
        return False
    doi_text = _text(doi, 300).lower()
    if doi_text:
        doi_compact = re.sub(r"[^0-9a-z]", "", doi_text)
        if len(doi_compact) >= 5 and doi_compact in haystack.lower():
            return True
    upper = haystack.upper()
    return any(variant in upper for variant in _number_variants(doc_number))


def _mapping_row(
    entry: dict,
    verified: bool,
    *,
    fetched_ok: bool,
    candidate_canonical: str,
    succeeded_urls: set[str],
    index: int,
    row_no: int,
    notes: list[str],
) -> dict:
    """청구항 구성 대응표 한 줄.

    원문이 확인되지 않은 후보에서는 발췌·위치·번역을 확정 표현으로 남기지
    않는다. 번역까지 지우는 이유: 확보하지 못한 원문의 번역문은 원문보다 더
    확인할 수 없는 진술이다.

    그런데 그것만으로는 부족했다. web 채널에서 verified 는 늘 거짓이므로 발췌·
    위치·번역은 전부 상수로 덮이는데, counterpart·degree·similar·different 는
    손대지 않아서 근거 없는 산문이 그대로 사용자에게 갔다. 검증되는 칸은 모두
    같은 문구가 되고 검증되지 않는 칸만 내용을 갖는 비대칭이었다.

    그래서 축을 하나 더 본다.

        verified          후보의 공식 원문을 대조했는가. web 채널에서는 늘 거짓.
        support_source    이 행의 대응 주장을 무엇을 읽고 썼는가.

    근거가 없는 행은 등급만 내리지 않고 서술 칸도 비운다. 등급을 내려도 산문이
    남아 있으면 사용자는 그 산문을 읽는다.
    """
    support = _one_of(
        entry.get("support_source"), MODEL_REPORTED_SUPPORT_SOURCES, SUPPORT_NONE
    )
    degree = _one_of(entry.get("degree"), DEGREES, DEGREE_UNKNOWN)
    counterpart = _text(entry.get("counterpart"), 1500)
    similar = _text(entry.get("similar"), 1500)
    different = _text(entry.get("different"), 1500)
    support_text = _text(entry.get("support_text"))
    support_url = _text(entry.get("support_url"), 1000)
    scope = _one_of(entry.get("support_scope"), SUPPORT_SCOPES, SCOPE_UNKNOWN)

    # --- page_text 주장의 최소 조건 ---------------------------------------
    # "페이지 본문을 읽었다"는 주장은 그 자체로는 모델의 자기 보고다. ARIA 가
    # 확인할 수 있는 것만 확인한다.
    #
    #   근거 텍스트가 실제로 있는가
    #   근거 주소가 이 후보의 전용 페이지인가
    #   그 주소로 성공한 열람을 ARIA 가 스트림에서 봤는가
    #
    # 세 가지를 통과해도 그 텍스트가 정말 그 페이지에 있었는지는 확인하지
    # 못한다. 그러려면 도구 결과 본문을 보존해 대조해야 하는데, 지금 스트림
    # 계층은 결과 본문을 버린다(claude_stream._on_user). agy 는 애초에 결과
    # 본문을 이벤트로 내보내지 않는다. 그래서 이 검사는 상한이 아니라 하한이다.
    if support == SUPPORT_PAGE:
        # 근거 주소를 적지 않았으면 이 후보의 전용 페이지를 뜻하는 것으로 읽는다.
        resolved = normalize_url(support_url) if support_url else candidate_canonical
        if not support_text:
            support = SUPPORT_NONE
            notes.append(
                f"후보 {index} 대응표 {row_no}행: 근거 텍스트 없이 "
                f"{SUPPORT_PAGE} 를 주장해 {SUPPORT_NONE} 으로 내렸습니다."
            )
        elif not fetched_ok:
            support = SUPPORT_SNIPPET
            notes.append(
                f"후보 {index} 대응표 {row_no}행: 이 후보의 페이지를 연 기록이 "
                f"없어 {SUPPORT_PAGE} 를 {SUPPORT_SNIPPET} 으로 내렸습니다."
            )
        elif not resolved or resolved != candidate_canonical:
            support = SUPPORT_SNIPPET
            notes.append(
                f"후보 {index} 대응표 {row_no}행: 근거 주소가 이 후보의 전용 "
                f"페이지가 아니어서 {SUPPORT_PAGE} 를 {SUPPORT_SNIPPET} 으로 "
                "내렸습니다."
            )
        elif resolved not in succeeded_urls:
            support = SUPPORT_SNIPPET
            notes.append(
                f"후보 {index} 대응표 {row_no}행: 근거 주소로 성공한 열람 기록이 "
                f"없어 {SUPPORT_PAGE} 를 {SUPPORT_SNIPPET} 으로 내렸습니다."
            )

    if support == SUPPORT_NONE:
        if degree != DEGREE_UNKNOWN or counterpart or similar or different:
            notes.append(
                f"후보 {index} 대응표 {row_no}행: 관측 근거가 없어 대응 정도를 "
                f"'{DEGREE_UNKNOWN}' 으로 내리고 대응 내용·유사점·차이점을 "
                "비웠습니다."
            )
        degree = DEGREE_UNKNOWN
        counterpart = ""
        similar = ""
        different = ""
        support_text = ""
        support_url = ""
        scope = SCOPE_UNKNOWN

    return {
        "feature": _text(entry.get("feature"), 800),
        "counterpart": counterpart,
        "degree": degree,
        "verified": verified,
        # 이 행의 근거. 후보 단위 provenance 와 다른 축이다.
        "support_source": support,
        "support_text": support_text,
        "support_scope": scope,
        "support_url": support_url,
        "page_supported": support == SUPPORT_PAGE,
        "source_location": (
            _text(entry.get("source_location"), 300) if verified else _UNVERIFIED_LOCATION
        ),
        "verbatim_excerpt": (
            _text(entry.get("verbatim_excerpt")) if verified else _UNVERIFIED_EXCERPT
        ),
        "translation": (
            _text(entry.get("translation")) if verified else _UNVERIFIED_EXCERPT
        ),
        "similar": similar,
        "different": different,
    }


def _string_list(value, *, limit: int = 20, text_limit: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        cleaned = _text(item, text_limit)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _term_expansion_row(entry: dict) -> dict:
    """명세서가 검색어를 어떻게 *넓혔는지* 모델이 보고한 한 줄.

    법적 범위를 하나로 확정하거나 청구항보다 좁게 읽었다는 자기보고를 받지
    않는다. 대신 가능한 의미를 복수로 보존하고, 실제로 더한 검색어와 의도적으로
    검색 제한에 쓰지 않은 명세서상 한정을 따로 기록한다.

    basis 의 진위는 ARIA 가 검증하지 못한다. 이 필드는 사용자가 명세서 원문에서
    대조할 수 있게 만드는 추적 정보다.
    """
    return {
        "claim_term": _text(entry.get("claim_term"), 500),
        "alternative_meanings": _string_list(
            entry.get("alternative_meanings"), text_limit=1000
        ),
        "expanded_terms": _string_list(entry.get("expanded_terms")),
        "basis": _text(entry.get("basis"), 800),
        "excluded_limitations": _string_list(
            entry.get("excluded_limitations"), text_limit=1000
        ),
    }


def _legacy_interpretation_as_expansion(entry: dict) -> dict:
    """v1 출력과 오래된 모델 응답을 안전한 v2 모양으로 낮춰 담는다."""
    reading = _text(entry.get("reading"), 1500)
    return {
        "claim_term": _text(entry.get("term"), 500),
        "alternative_meanings": [reading] if reading else [],
        "expanded_terms": [],
        "basis": _text(entry.get("basis"), 800),
        "excluded_limitations": [],
    }


def _normalize_candidate(
    entry: dict,
    index: int,
    notes: list[str],
    succeeded_urls: set[str],
    search_origin: str,
) -> dict:
    # 모델 보고이므로 MODEL_REPORTED_CHANNELS 만 인정한다. 모델이
    # patent_db 를 주장해도 web 으로 강제된다.
    channel = _one_of(entry.get("channel"), MODEL_REPORTED_CHANNELS, CHANNEL_WEB)
    claimed_channel = _text(entry.get("channel"), 60)
    if claimed_channel and claimed_channel != channel:
        # 조용히 바꾸지 않는다. 모델이 ARIA 전용 채널을 주장했다는 사실 자체가
        # 사용자가 알아야 할 정보다 — 다른 강등과 같은 취급을 한다.
        notes.append(
            f"후보 {index}: 모델이 보고한 채널 '{claimed_channel}' 은 모델이 "
            f"주장할 수 없는 값이므로 '{channel}' 로 바꿨습니다."
        )
    # 모델이 고를 수 있는 값만 받는다. official_record_response 는 ARIA 가
    # OPS 를 직접 부른 경로에만 붙으므로, 모델이 주장하면 스니펫으로 내려간다.
    provenance = _one_of(entry.get("provenance"), MODEL_REPORTED_PROVENANCE, PROV_SNIPPET)
    evidence = _one_of(
        entry.get("evidence_status"),
        MODEL_REPORTED_EVIDENCE_STATUS,
        EVIDENCE_CANDIDATE,
    )
    url = _text(entry.get("url"), 1000)
    canonical = normalize_url(url)

    # --- 관측과 대조 -------------------------------------------------------
    # 모델의 자기 보고를 그대로 받지 않는다. "페이지를 열어 봤다"는 주장은
    # ARIA 가 스트림에서 성공한 WebFetch 호출을 실제로 본 경우에만 인정한다.
    fetched_ok = bool(canonical) and canonical in succeeded_urls

    if evidence == EVIDENCE_REVIEWED and not fetched_ok:
        evidence = EVIDENCE_CANDIDATE
        notes.append(
            f"후보 {index}: 성공한 페이지 열람 기록과 URL 이 대조되지 않아 "
            f"{EVIDENCE_REVIEWED} 를 {EVIDENCE_CANDIDATE} 로 내렸습니다."
        )
    if provenance == PROV_WEBFETCH and not fetched_ok:
        # 열지 못한 페이지의 요약이라는 것은 성립하지 않는다.
        provenance = PROV_SNIPPET
        notes.append(
            f"후보 {index}: 성공한 페이지 열람 기록이 없어 {PROV_WEBFETCH} 를 "
            f"{PROV_SNIPPET} 로 내렸습니다."
        )

    # --- 증거 등급 강등 ----------------------------------------------------
    # web 채널에서는 원문 대조를 주장할 수 없다. WebFetch 가 돌려주는 것은
    # 페이지 원문이 아니라 다른 모델이 추출한 요약이기 때문이다.
    if provenance == PROV_RAW and channel in _CANNOT_VERIFY_ORIGINAL:
        provenance = PROV_WEBFETCH if fetched_ok else PROV_SNIPPET
        notes.append(
            f"후보 {index}: {channel} 채널에서는 원문 대조를 확인할 수 없으므로 "
            f"{PROV_RAW} 를 {provenance} 로 내렸습니다."
        )

    original_verified = provenance == PROV_RAW

    # 원문을 확보하지 못했으면 발췌와 위치를 확정 표현으로 남기지 않는다.
    # 위치도 보존하지 않는다 — '청구항 1, 3행' 같은 값은 원문을 본 사람만 쓸 수
    # 있는 진술이고, 확인되지 않은 발췌 옆에 남아 있으면 그 발췌가 실재한다는
    # 인상을 준다.
    excerpt = _text(entry.get("verbatim_excerpt"))
    location = _text(entry.get("source_location"), 300)
    if not original_verified:
        if excerpt:
            notes.append(
                f"후보 {index}: 원문이 확인되지 않아 직접 발췌를 "
                f"'{_UNVERIFIED_EXCERPT}' 로 바꿨습니다."
            )
        if location:
            notes.append(
                f"후보 {index}: 원문이 확인되지 않아 원문 위치를 "
                f"'{_UNVERIFIED_LOCATION}' 로 바꿨습니다."
            )
        excerpt = _UNVERIFIED_EXCERPT
        location = _UNVERIFIED_LOCATION

    provisional = entry.get("provisional")
    if not isinstance(provisional, bool):
        provisional = True
    if not original_verified and not provisional:
        provisional = True
        notes.append(f"후보 {index}: 원문 미검증이므로 잠정 분류로 표시했습니다.")

    doc_number = _text(entry.get("doc_number"), 120)
    doi = _text(entry.get("doi"), 200)
    title = _text(entry.get("title"), 500)
    applicant = _text(entry.get("applicant"), 300)
    family = _text(entry.get("family"), 300)
    note = _text(entry.get("note"))

    # --- 후보 식별 게이트 ---------------------------------------------------
    # 여기까지의 강등은 전부 "이 후보를 무엇으로 알게 되었나"에 관한 것이고,
    # 후보가 실재하는 문헌인지는 아무도 확인하지 않았다. 2026-08-25 실행에서
    # 실재하는 출원번호에 지어낸 명칭과 기술 내용이 결합된 후보가 그룹 분류와
    # 구성 대응표를 그대로 받은 이유가 이것이다.
    #
    # 두 단계로 나눈다.
    #   url_is_document   주소가 문헌 하나를 가리키는 전용 페이지인가
    #   identifier_url_matched  그 주소 안에 이 후보의 식별번호가 실제로 들어 있는가
    #
    # 전자가 아니면 후보를 그룹과 대응표에서 뺀다(격리). 후자가 아니면 후보는
    # 남기되 확인되지 않은 서지정보를 인쇄하지 않는다 — 번호는 검색 단서로
    # 쓸모가 있고, 명칭·출원인이야말로 지어내기 쉬운 값이다.
    url_is_document = is_document_url(url)
    # 대조할 식별자가 없으면 번호-주소 대조를 요구할 수 없다. DOI 가 없는 오래된
    # 논문이나 학회 자료가 여기 걸린다. 이때 막으려는 실패(실재하는 번호에 다른
    # 발명의 명칭을 붙이기) 자체가 성립하지 않으므로, 실제로 연 전용 페이지라는
    # 사실을 식별 근거로 쓴다. 번호나 DOI 가 있으면 반드시 대조한다.
    has_identifier = bool(_number_variants(doc_number) or _text(doi, 300))
    #
    # 이름을 조심해서 붙인다. 이 값은 "이 문헌이 무엇인지 확인됐다"가 아니라
    # "주장한 번호가 실제로 연 주소 안에 들어 있다"이다. 올바른 문헌 페이지를
    # 열고도 모델이 엉뚱한 명칭을 쓰면 이 검사는 통과한다. 명칭·출원인까지
    # 확인하려면 페이지 관측 결과의 서지정보와 대조해야 하는데, 그러려면 도구
    # 결과 본문이 필요하다(위 _mapping_row 주석 참조).
    identifier_url_matched = bool(
        fetched_ok
        and url_is_document
        and (identity_in_url(doc_number, doi, url) if has_identifier else True)
    )
    quarantined = not (fetched_ok and url_is_document)
    quarantine_reason = ""
    if quarantined:
        if not url_is_document:
            quarantine_reason = (
                "후보 전용 페이지 주소가 아닙니다(빈 값·포털 첫 화면·검색 결과 주소)."
            )
        else:
            quarantine_reason = "이 주소로 성공한 페이지 열람 기록이 없습니다."
        notes.append(
            f"후보 {index}: {quarantine_reason} 그룹 분류와 구성 대응표에서 "
            "제외하고 미확인 검색 단서로 격리했습니다."
        )

    if not identifier_url_matched and (title or applicant or family):
        notes.append(
            f"후보 {index}: 문헌번호와 페이지가 같은 문헌임을 확인하지 못해 "
            "명칭·출원인·패밀리를 출력에서 제외했습니다."
        )
        title = ""
        applicant = ""
        family = ""

    raw_mapping = entry.get("mapping")
    mapping = (
        [
            _mapping_row(
                row,
                original_verified,
                fetched_ok=fetched_ok,
                candidate_canonical=canonical,
                succeeded_urls=succeeded_urls,
                index=index,
                row_no=row_no,
                notes=notes,
            )
            for row_no, row in enumerate(raw_mapping[:_MAX_MAPPING_ROWS], start=1)
            if isinstance(row, dict)
        ]
        if isinstance(raw_mapping, list)
        else []
    )

    if quarantined:
        if mapping:
            notes.append(
                f"후보 {index}: 문헌 식별이 확인되지 않아 구성 대응표 "
                f"{len(mapping)}행을 출력에서 제외했습니다."
            )
        mapping = []
        note = ""

    # A/B/C 진입 하한. 원문 대조(raw_verified)는 요구하지 않는다 — web 채널에서는
    # 그 값이 나올 수 없어서 모든 후보가 빠져 버린다. 대신 두 가지를 요구한다.
    #
    #   identifier_url_matched  주장한 번호가 실제로 연 주소 안에 있다
    #   page_supported_rows     그 페이지 관측에 근거한 대응 행이 하나 이상 있다
    #
    # 앞의 것을 빼면, 번호는 A 라고 적고 B 의 페이지를 연 후보가 그룹에 들어간다.
    # 그 후보의 대응 행은 주장한 문헌이 아닌 다른 문헌에서 온 것이다. 서지정보만
    # 지우는 것으로는 부족하다 — 대응 관계 자체가 다른 문헌의 것이기 때문이다.
    page_supported_rows = sum(1 for row in mapping if row["page_supported"])
    group_eligible = bool(
        not quarantined and identifier_url_matched and page_supported_rows
    )
    if not quarantined and not group_eligible:
        reason = (
            "문헌번호가 실제로 연 주소에서 확인되지 않았습니다"
            if not identifier_url_matched
            else "페이지 관측에 근거한 대응표 행이 없습니다"
        )
        notes.append(f"후보 {index}: {reason}. 그룹 분류에서 제외했습니다.")

    if mapping and not original_verified:
        discarded = sum(
            1
            for row in (raw_mapping or [])
            if isinstance(row, dict)
            and (row.get("verbatim_excerpt") or row.get("translation"))
        )
        if discarded:
            notes.append(
                f"후보 {index}: 구성 대응표 {discarded}행의 발췌·번역을 "
                f"'{_UNVERIFIED_EXCERPT}' 로 바꿨습니다."
            )

    # 그룹 값은 모델이 정하지 않는다. group_eligible 이 거짓이면 무조건 null 이다.
    # A/B/C 중 아무 글자나 남겨 두면, 격리 영역에 찍히는 후보가 다운스트림에서는
    # 분류된 후보처럼 읽힌다 — 채널 대조와 프런트 집계가 그 값을 그대로 쓴다.
    group = _one_of(entry.get("group"), GROUPS, "C") if group_eligible else None

    # 검증되지 않은 분류는 **버리지 않고 다른 칸에 둔다.** 예전에는 group 에
    # null 을 넣는 것으로 끝냈는데, 그러면 모델이 실제로 내린 판단이 기록에서
    # 통째로 사라진다. 사용자에게는 "그 후보를 어떻게 봤는지"가 다시 확인해 볼
    # 단서이고, 없앨 이유는 "검증되지 않았다"는 것뿐이다 — 그건 값을 지우는
    # 대신 칸을 나눠서 말하면 된다.
    #
    # 두 칸은 **동시에 차지 않는다.** group 이 차면 provisional_group 은 null
    # 이다. 둘 다 값이 있으면 화면과 집계가 어느 쪽을 이 후보의 분류로 읽어야
    # 하는지 알 수 없고, 결국 둘 중 하나가 조용히 승격된다.
    claimed_group = entry.get("group") if entry.get("group") in GROUPS else None
    provisional_group = None if group_eligible else claimed_group
    classification_basis = (
        CLASSIFICATION_ORIGINAL
        if group_eligible and original_verified
        else CLASSIFICATION_PAGE
        if group_eligible
        else CLASSIFICATION_SEARCH
        if provisional_group
        else CLASSIFICATION_NONE
    )

    return {
        "index": index,
        "group": group,
        # 검증되지 않은 잠정 A/B/C. group 이 null 인 후보에만 값이 있다.
        "provisional_group": provisional_group,
        # 모델이 고르는 값이 아니라 위의 ARIA 게이트 결과로 계산한다.
        "classification_basis": classification_basis,
        "provisional": provisional,
        "channel": channel,
        "doc_type": _one_of(entry.get("doc_type"), DOC_TYPES, "other"),
        "doc_number": doc_number,
        "doi": doi,
        "title": title,
        "applicant": applicant,
        "url": url,
        "canonical_url": canonical,
        "family": family,
        "provenance": provenance,
        "evidence_status": evidence,
        "original_verified": original_verified,
        # ARIA 가 관측한 사실. 모델의 보고가 아니다.
        #
        # '이 주소를 열었다'가 아니라 '이 주소의 본문을 실제로 읽은 기록이
        # 있다'는 뜻이다. 포인터만 돌려주는 Provider 에서는 열람 호출이
        # 성공해도 본문을 읽기 전까지 거짓이다(observed() 주석 참조).
        "page_fetch_succeeded": fetched_ok,
        # 후보 식별 게이트의 결과. 모두 ARIA 가 계산한다.
        "url_is_document": url_is_document,
        # "이 문헌이 무엇인지 확인됨"이 아니다. 주장한 번호가 실제로 연 주소
        # 안에 들어 있다는 것뿐이다.
        "identifier_url_matched": identifier_url_matched,
        "quarantined": quarantined,
        "quarantine_reason": quarantine_reason,
        "group_eligible": group_eligible,
        "page_supported_rows": page_supported_rows,
        # ARIA 가 서버에서 공식 문헌을 조회한 기록. 조회하지 않은 후보는 빈
        # dict 다. "조회했는데 실패" 와 "아예 조회하지 않음" 을 구별해야 하므로
        # 없는 키로 두지 않는다.
        "official_evidence": {},
        "verification": empty_candidate_verification(
            reason_code="official_verification_not_run",
            detail="공식 문헌 검증을 아직 실행하지 않았습니다.",
        ),
        "verbatim_excerpt": excerpt,
        "source_location": location,
        "mapping": mapping,
        "note": note,
        # 이 값은 모델이 고르는 것이 아니라 ARIA 가 실행 경로에서 붙인다.
        "search_origins": [search_origin],
        "origin_groups": {search_origin: group},
        "origin_provisional_groups": {search_origin: provisional_group},
        # 이 후보를 데려온 검색 경로. 모델 보고 경로는 언제나 web 이다.
        "discovery_origins": [DISCOVERY_WEB],
        # EPO 독립 검색이 같은 문헌을 데려왔을 때 그 기록이 붙는 자리.
        # 비어 있으면 EPO 검색에서는 나오지 않았다는 뜻이다.
        "epo_discovery": {},
    }


def parse(
    text: str,
    observed_section: dict | None = None,
    spec_provided: bool = False,
    search_origin: str = ORIGIN_CLAIM_ONLY,
) -> tuple[dict, list[str]]:
    """보고서에서 감사 블록을 읽어 정규화한다.

    (모델 보고 부분, 정규화 메모) 를 돌려준다. 정규화 메모는 ARIA 가 무엇을
    고쳤는지이며 화면에 그대로 보여준다.

    observed_section 은 observed() 가 만든 관측 기록이다. 후보의 URL 을 실제로
    성공한 WebFetch 호출과 대조하는 데 쓴다. 넘기지 않으면 성공한 열람이 하나도
    없는 것으로 보고 전부 내린다 — 대조할 근거가 없으면 인정하지 않는다.

    spec_provided 는 이 독립 실행에 출원발명 문서를 넣었는지다. 넣었는데 용어
    확장 기록이 비어 있으면 그 사실을 메모로 남긴다. 명세서가 검색어에 어떻게
    반영됐는지 확인할 방법이 없다는 뜻이고, 사용자가 그것을 알아야 한다.
    """
    matches = _BLOCK.findall(text or "")
    if not matches:
        raise SearchLogError("보고서에서 검색 감사 블록을 찾지 못했습니다.")
    if len(matches) > 1:
        raise SearchLogError(
            f"검색 감사 블록이 {len(matches)}개 있습니다. 하나만 있어야 합니다."
        )
    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise SearchLogError(f"검색 감사 블록이 JSON 이 아닙니다: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise SearchLogError("검색 감사 블록은 객체여야 합니다.")

    notes: list[str] = []
    if search_origin not in SEARCH_ORIGINS:
        raise SearchLogError(f"알 수 없는 검색 경로입니다: {search_origin}")

    raw_rounds = payload.get("rounds")
    rounds: list[dict] = []
    if isinstance(raw_rounds, list):
        for position, entry in enumerate(raw_rounds, start=1):
            if not isinstance(entry, dict):
                continue
            number = entry.get("round")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                number = position
            queries = entry.get("queries")
            cleaned = (
                [_text(q, 500) for q in queries[:_MAX_QUERIES] if _text(q, 500)]
                if isinstance(queries, list)
                else []
            )
            rounds.append(
                {
                    "round": number,
                    "channel": _one_of(
                        entry.get("channel"), MODEL_REPORTED_CHANNELS, CHANNEL_WEB
                    ),
                    "queries": cleaned,
                    "note": _text(entry.get("note")),
                    "search_origin": search_origin,
                }
            )
    if not rounds:
        notes.append("모델이 검색 라운드를 보고하지 않았습니다.")
    if len(rounds) > MAX_ROUNDS:
        notes.append(
            f"검색 확장 상한은 {MAX_ROUNDS}라운드인데 {len(rounds)}라운드가 "
            "보고되었습니다."
        )

    raw_expansions = payload.get("term_expansions")
    expansions = (
        [
            _term_expansion_row(entry)
            for entry in raw_expansions[:_MAX_INTERPRETATIONS]
            if isinstance(entry, dict) and _text(entry.get("claim_term"), 500)
        ]
        if isinstance(raw_expansions, list)
        else []
    )

    # 새 계약으로 실행했는데 모델이 이전 필드명을 되돌리는 경우 결과 전체를
    # 버리지는 않는다. 좁힘 주장은 폐기하고 대안 의미만 보존한다.
    raw_legacy = payload.get("claim_interpretation")
    if not expansions and isinstance(raw_legacy, list):
        expansions = [
            _legacy_interpretation_as_expansion(entry)
            for entry in raw_legacy[:_MAX_INTERPRETATIONS]
            if isinstance(entry, dict) and _text(entry.get("term"), 500)
        ]
        if expansions:
            notes.append(
                "모델이 이전 claim_interpretation 형식을 출력해, 좁힘 주장은 "
                "버리고 용어 확장 기록으로 변환했습니다."
            )

    if spec_provided and not expansions:
        notes.append(
            "명세서 보조 검색을 실행했지만 용어 확장 기록이 비어 있습니다. "
            "명세서가 검색어에 어떻게 반영됐는지 확인할 수 없습니다."
        )
    if expansions and not spec_provided:
        notes.append(
            "출원발명 문서를 넣지 않은 실행인데 용어 확장 기록이 보고되었습니다. "
            "명세서 근거로 제시된 위치는 대조할 자료가 없습니다."
        )

    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise SearchLogError("검색 감사 블록에 candidates 배열이 없습니다.")
    succeeded = {
        normalize_url(url)
        for url in ((observed_section or {}).get("succeeded_fetch_urls") or [])
        if normalize_url(url)
    }
    candidates = [
        _normalize_candidate(entry, index, notes, succeeded, search_origin)
        for index, entry in enumerate(raw_candidates[:_MAX_CANDIDATES], start=1)
        if isinstance(entry, dict)
    ]

    raw_failures = payload.get("access_failures")
    failures: list[dict] = []
    if isinstance(raw_failures, list):
        for entry in raw_failures[:_MAX_CANDIDATES]:
            if not isinstance(entry, dict):
                continue
            failures.append(
                {
                    "url": _text(entry.get("url"), 1000),
                    "reason": _text(entry.get("reason"), 500),
                    "search_origin": search_origin,
                }
            )

    reported = {
        "rounds": rounds,
        "term_expansions": expansions,
        "candidates": candidates,
        "access_failures": failures,
    }
    return reported, notes


def _identity_key(candidate: dict, fallback: str) -> str:
    """두 독립 실행에서 같은 문헌을 합치기 위한 보수적인 식별 키."""
    unknown = {
        "",
        "확인 필요",
        "문헌번호 확인 필요",
        "unknown",
        "n/a",
        "none",
        "not available",
    }
    raw_doc_number = _text(candidate.get("doc_number"))
    doc_number = (
        ""
        if raw_doc_number.lower() in unknown
        else re.sub(r"[^0-9A-Z]", "", raw_doc_number.upper())
    )
    if doc_number:
        return f"doc:{doc_number}"
    doi = _text(candidate.get("doi"), 300).lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    if doi in unknown:
        doi = ""
    if doi:
        return f"doi:{doi}"
    canonical = _text(candidate.get("canonical_url"), 1200)
    if canonical:
        return f"url:{canonical}"
    title = " ".join(_text(candidate.get("title"), 500).lower().split())
    applicant = " ".join(_text(candidate.get("applicant"), 300).lower().split())
    if title and title not in unknown:
        return f"title:{title}|{applicant}"
    return fallback


def _evidence_score(candidate: dict) -> tuple[int, int, int, int, int]:
    """두 경로가 같은 문헌을 냈을 때 어느 쪽 기록을 남길지 고르는 순위.

    식별이 확인된 쪽을 먼저 본다. 격리된 후보의 서지정보는 비어 있으므로,
    대응표 행 수만 비교하면 확인된 후보가 격리된 후보에게 밀릴 수 있다.
    """
    return (
        int(not candidate.get("quarantined", False)),
        int(bool(candidate.get("identifier_url_matched"))),
        int(bool(candidate.get("original_verified"))),
        int(bool(candidate.get("page_fetch_succeeded"))),
        len(candidate.get("mapping") or []),
    )


def _with_classification_metadata(candidate: dict) -> dict:
    """후보의 분류 불변식과 출처별 정식/잠정 지도를 맞춘 복사본."""
    value = dict(candidate)
    view = classification_view(value)
    value["group"] = view["group"]
    value["provisional_group"] = view["provisional_group"]
    value["classification_basis"] = view["basis"]
    value.setdefault(
        "verification",
        empty_candidate_verification(
            reason_code="legacy_or_not_requested",
            detail="후보 단위 공식 검증 기록이 없습니다.",
        ),
    )

    origins = list(value.get("search_origins") or [])
    formal = {
        origin: group
        for origin, group in dict(value.get("origin_groups") or {}).items()
        if origin in SEARCH_ORIGINS and group in GROUPS
    }
    provisional = {
        origin: group
        for origin, group in dict(
            value.get("origin_provisional_groups") or {}
        ).items()
        if origin in SEARCH_ORIGINS and group in GROUPS
    }
    if not view["group"]:
        # 예전 origin_groups에는 검증되지 않은 모델 분류도 들어갔다. 후보 자체가
        # 정식이 아닌데 출처 지도만 정식으로 남으면 병합 때 다시 승격된다.
        formal = {}
    if not view["provisional_group"]:
        provisional = {}
    if len(origins) == 1:
        origin = origins[0]
        if view["group"]:
            formal.setdefault(origin, view["group"])
        elif view["provisional_group"]:
            provisional.setdefault(origin, view["provisional_group"])
    value["origin_groups"] = formal
    value["origin_provisional_groups"] = provisional
    # 발견 경로는 계산하지 않고 정규화만 한다. 옛 후보는 web 으로 읽는다.
    value["discovery_origins"] = discovery_origins(value)
    value.setdefault("epo_discovery", {})
    return value


def merge_reported(*reports: dict | None) -> dict | None:
    """독립 검색 결과를 합집합으로 병합한다.

    claim_only 를 먼저 넘기는 것이 계약이다. 같은 문헌이 두 경로에 있으면 청구항
    단독 실행의 A/B/C 분류를 유지하고, 더 강하게 확인된 증거 필드만 보강한다.
    따라서 명세서 보조 실행이 기본 후보를 지우거나 재분류할 수 없다.
    """
    available = [report for report in reports if report is not None]
    if not available:
        return None

    rounds: list[dict] = []
    expansions: list[dict] = []
    failures: list[dict] = []
    candidates: dict[str, dict] = {}

    for report_index, report in enumerate(available):
        rounds.extend(report.get("rounds") or [])
        for row in report.get("term_expansions") or []:
            if row not in expansions:
                expansions.append(row)
        for failure in report.get("access_failures") or []:
            if failure not in failures:
                failures.append(failure)

        for candidate_index, candidate in enumerate(report.get("candidates") or []):
            candidate = _with_classification_metadata(candidate)
            key = _identity_key(candidate, f"anon:{report_index}:{candidate_index}")
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = candidate
                continue

            prior_origins = list(existing.get("search_origins") or [])
            new_origins = list(candidate.get("search_origins") or [])
            origins = [
                origin
                for origin in SEARCH_ORIGINS
                if origin in prior_origins or origin in new_origins
            ]
            origin_groups = dict(existing.get("origin_groups") or {})
            origin_groups.update(candidate.get("origin_groups") or {})
            origin_provisional_groups = dict(
                existing.get("origin_provisional_groups") or {}
            )
            origin_provisional_groups.update(
                candidate.get("origin_provisional_groups") or {}
            )

            # 더 잘 확인된 경로의 서지·증거 필드를 사용하되, 기본 검색의 분류는
            # 명세서 보조 검색이 덮어쓰지 못한다.
            if _evidence_score(candidate) > _evidence_score(existing):
                replacement = dict(candidate)
                replacement_view = classification_view(replacement)
                if not replacement_view["group"]:
                    # 검증되지 않은 분류도 버리지는 않되 정식 칸으로 물려받지
                    # 않는다. claim_only의 잠정 분류를 우선 보존한다.
                    replacement["group"] = None
                    base_provisional = origin_provisional_groups.get(
                        ORIGIN_CLAIM_ONLY
                    )
                    if base_provisional not in GROUPS:
                        base_provisional = (
                            classification_view(existing)["provisional_group"]
                            or replacement_view["provisional_group"]
                        )
                    replacement["provisional_group"] = (
                        base_provisional if base_provisional in GROUPS else None
                    )
                    replacement["classification_basis"] = (
                        CLASSIFICATION_SEARCH
                        if replacement["provisional_group"]
                        else CLASSIFICATION_NONE
                    )
                else:
                    base_group = origin_groups.get(ORIGIN_CLAIM_ONLY)
                    if base_group not in GROUPS:
                        # 검증 근거가 없는 claim_only 잠정 등급을, 다른 레인의
                        # 근거를 빌려 정식으로 승격하지 않는다.
                        base_group = replacement_view["group"]
                    replacement["group"] = (
                        base_group if base_group in GROUPS else replacement_view["group"]
                    )
                    replacement["provisional_group"] = None
                existing = replacement

            discovered = [
                origin
                for origin in DISCOVERY_ORIGINS
                if origin in discovery_origins(existing)
                or origin in discovery_origins(candidate)
            ]
            epo_discovery = dict(existing.get("epo_discovery") or {}) or dict(
                candidate.get("epo_discovery") or {}
            )

            existing["search_origins"] = origins
            existing["origin_groups"] = origin_groups
            existing["origin_provisional_groups"] = origin_provisional_groups
            existing["discovery_origins"] = discovered
            existing["epo_discovery"] = epo_discovery
            if candidate.get("note") and candidate.get("note") != existing.get("note"):
                notes = [value for value in (existing.get("note"), candidate.get("note")) if value]
                existing["note"] = " / ".join(dict.fromkeys(notes))
            candidates[key] = existing

    merged_candidates = list(candidates.values())
    for index, candidate in enumerate(merged_candidates, start=1):
        normalized = _with_classification_metadata(candidate)
        candidate.clear()
        candidate.update(normalized)
        candidate["index"] = index

    return {
        "rounds": rounds,
        "term_expansions": expansions,
        "candidates": merged_candidates,
        "access_failures": failures,
    }


def observed(
    tool_calls: list[dict] | None,
    tool_uses: list[str] | None,
    search_origin: str | None = None,
) -> dict:
    """스트림에서 ARIA 가 직접 본 것. 모델의 자기 보고가 아니다."""
    calls = [dict(call) for call in (tool_calls or [])]
    if search_origin:
        for call in calls:
            call["search_origin"] = search_origin
    uses = list(tool_uses or [])
    counts: dict[str, int] = {}
    for name in uses:
        counts[name] = counts.get(name, 0) + 1
    # 호출 수와 질의 수는 다른 값이다. Codex 는 한 호출에 질의 여러 개를 묶어
    # 보내고, 최상위 query 는 CLI 가 첫 질의를 잘라 만든 표시용 문자열이다
    # (" ..." 로 끝난다). 그것 하나만 목록에 넣으면 관측 기록이 실제의 1/4 이
    # 되고, 진짜 질의 목록은 모델 자기보고에만 남는다 — 자기보고를 관측으로
    # 대체하는 것이 이 모듈의 존재 이유인데 거기서 구멍이 난다.
    search_calls = [
        call
        for call in calls
        if call.get("name") in _SEARCH_TOOL_NAMES
        and _call_kind(call) == INPUT_KIND_QUERY
    ]
    queries: list[str] = []
    for call in search_calls:
        data = call["input"]
        batch = data.get("queries")
        if isinstance(batch, list) and batch:
            queries.extend(
                str(part).strip() for part in batch if str(part).strip()
            )
        elif data.get("query"):
            queries.append(data["query"])
    # URL 조회는 검색이 아니고 페이지 열람도 아니다. 세 번째 종류로 따로 센다.
    # attempted/succeeded_fetch_urls 에는 절대 넣지 않는다 — 이 호출은 성공
    # 신호를 주지 않으므로, 넣는 순간 열지 못한 URL 이 열람 기록이 된다.
    # 2026-08-30 실측에서 열린 URL 과 실패한 URL 의 이벤트가 완전히 같았다.
    url_lookups = [
        call["input"]["url"]
        for call in calls
        if call.get("name") in _SEARCH_TOOL_NAMES
        and _call_kind(call) == INPUT_KIND_URL
        and isinstance(call.get("input"), dict)
        and call["input"].get("url")
    ]
    # 시도한 열람과 성공한 열람을 나눈다. 403 이나 유료 장벽으로 실패한 호출을
    # '열람했다'로 세면 증거 등급이 근거 없이 올라간다.
    attempted = [
        call["input"]["url"]
        for call in calls
        if call.get("name") in _FETCH_TOOL_NAMES
        and isinstance(call.get("input"), dict)
        and call["input"].get("url")
    ]
    # 열람 성공과 '본문을 실제로 읽었다'는 같은 말이 아니다. agy 의
    # read_url_content 는 가져온 페이지를 파일에 저장하고 경로만 돌려주므로,
    # 호출이 성공한 시점에는 아직 아무도 본문을 보지 않았다. 그 상태를 열람으로
    # 세면 근거 없는 대응표가 page_text 등급을 받는다 — 2026-08-25 이전 실행이
    # 전부 그랬다(원문 확인 0건, 근거 문장 0건인데 열람 성공은 참).
    #
    # 그래서 Provider 가 content_read 로 표시한 호출만 센다. 본문을 그대로
    # 돌려주는 Provider(WebFetch) 는 이 필드를 붙이지 않으며, 그때는 예전
    # 계약대로 성공 여부로 판단한다.
    succeeded = [
        call["input"]["url"]
        for call in calls
        if call.get("name") in _FETCH_TOOL_NAMES
        and call.get("ok") is True
        and bool(call.get("content_read", True))
        and isinstance(call.get("input"), dict)
        and call["input"].get("url")
    ]
    failures = [
        {
            "name": call.get("name"),
            "ts": call.get("ts"),
            "input": call.get("input"),
            "error": call.get("error"),
            "search_origin": call.get("search_origin"),
        }
        for call in calls
        if call.get("ok") is False
    ]
    # ok is None 은 "성공"이 아니라 "관측할 수 없음"이다. 확인된 실패와 같은
    # 목록에 넣지 않고, 그렇다고 성공으로 승격하지도 않는다. 이 목록이 없으면
    # "확인된 실패 0건"이 화면에서 "전부 성공"으로 읽힌다.
    unknown = [
        {
            "name": call.get("name"),
            "ts": call.get("ts"),
            "input": call.get("input"),
            "search_origin": call.get("search_origin"),
        }
        for call in calls
        if call.get("ok") is None
    ]
    result = {
        "tool_names": sorted(counts),
        "tool_call_counts": counts,
        "tool_calls": calls,
        # ARIA 가 스트림에서 직접 읽은 검색어다. 보고서가 무엇이라고 쓰든
        # 실제로 나간 질의는 이것이다. 호출 수와는 다른 값이므로 따로 센다.
        "search_queries": queries,
        "search_call_count": len(search_calls),
        # 연 주소가 아니라 '열려고 한' 주소다. 성공 여부는 아래에 따로 있다.
        "attempted_fetch_urls": attempted,
        # 열람에 성공했고 그 본문을 실제로 읽은 것까지 확인된 주소.
        "succeeded_fetch_urls": succeeded,
        # 검색어가 아니라 URL 로 부른 호출. 시도일 뿐 열람이 아니다.
        "url_lookup_attempts": url_lookups,
        # 구조적으로 실패가 확인된 호출.
        "tool_failures": failures,
        # 성공도 실패도 관측할 수 없었던 호출.
        "unknown_tool_outcomes": unknown,
    }
    if search_origin:
        result["search_queries_by_origin"] = {search_origin: queries}
    return result


def merge_observed(*sections: dict) -> dict:
    calls: list[dict] = []
    uses: list[str] = []
    by_origin: dict[str, list[str]] = {}
    for section in sections:
        calls.extend(section.get("tool_calls") or [])
        counts = section.get("tool_call_counts") or {}
        for name, count in counts.items():
            uses.extend([name] * int(count or 0))
        for origin, queries in (section.get("search_queries_by_origin") or {}).items():
            by_origin.setdefault(origin, []).extend(queries or [])
    merged = observed(calls, uses)
    merged["search_queries_by_origin"] = by_origin
    return merged


def empty_epo_section(enabled: bool = False, reason: str = "") -> dict:
    """EPO 를 돌리지 않은 실행의 기록. 키는 늘 같은 모양으로 있어야 한다.

    없는 키로 두면 화면과 통계가 "EPO 를 안 켰다"와 "켰는데 기록이 없다"를
    구별하지 못한다.
    """
    return {
        "enabled": enabled,
        "backend_id": EPO_BACKEND_ID,
        "reason": reason,
        "channel_budget": {},
        "lanes": [],
        "error": "",
    }


def epo_lane_record(
    *,
    origin: str,
    run,
    status: str,
    error: str = "",
) -> dict:
    """EPO 레인 하나의 기록. 에이전트 결과를 그대로 옮긴다.

    후보를 다른 레인과 합치지 않는다. 합치면 어느 검색어가 어느 후보를 데려
    왔는지 잃고, 그것이 이 단계에서 재려는 값이다.
    """
    data = run.to_dict() if run is not None else {}
    return {
        "id": lane_id(LANE_CHANNEL_EPO, origin),
        "channel": LANE_CHANNEL_EPO,
        "origin": origin,
        "status": status,
        "error": error,
        "termination_reason": data.get("termination_reason", ""),
        "termination_detail": data.get("termination_detail", ""),
        "cancelled": bool(data.get("cancelled", False)),
        "rounds": data.get("rounds", []),
        "queries": data.get("queries", []),
        "candidates": data.get("candidates", []),
        "search_calls": data.get("search_calls", 0),
        "detail_fetches": data.get("detail_fetches", 0),
        "invalid_responses": data.get("invalid_responses", 0),
        "notes": data.get("notes", []),
        "usage": data.get("usage", {}),
        # 이 레인이 검색 전에 청구항을 어떻게 나눠 읽었는가. 검색어의 근거이며
        # 별도 모델 턴 없이 첫 응답에서 온다.
        "claim_analysis": data.get("claim_analysis", {}),
        # 모델이 고른 유망 후보. 최종 대응표로 넘어가는 것은 이 목록뿐이다.
        "shortlist": data.get("shortlist", []),
        # 상한·대조 실패로 넘기지 않은 것. 조용히 누락하지 않는다.
        "excluded": data.get("excluded", []),
        # NO_TOOLS 계획 턴에서 감지된 도구 호출과 그때의 격리 수준.
        "tool_violations": data.get("tool_violations", []),
        "tool_isolation": data.get("tool_isolation", ""),
        # 검색하지 않는 최종 선택 턴. 검색 라운드와 사용량 축이 다르다.
        "selection": data.get("selection", {}),
    }


def epo_candidate(
    *,
    index: int,
    doc_number: str,
    title: str = "",
    source_url: str = "",
    lanes: list[str] | None = None,
    search_origins: list[str] | None = None,
    shortlist_reasons: list[dict] | None = None,
    artifact_ids: list[str] | None = None,
    evidence_fields: list[str] | None = None,
    first_seen_round: int = 0,
) -> dict:
    """EPO 독립 검색이 데려온 후보 하나를 대응표에 넣을 모양으로 만든다.

    모델이 채우는 칸이 하나도 없다. 문헌번호·명칭·주소는 ARIA 가 받아 보존한
    OPS 응답에서 나왔고, 모델이 준 것은 "이것을 다시 보라"는 선택과 그 이유뿐이다.
    그래서 shortlist 의 이유는 note 가 아니라 별도 칸에 담는다 — note 는
    보고서에서 대응 판단으로 읽히는 자리다.

    **페이지 분류를 만들지 않는다.** 이 후보에는 열어 본 웹 페이지가 없으므로
    page_fetch_succeeded 도 identifier_url_matched 도 거짓이고, 그 값으로
    page_observed 근거가 만들어질 수 없다. 존재하지 않는 관측을 지어내지 않기
    위해서다. 정식 분류는 공식 응답 대조를 통과한 뒤에만 붙는다.
    """
    url = _text(source_url, 1000)
    return {
        "index": index,
        "group": None,
        "provisional_group": None,
        "classification_basis": CLASSIFICATION_NONE,
        "provisional": True,
        "channel": CHANNEL_PATENT_DB,
        "backend_id": EPO_BACKEND_ID,
        "doc_type": "patent",
        "doc_number": _text(doc_number, 120),
        "doi": "",
        "title": _text(title, 500),
        "applicant": "",
        "url": url,
        "canonical_url": normalize_url(url),
        "family": "",
        "provenance": PROV_OFFICIAL_RESPONSE,
        "evidence_status": EVIDENCE_CANDIDATE,
        # OPS 응답을 받아 보존했다는 것과 그 문장이 원문 인용이라는 것은 다르다.
        # 원문 등급은 provenance 검증기만 붙일 수 있고 여기서 열지 않는다.
        "original_verified": False,
        # 웹 축의 값은 전부 거짓이다. 이 후보에는 연 페이지가 없다.
        "page_fetch_succeeded": False,
        "url_is_document": bool(url),
        "identifier_url_matched": False,
        "quarantined": False,
        "quarantine_reason": "",
        "group_eligible": False,
        "page_supported_rows": 0,
        "official_evidence": {},
        "verification": empty_candidate_verification(
            reason_code="official_verification_not_run",
            detail="공식 문헌 검증을 아직 실행하지 않았습니다.",
        ),
        "verbatim_excerpt": UNVERIFIED_EXCERPT,
        "source_location": UNVERIFIED_LOCATION,
        "mapping": [],
        "note": "",
        "search_origins": list(search_origins or [ORIGIN_CLAIM_ONLY]),
        "origin_groups": {},
        "origin_provisional_groups": {},
        "discovery_origins": [DISCOVERY_EPO],
        "epo_discovery": {
            "lanes": list(lanes or []),
            "first_seen_round": int(first_seen_round or 0),
            # 검증 단계가 이 참조로 원본을 다시 읽는다. 같은 자료를 다시
            # 내려받지 않기 위한 유일한 통로다.
            "artifact_ids": list(artifact_ids or []),
            "evidence_fields": sorted(evidence_fields or []),
            "shortlist": [dict(item) for item in (shortlist_reasons or [])],
        },
    }


def _shortlisted(epo: dict | None) -> list[dict]:
    """완료된 EPO 레인의 shortlist 를 문헌 단위로 모은다.

    완료되지 않은 레인은 제외한다. 채널 대조와 같은 기준을 쓴다 — 중간에
    끊긴 레인의 목록을 최종 대응표에 올리면, 예산이 마른 실행과 실제로 그
    문헌만 찾은 실행이 같아 보인다.
    """
    rows: dict[str, dict] = {}
    for lane in (epo or {}).get("lanes") or []:
        if not isinstance(lane, dict):
            continue
        if lane.get("status") != "ok":
            continue
        if lane.get("termination_reason") not in _COMPARABLE_EPO_TERMINATIONS:
            continue
        candidates = {
            _text(item.get("doc_number"), 120): item
            for item in lane.get("candidates") or []
            if isinstance(item, dict) and item.get("doc_number")
        }
        for entry in lane.get("shortlist") or []:
            if not isinstance(entry, dict):
                continue
            number = _text(entry.get("doc_number"), 120)
            source = candidates.get(number)
            if source is None:
                # epo_agent 가 이미 걸러 낸 값이다. 여기 남아 있으면 옛 기록이며
                # 근거가 없으므로 올리지 않는다.
                continue
            key = "|".join(sorted(_comparison_number_variants(number))) or number
            row = rows.setdefault(
                key,
                {
                    "doc_number": number,
                    "title": _text(source.get("title"), 500),
                    "source_url": _text(source.get("source_url"), 1000),
                    "lanes": [],
                    "search_origins": [],
                    "artifact_ids": [],
                    "evidence_fields": [],
                    "shortlist": [],
                    "first_seen_round": int(source.get("first_seen_round") or 0),
                },
            )
            lane_id_value = _text(lane.get("id"), 120)
            if lane_id_value and lane_id_value not in row["lanes"]:
                row["lanes"].append(lane_id_value)
            origin = _text(lane.get("origin"), 60)
            if origin in SEARCH_ORIGINS and origin not in row["search_origins"]:
                row["search_origins"].append(origin)
            for artifact_id in source.get("artifact_ids") or []:
                if artifact_id and artifact_id not in row["artifact_ids"]:
                    row["artifact_ids"].append(artifact_id)
            for name in (source.get("evidence") or {}):
                if name not in row["evidence_fields"]:
                    row["evidence_fields"].append(name)
            row["shortlist"].append(
                {
                    "lane": lane_id_value,
                    "round": int(entry.get("round") or 0),
                    "reason": _text(entry.get("reason"), 2000),
                    "matched_elements": _string_list(entry.get("matched_elements")),
                }
            )
            if not row["title"]:
                row["title"] = _text(source.get("title"), 500)
    return list(rows.values())


def empty_reported(*, web_report_error: str = "") -> dict:
    """후보 목록의 빈 골격. 웹 보고를 읽지 못한 실행에서 쓴다.

    키 모양은 parse()/merge_reported() 가 만드는 것과 **같아야** 한다. 다운스트림
    (검증·보고서·화면)이 모양으로 분기하지 않고 내용으로만 판단하게 하기
    위해서다.

    ``web_report_error`` 는 이 골격이 왜 비었는지다. 비어 있지 않다는 것은 "웹
    채널이 후보를 못 찾았다"가 아니라 **"웹 채널의 출력을 읽지 못했다"**는 뜻이고,
    그 둘은 사용자에게 전혀 다른 말이다. 이 값을 잃으면 EPO 만으로 만든 결과가
    두 채널을 다 돌린 결과처럼 읽힌다.
    """
    return {
        "rounds": [],
        "term_expansions": [],
        "candidates": [],
        "access_failures": [],
        "web_report_error": _text(web_report_error, 2000),
    }


def empty_literature_section(*, enabled: bool = False, reason: str = "") -> dict:
    """ARIA 서지 검색 채널의 빈 기록. 돌지 않은 실행에서도 같은 모양으로 남는다.

    "검색하지 않았다"와 "검색했는데 0건이었다"를 구분할 수 있어야 한다. 전자는
    enabled=False 이고 reason 이 있으며, 후자는 enabled=True 에 queries 가 있고
    candidates 가 비어 있다.
    """
    return {
        "enabled": bool(enabled),
        "backend_id": LITERATURE_BACKEND_ID,
        "reason": _text(reason, 500),
        "queries": [],
        # 후보표에 올린 문헌.
        "candidates": [],
        # 받은 문헌 전부. 상한에 걸려 올리지 못한 것도 여기 남는다.
        "discovered": [],
        "usage": {},
        "limits": {},
    }


def merge_epo_discoveries(
    reported: dict | None,
    epo: dict | None,
    *,
    web_report_error: str = "",
) -> tuple[dict | None, list[str]]:
    """EPO 독립 검색의 유망 후보를 주 대응표 후보 목록에 연결한다.

    **epo.lanes 원본은 손대지 않는다.** 이 함수는 reported 쪽에만 쓴다. 레인
    기록은 검색 감사용으로 그대로 남아야, "EPO 를 켜서 무엇을 더 찾았는가"를
    나중에도 물을 수 있다.

    같은 문헌이 양쪽에 있으면 **웹 후보를 기준으로 합친다.** 웹 후보에는 페이지
    관측 근거로 받은 분류가 붙어 있을 수 있고, 그 분류를 EPO 발견 사실로 덮으면
    안 된다(충돌 규칙은 공식 응답 대조 단계가 판정한다). EPO 쪽에서 오는 것은
    발견 경로 표시와 재사용할 아티팩트 참조뿐이다.
    """
    notes: list[str] = []
    rows = _shortlisted(epo)
    if not rows:
        return reported, notes

    if reported is None:
        # 웹 보고를 읽지 못했다. 그렇다고 완료된 EPO 검색까지 버릴 이유는 없다 —
        # 두 채널은 격리되어 있고, 한쪽의 출력 형식 오류가 다른 쪽이 실제로 받아
        # 보존한 공식 응답을 무효로 만들지 않는다.
        #
        # 웹의 실패 상태는 골격 안에 그대로 들고 간다. 지우면 EPO 만으로 만든
        # 결과가 두 채널을 다 돌린 결과처럼 보인다.
        reported = empty_reported(web_report_error=web_report_error)
        notes.append(
            "웹 채널의 구조화 결과를 읽지 못했지만 완료된 EPO 독립 검색이 있어 "
            "EPO 후보만으로 후보 목록을 만들었습니다. 이 목록에는 웹 검색이 찾은 "
            "문헌이 하나도 들어 있지 않습니다."
        )

    candidates = list(reported.get("candidates") or [])
    by_variant: dict[str, dict] = {}
    for candidate in candidates:
        for variant in _comparison_number_variants(candidate.get("doc_number")):
            by_variant.setdefault(variant, candidate)

    next_index = max((int(item.get("index") or 0) for item in candidates), default=0)
    for row in rows:
        matched = None
        for variant in _comparison_number_variants(row["doc_number"]):
            if variant in by_variant:
                matched = by_variant[variant]
                break
        if matched is not None:
            # 중복이다. 후보를 새로 만들지 않고 출처만 늘린다.
            origins = discovery_origins(matched)
            if DISCOVERY_EPO not in origins:
                origins.append(DISCOVERY_EPO)
            matched["discovery_origins"] = [
                origin for origin in DISCOVERY_ORIGINS if origin in origins
            ]
            existing = dict(matched.get("epo_discovery") or {})
            merged_artifacts = list(existing.get("artifact_ids") or [])
            for artifact_id in row["artifact_ids"]:
                if artifact_id not in merged_artifacts:
                    merged_artifacts.append(artifact_id)
            matched["epo_discovery"] = {
                "lanes": row["lanes"],
                "first_seen_round": row["first_seen_round"],
                "artifact_ids": merged_artifacts,
                "evidence_fields": sorted(row["evidence_fields"]),
                "shortlist": row["shortlist"],
                # 웹 후보의 표기와 OPS 표기가 다를 수 있다. 둘 다 남긴다.
                "doc_number": row["doc_number"],
            }
            notes.append(
                f"후보 {matched.get('index')}: 같은 공개번호를 EPO 독립 검색"
                f"({', '.join(row['lanes']) or '레인 미기록'})도 찾아 발견 경로를 "
                "web + epo 로 합쳤습니다."
            )
            continue

        next_index += 1
        candidate = epo_candidate(
            index=next_index,
            doc_number=row["doc_number"],
            title=row["title"],
            source_url=row["source_url"],
            lanes=row["lanes"],
            search_origins=row["search_origins"] or [ORIGIN_CLAIM_ONLY],
            shortlist_reasons=row["shortlist"],
            artifact_ids=row["artifact_ids"],
            evidence_fields=row["evidence_fields"],
            first_seen_round=row["first_seen_round"],
        )
        candidates.append(candidate)
        for variant in _comparison_number_variants(candidate["doc_number"]):
            by_variant.setdefault(variant, candidate)
        notes.append(
            f"후보 {next_index}: EPO 독립 검색이 찾은 {row['doc_number']} 를 "
            "대응표 후보로 올렸습니다. 정식 A/B/C 는 공식 응답에 구성 대응이 "
            "대조된 뒤에만 붙습니다."
        )

    reported["candidates"] = candidates
    return reported, notes


def _doi_key(raw) -> str:
    """후보 표기에서 DOI 키 하나를 뽑는다. 아니면 빈 문자열.

    특허번호 변형 규칙(_comparison_number_variants)을 DOI 에 쓰면 안 된다. 그쪽은
    앞 두 글자를 국가코드로 보고 떼어 내므로 ``10.3390/...`` 의 ``10`` 이 사라진다.
    """
    from .patent_search import literature_client

    try:
        return literature_client.normalize_doi(raw)
    except Exception:
        return ""


def candidate_doi(candidate: dict) -> str:
    """후보의 DOI. doi 칸을 먼저 보고 없으면 문헌번호에서 찾는다."""
    entry = candidate or {}
    return _doi_key(entry.get("doi")) or _doi_key(entry.get("doc_number"))


def literature_candidate(
    *,
    index: int,
    doi: str,
    title: str = "",
    authors: str = "",
    container: str = "",
    source_url: str = "",
    search_origins: list[str] | None = None,
    queries: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    evidence_fields: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict:
    """ARIA 의 서지 검색이 데려온 논문 후보 하나.

    epo_candidate 와 같은 원칙이다 — 모델이 채우는 칸이 하나도 없다. DOI·제목·
    저자·저널은 ARIA 가 받아 보존한 Crossref/Europe PMC 응답에서 나왔고, 모델이
    준 것은 검색어뿐이다.

    **페이지 분류를 만들지 않는다.** 이 후보에는 열어 본 웹 페이지가 없다. 정식
    분류는 공식 서지 대조를 통과한 뒤에만 붙는다.
    """
    url = _text(source_url, 1000)
    key = _doi_key(doi)
    return {
        "index": index,
        "group": None,
        "provisional_group": None,
        "classification_basis": CLASSIFICATION_NONE,
        "provisional": True,
        "channel": CHANNEL_PATENT_DB,
        "backend_id": LITERATURE_BACKEND_ID,
        "doc_type": "paper",
        "doc_number": key,
        "doi": key,
        "title": _text(title, 500),
        # 논문에는 출원인이 없다. 저자를 그 칸에 넣되 라벨은 보고서가 정한다.
        "applicant": _text(authors, 500),
        "url": url,
        "canonical_url": normalize_url(url),
        "family": "",
        "provenance": PROV_OFFICIAL_RESPONSE,
        "evidence_status": EVIDENCE_CANDIDATE,
        "original_verified": False,
        "page_fetch_succeeded": False,
        "url_is_document": bool(url),
        "identifier_url_matched": False,
        "quarantined": False,
        "quarantine_reason": "",
        "group_eligible": False,
        "page_supported_rows": 0,
        "official_evidence": {},
        "verification": empty_candidate_verification(
            reason_code="official_verification_not_run",
            detail="공식 서지 대조를 아직 실행하지 않았습니다.",
        ),
        "verbatim_excerpt": UNVERIFIED_EXCERPT,
        "source_location": UNVERIFIED_LOCATION,
        "mapping": [],
        "note": "",
        "search_origins": list(search_origins or [ORIGIN_CLAIM_ONLY]),
        "origin_groups": {},
        "origin_provisional_groups": {},
        "discovery_origins": [DISCOVERY_LITERATURE],
        "literature_discovery": {
            "queries": [_text(item, 400) for item in (queries or [])],
            "sources": sorted(sources or []),
            "container": _text(container, 300),
            "artifact_ids": list(artifact_ids or []),
            "evidence_fields": sorted(evidence_fields or []),
        },
    }


def merge_literature_discoveries(
    reported: dict | None,
    discovery: dict | None,
    *,
    web_report_error: str = "",
) -> tuple[dict | None, list[str]]:
    """ARIA 서지 검색의 후보를 주 대응표 후보 목록에 연결한다.

    merge_epo_discoveries 와 같은 규칙이다. 같은 문헌이 양쪽에 있으면 **웹 후보를
    기준으로 합치고**, 서지 쪽에서 오는 것은 발견 경로 표시와 아티팩트 참조뿐이다.

    맞추는 키는 DOI 다. 웹 후보가 DOI 를 적었으면 같은 문헌으로 묶이고, 제목만
    적었으면 묶이지 않는다 — 제목으로 문헌을 동일시하면 같은 학회의 후속 논문이
    선행 논문과 하나로 합쳐진다.
    """
    notes: list[str] = []
    rows = [
        row
        for row in ((discovery or {}).get("candidates") or [])
        if isinstance(row, dict) and _doi_key(row.get("doi") or row.get("doc_number"))
    ]
    if not rows:
        return reported, notes

    if reported is None:
        reported = empty_reported(web_report_error=web_report_error)
        notes.append(
            "웹 채널의 구조화 결과를 읽지 못했지만 ARIA 서지 검색 결과가 있어 "
            "그 후보만으로 목록을 만들었습니다. 이 목록에는 웹 검색이 찾은 문헌이 "
            "하나도 들어 있지 않습니다."
        )

    candidates = list(reported.get("candidates") or [])
    by_doi: dict = {}
    for candidate in candidates:
        key = candidate_doi(candidate)
        if key:
            by_doi.setdefault(key, candidate)

    next_index = max((int(item.get("index") or 0) for item in candidates), default=0)
    for row in rows:
        key = _doi_key(row.get("doi") or row.get("doc_number"))
        matched = by_doi.get(key)
        if matched is not None:
            origins = discovery_origins(matched)
            if DISCOVERY_LITERATURE not in origins:
                origins.append(DISCOVERY_LITERATURE)
            matched["discovery_origins"] = [
                origin for origin in DISCOVERY_ORIGINS if origin in origins
            ]
            existing = dict(matched.get("literature_discovery") or {})
            merged_artifacts = list(existing.get("artifact_ids") or [])
            for artifact_id in row.get("artifact_ids") or []:
                if artifact_id not in merged_artifacts:
                    merged_artifacts.append(artifact_id)
            matched["literature_discovery"] = {
                "queries": [_text(item, 400) for item in (row.get("queries") or [])],
                "sources": sorted(row.get("sources") or []),
                "container": _text(row.get("container"), 300),
                "artifact_ids": merged_artifacts,
                "evidence_fields": sorted(row.get("evidence_fields") or []),
            }
            # 웹 후보가 DOI 만 적고 제목을 못 적은 경우가 흔하다. ARIA 가 공식
            # 응답에서 받은 제목으로 빈 칸만 채운다 — 이미 있는 값은 덮지 않는다.
            if not _text(matched.get("title"), 500) and row.get("title"):
                matched["title"] = _text(row.get("title"), 500)
            notes.append(
                f"후보 {matched.get('index')}: 같은 DOI 를 ARIA 서지 검색도 찾아 "
                "발견 경로를 합쳤습니다."
            )
            continue

        next_index += 1
        candidate = literature_candidate(
            index=next_index,
            doi=key,
            title=row.get("title") or "",
            authors=row.get("authors") or "",
            container=row.get("container") or "",
            source_url=row.get("url") or f"https://doi.org/{key}",
            search_origins=row.get("search_origins") or [ORIGIN_CLAIM_ONLY],
            queries=row.get("queries") or [],
            artifact_ids=row.get("artifact_ids") or [],
            evidence_fields=row.get("evidence_fields") or [],
            sources=row.get("sources") or [],
        )
        candidates.append(candidate)
        by_doi.setdefault(key, candidate)
        notes.append(
            f"후보 {next_index}: ARIA 서지 검색이 찾은 {key} 를 대응표 후보로 "
            "올렸습니다. 정식 A/B/C 는 공식 응답에 구성 대응이 대조된 뒤에만 "
            "붙습니다."
        )

    reported["candidates"] = candidates
    return reported, notes


def web_lane_record(record: dict) -> dict:
    """옛 웹 레인 기록에 레인 id 와 채널을 붙인다. 내용은 그대로 둔다."""
    origin = str(record.get("id") or ORIGIN_CLAIM_ONLY)
    merged = dict(record)
    merged["id"] = lane_id(LANE_CHANNEL_WEB, origin)
    merged["channel"] = LANE_CHANNEL_WEB
    merged["origin"] = origin
    return merged


# 두 채널의 후보를 맞출 때 쓰는 기준. 기록에 적어서, 나중에 숫자를 읽는
# 사람이 무엇으로 맞춘 숫자인지 알 수 있게 한다.
#
# URL 대조용 _number_variants()는 국가코드를 떼어 낸 숫자도 만든다. URL 안에서
# 번호를 찾을 때는 유용하지만 채널끼리 문헌을 맞출 때 쓰면 EP1000000과
# US1000000이 같은 것으로 묶인다. 여기서는 국가코드를 보존하고 종류코드만
# 선택적으로 제거한다.
CHANNEL_MATCH_BASIS = "country_scoped_publication_number_variants"

_COMPARABLE_EPO_TERMINATIONS = frozenset(
    {
        "llm_finished",
        "round_limit",
        "search_call_limit",
        "detail_fetch_limit",
        "no_new_candidates",
    }
)


_UNKNOWN_DOCUMENT_NUMBERS = frozenset(
    {
        "",
        "확인 필요",
        "문헌번호 확인 필요",
        "unknown",
        "n/a",
        "none",
        "not available",
    }
)


def _comparison_number_variants(raw) -> set[str]:
    """채널 대조 전용 문헌번호 표기 변형.

    ``EP 1 000 000``과 ``EP1000000A1``은 맞추되, 국가코드가 다른 문헌이나
    국가코드가 없는 숫자를 추측으로 같은 문헌에 붙이지 않는다. 이 단계는
    패밀리 판정이 아니며 공개번호 표기 차이만 흡수한다.
    """
    text = _text(raw, 300)
    if text.lower() in _UNKNOWN_DOCUMENT_NUMBERS:
        return set()
    compact = re.sub(r"[^0-9A-Z]", "", text.upper())
    if len(compact) < 5:
        return set()

    # 국가코드가 있는 공개번호는 그 범위 안에서만 종류코드를 제거한다.
    if re.match(r"^[A-Z]{2}\d", compact):
        publication = re.sub(r"[A-Z]\d?$", "", compact)
        return {f"publication:{publication}"}

    # 국가코드가 없는 값은 같은 맨문자열끼리만 맞춘다. EP/US 등의 후보에
    # 추측으로 붙이는 순간 교차 발견 수가 사실보다 부풀기 때문이다.
    return {f"bare:{compact}"}


def _union_find(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """(항목, 표기) 쌍에서 같은 문헌끼리 묶은 대표 이름표를 돌려준다."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left
    return {node: find(node) for node in parent}


def _comparable(candidates: list[dict]) -> tuple[list[dict], int]:
    """대조할 수 있는 후보와, 문헌번호가 없어 대조하지 못한 수."""
    usable: list[dict] = []
    unidentified = 0
    for candidate in candidates:
        variants = _comparison_number_variants(candidate.get("doc_number"))
        if not variants:
            unidentified += 1
            continue
        usable.append({**candidate, "_variants": variants})
    return usable, unidentified


def empty_channel_comparison(reason: str = "") -> dict:
    """대조하지 않은 실행의 기록. 키 모양은 늘 같아야 한다."""
    return {
        "compared": False,
        "complete": False,
        "reason": reason,
        "match_basis": CHANNEL_MATCH_BASIS,
        "web": {
            "total": 0,
            "identified": 0,
            "unique_identified": 0,
            "unidentified": 0,
            "quarantined": 0,
        },
        "epo": {
            "total": 0,
            "identified": 0,
            "unique_identified": 0,
            "unidentified": 0,
            "excluded_lanes": [],
        },
        "counts": {"both": 0, "epo_only": 0, "web_only": 0},
        "both": [],
        "epo_only": [],
        "web_only": [],
    }


def compare_channels(reported: dict | None, epo: dict | None) -> dict:
    """웹 채널과 EPO 채널이 각각 무엇을 찾았는지 대조한다.

    두 채널의 기록을 **섞지 않는다.** 이 절은 파생된 관점이고, reported 와 epo
    는 손대지 않은 채로 남는다. 어느 쪽 후보도 다른 쪽으로 옮기거나 다시
    분류하지 않는다 — 그렇게 하는 순간 "EPO 가 무엇을 더 데려왔는가"를 물을 수
    없게 되고, 그 질문이 이 채널을 켜 둘 이유의 전부다.

    같은 문헌을 두 채널이 다르게 적는다. 모델 보고는 "EP 1 000 000"처럼 쓰고
    OPS 는 종류코드까지 붙인 "EP1000000A1"을 준다. 비교 전용 변형은 국가코드를
    보존한 채 종류코드만 제거한다. 따라서 같은 숫자의 EP/US 문헌을 합치지 않고,
    같은 패밀리의 서로 다른 공개번호도 이 단계에서 추측으로 합치지 않는다.

    문헌번호가 없는 후보(DOI 만 있는 논문 등)는 대조 대상이 아니다. 세어서
    따로 적고 어느 목록에도 넣지 않는다. web_only 에 넣으면 "EPO 가 놓쳤다"로
    읽히는데, OPS 에 있을 수 없는 문헌을 놓쳤다고 적는 것은 거짓이다.
    """
    section = epo or {}
    lanes = list(section.get("lanes") or [])
    if not section.get("enabled") or not lanes:
        return empty_channel_comparison(
            reason=str(section.get("reason") or "EPO 채널이 실행되지 않았습니다.")
        )
    if reported is None:
        return empty_channel_comparison(
            reason="웹 채널의 구조화 결과가 없어 두 채널을 대조하지 않았습니다."
        )

    comparable_lanes = [
        lane
        for lane in lanes
        if lane.get("status") == "ok"
        and int(lane.get("search_calls") or 0) > 0
        and lane.get("termination_reason") in _COMPARABLE_EPO_TERMINATIONS
    ]
    excluded_lanes = [
        str(lane.get("id") or "(이름 없는 EPO 레인)")
        for lane in lanes
        if lane not in comparable_lanes
    ]
    if not comparable_lanes:
        return empty_channel_comparison(
            reason="완료된 OPS 검색이 없어 두 채널을 대조하지 않았습니다."
        )

    # **웹이 데려온 후보만** 웹 쪽 숫자에 넣는다. EPO 독립 검색이 데려온 후보도
    # merge_epo_discoveries 를 거치면 같은 목록에 들어 있는데, 그것을 웹으로
    # 세면 "EPO 에서만 찾았다"가 실행할 때마다 0 이 된다 — 이 절이 답하려는
    # 질문이 통째로 사라진다.
    web_candidates = [
        candidate
        for candidate in ((reported or {}).get("candidates") or [])
        if DISCOVERY_WEB in discovery_origins(candidate)
    ]
    web_usable, web_unidentified = _comparable(web_candidates)

    # 같은 문헌이 두 EPO 레인에 다 나올 수 있다. 레인 이름을 모아 둔다.
    epo_candidates: list[dict] = []
    for lane in comparable_lanes:
        for candidate in lane.get("candidates") or []:
            epo_candidates.append({**candidate, "lane": lane.get("id", "")})
    epo_usable, epo_unidentified = _comparable(epo_candidates)

    pairs: list[tuple[str, str]] = []
    for index, candidate in enumerate(web_usable):
        for variant in candidate["_variants"]:
            pairs.append((f"n:{variant}", f"web:{index}"))
    for index, candidate in enumerate(epo_usable):
        for variant in candidate["_variants"]:
            pairs.append((f"n:{variant}", f"epo:{index}"))

    roots = _union_find(pairs)
    groups: dict[str, dict[str, list[dict]]] = {}
    for index, candidate in enumerate(web_usable):
        group = groups.setdefault(roots[f"web:{index}"], {"web": [], "epo": []})
        group["web"].append(candidate)
    for index, candidate in enumerate(epo_usable):
        group = groups.setdefault(roots[f"epo:{index}"], {"web": [], "epo": []})
        group["epo"].append(candidate)

    both: list[dict] = []
    epo_only: list[dict] = []
    web_only: list[dict] = []
    for group in groups.values():
        entry = _comparison_entry(group)
        if group["web"] and group["epo"]:
            both.append(entry)
        elif group["epo"]:
            epo_only.append(entry)
        else:
            web_only.append(entry)

    for bucket in (both, epo_only, web_only):
        bucket.sort(key=lambda item: item["doc_number"])

    return {
        "compared": True,
        "complete": not excluded_lanes,
        "reason": "",
        "match_basis": CHANNEL_MATCH_BASIS,
        "web": {
            "total": len(web_candidates),
            "identified": len(web_usable),
            "unique_identified": len(both) + len(web_only),
            "unidentified": web_unidentified,
            # 식별이 확인되지 않은 후보가 웹 쪽 숫자에 몇 건 섞였는지 알려 준다.
            # 이것을 모르면 "웹이 이미 찾았다"가 얼마나 단단한 말인지 모른다.
            "quarantined": sum(
                1 for item in web_candidates if item.get("quarantined")
            ),
        },
        "epo": {
            "total": len(epo_candidates),
            "identified": len(epo_usable),
            "unique_identified": len(both) + len(epo_only),
            "unidentified": epo_unidentified,
            "excluded_lanes": excluded_lanes,
        },
        "counts": {
            "both": len(both),
            "epo_only": len(epo_only),
            "web_only": len(web_only),
        },
        "both": both,
        "epo_only": epo_only,
        "web_only": web_only,
    }


def _comparison_entry(group: dict[str, list[dict]]) -> dict:
    """묶인 문헌 하나. 각 채널이 적은 표기를 **그대로** 들고 있는다."""
    web = [
        {
            "doc_number": _text(item.get("doc_number"), 120),
            "title": _text(item.get("title"), 500),
            "origins": list(item.get("search_origins") or []),
            "quarantined": bool(item.get("quarantined", False)),
        }
        for item in group["web"]
    ]
    lanes_by_number: dict[str, dict] = {}
    for item in group["epo"]:
        number = _text(item.get("doc_number"), 120)
        row = lanes_by_number.setdefault(
            number,
            {"doc_number": number, "title": _text(item.get("title"), 500), "lanes": []},
        )
        if item.get("lane") and item["lane"] not in row["lanes"]:
            row["lanes"].append(item["lane"])
    epo = list(lanes_by_number.values())
    representatives = web + epo
    representative = representatives[0]
    title = next(
        (str(item.get("title") or "") for item in representatives if item.get("title")),
        "",
    )
    return {
        "doc_number": representative.get("doc_number", ""),
        "title": title,
        "web": web,
        "epo": epo,
    }


def build(
    *,
    claim_text: str,
    prompt_id: str,
    prompt_sha256: str,
    runtime_context_sha256: str = "",
    reasoning_effort: str = "",
    claim_boundary_neutralized: bool,
    spec_document: dict | None = None,
    spec_boundary_neutralized: bool = False,
    search_focus: dict | None = None,
    focus_boundary_neutralized: bool = False,
    started_at: str | None,
    completed_at: str | None,
    tool_calls: list[dict] | None,
    tool_uses: list[str] | None,
    tool_policy_name: str,
    allowed_tools: tuple[str, ...] | list[str],
    reported: dict | None,
    notes: list[str] | None,
    error: str | None,
    advertised_tools_enforced: bool = True,
    observed_section: dict | None = None,
    search_strategy: str | None = None,
    search_lanes: list[dict] | None = None,
    max_tool_calls_total: int | None = None,
    lane_budgets: dict[str, int] | None = None,
    max_content_reads_total: int | None = None,
    content_read_lane_budgets: dict[str, int] | None = None,
    lanes: list[dict] | None = None,
    epo: dict | None = None,
    literature: dict | None = None,
    verification: dict | None = None,
) -> dict:
    """저장할 감사 기록을 만든다.

    모델 보고를 읽지 못했어도 기록은 남긴다. "무엇을 검색했는지"는 ARIA 가
    관측한 부분에 이미 들어 있고, 그 편이 실패한 실행에서 더 중요하다.
    """
    strategy = search_strategy or (
        "isolated_union" if spec_document is not None else ORIGIN_CLAIM_ONLY
    )
    epo_section = epo or empty_epo_section()
    literature_section = literature or empty_literature_section()
    channels_used = {
        candidate.get("channel")
        for candidate in ((reported or {}).get("candidates") or [])
        if candidate.get("channel")
    }
    if any(
        lane.get("candidates")
        for lane in (epo_section.get("lanes") or [])
        if isinstance(lane, dict)
    ):
        channels_used.add(CHANNEL_PATENT_DB)
    if literature_section.get("candidates"):
        channels_used.add(CHANNEL_PATENT_DB)
    return {
        "version": MANIFEST_VERSION,
        "generated_at": _utcnow_iso(),
        # channels 는 이 스키마가 아는 채널 목록이다(원래 의미 그대로 둔다 —
        # 이미 디스크에 있는 기록과 키가 어긋나지 않게).
        #
        # 다만 이것만 있으면 웹 검색만 한 실행의 기록에도 patent_db 가 보여
        # "이 검색은 특허 DB 도 봤다"는 인상을 준다. 그래서 이번 실행에서
        # 실제로 후보가 나온 채널을 따로 남긴다.
        "channels": list(KNOWN_CHANNELS),
        # 그룹 정의를 기록에 싣는다. 렌더러가 자기 표를 들고 있으면 정의를 고친
        # 뒤 갱신되지 않은 렌더러가 옛 제목으로 인쇄한다. 실제로 그렇게 어긋난
        # 적이 있다(GROUP_DEFINITIONS 주석 참조).
        "group_schema_version": GROUP_SCHEMA_VERSION,
        "group_definitions": dict(GROUP_DEFINITIONS),
        "channels_used": sorted(channels_used),
        "input": {
            "claim_text": claim_text,
            "claim_boundary_neutralized": claim_boundary_neutralized,
            # 넣었으면 파일 신원만 남긴다. 명세서 본문은 최종 프롬프트와
            # 첨부 원본에 이미 있고, 감사 기록에 한 벌 더 복사할 이유가 없다.
            "spec_document": spec_document,
            "spec_boundary_neutralized": spec_boundary_neutralized,
            # 원 보고서 전체는 복사하지 않는다. 선택된 구성 스냅샷과 그 출처만
            # 남겨 이 검색의 범위와 1차→2차 순서를 재현할 수 있게 한다.
            "search_focus": search_focus,
            "focus_boundary_neutralized": focus_boundary_neutralized,
        },
        "prompt": {
            "id": prompt_id,
            # 이 값은 **프롬프트 템플릿 파일**의 해시다. 모델에게 실제로 간
            # 프롬프트가 아니다. 런타임 컨텍스트와 청구항이 붙기 전 값이라,
            # 런타임 컨텍스트만 바뀐 두 실행이 여기서는 같아 보인다 —
            # 2026-08-30 에 이 값을 근거로 "프롬프트 동일"이라고 잘못 보고했다.
            # 실제로는 레인 해시가 3b53ad43… → bc0564c5… 로 달랐다.
            #
            # sha256 은 옛 기록·클라이언트 호환을 위해 남긴다. 새 코드는
            # template_sha256 을 읽고, "같은 프롬프트인가"는 아래 두 값으로
            # 판단해야 한다.
            "sha256": prompt_sha256,
            "template_sha256": prompt_sha256,
            "runtime_context_sha256": runtime_context_sha256,
            # 레인별로 모델에게 실제로 간 프롬프트의 해시.
            "effective_prompt_sha256": {
                str(lane.get("id") or ""): str(lane.get("prompt_sha256") or "")
                for lane in (search_lanes or [])
                if lane.get("id")
            },
        },
        # 추론강도. **실제로 적용된 값은 적지 않는다.** CLI 가 알려주지 않으므로
        # 확정할 수 없고, 추정값을 적으면 감사 기록이 관측이 아니라 추측이 된다.
        # 여기 있는 것은 "우리가 무엇을 요청했는가" 뿐이다.
        "reasoning_effort": {
            # 사용자가 고른 값. 빈 문자열이면 고르지 않았다는 뜻이다.
            "requested": str(reasoning_effort or ""),
            # 아무 것도 넘기지 않아 모델 카탈로그의 기본값에 맡겼는가.
            "model_default": not str(reasoning_effort or "").strip(),
        },
        "policy": {
            "name": tool_policy_name,
            "allowed_tools": list(allowed_tools),
            "advertised_tools_enforced": advertised_tools_enforced,
            "max_rounds": MAX_ROUNDS,
            "search_strategy": strategy,
            "candidate_merge": "union" if strategy == "isolated_union" else "single",
            "max_tool_calls_total": max_tool_calls_total,
            "lane_budgets": dict(lane_budgets or {}),
            # 페이지 본문 읽기는 검색 호출과 다른 예산으로 센다. 한 예산에
            # 섞으면 본문을 성실히 읽을수록 검색 예산이 마른다.
            "max_content_reads_total": max_content_reads_total,
            "content_read_lane_budgets": dict(content_read_lane_budgets or {}),
            # ARIA 는 WebSearch 의 검색 도메인을 기술적으로 제한하지 못한다.
            # 기록에 남겨 두어야 나중에 이 실행의 범위를 오해하지 않는다.
            "search_domain_restriction": False,
        },
        "timing": {"started_at": started_at, "completed_at": completed_at},
        # search_lanes 는 **웹 레인만** 담는 옛 키다. 이미 디스크에 있는
        # 기록과 화면이 이 모양을 읽으므로 뜻을 바꾸지 않는다.
        "search_lanes": list(search_lanes or []),
        # v5 의 정본. 네 레인 전부를 고정 id 로 담는다.
        "lanes": list(lanes or []),
        # EPO 채널의 기록. 웹과 **섞지 않는다** — 후보·검색어·오류·종료 사유·
        # 사용량이 전부 여기 따로 있다. 초기 보정 단계에서 두 채널을 비교하려면
        # 섞이지 않은 채로 남아 있어야 한다.
        "epo": epo_section,
        # v10 의 정본. ARIA 가 Crossref·Europe PMC 에 직접 물어 만든 기록이다.
        # 웹·EPO 와 섞지 않는다 — 이 채널만 논문을 **식별**할 수 있으므로, 무엇을
        # 물어 무엇을 받았는지가 따로 남아야 "웹이 못 찾은 것을 여기서 찾았는가"에
        # 답할 수 있다.
        "literature": literature_section,
        # Codex 후보의 공식 문헌 확보·2차 분류 기록. 검색 전체 요약과 별도로
        # 각 후보에도 verification 객체가 있어 부분 실패를 추적할 수 있다.
        "verification": dict(verification or {}),
        # v6 의 정본. 두 채널이 각각 무엇을 찾았는지 대조한 **파생** 기록이다.
        # 위의 reported 와 epo 는 그대로 두고, 여기서만 맞춰 본다.
        "channel_comparison": compare_channels(reported, epo_section),
        "observed": observed_section or observed(tool_calls, tool_uses),
        "reported": reported,
        "normalization_notes": list(notes or []),
        "error": error,
    }
