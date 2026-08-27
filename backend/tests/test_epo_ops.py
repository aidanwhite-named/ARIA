"""EPO OPS 연동 — 설정 배선, 자격증명 가림, 토큰 확인.

이 파일의 어떤 테스트도 실제 네트워크를 열지 않는다. check_credentials 는
urlopen 을 가짜로 바꿔서만 부른다.
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
from app.patent_search import epo_backend


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


def test_status_separates_credentials_from_wiring() -> None:
    """자격증명이 있어도 configured 는 False 다 — 검색 배선이 없기 때문이다."""
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
    assert with_creds.configured is False
    assert "검색 경로" in with_creds.detail


def test_search_never_opens_network() -> None:
    backend = epo_backend.EpoOpsBackend()
    backend.configure(
        {
            epo_backend.SETTING_CONSUMER_KEY: "key",
            epo_backend.SETTING_CONSUMER_SECRET: "secret",
        }
    )
    with pytest.raises(patent_search.PatentSearchNotConfigured):
        backend.search(patent_search.PatentSearchQuery(text="anything"))


def test_describe_all_covers_every_backend() -> None:
    ids = {status.backend_id for status in patent_search.describe_all(dict(DEFAULTS))}
    assert ids == set(patent_search.BACKEND_IDS)


# ------------------------------------------------------------- 자격증명 검증


def test_credentials_reject_whitespace(client) -> None:
    """붙여넣기로 딸려 온 줄바꿈은 조용히 자르지 않고 거절한다."""
    response = client.put(
        "/api/settings", json={"values": {"epo_consumer_key": "abc def"}}
    )
    assert response.status_code == 400
    response = client.put(
        "/api/settings", json={"values": {"epo_consumer_secret": "x" * 300}}
    )
    assert response.status_code == 400


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


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *exc):  # noqa: D105
        self.close()
        return False


def _patch_urlopen(monkeypatch, handler) -> dict:
    seen: dict = {}

    def fake_urlopen(request, timeout=None, context=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["data"] = request.data
        seen["context"] = context
        return handler()

    monkeypatch.setattr(epo_backend.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_check_requires_both_values() -> None:
    assert epo_backend.check_credentials("", "secret").ok is False
    assert epo_backend.check_credentials("key", "").ok is False


def test_check_success(monkeypatch) -> None:
    seen = _patch_urlopen(
        monkeypatch,
        lambda: _FakeResponse(
            json.dumps({"access_token": "tok", "expires_in": "1200"}).encode()
        ),
    )
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is True and result.expires_in == 1200
    # 토큰은 결과 어디에도 실리지 않는다.
    assert "tok" not in result.detail
    # client_credentials 를 상수 주소로만 보낸다. 인증서 검증은 켜져 있다.
    assert seen["url"] == epo_backend.TOKEN_URL
    assert seen["method"] == "POST"
    assert seen["data"] == b"grant_type=client_credentials"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


def test_check_rejects_response_without_token(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, lambda: _FakeResponse(json.dumps({"ok": 1}).encode()))
    assert epo_backend.check_credentials("key", "secret").ok is False


def test_check_maps_401(monkeypatch) -> None:
    def raise_401():
        raise urllib.error.HTTPError(
            epo_backend.TOKEN_URL,
            401,
            "Unauthorized",
            {},
            io.BytesIO(json.dumps({"description": "invalid client"}).encode()),
        )

    _patch_urlopen(monkeypatch, raise_401)
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is False and result.http_status == 401
    assert "invalid client" in result.detail


def test_check_redacts_credentials_echoed_back(monkeypatch) -> None:
    """중간 장비가 요청 헤더를 오류 페이지에 찍어도 화면으로 새지 않는다."""

    def raise_400():
        raise urllib.error.HTTPError(
            epo_backend.TOKEN_URL,
            400,
            "Bad Request",
            {},
            io.BytesIO(b"rejected key=THEKEY secret=THESECRET"),
        )

    _patch_urlopen(monkeypatch, raise_400)
    result = epo_backend.check_credentials("THEKEY", "THESECRET")
    assert "THEKEY" not in result.detail and "THESECRET" not in result.detail


def test_check_redacts_the_basic_authorization_value(monkeypatch) -> None:
    """프록시가 Authorization 헤더를 반사해도 Base64 자격증명이 새지 않는다."""

    encoded = base64.b64encode(b"THEKEY:THESECRET").decode("ascii")

    def raise_400():
        raise urllib.error.HTTPError(
            epo_backend.TOKEN_URL,
            400,
            "Bad Request",
            {},
            io.BytesIO(f"Authorization: Basic {encoded}".encode()),
        )

    _patch_urlopen(monkeypatch, raise_400)
    result = epo_backend.check_credentials("THEKEY", "THESECRET")
    assert encoded not in result.detail
    assert f"Basic {encoded}" not in result.detail


def test_check_reports_tls_failure_without_bypassing(monkeypatch) -> None:
    def raise_tls():
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed")
        )

    _patch_urlopen(monkeypatch, raise_tls)
    result = epo_backend.check_credentials("key", "secret")
    assert result.ok is False
    assert "인증서" in result.detail


def test_check_reports_connection_failure(monkeypatch) -> None:
    def raise_conn():
        raise urllib.error.URLError(OSError("no route to host"))

    _patch_urlopen(monkeypatch, raise_conn)
    assert epo_backend.check_credentials("key", "secret").ok is False
