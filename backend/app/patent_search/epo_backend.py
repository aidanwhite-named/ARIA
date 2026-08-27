"""EPO OPS(Open Patent Services) 특허 검색 백엔드.

이 단계에서 하는 일은 둘뿐이다.

  1) 자격증명(Consumer Key / Consumer Secret)을 설정에서 받아 보관하고, 그
     자격증명이 EPO 쪽에서 실제로 발급·활성화된 것인지 **사용자가 버튼을 눌렀을
     때만** 확인한다(check_credentials). 확인은 OAuth 토큰 발급 호출 하나뿐이며
     특허 데이터는 한 건도 받지 않는다.
  2) 검색 경로에는 아직 연결하지 않는다. search() 는 네트워크를 열지 않고
     PatentSearchNotConfigured 를 던진다.

2번이 남아 있는 이유는 권한이 아니라 증거 계약이다. 이 패키지의 계약은 응답
원본 바이트를 보존하고(artifacts), 등록된 소스 프로필로 재파싱한 뒤(parsers)
발췌를 원본에 대조해서야(provenance) 후보를 내보내는 것이다. OPS 응답에 대한
그 배선 없이 검색만 붙이면, 검증되지 않은 값이 '특허 DB 채널' 이름을 달고
보고서에 들어간다 — web 채널이 가진 증거 약점을 그대로 들여오면서 이름만 더
강해 보이는 최악의 조합이다.

OPS 인증 방식
-------------
OAuth2 client_credentials 다. Consumer Key/Secret 를 Basic 으로 실어 토큰
엔드포인트를 치면 수명이 짧은 bearer 토큰이 나오고, 이후 검색 호출은 그 토큰을
쓴다. 즉 여기서 성공한다는 것은 "키가 EPO 에 등록되어 있고 살아 있다"는 뜻이며,
그 이상(할당량이 얼마나 남았는가, 어떤 서비스가 열려 있는가)은 보증하지 않는다.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import (
    BackendStatus,
    PatentSearchBackend,
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

# 토큰 엔드포인트. 상수이며 설정으로 바꿀 수 없다 — 자격증명을 보내는 주소를
# 사용자 입력이나 응답 본문이 바꿀 수 있으면 그 순간 자격증명 유출 경로가 된다.
TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"

# 응답 본문을 읽는 상한. 오류 메시지 몇 줄이면 충분하다.
_MAX_BODY_BYTES = 64 * 1024

_NOT_WIRED_DETAIL = (
    "자격증명은 저장되어 있으나 검색 경로 연결(응답 원본 보존·출처 검증)이 아직 "
    "구현되지 않아 실제 검색은 수행되지 않습니다."
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
    """
    cleaned = text
    for secret in secrets:
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


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


def check_credentials(key: str, secret: str, timeout: float = 15.0) -> CredentialCheck:
    """OPS 토큰 발급을 한 번 시도해 자격증명이 살아 있는지 확인한다.

    사용자가 설정 화면에서 버튼을 눌렀을 때만 호출된다. 실행(runner) 경로는 이
    함수를 부르지 않는다. 특허 데이터는 요청하지 않으며, 성공해도 토큰을
    저장하지 않는다.
    """
    key = (key or "").strip()
    secret = (secret or "").strip()
    if not key or not secret:
        return CredentialCheck(False, _NO_CREDENTIALS_DETAIL)

    basic = base64.b64encode(f"{key}:{secret}".encode()).decode("ascii")
    request = urllib.request.Request(
        TOKEN_URL,
        data=b"grant_type=client_credentials",
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    # 인증서 검증은 끄지 않는다. 이 PC 의 외부 HTTPS 는 중간 장비가 종단하고
    # 재서명하지만 그 루트는 Windows 인증서 저장소에 있고, 표준 ssl 의 기본
    # 컨텍스트는 Windows 저장소를 읽는다. 그래도 실패하면 우회하지 말고 실패한
    # 사실을 그대로 보고한다 — verify 를 끄면 자격증명을 아무에게나 보내게 된다.
    context = ssl.create_default_context()

    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=context
        ) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(_MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:  # URLError 의 하위라 먼저 잡는다
        status = int(exc.code)
        detail = _describe_error_body(exc.read(_MAX_BODY_BYTES) or b"", key, secret)
        return CredentialCheck(False, _http_error_detail(status, detail), status)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return CredentialCheck(
                False,
                "TLS 인증서 검증에 실패했습니다. 이 PC 의 외부 HTTPS 를 중간 "
                "장비가 재서명하고 있다면 그 루트 인증서를 신뢰 목록에 추가해야 "
                "합니다. 검증을 끄고 진행하지는 않습니다.",
            )
        return CredentialCheck(
            False,
            f"EPO OPS 에 접속하지 못했습니다: {_redact(str(reason), key, secret)}",
        )
    except (TimeoutError, OSError) as exc:
        return CredentialCheck(
            False, f"EPO OPS 접속이 실패했습니다: {_redact(str(exc), key, secret)}"
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


class EpoOpsBackend(PatentSearchBackend):
    id = "epo"
    display_name = "EPO OPS"

    def __init__(self) -> None:
        self._key = ""
        self._secret = ""

    def configure(self, values: Mapping[str, Any]) -> None:
        self._key = str(values.get(SETTING_CONSUMER_KEY, "") or "").strip()
        self._secret = str(values.get(SETTING_CONSUMER_SECRET, "") or "").strip()

    @property
    def has_credentials(self) -> bool:
        return bool(self._key and self._secret)

    def status(self) -> BackendStatus:
        # configured 는 "실제로 검색을 수행할 수 있는가"다. 자격증명이 있어도
        # 검색 배선이 없으므로 아직 False 다. 이 둘을 뭉뚱그리면 화면은
        # "연동됨"이라고 말하는데 검색은 안 되는 상태가 된다.
        return BackendStatus(
            backend_id=self.id,
            display_name=self.display_name,
            enabled=True,
            configured=False,
            detail=(
                _NOT_WIRED_DETAIL if self.has_credentials else _NO_CREDENTIALS_DETAIL
            ),
        )

    def search(self, query: PatentSearchQuery) -> PatentSearchResponse:
        raise PatentSearchNotConfigured(
            _NOT_WIRED_DETAIL if self.has_credentials else _NO_CREDENTIALS_DETAIL
        )
