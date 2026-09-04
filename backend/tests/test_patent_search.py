"""특허 검색 연동 모듈과 Kiwee 토글.

이 단계의 계약을 못 박는다:
- 기본값은 꺼짐.
- 꺼져 있으면 백엔드는 None.
- 켜져 있어도 search() 는 네트워크를 열지 않고 NotConfigured 를 던진다.
- manifest 의 provenance/channel 은 이 단계에서 바뀌지 않는다.
"""

from __future__ import annotations

import pytest

from app import patent_search, search_manifest, settings_service
from app.patent_search import base
from app.patent_search.kiwee_backend import KiweePatentSearchBackend


def test_default_is_off() -> None:
    assert patent_search.SETTING_KEY == "kiwee_integration_enabled"
    assert patent_search.is_enabled({}) is False
    assert patent_search.is_enabled({patent_search.SETTING_KEY: False}) is False


def test_get_backend_none_when_off() -> None:
    assert patent_search.get_backend({patent_search.SETTING_KEY: False}) is None
    assert patent_search.get_backend({}) is None


def test_get_backend_returns_kiwee_when_on() -> None:
    backend = patent_search.get_backend({patent_search.SETTING_KEY: True})
    assert isinstance(backend, KiweePatentSearchBackend)
    assert backend.id == "kiwee"


def test_unknown_backend_is_none() -> None:
    assert (
        patent_search.get_backend({patent_search.SETTING_KEY: True}, "nope") is None
    )


def test_search_never_touches_network() -> None:
    """켜져 있어도 search 는 NotConfigured. 접속 시도 자체가 없어야 한다."""
    backend = patent_search.get_backend({patent_search.SETTING_KEY: True})
    assert backend is not None
    with pytest.raises(base.PatentSearchNotConfigured):
        backend.search(base.PatentSearchQuery(text="anything"))


def test_status_enabled_but_not_configured() -> None:
    backend = KiweePatentSearchBackend()
    status = backend.status()
    assert status.enabled is True
    assert status.configured is False
    assert status.detail


def test_describe_reflects_toggle() -> None:
    off = patent_search.describe({patent_search.SETTING_KEY: False})
    assert off.enabled is False
    assert off.configured is False

    on = patent_search.describe({patent_search.SETTING_KEY: True})
    assert on.enabled is True
    assert on.configured is False
    assert on.display_name


def test_register_backend_roundtrip() -> None:
    class _Fake(base.PatentSearchBackend):
        id = "fake_test"
        display_name = "가짜"

        def status(self) -> base.BackendStatus:
            return base.BackendStatus(self.id, self.display_name, True, True)

        def search(self, query: base.PatentSearchQuery) -> base.PatentSearchResponse:
            return base.PatentSearchResponse(records=(), total_found=0)

    patent_search.register_backend("fake_test", _Fake)
    try:
        backend = patent_search.get_backend(
            {patent_search.SETTING_KEY: True}, "fake_test"
        )
        assert isinstance(backend, _Fake)
    finally:
        patent_search._REGISTRY.pop("fake_test", None)


def test_model_reported_channel_stays_web_only() -> None:
    """모델이 주장할 수 있는 채널은 web 뿐이다.

    이전 단계에서는 patent_db 가 아예 없다는 것으로 이 불변식을 표현했다.
    지금은 ARIA 생산자용 채널로 존재하되 모델 보고 목록에는 없다는 형태로
    바뀌었다. 지켜야 할 것은 이름의 부재가 아니라 '모델이 그 라벨을 붙일 수
    없다'는 성질이다.
    """
    import json
    parsed, _ = search_manifest.parse(json.dumps({"candidates": [{
        "group": "A", "channel": "patent_db", "evidence_level": "official_full_text",
        "doc_number": "EP123A1", "mapping": [],
    }]}))
    assert "channel" not in parsed["candidates"][0]
    assert "evidence_level" not in parsed["candidates"][0]


# --------------------------------------------------------------- settings 배선


def test_setting_default_off_via_service(client) -> None:
    values = client.get("/api/settings").json()["values"]
    assert values["kiwee_integration_enabled"] is False


def test_setting_toggle_and_coerce(client) -> None:
    updated = client.put(
        "/api/settings", json={"values": {"kiwee_integration_enabled": True}}
    ).json()
    assert updated["values"]["kiwee_integration_enabled"] is True
    # 켜면 아직 실제 검색이 안 된다는 경고가 뜬다.
    assert any("Kiwee" in w for w in updated["warnings"])
    # 원복
    restored = client.put(
        "/api/settings", json={"values": {"kiwee_integration_enabled": False}}
    ).json()
    assert restored["values"]["kiwee_integration_enabled"] is False
    assert not any("Kiwee" in w for w in restored["warnings"])
