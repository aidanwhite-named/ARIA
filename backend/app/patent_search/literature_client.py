"""비특허문헌(NPL) 조회 클라이언트 — Crossref 와 Europe PMC.

왜 있는가
---------
웹 검색 채널은 논문을 **식별할 수 없다.** agy 의 ``search_web`` 은 결과 목록이
아니라 줄글 요약과 익명 각주를 돌려준다(2026-09-01 실측: 검색 16회에 각주 84개,
전부 ``vertexaisearch.cloud.google.com/grounding-api-redirect/...`` 형태에 도메인
라벨만 붙어 있고 제목도 DOI 도 없었다). 그래서 모델이 후보로 적을 수 있는 것은
요약문이 우연히 제목을 써 준 문헌뿐이고, 같은 실행에서 84개 중 2건만 후보가
됐다. 나머지 82건은 "못 찾은 것"이 아니라 **주소가 없어서 적을 수 없었던 것**이다.

그 각주를 ARIA 가 풀어서 대신 적어 줄 수도 없다. 이 PC 의 네트워크가
``vertexaisearch.cloud.google.com`` 을 차단한다(2026-09-01 실측: 사내 정책 차단
페이지가 돌아온다). 리다이렉트를 따라갈 수 없으면 각주는 영원히 익명이다.

그래서 방향을 바꾼다. 검색 결과를 해석하려 애쓰는 대신 **ARIA 가 직접 서지
데이터베이스에 묻는다.** 두 곳 다 자격증명이 필요 없고 이 PC 에서 도달한다.

    Crossref     발행사가 직접 등록한 서지. 제목 검색이 강하다.
    Europe PMC   초록·전문 색인. 개념 검색이 강하고 초록이 평문으로 온다.

두 곳을 함께 쓰는 이유 (2026-09-01 실측, 목표 문헌 10.3390/s25103219)

    질의 "computer vision sensor low-power edge detection circuit"
        → Crossref 1위, Europe PMC 상위권 밖
    질의 "CMOS image sensor edge mask thresholding during analog-to-digital
         conversion 1-bit edge image"
        → Europe PMC 1위, Crossref 상위권 밖

한쪽만으로는 놓친다. 색인 방식이 다르기 때문이지 한쪽이 나빠서가 아니다.

무엇을 보증하고 무엇을 보증하지 않는가
--------------------------------------
보증한다 : 여기서 받은 바이트를 그대로 아티팩트에 보존하고, 그 아티팩트에서
           다시 뽑은 값만 근거로 쓴다(literature_parser 가 경로를 만든다).
보증하지 않는다 : 그 초록이 논문 **원문(PDF)** 의 발췌라는 것. 발행사가 등록한
           메타데이터이지 조판된 원문이 아니다. 그래서 등록하는 소스 프로필은
           둘 다 ``raw_capable=False`` 다. mdpi.com 본문은 403 으로 막혀 있고
           (2026-09-01 실측) 이 경로는 그 우회가 아니라 **다른 종류의 근거**다.

MDPI 를 직접 열지 않는 것이 핵심이다. 같은 문헌의 초록을 발행사가 Crossref 에
등록해 두었으므로, 막힌 문을 두드리는 대신 열려 있는 문으로 같은 사실을 받는다.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from .base import PatentSearchError

# 조회 대상. 값은 감사 기록에 그대로 남으므로 바꾸지 않는다.
SOURCE_CROSSREF = "crossref"
SOURCE_EUROPEPMC = "europepmc"
SOURCES = (SOURCE_CROSSREF, SOURCE_EUROPEPMC)

CROSSREF_BASE = "https://api.crossref.org"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# 응답 본문 상한. 넘으면 자르지 않고 오류로 만든다 — 잘린 JSON 은 파싱에
# 실패하고, 실패한 파싱을 "필드가 없다"로 읽으면 기록이 거짓이 된다.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

DEFAULT_TIMEOUT_SECONDS = 20.0
# 이 클라이언트가 쓰는 네트워크 시간의 총합. EPO 와 같은 방식으로 센다 —
# 호출 수만 세면 느린 응답 하나가 실행 전체를 잡아 둘 수 있다.
DEFAULT_HTTP_BUDGET_SECONDS = 60.0

# 한 질의가 받아 올 결과 수의 상한.
MAX_ROWS_PER_QUERY = 20

# Crossref 에서 받아 올 필드. 전부 받으면 참고문헌 목록까지 딸려 와 응답이
# 수백 KB 가 된다. 필요한 것만 고르면 보존 아티팩트도 작아진다.
_CROSSREF_SELECT = (
    "DOI",
    "title",
    "author",
    "container-title",
    "issued",
    "abstract",
    "URL",
    "type",
    "publisher",
)

_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")

# 검색어에서 떼어 낼 검색엔진 문법. 이 API 들은 불리언·필드 연산자를 이 형태로
# 받지 않으며, 그대로 보내면 연산자가 검색어의 일부로 들어가 결과가 망가진다.
_ENGINE_OPERATORS = re.compile(
    r"""
    \bsite:\S+          # site:patents.google.com
    | \bfiletype:\S+
    | \bintitle:
    | \binurl:
    | \bOR\b
    | \bAND\b
    | [()"“”]
    """,
    re.VERBOSE,
)


class LiteratureError(PatentSearchError):
    """서지 API 조회 실패. HTTP 상태를 함께 든다."""

    def __init__(self, message: str = "", *, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status or 0)


class LiteratureBudgetExceeded(LiteratureError):
    """네트워크 시간 예산 초과. 남은 조회는 시도하지 않는다."""


def normalize_doi(value) -> str:
    """자유 표기에서 DOI 하나를 뽑아 소문자로 정규화한다.

    ``https://doi.org/10.3390/s25103219``, ``doi:10.3390/S25103219``,
    ``10.3390/s25103219`` 를 모두 같은 값으로 만든다. DOI 가 아니면
    :class:`LiteratureError` 를 던진다 — 특허번호를 조용히 통과시키면
    Crossref 에 없는 번호로 조회가 나가고, 그 404 가 "논문이 없다"로 읽힌다.

    끝에 붙은 문장부호는 떼어 낸다. 요약문에서 긁어 온 DOI 에는 마침표나
    괄호가 따라오는 일이 흔하다. 다만 DOI 자체에 마침표가 들어갈 수 있으므로
    **끝자리만** 본다.
    """
    text = str(value or "").strip()
    if not text:
        raise LiteratureError("DOI 가 비어 있습니다.")
    match = _DOI_PATTERN.search(text)
    if match is None:
        raise LiteratureError(f"DOI 형식이 아닙니다: {text[:120]}")
    doi = match.group(0).rstrip(".,;:)]}>")
    return doi.lower()


def looks_like_doi(value) -> bool:
    """조용히 참·거짓만 돌려준다. 후보를 고를 때 쓴다."""
    try:
        normalize_doi(value)
    except LiteratureError:
        return False
    return True


def plain_query(value: str, *, max_terms: int = 24) -> str:
    """검색엔진 문법이 섞인 질의를 서지 API 가 읽는 자연어로 바꾼다.

    모델은 ``"CIS" "motion detection" site:patents.google.com`` 처럼 구글 문법으로
    검색어를 쓴다. Crossref 와 Europe PMC 는 그 문법을 모른다. 따옴표를 그대로
    보내면 따옴표가 토큰이 되고, ``site:`` 는 검색어 단어가 된다.

    연산자를 **해석하지 않고 제거한다.** OR 를 진짜 OR 로 옮기려면 질의 언어를
    번역해야 하는데, 그러면 모델이 쓴 질의와 ARIA 가 보낸 질의가 달라진 채로
    기록에는 하나만 남는다. 여기서는 단어만 남기고, 무엇을 보냈는지는 호출부가
    그대로 기록한다.
    """
    text = _ENGINE_OPERATORS.sub(" ", str(value or ""))
    words = [word for word in text.split() if word]
    return " ".join(words[:max_terms])


@dataclass(frozen=True)
class HttpResponse:
    """전송 계층의 응답. 테스트가 이것만 만들면 네트워크 없이 전 경로가 돈다."""

    status: int
    headers: dict
    body: bytes


@dataclass(frozen=True)
class LiteratureCall:
    """조회 하나의 결과. 본문은 **가공되지 않은 바이트**다."""

    url: str
    status: int
    headers: dict
    body: bytes
    elapsed_seconds: float
    source: str          # crossref | europepmc
    kind: str            # search | detail
    # 결과 0건. 실패가 아니라 빈 결과다.
    no_results: bool = False

    @property
    def byte_count(self) -> int:
        return len(self.body)


def _default_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """기본 전송의 진입점. 실제 구현을 **이름으로** 부른다.

    epo_client._default_transport 와 같은 이유다. dataclass 기본값은 클래스가
    만들어질 때 함수 객체로 굳으므로, 실제 구현을 직접 두면 conftest 가
    모듈 속성을 바꿔도 이미 굳은 기본값은 바뀌지 않는다. 그러면 전송을 주입하지
    않은 테스트가 조용히 진짜 네트워크를 연다.
    """
    return _live_transport(request, timeout)


def _live_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """표준 라이브러리 전송. 인증서 검증은 끄지 않는다."""
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=context
        ) as response:
            return HttpResponse(
                status=int(getattr(response, "status", 0) or 0),
                headers=dict(response.headers.items()),
                body=response.read(MAX_RESPONSE_BYTES + 1),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=exc.read(MAX_RESPONSE_BYTES + 1) or b"",
        )


@dataclass
class LiteratureClient:
    """Crossref·Europe PMC 조회. 자격증명이 없다.

    ``mailto`` 는 Crossref 의 예의 풀(polite pool) 표시다. 넣으면 더 안정적인
    큐로 들어간다. 사용자 식별에 쓰지 않으며, 넣지 않아도 동작한다.
    """

    mailto: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    http_budget_seconds: float = DEFAULT_HTTP_BUDGET_SECONDS
    transport: Callable[[urllib.request.Request, float], HttpResponse] = (
        _default_transport
    )
    _spent_seconds: float = field(default=0.0, init=False)
    _calls_by_kind: dict = field(default_factory=dict, init=False)

    # --- 예산 -----------------------------------------------------------
    @property
    def remaining_budget(self) -> float:
        if not self.http_budget_seconds:
            return float("inf")
        return max(0.0, self.http_budget_seconds - self._spent_seconds)

    def _require_budget(self) -> None:
        if self.remaining_budget <= 0:
            raise LiteratureBudgetExceeded(
                f"서지 조회 네트워크 시간 예산"
                f"({self.http_budget_seconds:.0f}초)을 모두 사용했습니다."
            )

    def usage(self) -> dict:
        """이번 실행에서 쓴 호출·시간. manifest 에 그대로 실린다."""
        return {
            "calls_by_kind": dict(self._calls_by_kind),
            "http_seconds": round(self._spent_seconds, 3),
            "http_budget_seconds": self.http_budget_seconds,
        }

    # --- 전송 -----------------------------------------------------------
    def _user_agent(self) -> str:
        # Crossref 는 연락처가 담긴 User-Agent 를 권한다. 없으면 익명으로 간다.
        if self.mailto:
            return f"ARIA/1.0 (https://github.com/; mailto:{self.mailto})"
        return "ARIA/1.0"

    def _send(self, url: str, *, source: str, kind: str) -> LiteratureCall:
        self._require_budget()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self._user_agent(), "Accept": "application/json"},
            method="GET",
        )
        started = time.monotonic()
        try:
            response = self.transport(request, self.timeout_seconds)
        except LiteratureError:
            raise
        except Exception as exc:  # 네트워크 계층 오류
            self._spent_seconds += time.monotonic() - started
            raise LiteratureError(
                f"{source} 조회에 실패했습니다: {type(exc).__name__}: {exc}"
            ) from exc
        elapsed = time.monotonic() - started
        self._spent_seconds += elapsed
        self._calls_by_kind[kind] = self._calls_by_kind.get(kind, 0) + 1

        body = response.body or b""
        if len(body) > MAX_RESPONSE_BYTES:
            raise LiteratureError(
                f"{source} 응답이 상한({MAX_RESPONSE_BYTES} 바이트)을 넘었습니다.",
                status=response.status,
            )
        # Crossref 는 없는 DOI 에 404 를 준다. 상세 조회의 404 는 실패가 아니라
        # "그 문헌이 없다"이므로 빈 결과로 표시하고 호출부가 판단하게 한다.
        no_results = response.status == 404
        if response.status >= 400 and not no_results:
            raise LiteratureError(
                f"{source} 조회가 HTTP {response.status} 로 실패했습니다.",
                status=response.status,
            )
        return LiteratureCall(
            url=url,
            status=int(response.status or 0),
            headers=dict(response.headers or {}),
            body=body,
            elapsed_seconds=round(elapsed, 3),
            source=source,
            kind=kind,
            no_results=no_results,
        )

    # --- Crossref -------------------------------------------------------
    def search_crossref(self, query: str, *, rows: int = 10) -> LiteratureCall:
        """서지 검색. 제목·저자·저널 문자열에 강하다."""
        params = {
            "query.bibliographic": plain_query(query),
            "rows": str(_clamp_rows(rows)),
            "select": ",".join(_CROSSREF_SELECT),
        }
        if self.mailto:
            params["mailto"] = self.mailto
        url = f"{CROSSREF_BASE}/works?" + urllib.parse.urlencode(params)
        return self._send(url, source=SOURCE_CROSSREF, kind="search")

    def fetch_crossref(self, doi: str) -> LiteratureCall:
        """DOI 하나의 등록 서지를 받는다."""
        key = normalize_doi(doi)
        url = f"{CROSSREF_BASE}/works/{urllib.parse.quote(key, safe='')}"
        if self.mailto:
            url += "?" + urllib.parse.urlencode({"mailto": self.mailto})
        return self._send(url, source=SOURCE_CROSSREF, kind="detail")

    # --- Europe PMC -----------------------------------------------------
    def search_europepmc(self, query: str, *, rows: int = 10) -> LiteratureCall:
        """초록·전문 색인 검색. 개념·문장 표현에 강하다."""
        params = {
            "query": plain_query(query),
            "format": "json",
            "pageSize": str(_clamp_rows(rows)),
            # core 로 받으면 검색 한 번에 초록까지 온다. 상세 조회를 따로 하지
            # 않아도 되므로 호출 수가 절반이 된다.
            "resultType": "core",
        }
        url = f"{EUROPEPMC_BASE}/search?" + urllib.parse.urlencode(params)
        return self._send(url, source=SOURCE_EUROPEPMC, kind="search")

    def fetch_europepmc(self, doi: str) -> LiteratureCall:
        """DOI 로 초록을 받는다. Europe PMC 에는 DOI 상세 엔드포인트가 없어
        검색 엔드포인트에 DOI 를 정확히 지정해 부른다."""
        key = normalize_doi(doi)
        params = {
            "query": f'DOI:"{key}"',
            "format": "json",
            "pageSize": "1",
            "resultType": "core",
        }
        url = f"{EUROPEPMC_BASE}/search?" + urllib.parse.urlencode(params)
        return self._send(url, source=SOURCE_EUROPEPMC, kind="detail")


def _clamp_rows(rows) -> int:
    try:
        value = int(rows)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, MAX_ROWS_PER_QUERY))


def response_json(call: LiteratureCall) -> dict:
    """보존된 바이트를 JSON 으로 읽는다. 실패는 오류로 만든다."""
    try:
        document = json.loads(call.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiteratureError(
            f"{call.source} 응답을 JSON 으로 읽지 못했습니다: {exc}",
            status=call.status,
        ) from exc
    if not isinstance(document, dict):
        raise LiteratureError(
            f"{call.source} 응답이 객체가 아닙니다.", status=call.status
        )
    return document
