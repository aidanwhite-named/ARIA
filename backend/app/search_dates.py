"""선택적 검색 기준일 — 비어 있으면 **날짜를 보지 않는다**.

무엇을 정하는가
---------------
유사문헌 검색 실행에 사용자가 날짜 하나를 넣을 수 있다. 그 날짜까지 **공개된**
문헌만 검색 대상으로 삼겠다는 뜻이다. 넣지 않으면 날짜 조건이 아예 없다.

비어 있을 때 오늘 날짜를 채우지 않는다
--------------------------------------
"기준일이 없으면 오늘까지"는 그럴듯하지만 틀린 기본값이다. 그렇게 하면
   * 실행한 날짜에 따라 같은 청구항의 검색 범위가 달라지고,
   * 아직 공개되지 않은(그러나 곧 공개될) 문헌을 조용히 지우며,
   * 감사 기록에는 사용자가 넣지도 않은 날짜가 "적용된 기준일"로 남는다.
비어 있으면 과거·최근·미래 공개문헌을 구분 없이 관련성 중심으로 본다.

출원일이 아니라 공개일이다
--------------------------
선행문헌 판단에서 의미가 있는 것은 그 문헌이 **언제 공중에 알려졌는가**이므로
공개일(publication date)로만 판정한다. 출원일·우선일은 공개보다 몇 년 앞서므로,
그것으로 자르면 기준일 당시 아무도 볼 수 없었던 문헌이 대상에 들어온다.

확인할 수 없는 공개일을 배제로 읽지 않는다
------------------------------------------
공개일을 모르는 후보는 "기준일 이후"가 아니라 **모르는 것**이다. 지우지 않고
``publication_date_unknown`` 으로 표시해 사용자가 직접 판단하게 둔다. 부분
날짜(연도만, 연월만)도 같다 — 그 기간의 마지막 날이 기준일보다 뒤이고 첫날이
앞이면 판정할 수 없으므로 ``ambiguous`` 로 남긴다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

#: 날짜 필터를 적용하지 않는다는 값. None 이 아니라 빈 문자열로 통일한다 —
#: JSON·DB·프론트엔드가 각자 다른 "없음"을 쓰면 세 곳에서 각각 판정해야 한다.
NO_CUTOFF = ""

# 후보 하나가 기준일에 대해 갖는 상태.
STATUS_NO_LIMIT = "no_date_limit"        # 기준일 자체가 없다
STATUS_WITHIN = "within_cutoff"          # 기준일까지 공개됐다
STATUS_AFTER = "after_cutoff"            # 기준일 뒤에 공개됐다 — 유일한 제외 사유
STATUS_AMBIGUOUS = "ambiguous"           # 부분 날짜라 기준일 전후를 가릴 수 없다
STATUS_UNKNOWN = "publication_date_unknown"  # 공개일을 확인하지 못했다
STATUSES = (
    STATUS_NO_LIMIT,
    STATUS_WITHIN,
    STATUS_AFTER,
    STATUS_AMBIGUOUS,
    STATUS_UNKNOWN,
)

STATUS_LABELS = {
    STATUS_NO_LIMIT: "날짜 제한 없음",
    STATUS_WITHIN: "기준일까지 공개됨",
    STATUS_AFTER: "기준일 이후 공개됨",
    STATUS_AMBIGUOUS: "공개일이 부분값이라 기준일 전후를 확정할 수 없음",
    STATUS_UNKNOWN: "공개일 미확인",
}

#: 제외 사유 코드. 매니페스트의 excluded 목록에 그대로 들어간다.
EXCLUDE_REASON = "published_after_cutoff"

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_PARTIAL_MONTH = re.compile(r"^(\d{4})[-/.](\d{1,2})$")
_PARTIAL_YEAR = re.compile(r"^(\d{4})$")
_LOOSE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")

PRECISION_DAY = "day"
PRECISION_MONTH = "month"
PRECISION_YEAR = "year"


class DateInputError(ValueError):
    """사용자가 넣은 기준일을 읽지 못했다. 조용히 고치지 않고 거절한다."""


def _valid(year: int, month: int, day: int) -> bool:
    if not (1000 <= year <= 9999):
        return False
    if not (1 <= month <= 12):
        return False
    return 1 <= day <= calendar.monthrange(year, month)[1]


def normalize_cutoff(value) -> str:
    """사용자 입력을 ``YYYY-MM-DD`` 로 정규화한다. 비었으면 :data:`NO_CUTOFF`.

    받는 모양은 ``YYYY-MM-DD`` 와 ``YYYYMMDD`` 뿐이다. 자유 표기를 추측해서
    받으면 "2026-01-02"가 1월 2일인지 2월 1일인지 우리가 정하게 되고, 그 추측이
    검색 범위를 바꾼다. 모르는 모양은 거절한다.
    """
    text = str(value or "").strip()
    if not text:
        return NO_CUTOFF
    match = _ISO.match(text) or _COMPACT.match(text)
    if match is None:
        raise DateInputError(
            f"검색 기준일은 YYYY-MM-DD 형식이어야 합니다: {text[:40]!r}"
        )
    year, month, day = (int(part) for part in match.groups())
    if not _valid(year, month, day):
        raise DateInputError(f"실제로 존재하지 않는 날짜입니다: {text[:40]!r}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def to_compact(cutoff: str) -> str:
    """``YYYY-MM-DD`` → ``YYYYMMDD``. EPO CQL 의 날짜 표기다."""
    normalized = normalize_cutoff(cutoff)
    return normalized.replace("-", "") if normalized else ""


def parse_publication_date(value) -> tuple[str, str]:
    """문헌의 공개일 표기에서 (정규화 값, 정밀도) 를 뽑는다.

    EPO 는 ``20260115``, Crossref 는 ``2026-01-15``·``2026-01``·``2026``,
    Europe PMC 는 ``2026-01-15`` 를 준다. 셋 다 같은 자리에서 읽는다.

    읽지 못하면 ``("", "")`` 을 준다. 예외를 던지지 않는다 — 여기서 실패하는 것은
    사용자 입력이 아니라 외부 응답이고, 그 실패의 답은 "모른다"이지 거절이 아니다.
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    match = _COMPACT.match(text) or _ISO.match(text) or _LOOSE.match(text)
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
        if _valid(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}", PRECISION_DAY
        # 관청이 일자를 00 으로 채워 보내는 일이 있다(월까지만 아는 문헌).
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}", PRECISION_MONTH
        return f"{year:04d}", PRECISION_YEAR
    match = _PARTIAL_MONTH.match(text)
    if match is not None:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}", PRECISION_MONTH
        return f"{year:04d}", PRECISION_YEAR
    match = _PARTIAL_YEAR.match(text)
    if match is not None:
        return f"{int(match.group(1)):04d}", PRECISION_YEAR
    return "", ""


def _bounds(normalized: str, precision: str) -> tuple[str, str]:
    """부분 날짜가 가리키는 기간의 첫날과 마지막 날."""
    if precision == PRECISION_DAY:
        return normalized, normalized
    if precision == PRECISION_MONTH:
        year, month = (int(part) for part in normalized.split("-"))
        last = calendar.monthrange(year, month)[1]
        return f"{normalized}-01", f"{normalized}-{last:02d}"
    return f"{normalized}-01-01", f"{normalized}-12-31"


@dataclass(frozen=True)
class DateVerdict:
    """후보 하나에 대한 기준일 판정. 매니페스트와 보고서가 같은 값을 읽는다."""

    status: str
    publication_date: str = ""
    precision: str = ""
    #: 이 후보를 대상에서 빼야 하는가. :data:`STATUS_AFTER` 하나뿐이다.
    excluded: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "label": STATUS_LABELS.get(self.status, self.status),
            "publication_date": self.publication_date,
            "publication_date_precision": self.precision,
            "excluded": self.excluded,
            "detail": self.detail,
        }


def evaluate(raw_publication_date, cutoff: str) -> DateVerdict:
    """공개일 하나를 기준일과 견준다. 기준일이 없으면 아무 것도 판정하지 않는다."""
    normalized_cutoff = str(cutoff or "").strip()
    published, precision = parse_publication_date(raw_publication_date)
    if not normalized_cutoff:
        return DateVerdict(
            status=STATUS_NO_LIMIT,
            publication_date=published,
            precision=precision,
            detail="검색 기준일을 지정하지 않아 날짜 조건을 적용하지 않았습니다.",
        )
    if not published:
        return DateVerdict(
            status=STATUS_UNKNOWN,
            detail=(
                "이 후보의 공개일을 확인하지 못했습니다. 기준일 판정을 하지 않고 "
                "'공개일 미확인'으로 남깁니다."
            ),
        )
    first, last = _bounds(published, precision)
    if last <= normalized_cutoff:
        return DateVerdict(
            status=STATUS_WITHIN,
            publication_date=published,
            precision=precision,
            detail=f"공개일 {published} 은 기준일 {normalized_cutoff} 이전입니다.",
        )
    if first > normalized_cutoff:
        return DateVerdict(
            status=STATUS_AFTER,
            publication_date=published,
            precision=precision,
            excluded=True,
            detail=(
                f"공개일 {published} 은 기준일 {normalized_cutoff} 뒤입니다."
            ),
        )
    return DateVerdict(
        status=STATUS_AMBIGUOUS,
        publication_date=published,
        precision=precision,
        detail=(
            f"공개일이 {published} 까지만 확인돼 기준일 {normalized_cutoff} "
            "전후를 확정할 수 없습니다. 제외하지 않았습니다."
        ),
    )


# --- 후보에서 공개일 찾기 ---------------------------------------------------
#
# 채널마다 공개일이 다른 자리에 온다. 한 함수가 전부 알아야 "웹 후보에는 날짜가
# 없어서 필터가 안 걸린다"가 조용히 생기지 않는다.
_CANDIDATE_DATE_KEYS = ("publication_date",)


def candidate_publication_date(candidate: dict) -> str:
    """후보에서 공개일 표기를 찾는다. 없으면 빈 문자열.

    보는 자리는 세 곳이고 순서가 곧 신뢰 순서다.

      1. ``publication_date`` — ARIA 가 공식 응답에서 채운 값
      2. ``official_evidence.publication_date`` — 검증 단계가 확보한 값
      3. 채널별 발견 기록(``epo_discovery`` · ``literature_discovery``)
    """
    entry = candidate or {}
    for key in _CANDIDATE_DATE_KEYS:
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    official = entry.get("official_evidence")
    if isinstance(official, dict):
        value = str(official.get("publication_date") or "").strip()
        if value:
            return value
    for key in ("epo_discovery", "literature_discovery"):
        block = entry.get(key)
        if isinstance(block, dict):
            value = str(block.get("publication_date") or "").strip()
            if value:
                return value
    return ""


def annotate(candidate: dict, cutoff: str) -> DateVerdict:
    """후보에 기준일 판정을 적어 넣고 그 판정을 돌려준다.

    후보를 지우지 않는다. 지우는 판단은 호출부가 :attr:`DateVerdict.excluded`
    를 보고 하며, 여기서는 **모든** 후보에 상태를 남긴다 — 통과한 후보에도
    상태가 있어야 "날짜를 봤다"와 "날짜가 없어서 그냥 뒀다"를 구분할 수 있다.
    """
    verdict = evaluate(candidate_publication_date(candidate), cutoff)
    if verdict.publication_date and not str(
        candidate.get("publication_date") or ""
    ).strip():
        candidate["publication_date"] = verdict.publication_date
    candidate["publication_date_status"] = verdict.status
    candidate["publication_date_detail"] = verdict.detail
    return verdict


def filter_candidates(reported: dict | None, cutoff: str) -> dict:
    """후보 목록에 기준일을 적용한다. 감사 기록에 넣을 요약을 돌려준다.

    외부 검색 API 가 날짜를 지원하면 검색 단계에서 이미 좁혀졌고, 지원하지 않는
    채널(웹 검색·Crossref 서지 검색)의 후보는 여기서 걸러진다. 두 경로가 같은
    판정 함수를 쓰므로 "검색으로 걸렀나 나중에 걸렀나"로 결과가 갈리지 않는다.

    제외된 후보는 목록에서 빠지지만 **사라지지 않는다.** 번호·제목·공개일·사유가
    반환값의 ``excluded`` 에 그대로 남아 매니페스트에 저장된다.
    """
    normalized = str(cutoff or "").strip()
    candidates = list((reported or {}).get("candidates") or [])
    kept: list[dict] = []
    excluded: list[dict] = []
    counts = {status: 0 for status in STATUSES}
    for candidate in candidates:
        verdict = annotate(candidate, normalized)
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
        if verdict.excluded:
            excluded.append(
                {
                    "index": int(candidate.get("index") or 0),
                    "doc_number": str(candidate.get("doc_number") or ""),
                    "doi": str(candidate.get("doi") or ""),
                    "title": str(
                        candidate.get("title")
                        or candidate.get("reported_title")
                        or ""
                    ),
                    "publication_date": verdict.publication_date,
                    "reason_code": EXCLUDE_REASON,
                    "detail": verdict.detail,
                }
            )
            continue
        kept.append(candidate)
    if reported is not None:
        reported["candidates"] = kept
    return {
        "cutoff": normalized,
        "applied": bool(normalized),
        "basis": "publication_date",
        "evaluated": len(candidates),
        "kept": len(kept),
        "excluded": excluded,
        "status_counts": {key: value for key, value in counts.items() if value},
        "unknown_publication_date": counts.get(STATUS_UNKNOWN, 0),
    }


def empty_section(cutoff: str = NO_CUTOFF, *, reason: str = "") -> dict:
    """날짜 필터를 돌리지 않은 실행의 기록. 모양은 같게 남긴다.

    "적용하지 않았다"와 "기록이 없다"를 구분하려면 키가 언제나 있어야 한다.
    """
    normalized = str(cutoff or "").strip()
    return {
        "cutoff": normalized,
        "applied": False,
        "basis": "publication_date",
        "evaluated": 0,
        "kept": 0,
        "excluded": [],
        "status_counts": {},
        "unknown_publication_date": 0,
        "reason": reason
        or (
            "검색 기준일을 지정하지 않아 날짜 조건 없이 검색했습니다."
            if not normalized
            else ""
        ),
    }


def describe(cutoff: str) -> str:
    """보고서 한 줄. 값이 없으면 '없음'을 **명시한다**."""
    normalized = str(cutoff or "").strip()
    if not normalized:
        return "날짜 제한 없음"
    return f"{normalized} 까지 공개된 문헌"
