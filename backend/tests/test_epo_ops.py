"""EPO OPS 연동 — 설정 배선, 자격증명 가림, 토큰 확인.

이 파일의 어떤 테스트도 실제 네트워크를 열지 않는다. EPO 로 나가는 경로는
epo_client._live_transport 하나뿐이고, 여기서는 그것만 가짜로 바꾼다.
"""

from __future__ import annotations

import base64
import io
import json
import ssl
import urllib.error

import pytest

from app import patent_search, settings_service
from app.config import DEFAULTS
from app.patent_search import epo_backend, epo_client


# ------------------------------------------------------------------ 설정 배선


def test_setting_key_names_match_defaults() -> None:
    """키 이름은 두 곳에 적혀 있다(순환 import 회피). 어긋나면 여기서 걸린다."""
    for key in (
        epo_backend.SETTING_ENABLED,
        epo_backend.SETTING_CONSUMER_KEY,
        epo_backend.SETTING_CONSUMER_SECRET,
    ):
        assert key in DEFAULTS, key
        assert key in settings_service.EDITABLE_KEYS, key
    assert DEFAULTS[epo_backend.SETTING_ENABLED] is False
    assert DEFAULTS[epo_backend.SETTING_CONSUMER_KEY] == ""
    assert DEFAULTS[epo_backend.SETTING_CONSUMER_SECRET] == ""


def test_toggles_are_independent() -> None:
    """EPO 를 켜도 Kiwee 는 꺼진 채로 있어야 한다."""
    values = {epo_backend.SETTING_ENABLED: True}
    assert patent_search.is_enabled(values, "epo") is True
    assert patent_search.is_enabled(values, "kiwee") is False
    assert patent_search.is_enabled({patent_search.SETTING_KEY: True}, "epo") is False


def test_get_backend_injects_credentials() -> None:
    backend = patent_search.get_backend(
        {
            epo_backend.SETTING_ENABLED: True,
            epo_backend.SETTING_CONSUMER_KEY: "key",
            epo_backend.SETTING_CONSUMER_SECRET: "secret",
        },
        "epo",
    )
    assert isinstance(backend, epo_backend.EpoOpsBackend)
    assert backend.has_credentials is True


def test_configured_follows_credentials() -> None:
    """검색이 배선된 뒤로 configured 는 곧 '자격증명이 있는가'다.

    예전에는 자격증명이 있어도 False 였다(검색 경로가 없었으므로). 그 구분은
    이제 다른 축으로 옮겨 갔다 — 증거 등급의 상한이 exact 이고 raw 는 나오지
    않는다는 사실이며, test_epo_search 가 그쪽을 지킨다.
    """
    on = {epo_backend.SETTING_ENABLED: True}
    without = patent_search.describe(on, "epo")
    assert without.enabled is True and without.configured is False
    assert "Consumer Key" in without.detail

    with_creds = patent_search.describe(
        {
            **on,
            epo_backend.SETTING_CONSUMER_KEY: "key",
            epo_backend.SETTING_CONSUMER_SECRET: "secret",
        },
        "epo",
    )
    assert with_creds.configured is True
    assert "원문" in with_creds.detail


def test_search_without_credentials_never_opens_network() -> None:
    """자격증명이 없으면 네트워크를 열기 전에 멈춘다.

    conftest 의 block_epo_network 가 실제 요청을 막으므로, 이 테스트가 조용히
    바깥으로 나가는 일은 없다.
    """
    backend = epo_backend.EpoOpsBackend()
    backend.configure({})
    with pytest.raises(patent_search.PatentSearchNotConfigured):
        backend.search(patent_search.PatentSearchQuery(text="anything"))


def test_describe_all_covers_every_backend() -> None:
    ids = {status.backend_id for status in patent_search.describe_all(dict(DEFAULTS))}
    assert ids == set(patent_search.BACKEND_IDS)


# ------------------------------------------------------------- 자격증명 검증


def test_credentials_reject_inner_whitespace(client) -> None:
    """값 **가운데** 공백은 잘못 복사된 값이므로 거절한다.

    앞뒤 공백과 다르게 다룬다. 그 차이는 화면 안내에도 그대로 적혀 있어야
    한다 — 안내는 "거절한다"고 하는데 코드가 조용히 잘라내면, 사용자는 자기가
    넣은 값과 저장된 값이 다르다는 사실을 모른다.
    """
    response = client.put(
        "/api/settings", json={"values": {"epo_consumer_key": "abc def"}}
    )
    assert response.status_code == 400
    response = client.put(
        "/api/settings", json={"values": {"epo_consumer_secret": "x" * 300}}
    )
    assert response.status_code == 400


def test_credentials_trim_paste_whitespace(client) -> None:
    """앞뒤 공백·줄바꿈은 붙여넣기 부산물로 보고 잘라낸다. 화면 안내와 같다."""
    updated = client.put(
        "/api/settings",
        json={"values": {"epo_consumer_key": "  PASTEDKEY\n"}},
    ).json()
    assert updated["values"]["epo_consumer_key"] == "PASTEDKEY"
    client.put("/api/settings", json={"values": {"epo_consumer_key": ""}})


def test_secret_is_stored_but_never_returned(client) -> None:
    updated = client.put(
        "/api/settings",
        json={
            "values": {
                "epo_consumer_key": "CONSUMER-KEY",
                "epo_consumer_secret": "CONSUMER-SECRET",
            }
        },
    ).json()
    # Key 는 식별자라 되돌려주고, Secret 은 지워서 내보낸다.
    assert updated["values"]["epo_consumer_key"] == "CONSUMER-KEY"
    assert updated["values"]["epo_consumer_secret"] == ""
    assert updated["secrets_set"]["epo_consumer_secret"] is True
    assert "CONSUMER-SECRET" not in json.dumps(updated)

    # 다시 읽어도 마찬가지고, 저장 자체는 살아 있다.
    fetched = client.get("/api/settings").json()
    assert fetched["values"]["epo_consumer_secret"] == ""
    assert fetched["secrets_set"]["epo_consumer_secret"] is True

    cleared = client.put(
        "/api/settings", json={"values": {"epo_consumer_secret": ""}}
    ).json()
    assert cleared["secrets_set"]["epo_consumer_secret"] is False


def test_warning_when_enabled_without_credentials(client) -> None:
    updated = client.put(
        "/api/settings", json={"values": {"epo_integration_enabled": True}}
    ).json()
    assert any("EPO OPS" in note for note in updated["warnings"])
    restored = client.put(
        "/api/settings", json={"values": {"epo_integration_enabled": False}}
    ).json()
    assert not any("EPO OPS" in note for note in restored["warnings"])


def test_check_endpoint_refuses_when_integration_off(client) -> None:
    client.put("/api/settings", json={"values": {"epo_integration_enabled": False}})
    assert client.post("/api/settings/epo/check").status_code == 400


# ------------------------------------------------------------- 토큰 확인 로직


def _patch_transport(monkeypatch, handler) -> dict:
    """check_credentials 가 쓰는 전송 계층을 가짜로 바꾼다.

    EPO 로 나가는 경로는 epo_client._live_transport 하나뿐이므로 여기만 막으면
    된다. 예전에는 urllib.request.urlopen 을 프로세스 전역에서 바꿨는데, 그러면
    EPO 와 무관한 코드까지 영향을 받는다.
    """
    seen: dict = {}

    def fake_transport(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["data"] = request.data
        seen["timeout"] = timeout
        return handler()

    monkeypatch.setattr(epo_client, "_live_transport", fake_transport)
    return seen


def response(body: bytes, status: int = 200) -> epo_client.HttpResponse:
    return epo_client.HttpResponse(status=status, headers={}, body=body)


def test_check_requires_both_values() -> None:
    assert epo_backend.check_credentials("", "secret").ok is False
    assert epo_backend.check_credentials("key", "").ok is False


def test_check_success(monkeypatch) -> None:
    seen = _patch_transport(
        monkeypatch,
        lambda: response(
            json.dumps({"access_token": "tok", "expires_in": "1200"}).encode()
        ),
    )
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is True and result.expires_in == 1200
    # 토큰은 결과 어디에도 실리지 않는다.
    assert "tok" not in result.detail
    # client_credentials 를 상수 주소로만 보낸다.
    assert seen["url"] == epo_client.TOKEN_URL
    assert seen["method"] == "POST"
    assert seen["data"] == b"grant_type=client_credentials"
    assert seen["headers"]["Authorization"].startswith("Basic ")


def test_check_uses_the_same_transport_as_search() -> None:
    """EPO 로 나가는 경로는 하나여야 한다.

    둘이면 인증서 정책도 둘이 되고, 테스트에서 막을 지점도 둘이 된다. 실제로
    두 번째 경로를 막으려다 urllib.request.urlopen 을 프로세스 전역에서
    바꿔 버린 적이 있다.
    """
    assert epo_backend.TOKEN_URL == epo_client.TOKEN_URL


def test_check_verifies_certificates() -> None:
    """인증서 검증을 끄지 않는다. 전송 계층이 기본 컨텍스트를 쓴다."""
    source = io.open(
        epo_client.__file__, encoding="utf-8"
    ).read()
    assert "ssl.create_default_context()" in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source


def test_check_rejects_response_without_token(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda: response(json.dumps({"ok": 1}).encode()))
    assert epo_backend.check_credentials("key", "secret").ok is False


def test_check_maps_401(monkeypatch) -> None:
    _patch_transport(
        monkeypatch,
        lambda: response(
            json.dumps({"description": "invalid client"}).encode(), status=401
        ),
    )
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is False and result.http_status == 401
    assert "invalid client" in result.detail


def test_check_redacts_credentials_echoed_back(monkeypatch) -> None:
    """중간 장비가 요청 헤더를 오류 페이지에 찍어도 화면으로 새지 않는다."""
    _patch_transport(
        monkeypatch,
        lambda: response(b"rejected key=THEKEY secret=THESECRET", status=400),
    )
    result = epo_backend.check_credentials("THEKEY", "THESECRET")
    assert "THEKEY" not in result.detail and "THESECRET" not in result.detail


def test_check_redacts_the_basic_authorization_value(monkeypatch) -> None:
    """프록시가 Authorization 헤더를 반사해도 Base64 자격증명이 새지 않는다."""
    encoded = base64.b64encode(b"THEKEY:THESECRET").decode("ascii")
    _patch_transport(
        monkeypatch,
        lambda: response(f"Authorization: Basic {encoded}".encode(), status=400),
    )
    result = epo_backend.check_credentials("THEKEY", "THESECRET")
    assert encoded not in result.detail
    assert f"Basic {encoded}" not in result.detail


def test_check_reports_tls_failure_without_bypassing(monkeypatch) -> None:
    def raise_tls():
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed")
        )

    _patch_transport(monkeypatch, raise_tls)
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is False
    assert "인증서" in result.detail


def test_check_reports_connection_failure(monkeypatch) -> None:
    def raise_conn():
        raise urllib.error.URLError(OSError("no route to host"))

    _patch_transport(monkeypatch, raise_conn)
    assert epo_backend.check_credentials("key", "secret").ok is False
