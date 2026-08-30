"""EPO OPS HTTP 클라이언트 — 토큰, 재시도, 사용량 관측.

경계
----
이 모듈은 **바이트만 다룬다.** 응답을 해석하지 않고, 후보를 만들지 않고,
XML 을 읽지 않는다. 그건 epo_parser 가 보존된 아티팩트에서 다시 할 일이다.
여기서 파싱해서 넘기면, 검증기가 대조할 '원본'이 이미 한 번 가공된 것이 된다.

시간 예산이 둘인 이유
---------------------
    timeout             HTTP 요청 **하나**가 기다리는 시간
    http_budget_seconds 이 클라이언트가 쓰는 **네트워크 시간의 총합**

EPO 채널 전체의 벽시계(LLM 턴 포함)와 네트워크 시간은 다른 축이다. 하나로
묶으면 모델이 오래 생각한 실행에서 OPS 호출이 남은 예산 없이 시작되고, 그
실패가 "EPO 가 느리다"로 기록된다. 채널 벽시계는 이 클라이언트를 부르는
쪽(3단계 러너)이 따로 건다.

토큰
----
메모리에만 둔다. 디스크·DB·로그·응답 어디에도 쓰지 않는다. 만료 전에 미리
바꾸고, 401 을 받으면 **한 번만** 다시 받아 재시도한다. 두 번 이상 하지
않는 이유는, 자격증명이 실제로 틀렸을 때 무한히 토큰을 받으러 가는 경로가
되기 때문이다.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from . import epo_parser, epo_quota
from .base import PatentSearchError

# --- 고정 주소. 설정으로도 응답으로도 바뀌지 않는다 -----------------------
BASE_URL = "https://ops.epo.org/3.2"
TOKEN_URL = f"{BASE_URL}/auth/accesstoken"
SEARCH_URL = f"{BASE_URL}/rest-services/published-data/search/biblio"
PUBLICATION_URL = f"{BASE_URL}/rest-services/published-data/publication/docdb"

# 상세 조회로 받을 수 있는 구성요소. 허용 목록 밖은 부르지 않는다.
CONSTITUENT_BIBLIO = "biblio"
CONSTITUENT_ABSTRACT = "abstract"
CONSTITUENT_CLAIMS = "claims"
CONSTITUENT_DESCRIPTION = "description"
CONSTITUENTS = (
    CONSTITUENT_BIBLIO,
    CONSTITUENT_ABSTRACT,
    CONSTITUENT_CLAIMS,
    CONSTITUENT_DESCRIPTION,
)

# 질의당 결과 상한. 사용자 확정값.
MAX_RESULTS_PER_QUERY = 20
# 응답 하나를 읽는 상한. 청구항·설명은 클 수 있지만 무한하지는 않다. 넘으면
# 자르지 않고 실패시킨다 — 잘린 바이트를 아티팩트로 보존하면 그 해시는 원본의
# 해시가 아니고, 그 위에서 내린 판정은 재현되지 않는다.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# 일시적 실패 재시도. 사용자 확정값(최대 2회).
MAX_RETRIES = 2
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Retry-After 를 존중하되, 이만큼을 넘으면 기다리지 않고 채널을 끝낸다.
MAX_RETRY_SLEEP_SECONDS = 30.0

DEFAULT_TIMEOUT = 30.0
DEFAULT_HTTP_BUDGET_SECONDS = 120.0
# 만료 직전에 쓰다가 401 을 맞지 않도록 미리 바꾼다.
TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class OpsError(PatentSearchError):
    """OPS 호출이 실패했다."""


class OpsAuthError(OpsError):
    """자격증명이 거절되었다. 재시도로 풀리지 않는다."""


class OpsBudgetExceeded(OpsError):
    """이 클라이언트에 허용된 네트워크 시간을 다 썼다."""


class OpsUnavailable(OpsError):
    """일시적 오류가 재시도 후에도 계속된다."""


class OpsCancelled(OpsError):
    """사용자가 실행을 취소했다.

    재시도 대기 중에도 던져진다. Retry-After 로 20초를 기다리는 동안 취소를
    못 보면, 사용자가 멈춘 실행이 20초 더 살아 있으면서 할당량을 쓴다.
    """


def cancellable_sleep(is_cancelled, *, slice_seconds: float = 0.1, sleep=time.sleep):
    """취소를 볼 수 있는 대기 함수를 만든다.

    통째로 자면 그 시간 동안 취소가 반영되지 않는다. 잘게 나눠 자면서 매번
    확인하고, 취소면 남은 시간을 버리고 즉시 멈춘다.
    """

    def _sleep(seconds: float) -> None:
        if is_cancelled():
            raise OpsCancelled("사용자가 실행을 취소했습니다.")
        remaining = max(0.0, float(seconds or 0.0))
        while remaining > 0:
            step = min(slice_seconds, remaining)
            sleep(step)
            remaining -= step
            if is_cancelled():
                raise OpsCancelled("사용자가 실행을 취소했습니다.")

    return _sleep


def scrub(text: str, *secrets: str) -> str:
    """문자열에서 자격증명 조각을 지운다.

    key/secret 원문뿐 아니라 **base64 로 인코딩된 Basic 값과 헤더 전체**도
    지운다. 중간 장비의 오류 페이지가 요청 헤더를 그대로 찍어 주는 일이
    있는데, 그때 화면에 나타나는 것은 원문이 아니라 base64 다.
    """
    cleaned = str(text or "")
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def credential_tokens(key: str, secret: str) -> tuple[str, ...]:
    """scrub 에 넘길 값 묶음. 한 곳에서 만들어 빠뜨리지 않게 한다."""
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        return (key, secret)
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode("ascii")
    return (key, secret, basic, f"Basic {basic}")


@dataclass(frozen=True)
class HttpResponse:
    """전송 계층의 응답. 테스트가 이것만 만들면 네트워크 없이 전 경로가 돈다."""

    status: int
    headers: dict
    body: bytes


@dataclass(frozen=True)
class OpsCall:
    """OPS 호출 하나의 결과. 본문은 **가공되지 않은 바이트**다."""

    url: str
    status: int
    headers: dict
    body: bytes
    elapsed_seconds: float
    retries: int
    kind: str  # token | search | detail
    # 검색 결과가 0건이라 OPS 가 404 로 답한 호출. 실패가 아니라 빈 결과다.
    no_results: bool = False

    @property
    def byte_count(self) -> int:
        return len(self.body)


_NO_RESULTS_FAULT_CODE = "SERVER.EntityNotFound"
_NO_RESULTS_FAULT_MESSAGE = "No results found"


def _local_name(tag) -> str:
    """네임스페이스 접두를 떼어낸 태그 이름. OPS 는 기본 네임스페이스를 쓴다."""
    return str(tag).rpartition("}")[2]


def _fault_fields(body: bytes) -> dict[str, str]:
    """OPS fault 문서의 code/message 를 읽는다. 아니면 빈 dict.

    substring 검사를 쓰지 않는다. 검색 결과 본문 어딘가에 같은 문자열이
    들어 있기만 해도 0건으로 오판하기 때문이다. 파싱은 epo_parser 의
    경화된 경로를 쓴다 — DOCTYPE/ENTITY 선언과 크기 상한을 함께 거른다.
    """
    try:
        root = epo_parser.parse_xml(body)
    except Exception:
        return {}
    if _local_name(root.tag) != "fault":
        return {}
    fields = {}
    for child in root:
        name = _local_name(child.tag)
        if name in ("code", "message"):
            fields[name] = (child.text or "").strip()
    return fields


def _is_search_no_results(kind: str, status: int, body: bytes) -> bool:
    """검색 엔드포인트의 '결과 0건'만 정상 빈 결과로 인정한다.

    404 전체를 정상으로 돌리지 않는다. 상세 조회의 404 는 "그 문헌이 없다"는
    다른 사실이고, 엔드포인트 자체가 사라져도 같은 상태 코드가 온다. 네 조건이
    모두 맞을 때만 빈 결과로 취급한다 — 엔드포인트가 search 이고, 404 이고,
    본문이 OPS fault 문서이고, code/message 가 정확히 일치할 때.
    """
    if kind != "search" or status != 404:
        return False
    fields = _fault_fields(body)
    return (
        fields.get("code") == _NO_RESULTS_FAULT_CODE
        and fields.get("message") == _NO_RESULTS_FAULT_MESSAGE
    )


def _default_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """기본 전송의 진입점. 실제 구현을 **이름으로** 부른다.

    한 겹 감싸는 이유는 테스트다. dataclass 의 기본값은 클래스가 만들어질 때
    함수 객체로 굳으므로, 그 자리에 실제 구현을 직접 두면 나중에 모듈 속성을
    바꿔도 이미 굳은 기본값은 바뀌지 않는다. 그러면 전송 계층을 주입하지 않은
    테스트가 조용히 진짜 네트워크를 연다 — 실제로 한 번 그렇게 됐다.

    여기서 이름으로 부르면 conftest 가 _live_transport 를 막을 수 있고,
    전송을 주입하지 않은 호출은 통과가 아니라 실패가 된다.
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


def _retry_after_seconds(headers) -> float | None:
    """Retry-After 를 초로 읽는다. 날짜 형식이면 해석하지 않는다.

    날짜를 파싱하지 않는 것은 의도다. 서버 시계와 우리 시계가 다르면 음수나
    몇 시간짜리 대기가 나오는데, 그건 기다림이 아니라 멈춤이다. 해석할 수
    없으면 호출부의 기본 대기를 쓴다.
    """
    for key, value in dict(headers or {}).items():
        if str(key).lower() != "retry-after":
            continue
        try:
            return max(0.0, float(str(value).strip()))
        except (TypeError, ValueError):
            return None
    return None


@dataclass
class OpsClient:
    """OPS 호출자. 자격증명과 토큰은 이 객체 밖으로 나가지 않는다."""

    key: str
    secret: str
    ledger: epo_quota.QuotaLedger = field(default_factory=epo_quota.QuotaLedger)
    transport: Callable[[urllib.request.Request, float], HttpResponse] = (
        _default_transport
    )
    timeout: float = DEFAULT_TIMEOUT
    http_budget_seconds: float = DEFAULT_HTTP_BUDGET_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    _token: str = field(default="", init=False, repr=False)
    _token_expires_at: float = field(default=0.0, init=False, repr=False)
    _spent_seconds: float = field(default=0.0, init=False)
    calls: list = field(default_factory=list, init=False)

    # 로그·예외에 절대 담기지 않게, 표현을 명시적으로 막는다.
    def __repr__(self) -> str:  # pragma: no cover - 디버깅 표시용
        return f"<OpsClient spent={self._spent_seconds:.1f}s calls={len(self.calls)}>"

    # --- 예산 -----------------------------------------------------------
    @property
    def remaining_budget(self) -> float:
        if not self.http_budget_seconds:
            return float("inf")
        return max(0.0, self.http_budget_seconds - self._spent_seconds)

    def _require_budget(self) -> float:
        remaining = self.remaining_budget
        if remaining <= 0:
            raise OpsBudgetExceeded(
                f"EPO OPS 네트워크 시간 예산({self.http_budget_seconds:.0f}초)을 "
                "다 썼습니다."
            )
        return min(self.timeout, remaining) if remaining != float("inf") else self.timeout

    # --- 토큰 -----------------------------------------------------------
    def _token_valid(self) -> bool:
        return bool(self._token) and self.clock() < self._token_expires_at

    def _acquire_token(self) -> None:
        """client_credentials 로 토큰을 받는다. 실패는 재시도하지 않는다."""
        tokens = credential_tokens(self.key, self.secret)
        if len(tokens) < 4:
            raise OpsAuthError("Consumer Key 와 Consumer Secret 가 필요합니다.")
        request = urllib.request.Request(
            TOKEN_URL,
            data=b"grant_type=client_credentials",
            method="POST",
            headers={
                "Authorization": tokens[3],
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        response = self._send(request, kind="token")
        if response.status in (400, 401, 403):
            raise OpsAuthError(
                "EPO OPS 가 자격증명을 거절했습니다"
                f"(HTTP {response.status}). 설정 화면에서 Consumer Key/Secret 를 "
                "확인하십시오."
            )
        if response.status >= 400:
            raise OpsUnavailable(
                f"토큰을 받지 못했습니다(HTTP {response.status})."
            )
        try:
            payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise OpsUnavailable("토큰 응답을 해석하지 못했습니다.") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise OpsUnavailable("토큰 응답에 access_token 이 없습니다.")
        try:
            lifetime = float(payload.get("expires_in"))
        except (TypeError, ValueError):
            lifetime = 1200.0
        self._token = str(token)
        self._token_expires_at = self.clock() + max(
            0.0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS
        )

    # --- 전송 -----------------------------------------------------------
    def _send(self, request: urllib.request.Request, *, kind: str) -> HttpResponse:
        """한 번 보낸다. 재시도는 부르는 쪽이 한다.

        **쿼터 예약은 여기서 한다.** 논리 검색 하나당 한 번 검사하면, 토큰이
        없을 때 토큰 응답과 검색 응답 두 개가 예약 하나를 나눠 쓰고 한도를
        넘긴다(실측 36바이트 초과). 401 재인증이 끼면 더 늘어난다. 예약 단위는
        실제 전송 하나여야 한다.

        예약은 무슨 일이 있어도 풀려야 하므로 finally 에서 정산한다. 남으면
        쓰지도 않은 양이 한도를 차지한 채 굳는다.
        """
        timeout = self._require_budget()
        reservation = self.ledger.reserve(MAX_RESPONSE_BYTES)
        started = self.clock()
        try:
            response = self.transport(request, timeout)
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            self.ledger.settle(reservation, body_bytes=0, headers=None)
            self._spent_seconds += self.clock() - started
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise OpsUnavailable(
                    "TLS 인증서 검증에 실패했습니다. 검증을 끄고 진행하지 "
                    "않습니다."
                ) from exc
            raise OpsUnavailable(
                "EPO OPS 에 접속하지 못했습니다: "
                f"{scrub(str(reason), *credential_tokens(self.key, self.secret))}"
            ) from exc
        except BaseException:
            # 전송 계층이 예상 밖으로 터져도 예약은 푼다.
            self.ledger.settle(reservation, body_bytes=0, headers=None)
            raise
        elapsed = self.clock() - started
        self._spent_seconds += elapsed

        # 사용량은 **무슨 일이 있어도** 먼저 기록한다. 상한 초과로 버릴
        # 응답이라도 바이트는 이미 내려받았고 EPO 는 그만큼을 과금한다.
        # 기록 전에 예외를 던지면 그 바이트와 quota 헤더가 통째로 사라져,
        # 우리 숫자만 실제보다 작아진다.
        #
        # 토큰 발급 응답도 넣는다. 작지만 세지 않으면 계정 전체 사용량과
        # 우리 숫자가 계속 어긋난다.
        self.ledger.settle(
            reservation, body_bytes=len(response.body), headers=response.headers
        )
        self.calls.append(
            {
                "kind": kind,
                "status": response.status,
                "bytes": len(response.body),
                "elapsed_seconds": round(elapsed, 3),
            }
        )
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise OpsError(
                f"응답이 상한({MAX_RESPONSE_BYTES:,} bytes)을 넘습니다. "
                "잘린 바이트로는 원본 대조를 할 수 없으므로 중단합니다. "
                "받은 바이트와 quota 헤더는 이미 사용량에 반영했습니다."
            )
        return response

    def _request_with_retry(
        self, build_request: Callable[[str], urllib.request.Request], *, kind: str
    ) -> OpsCall:
        """인증 헤더를 붙여 보내고, 필요한 만큼만 재시도한다."""
        # 쿼터 예약은 _send 가 전송마다 한다. 여기서는 시작 전에 스로틀 상태만
        # 확인한다 — 이미 위험 상태면 토큰부터 받을 이유가 없다.
        self.ledger.check()
        if not self._token_valid():
            self._acquire_token()

        retries = 0
        reauthorized = False
        started = self.clock()
        while True:
            request = build_request(self._token)
            response = self._send(request, kind=kind)

            if response.status == 401 and not reauthorized:
                # 토큰이 우리 예상보다 먼저 죽었다. 한 번만 다시 받는다.
                reauthorized = True
                self._token = ""
                self._acquire_token()
                continue
            if response.status in (401, 403):
                detail = scrub(
                    response.body[:500].decode("utf-8", errors="replace"),
                    *credential_tokens(self.key, self.secret),
                )
                raise OpsAuthError(
                    f"EPO OPS 접근이 거부되었습니다(HTTP {response.status}). {detail}"
                )
            if response.status in RETRYABLE_STATUSES and retries < MAX_RETRIES:
                wait = _retry_after_seconds(response.headers)
                if wait is None:
                    wait = 1.0 * (retries + 1)
                if wait > MAX_RETRY_SLEEP_SECONDS or wait > self.remaining_budget:
                    raise OpsUnavailable(
                        f"EPO OPS 가 {wait:.0f}초 뒤 재시도를 요구했습니다. "
                        "남은 예산으로는 기다릴 수 없어 EPO 채널을 중단합니다."
                    )
                retries += 1
                # 기다린 시간도 예산에서 깎는다. 깎지 않으면 재시도를 여러 번
                # 하는 동안 120초 계약을 조용히 넘긴다 — 예산은 "요청에 쓴
                # 시간"이 아니라 "이 채널이 붙잡고 있은 시간"이다.
                self._spent_seconds += wait
                self.sleep(wait)
                # 스로틀 헤더가 위험 상태로 바뀌었으면 여기서 멈춘다.
                self.ledger.check()
                continue
            if response.status in RETRYABLE_STATUSES:
                raise OpsUnavailable(
                    f"EPO OPS 가 계속 실패합니다(HTTP {response.status}, "
                    f"재시도 {retries}회)."
                )
            if _is_search_no_results(kind, response.status, response.body):
                # OPS 는 "검색 결과 0건"을 404 + SERVER.EntityNotFound 로 알린다.
                # 이걸 오류로 올리면 레인이 provider_error 로 끝나고, 그 앞
                # 라운드에서 실제로 찾은 후보까지 채널 대조에서 통째로 빠진다
                # (2026-08-30 실행에서 1라운드 후보 2건이 그렇게 사라졌다).
                # 본문은 그대로 넘긴다 — 원본 fault 는 아티팩트로 보존한다.
                return OpsCall(
                    url=request.full_url,
                    status=response.status,
                    headers=dict(response.headers),
                    body=response.body,
                    elapsed_seconds=round(self.clock() - started, 3),
                    retries=retries,
                    kind=kind,
                    no_results=True,
                )
            if response.status >= 400:
                detail = scrub(
                    response.body[:500].decode("utf-8", errors="replace"),
                    *credential_tokens(self.key, self.secret),
                )
                raise OpsError(f"EPO OPS 오류(HTTP {response.status}). {detail}")

            return OpsCall(
                url=request.full_url,
                status=response.status,
                headers=dict(response.headers),
                body=response.body,
                elapsed_seconds=round(self.clock() - started, 3),
                retries=retries,
                kind=kind,
            )

    # --- 공개 호출 -------------------------------------------------------
    def search(self, cql: str, *, begin: int = 1, end: int = MAX_RESULTS_PER_QUERY):
        """검색 한 번. cql 은 epo_cql.build 가 만든 문자열이어야 한다."""
        if not str(cql or "").strip():
            raise OpsError("검색식이 비어 있습니다.")
        begin = max(1, int(begin))
        end = min(int(end), begin + MAX_RESULTS_PER_QUERY - 1)
        if end < begin:
            raise OpsError("결과 범위가 올바르지 않습니다.")
        query = urllib.parse.urlencode({"q": cql, "Range": f"{begin}-{end}"})
        url = f"{SEARCH_URL}?{query}"

        def build(token: str) -> urllib.request.Request:
            return urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                },
            )

        return self._request_with_retry(build, kind="search")

    def fetch(self, doc_key: str, constituent: str):
        """문헌 하나의 구성요소를 받는다. doc_key 는 ``CC.NUMBER.KK``."""
        if constituent not in CONSTITUENTS:
            raise OpsError(f"허용되지 않은 상세 조회 항목입니다: {constituent!r}")
        key = normalize_doc_key(doc_key)
        url = f"{PUBLICATION_URL}/{urllib.parse.quote(key)}/{constituent}"

        def build(token: str) -> urllib.request.Request:
            return urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                },
            )

        return self._request_with_retry(build, kind="detail")

    def usage(self) -> dict:
        """이 클라이언트가 쓴 것. manifest 와 화면에 그대로 실린다."""
        by_kind: dict[str, dict] = {}
        for call in self.calls:
            row = by_kind.setdefault(
                call["kind"], {"count": 0, "bytes": 0, "seconds": 0.0}
            )
            row["count"] += 1
            row["bytes"] += call["bytes"]
            row["seconds"] = round(row["seconds"] + call["elapsed_seconds"], 3)
        return {
            "calls_by_kind": by_kind,
            "http_seconds": round(self._spent_seconds, 3),
            "http_budget_seconds": self.http_budget_seconds,
            "quota": self.ledger.snapshot(),
        }


# 붙여 쓴 형태(EP1000000A1)와 점으로 나눈 형태(EP.1000000.A1)를 둘 다 받는다.
# kind code 는 문자 하나 + 선택적 숫자 하나다(A1, B2, U, T3 …).
_DOC_KEY_JOINED = re.compile(r"^([A-Z]{2})(\d+)([A-Z]\d?)?$")
_DOC_KEY_PART = re.compile(r"^[A-Z0-9]{1,20}$")


def normalize_doc_key(value: str) -> str:
    """``EP1000000A1`` / ``EP.1000000.A1`` 을 docdb 형식으로 맞춘다.

    문헌번호는 URL 경로에 들어간다. 형식을 강제하지 않으면 응답에서 온
    문자열이 경로를 벗어날 수 있다(``../``). 그래서 국가코드·번호·kind 를
    각각 검사하고, **통과한 조각만 다시 조립한다.** 원래 문자열을 그대로
    쓰지 않는 것이 핵심이다 — 검사를 통과했다는 것과 안전한 문자만 남았다는
    것은 다르다.
    """
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        raise OpsError("문헌번호가 비어 있습니다.")

    if "." in text:
        parts = [part for part in text.split(".") if part]
        if not 2 <= len(parts) <= 3:
            raise OpsError(f"문헌번호 형식이 올바르지 않습니다: {value!r}")
        country, number = parts[0], parts[1]
        kind = parts[2] if len(parts) == 3 else ""
    else:
        matched = _DOC_KEY_JOINED.match(text)
        if not matched:
            raise OpsError(f"문헌번호 형식이 올바르지 않습니다: {value!r}")
        country, number, kind = matched.group(1), matched.group(2), (
            matched.group(3) or ""
        )

    if len(country) != 2 or not country.isalpha():
        raise OpsError(f"국가코드가 올바르지 않습니다: {value!r}")
    if not number.isdigit():
        raise OpsError(f"문헌번호가 숫자가 아닙니다: {value!r}")
    for part in (country, number, kind):
        if part and not _DOC_KEY_PART.match(part):
            raise OpsError(f"문헌번호에 쓸 수 없는 문자가 있습니다: {value!r}")
    return ".".join(part for part in (country, number, kind) if part)
