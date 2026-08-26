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
                      검색 프롬프트 버전과 해시, 실행 시각

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

MANIFEST_VERSION = 4
_OPEN = "[ARIA_SEARCH_LOG_V1]"
_CLOSE = "[/ARIA_SEARCH_LOG_V1]"

_BLOCK = re.compile(
    r"(?:```[\w-]*\s*\n)?"
    + re.escape(_OPEN)
    + r"\s*(?P<payload>.*?)\s*"
    + re.escape(_CLOSE)
    + r"(?:\s*\n```)?",
    re.DOTALL,
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
PROVENANCE = (PROV_SNIPPET, PROV_WEBFETCH, PROV_RAW)

EVIDENCE_CANDIDATE = "candidate_only"
EVIDENCE_REVIEWED = "source_page_reviewed"
EVIDENCE_STATUS = (EVIDENCE_CANDIDATE, EVIDENCE_REVIEWED)

GROUPS = ("A", "B", "C")

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
SUPPORT_SOURCES = (SUPPORT_PAGE, SUPPORT_SNIPPET, SUPPORT_NONE)

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

_UNVERIFIED_EXCERPT = "원문에서 확인되지 않음"
_UNVERIFIED_LOCATION = "확인 필요"

# Provider 별 도구 이름. 감사 스키마는 같은 web 채널로 정규화하지만 실제 호출
# 이름은 tool_calls 에 그대로 보존한다.
# Provider 마다 도구 이름이 다르다. 한 곳에서만 정의해서 runner 의 진행
# 표시와 여기 감사 집계가 같은 목록을 보게 한다 — 갈라지면 검색은 돌았는데
# 횟수가 0으로 보이는 식으로 조용히 어긋난다.
#   Claude : WebSearch / WebFetch
#   agy    : search_web / read_url_content
#   Codex  : web_search (페이지를 여는 도구는 없다)
SEARCH_TOOL_NAMES = frozenset({"WebSearch", "search_web", "web_search"})
FETCH_TOOL_NAMES = frozenset({"WebFetch", "read_url_content"})
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


def _one_of(value, allowed: tuple[str, ...], fallback: str) -> str:
    candidate = _text(value, 60)
    return candidate if candidate in allowed else fallback


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
    support = _one_of(entry.get("support_source"), SUPPORT_SOURCES, SUPPORT_NONE)
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
    provenance = _one_of(entry.get("provenance"), PROVENANCE, PROV_SNIPPET)
    evidence = _one_of(entry.get("evidence_status"), EVIDENCE_STATUS, EVIDENCE_CANDIDATE)
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

    return {
        "index": index,
        "group": _one_of(entry.get("group"), GROUPS, "C"),
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
        "verbatim_excerpt": excerpt,
        "source_location": location,
        "mapping": mapping,
        "note": note,
        # 이 값은 모델이 고르는 것이 아니라 ARIA 가 실행 경로에서 붙인다.
        "search_origins": [search_origin],
        "origin_groups": {search_origin: _one_of(entry.get("group"), GROUPS, "C")},
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
            key = _identity_key(candidate, f"anon:{report_index}:{candidate_index}")
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = dict(candidate)
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

            # 더 잘 확인된 경로의 서지·증거 필드를 사용하되, 기본 검색의 분류는
            # 명세서 보조 검색이 덮어쓰지 못한다.
            if _evidence_score(candidate) > _evidence_score(existing):
                base_group = origin_groups.get(
                    ORIGIN_CLAIM_ONLY, existing.get("group", candidate.get("group"))
                )
                replacement = dict(candidate)
                replacement["group"] = base_group
                existing = replacement

            existing["search_origins"] = origins
            existing["origin_groups"] = origin_groups
            if candidate.get("note") and candidate.get("note") != existing.get("note"):
                notes = [value for value in (existing.get("note"), candidate.get("note")) if value]
                existing["note"] = " / ".join(dict.fromkeys(notes))
            candidates[key] = existing

    merged_candidates = list(candidates.values())
    for index, candidate in enumerate(merged_candidates, start=1):
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
    queries = [
        call["input"]["query"]
        for call in calls
        if call.get("name") in _SEARCH_TOOL_NAMES
        and isinstance(call.get("input"), dict)
        and call["input"].get("query")
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
    result = {
        "tool_names": sorted(counts),
        "tool_call_counts": counts,
        "tool_calls": calls,
        # ARIA 가 스트림에서 직접 읽은 검색어다. 보고서가 무엇이라고 쓰든
        # 실제로 나간 질의는 이것이다.
        "search_queries": queries,
        # 연 주소가 아니라 '열려고 한' 주소다. 성공 여부는 아래에 따로 있다.
        "attempted_fetch_urls": attempted,
        # 열람에 성공했고 그 본문을 실제로 읽은 것까지 확인된 주소.
        "succeeded_fetch_urls": succeeded,
        "tool_failures": failures,
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


def build(
    *,
    claim_text: str,
    prompt_id: str,
    prompt_version: int | None,
    prompt_sha256: str,
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
) -> dict:
    """저장할 감사 기록을 만든다.

    모델 보고를 읽지 못했어도 기록은 남긴다. "무엇을 검색했는지"는 ARIA 가
    관측한 부분에 이미 들어 있고, 그 편이 실패한 실행에서 더 중요하다.
    """
    strategy = search_strategy or (
        "isolated_union" if spec_document is not None else ORIGIN_CLAIM_ONLY
    )
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
        "channels_used": sorted(
            {
                candidate.get("channel")
                for candidate in ((reported or {}).get("candidates") or [])
                if candidate.get("channel")
            }
        ),
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
            "version": prompt_version,
            "sha256": prompt_sha256,
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
        "search_lanes": list(search_lanes or []),
        "observed": observed_section or observed(tool_calls, tool_uses),
        "reported": reported,
        "normalization_notes": list(notes or []),
        "error": error,
    }
