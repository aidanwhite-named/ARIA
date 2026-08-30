"""EPO OPS(Open Patent Services) 특허 검색 백엔드.

이 파일이 맡는 것은 **한 번의 검색 흐름 전체**다.

  1) 자격증명(Consumer Key / Consumer Secret)을 설정에서 받아 보관하고, 그
     자격증명이 EPO 쪽에서 실제로 발급·활성화된 것인지 **사용자가 버튼을 눌렀을
     때만** 확인한다(check_credentials). 확인은 OAuth 토큰 발급 호출 하나뿐이며
     특허 데이터는 한 건도 받지 않는다.
  2) 검색: 구조화된 질의 → CQL(epo_cql) → OPS 호출(epo_client) → **응답 원본
     바이트 보존**(artifacts) → 등록된 신뢰 파서로 재파싱(epo_parser) →
     후보 레코드.

순서가 중요하다. 후보를 만들기 **전에** 원본을 보존한다. 파싱한 결과에서
후보를 만들고 나중에 원본을 저장하면, 보존된 바이트가 그 후보의 근거라는
보장이 없다. 여기서는 아티팩트 id 를 먼저 얻고, 그 id 를 가리키는
EvidenceRef 를 후보의 모든 필드에 붙인다. 그래서 provenance.verify_excerpt 가
아무 때나 그 바이트를 다시 읽어 같은 판정을 재현할 수 있다.

어댑터가 읽은 값은 증거가 아니다
--------------------------------
아래에서 만드는 FieldValue.value 는 어댑터의 보고다. 판정은 언제나
provenance 가 보존 바이트에서 **다시 뽑은 값**으로 한다. 둘이 같은 파서를
쓰는 것과, 둘 중 하나를 믿는 것은 다른 얘기다.

증거 등급의 상한
----------------
EPO 채널의 상한은 지금 ``normalized``/``exact`` 이고 ``raw`` 는 나오지
않는다. 이유는 epo_parser 의 프로필 주석에 있다 — OPS XML 은 문헌마다 원문일
수도 EPO 번역일 수도 있어 정적 프로필로 원문을 증명할 수 없다. 그래도 web
채널보다 강하다. web 은 대조할 바이트 자체가 없다.

OPS 인증 방식
-------------
OAuth2 client_credentials 다. Consumer Key/Secret 를 Basic 으로 실어 토큰
엔드포인트를 치면 수명이 짧은 bearer 토큰이 나오고, 이후 검색 호출은 그 토큰을
쓴다. 즉 여기서 성공한다는 것은 "키가 EPO 에 등록되어 있고 살아 있다"는 뜻이며,
그 이상(할당량이 얼마나 남았는가, 어떤 서비스가 열려 있는가)은 보증하지 않는다.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from . import artifacts, epo_client, epo_cql, epo_parser, epo_quota
from .base import (
    BackendStatus,
    EvidenceRef,
    FieldValue,
    PatentRecord,
    PatentSearchBackend,
    PatentSearchError,
    PatentSearchNotConfigured,
    PatentSearchQuery,
    PatentSearchResponse,
)

# 설정 키. 이름의 단일 출처. config.DEFAULTS 는 순환 import 때문에 문자열을 직접
# 적으므로(이 패키지가 config 를 import 한다), 두 곳이 어긋나지 않는지는
# test_epo_ops 가 대조한다.
SETTING_ENABLED = "epo_integration_enabled"
SETTING_CONSUMER_KEY = "epo_consumer_key"
SETTING_CONSUMER_SECRET = "epo_consumer_secret"
# 사용량 상태. 사용자가 편집하는 값이 아니라 ARIA 가 관측해 적는 값이므로
# EDITABLE_KEYS 에 넣지 않는다. 화면에는 보여 준다.
SETTING_QUOTA_STATE = "epo_quota_state"
# 네트워크 시간 예산. 채널 전체 벽시계와 다른 축이다(모듈 주석 참조).
SETTING_HTTP_BUDGET = "epo_http_budget_seconds"
# 시간당 사용량 상한. 0 = 관측만 하고 차단하지 않음.
SETTING_HOURLY_LIMIT = "epo_hourly_quota_bytes"
# 한 실행에서 상세 조회할 후보 수 상한.
SETTING_MAX_DETAIL = "epo_max_detail_fetches"

# 토큰 엔드포인트의 단일 출처는 epo_client 다. 여기서 다시 정의하면 두 값이
# 어긋날 수 있고, 자격증명을 보내는 주소가 둘이 된다.
TOKEN_URL = epo_client.TOKEN_URL

# 응답 본문을 읽는 상한. 오류 메시지 몇 줄이면 충분하다.
_MAX_BODY_BYTES = 64 * 1024

# 한 실행에서 상세 조회할 후보 수. 사용자 확정값.
DEFAULT_MAX_DETAIL_FETCHES = 12

_READY_DETAIL = (
    "검색할 수 있습니다. 응답 원본을 보존하고 등록된 EPO 파서로 재파싱해 "
    "발췌를 대조합니다. 증거 등급 상한은 exact 이며 원문(raw) 등급은 나오지 "
    "않습니다."
)
_NO_CREDENTIALS_DETAIL = (
    "Consumer Key 와 Consumer Secret 가 설정되지 않았습니다. 설정 화면에서 "
    "입력하십시오."
)


@dataclass(frozen=True)
class CredentialCheck:
    """자격증명 확인 결과.

    토큰 값은 담지 않는다. 화면에 필요한 것은 "받았는가"이지 토큰 자체가 아니며,
    응답에 실어 보내면 브라우저 개발자 도구와 로그로 새어 나간다.
    """

    ok: bool
    detail: str
    http_status: int | None = None
    expires_in: int | None = None


def _redact(text: str, *secrets: str) -> str:
    """오류 본문에 자격증명이 섞여 있으면 지운다.

    EPO 가 키를 되돌려주지는 않지만, 중간 장비나 프록시의 오류 페이지가 요청
    헤더를 그대로 찍어 주는 경우가 있다. 그 문자열이 화면과 로그로 흘러가지
    않게 여기서 끊는다.

    구현은 epo_client.scrub 하나뿐이다. 보안상 중요한 지우기를 두 벌 두면
    한쪽만 고쳐지는 날이 온다.
    """
    return epo_client.scrub(text, *secrets)


def _describe_error_body(body: bytes, *secrets: str) -> str:
    """OPS 오류 본문에서 사람이 읽을 한 줄을 뽑는다. JSON 도 XML 도 온다."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("description", "error_description", "message", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _redact(value.strip(), *secrets)[:300]
    # XML 이거나 형식을 모르는 경우. 통째로 넣지 않고 앞부분만 남긴다.
    return _redact(" ".join(text.split()), *secrets)[:300]


def _http_error_detail(status: int, body_detail: str) -> str:
    """HTTP 상태를 사용자가 다음에 할 일로 번역한다."""
    if status in (400, 401):
        base = (
            "자격증명이 거절되었습니다. Consumer Key 와 Consumer Secret 를 다시 "
            "확인하십시오(공백이나 줄바꿈이 섞이지 않았는지도)."
        )
    elif status == 403:
        base = (
            "접근이 거부되었습니다. 키는 있으나 사용이 정지되었거나 할당량을 "
            "초과했을 수 있습니다. EPO 개발자 포털에서 상태를 확인하십시오."
        )
    elif status == 404:
        base = "토큰 엔드포인트를 찾지 못했습니다. OPS API 버전이 바뀌었을 수 있습니다."
    elif status == 429:
        base = "요청이 너무 잦아 거절되었습니다(스로틀링). 잠시 뒤 다시 시도하십시오."
    elif 500 <= status < 600:
        base = "EPO OPS 서버 오류입니다. 잠시 뒤 다시 시도하십시오."
    else:
        base = "확인에 실패했습니다."
    return f"{base} (HTTP {status})" + (f" — {body_detail}" if body_detail else "")


def check_credentials(
    key: str, secret: str, timeout: float = 15.0, transport=None
) -> CredentialCheck:
    """OPS 토큰 발급을 한 번 시도해 자격증명이 살아 있는지 확인한다.

    사용자가 설정 화면에서 버튼을 눌렀을 때만 호출된다. 실행(runner) 경로는 이
    함수를 부르지 않는다. 특허 데이터는 요청하지 않으며, 성공해도 토큰을
    저장하지 않는다.

    전송은 epo_client 의 것을 쓴다. ARIA 에서 EPO 로 나가는 경로를 하나로
    모으기 위해서다 — 경로가 둘이면 인증서 정책도 둘이 되고, 테스트에서
    네트워크를 막을 지점도 둘이 된다. 실제로 그 두 번째 경로를 막으려다
    urllib.request.urlopen 을 프로세스 전역에서 바꿔 버린 적이 있다.
    """
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        return CredentialCheck(False, _NO_CREDENTIALS_DETAIL)

    tokens = epo_client.credential_tokens(key, secret)
    request = urllib.request.Request(
        epo_client.TOKEN_URL,
        data=b"grant_type=client_credentials",
        method="POST",
        headers={
            "Authorization": tokens[3],
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    send = transport or epo_client._default_transport
    try:
        response = send(request, timeout)
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return CredentialCheck(
                False,
                "TLS 인증서 검증에 실패했습니다. 이 PC 의 외부 HTTPS 를 중간 "
                "장비가 재서명하고 있다면 그 루트 인증서를 신뢰 목록에 추가해야 "
                "합니다. 검증을 끄고 진행하지는 않습니다.",
            )
        return CredentialCheck(
            False,
            "EPO OPS 에 접속하지 못했습니다: "
            f"{_redact(str(reason), *tokens)}",
        )

    status = int(response.status or 0)
    body = response.body or b""
    if status >= 400:
        return CredentialCheck(
            False,
            _http_error_detail(status, _describe_error_body(body, *tokens)),
            status,
        )

    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return CredentialCheck(
            False,
            "토큰 응답을 해석하지 못했습니다. 중간 장비가 응답을 바꿨을 수 있습니다.",
            status,
        )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return CredentialCheck(False, "응답에 access_token 이 없습니다.", status)
    try:
        expires_in: int | None = int(payload.get("expires_in"))
    except (TypeError, ValueError):
        expires_in = None
    return CredentialCheck(
        True,
        "자격증명이 확인되었습니다. EPO OPS 가 접근 토큰을 발급했습니다.",
        status,
        expires_in,
    )


class DetailBudgetExceeded(PatentSearchError):
    """이 실행에서 허용된 상세 조회 횟수를 다 썼다."""


class EpoOpsBackend(PatentSearchBackend):
    id = "epo"
    display_name = "EPO OPS"

    def __init__(
        self,
        *,
        store: artifacts.ArtifactStore | None = None,
        client: epo_client.OpsClient | None = None,
    ) -> None:
        self._key = ""
        self._secret = ""
        self._store = store
        # 테스트가 가짜 전송 계층을 끼운 클라이언트를 직접 넣는다. 실행 경로는
        # configure 뒤에 만들어진 것을 쓴다.
        self._client = client
        self._ledger = epo_quota.QuotaLedger()
        self._http_budget = epo_client.DEFAULT_HTTP_BUDGET_SECONDS
        self._max_detail_fetches = DEFAULT_MAX_DETAIL_FETCHES
        self._detail_fetches = 0

    # --- 설정 -----------------------------------------------------------
    def configure(self, values: Mapping[str, Any]) -> None:
        self._key = str(values.get(SETTING_CONSUMER_KEY, "") or "").strip()
        self._secret = str(values.get(SETTING_CONSUMER_SECRET, "") or "").strip()
        self._http_budget = _positive_float(
            values.get(SETTING_HTTP_BUDGET), epo_client.DEFAULT_HTTP_BUDGET_SECONDS
        )
        self._max_detail_fetches = _positive_int(
            values.get(SETTING_MAX_DETAIL), DEFAULT_MAX_DETAIL_FETCHES
        )
        self._ledger = epo_quota.QuotaLedger(
            state=epo_quota.QuotaState.from_dict(values.get(SETTING_QUOTA_STATE)),
            hourly_limit=_positive_int(values.get(SETTING_HOURLY_LIMIT), 0),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    @property
    def ledger(self) -> epo_quota.QuotaLedger:
        return self._ledger

    def use_ledger(self, ledger: epo_quota.QuotaLedger) -> None:
        """전역 원장을 물려받는다.

        쿼터는 계정 단위로 하나뿐이므로 원장도 하나여야 한다. 백엔드마다 따로
        두면 각자 자기 스냅샷으로만 한도를 보고, 동시에 도는 두 실행이 같은
        잔량을 두 번 쓴다.
        """
        self._ledger = ledger
        if self._client is not None:
            self._client.ledger = ledger

    def quota_state(self) -> dict:
        """저장할 사용량 상태. 호출부가 AppSetting 에 적는다.

        이 모듈은 DB 를 모른다. 알게 하면 사용량 계산 테스트가 DB 테스트가
        되고, 그러면 아무도 경계 조건을 촘촘히 시험하지 않는다.
        """
        return self._ledger.state.to_dict()

    def usage(self) -> dict:
        """이번 실행에서 쓴 호출·바이트·시간. manifest 와 화면에 그대로 실린다."""
        client = self._client
        base = client.usage() if client is not None else {
            "calls_by_kind": {},
            "http_seconds": 0.0,
            "http_budget_seconds": self._http_budget,
            "quota": self._ledger.snapshot(),
        }
        base["detail_fetches"] = self._detail_fetches
        base["max_detail_fetches"] = self._max_detail_fetches
        return base

    def status(self) -> BackendStatus:
        return BackendStatus(
            backend_id=self.id,
            display_name=self.display_name,
            enabled=True,
            configured=self.has_credentials,
            detail=_READY_DETAIL if self.has_credentials else _NO_CREDENTIALS_DETAIL,
        )

    # --- 내부 자원 -------------------------------------------------------
    def _require_client(self) -> epo_client.OpsClient:
        if not self.has_credentials:
            raise PatentSearchNotConfigured(_NO_CREDENTIALS_DETAIL)
        if self._client is None:
            self._client = epo_client.OpsClient(
                key=self._key,
                secret=self._secret,
                ledger=self._ledger,
                http_budget_seconds=self._http_budget,
            )
        else:
            # 주입된 클라이언트도 같은 원장을 봐야 한다. 따로 세면 한도가
            # 두 벌이 되고, 둘 다 한도 아래인 채로 합계가 넘을 수 있다.
            self._client.ledger = self._ledger
        return self._client

    def _require_store(self) -> artifacts.ArtifactStore:
        if self._store is None:
            # 늦게 만든다. import 시점에 디렉터리를 건드리지 않기 위해서다.
            from ..config import PATHS

            PATHS.evidence_dir.mkdir(parents=True, exist_ok=True)
            self._store = artifacts.ArtifactStore(PATHS.evidence_dir.resolve())
        return self._store

    # --- 검색 -----------------------------------------------------------
    def search(self, query: PatentSearchQuery) -> PatentSearchResponse:
        """base 계약의 진입점. 자유 문장을 안전한 검색항 하나로 바꿔 검색한다."""
        term, dropped = epo_cql.from_free_text(query.text)
        notes: tuple[str, ...] = ()
        if dropped:
            # 자른 사실을 실제로 남긴다. 예전에는 "호출부가 기록한다"고 주석만
            # 적어 두고 기록 경로가 없었다 — 그러면 검색어가 조용히 바뀐 채
            # 실행되고, 0건이 나와도 왜 그런지 알 수 없다.
            notes = (
                "검색어가 길어 뒤쪽 "
                f"{len(dropped)}개 단어를 검색식에서 제외했습니다: "
                + ", ".join(dropped),
            )
        response = self.search_structured(term, max_results=query.max_results)
        return replace(response, notes=response.notes + notes) if notes else response

    def search_structured(
        self, node, *, max_results: int = epo_client.MAX_RESULTS_PER_QUERY
    ) -> PatentSearchResponse:
        """구조화된 질의로 검색한다. 2단계에서 LLM 도구가 부르는 입구다.

        ``node`` 는 epo_cql 의 Term/DateRange/Group 이다. CQL 문자열은 받지
        않는다 — 문자열을 받는 순간 검색식이 실행 명령이 된다.
        """
        client = self._require_client()
        cql = epo_cql.build(node)
        end = max(1, min(int(max_results or 0) or 1, epo_client.MAX_RESULTS_PER_QUERY))
        call = client.search(cql, begin=1, end=end)
        return self._materialize(call, cql=cql)

    def fetch_document(
        self, doc_key: str, constituent: str = epo_client.CONSTITUENT_CLAIMS
    ) -> PatentSearchResponse:
        """후보 하나의 상세를 받는다. 검색 스니펫과 증거 범위를 나누는 지점.

        검색 응답(biblio)에서 온 값과 여기서 온 값은 서로 다른 아티팩트를
        가리킨다. 그래서 "초록까지만 본 후보"와 "청구항까지 본 후보"가 기록에서
        구분된다 — 두 범위를 한 레코드에 뭉개면 그 구분이 사라진다.
        """
        if self._detail_fetches >= self._max_detail_fetches:
            raise DetailBudgetExceeded(
                f"상세 조회 상한({self._max_detail_fetches}건)에 도달했습니다."
            )
        client = self._require_client()
        # 예산은 **시도**에서 깎는다. 성공했을 때만 세면 빠르게 실패하는 상세
        # 조회가 상한을 소모하지 않아, 12건 상한이 걸린 채로 무한히 시도할 수
        # 있다. 실패한 호출도 OPS 사용량과 시간을 쓴다.
        self._detail_fetches += 1
        call = client.fetch(doc_key, constituent)
        return self._materialize(call, cql="")

    def _materialize(self, call, *, cql: str) -> PatentSearchResponse:
        """원본을 먼저 보존하고, 보존된 바이트에서 후보를 만든다.

        순서를 뒤집지 않는다. 파싱 결과로 후보를 만든 뒤 저장하면, 저장에
        실패했을 때 근거 없는 후보가 남는다.
        """
        store = self._require_store()
        artifact_id = store.put(call.body)

        if getattr(call, "no_results", False):
            # 검색 결과 0건. 원본 fault XML 은 바로 위에서 아티팩트로 보존했고
            # 사용량도 이미 반영됐다. 파서에는 넘기지 않는다 — fault 문서는
            # 검색 응답 스키마가 아니라서 읽으려 하면 오류가 된다.
            return PatentSearchResponse(
                records=(),
                total_found=0,
                raw_artifact_id=artifact_id,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        try:
            documents = epo_parser.read_documents(call.body)
            total = epo_parser.total_result_count(call.body) or len(documents)
        except epo_parser.EpoXmlError as exc:
            raise PatentSearchError(
                f"EPO 응답을 읽지 못했습니다: {exc} "
                f"(원본은 아티팩트 {artifact_id[:12]}… 에 보존되어 있습니다)"
            ) from exc

        records = tuple(
            _record_for(document, artifact_id) for document in documents
        )
        return PatentSearchResponse(
            records=records,
            total_found=total,
            raw_artifact_id=artifact_id,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )


def _record_for(document: epo_parser.EpoDocument, artifact_id: str) -> PatentRecord:
    """문헌 하나를 Provider 중립 레코드로. 모든 필드가 아티팩트를 가리킨다."""
    fields = {
        name: FieldValue(
            value=text,
            evidence=EvidenceRef(
                artifact_id=artifact_id,
                field_path=epo_parser.field_path(document.doc_key, name),
                profile_id=epo_parser.PROFILE_EPO_OPS_XML,
            ),
        )
        for name, text in document.text_fields().items()
    }
    # 제목은 언어가 붙은 키로만 들어 있다. 표시용으로 하나를 고르되, 그
    # 선택은 레코드의 title 에만 쓰고 증거 판정에는 쓰지 않는다.
    title = ""
    for key in ("title:en", "title:de", "title:fr"):
        if key in fields:
            title = fields[key].value
            break
    if not title:
        title = next(
            (value.value for key, value in fields.items() if key.startswith("title")),
            "",
        )
    return PatentRecord(
        doc_number=document.publication_number,
        title=title,
        fields=fields,
        source_url=document.espacenet_url,
    )


def _positive_int(value, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def _positive_float(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback
