"""agy 페이지 열람 허용 목록 병합.

2026-09-02 실행이 이 파일의 이유다. 허용 목록에 arxiv.org 가 없어서 agy 가
``read_url_content`` 를 자동 거부했고, **거부 하나가 턴 전체를 취소시켜** 이미
끝난 검색 5건과 감사 블록이 함께 사라졌다.

그래서 ARIA 가 권장 호스트를 병합한다. 남의 도구 설정 파일을 만지는 일이므로
지켜야 할 선이 그만큼 많고, 이 파일이 그 선을 지킨다.
"""

from __future__ import annotations

import json

import pytest

from app.providers import agy_permissions


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    """실제 홈 디렉터리 대신 임시 파일을 보게 만든다."""
    path = tmp_path / "antigravity-cli" / "settings.json"
    monkeypatch.setenv("ARIA_AGY_SETTINGS_PATH", str(path))
    return path


def _write(path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _backups(path) -> list:
    return sorted(path.parent.glob(f"{path.name}.aria-backup-*"))


# --------------------------------------------------------------- 병합 규칙


def test_existing_rules_survive_and_recommended_hosts_are_added(settings_file):
    """기존 항목을 덮어쓰지 않는다. 권장 호스트만 뒤에 붙인다.

    사용자가 직접 넣은 규칙은 ARIA 가 모르는 이유로 거기 있다. 병합이 아니라
    치환이 되면 그 이유가 조용히 사라진다.
    """
    _write(
        settings_file,
        {
            "allowNonWorkspaceAccess": True,
            "permissions": {"allow": ["read_url(patents.google.com)"]},
            "trustedWorkspaces": ["D:/develope/Forge"],
        },
    )

    state, added = agy_permissions.apply_recommended()

    document = _read(settings_file)
    rules = document["permissions"]["allow"]
    # 사용자가 넣었던 규칙이 **첫 자리 그대로** 남아 있다.
    assert rules[0] == "read_url(patents.google.com)"
    # 허용 목록 밖의 설정도 손대지 않는다.
    assert document["allowNonWorkspaceAccess"] is True
    assert document["trustedWorkspaces"] == ["D:/develope/Forge"]

    for host in agy_permissions.RECOMMENDED_HOSTS:
        assert f"read_url({host})" in rules
    assert added == list(agy_permissions.RECOMMENDED_HOSTS)
    assert state.missing == ()
    assert "patents.google.com" in state.allowed_hosts


def test_already_present_hosts_are_not_duplicated(settings_file):
    """이미 있는 규칙은 다시 추가하지 않는다."""
    _write(
        settings_file,
        {"permissions": {"allow": ["read_url(arxiv.org)", "read_url(www.mdpi.com)"]}},
    )

    _, added = agy_permissions.apply_recommended()

    rules = _read(settings_file)["permissions"]["allow"]
    assert rules.count("read_url(arxiv.org)") == 1
    assert rules.count("read_url(www.mdpi.com)") == 1
    assert "arxiv.org" not in added
    assert "www.mdpi.com" not in added


def test_second_run_changes_nothing_and_writes_no_backup(settings_file):
    """멱등하다. 바뀔 것이 없으면 파일도 백업도 만들지 않는다.

    설정 화면을 열 때마다 백업이 쌓이면 그건 백업이 아니라 쓰레기다.
    """
    _write(settings_file, {"permissions": {"allow": []}})

    agy_permissions.apply_recommended()
    first = settings_file.read_bytes()
    backups_after_first = len(_backups(settings_file))

    _, added = agy_permissions.apply_recommended()

    assert added == []
    assert settings_file.read_bytes() == first
    assert len(_backups(settings_file)) == backups_after_first


def test_a_backup_is_written_before_the_file_changes(settings_file):
    """쓰기 전에 복구 가능한 사본을 남긴다."""
    original = {"permissions": {"allow": ["read_url(patents.google.com)"]}}
    _write(settings_file, original)

    agy_permissions.apply_recommended()

    backups = _backups(settings_file)
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == original


def test_no_wildcard_rule_is_ever_written(settings_file):
    """read_url(*) 를 만들지 않는다. 범위를 넓히면 감사할 수 없게 된다."""
    _write(settings_file, {"permissions": {"allow": []}})

    agy_permissions.apply_recommended()

    rules = _read(settings_file)["permissions"]["allow"]
    assert "read_url(*)" not in rules
    assert all(agy_permissions.WILDCARD not in rule for rule in rules)


def test_an_existing_wildcard_is_reported_but_not_removed(settings_file):
    """사용자가 넣은 와일드카드는 지우지 않되 화면에 알린다."""
    _write(settings_file, {"permissions": {"allow": ["read_url(*)"]}})

    state, _ = agy_permissions.apply_recommended()

    assert state.wildcard is True
    assert "read_url(*)" in _read(settings_file)["permissions"]["allow"]


# ------------------------------------------------------------- 손상된 파일


def test_broken_json_is_not_overwritten(settings_file):
    """JSON 이 깨져 있으면 손대지 않고 오류를 낸다.

    새 파일로 덮어쓰면 trustedWorkspaces 처럼 ARIA 가 모르는 설정이 조용히
    사라진다. 고칠 수 있는 사람은 사용자뿐이다.
    """
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    broken = '{"permissions": {"allow": ["read_url(a.com)"'
    settings_file.write_text(broken, encoding="utf-8")

    with pytest.raises(agy_permissions.AgyPermissionsError) as excinfo:
        agy_permissions.apply_recommended()

    assert settings_file.read_text(encoding="utf-8") == broken
    assert _backups(settings_file) == []
    assert "JSON" in str(excinfo.value)


def test_broken_json_surfaces_as_an_error_state_instead_of_raising(settings_file):
    """읽기 경로는 터지지 않는다. 실패를 error 칸에 담아 화면에 보인다."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{", encoding="utf-8")

    state = agy_permissions.read_state()

    assert state.error
    assert state.allowed_hosts == ()
    # 목록을 읽지 못한 실행은 "제한 없음"이 아니라 "하나도 열 수 없음"이다.
    assert agy_permissions.allowed_hosts() == ()


def test_allow_that_is_not_a_list_is_refused(settings_file):
    """permissions.allow 가 배열이 아니면 덮어쓰지 않는다."""
    _write(settings_file, {"permissions": {"allow": "read_url(a.com)"}})

    with pytest.raises(agy_permissions.AgyPermissionsError):
        agy_permissions.apply_recommended()

    assert _read(settings_file)["permissions"]["allow"] == "read_url(a.com)"


# ------------------------------------------------------------- 없는 파일


def test_missing_file_is_left_alone_unless_creation_is_asked(settings_file):
    """agy 를 설치하지도 않은 기계에 설정 파일을 만들지 않는다."""
    state, added = agy_permissions.apply_recommended()

    assert not settings_file.exists()
    assert added == []
    assert state.exists is False
    assert state.missing == agy_permissions.RECOMMENDED_HOSTS


def test_missing_file_is_created_when_the_cli_was_found(settings_file):
    """agy 실행 파일을 확인한 경로에서는 권장 목록으로 만든다."""
    state, added = agy_permissions.apply_recommended(create=True)

    assert settings_file.exists()
    assert added == list(agy_permissions.RECOMMENDED_HOSTS)
    assert state.missing == ()
    assert _read(settings_file)["permissions"]["allow"] == [
        f"read_url({host})" for host in agy_permissions.RECOMMENDED_HOSTS
    ]


# ------------------------------------------------------------- 상태 읽기


def test_state_separates_applied_from_missing(settings_file):
    """설정 화면이 "무엇이 실제로 적용됐나"를 그릴 근거."""
    _write(
        settings_file,
        {"permissions": {"allow": ["read_url(arxiv.org)", "read_url(직접.example)"]}},
    )

    state = agy_permissions.read_state()

    assert state.applied == ("arxiv.org",)
    assert "arxiv.org" not in state.missing
    assert "dl.acm.org" in state.missing
    # 사용자가 직접 넣은 호스트도 "지금 열 수 있는 주소"에는 들어간다.
    assert "직접.example" in state.allowed_hosts
    assert state.to_dict()["recommended"] == list(agy_permissions.RECOMMENDED_HOSTS)


def test_rules_that_are_not_read_url_are_ignored(settings_file):
    """다른 종류의 규칙은 열람 허용 호스트로 읽지 않는다."""
    _write(
        settings_file,
        {
            "permissions": {
                "allow": ["run_command(git)", "write_to_file(*)", "read_url(arxiv.org)"]
            }
        },
    )

    state = agy_permissions.read_state()

    assert state.allowed_hosts == ("arxiv.org",)


# ------------------------------------------------- 일회성 자동 적용


@pytest.fixture()
def fresh_marker(client):
    """이 설치가 아직 자동 적용을 하지 않은 상태로 되돌린다."""
    from app.db import session_scope
    from app.models import AppSetting
    from app import settings_service

    def clear() -> None:
        with session_scope() as session:
            row = session.get(AppSetting, settings_service.AGY_MIGRATION_KEY)
            if row is not None:
                session.delete(row)
            session.flush()

    clear()
    yield
    clear()


def test_the_migration_applies_once_and_records_the_version(
    settings_file, fresh_marker
):
    """설치당 한 번 적용하고, 끝났다는 사실을 버전으로 남긴다."""
    from app.db import session_scope
    from app import settings_service

    _write(settings_file, {"permissions": {"allow": []}})

    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is False
        _, added = settings_service.apply_agy_allowlist(session, forced=False)

    assert added == list(agy_permissions.RECOMMENDED_HOSTS)
    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is True


def test_a_host_the_user_deleted_is_not_added_back(settings_file, fresh_marker):
    """지운 호스트를 프로그램이 되살리지 않는다.

    이것이 자동 적용을 일회성으로 만든 이유다. 검사할 때마다 병합하면 사용자가
    지웠다는 사실이 조용히 되돌려지고, 설정 화면을 여는 것만으로 검사가 도는
    구조라 지운 사람은 자기가 지웠는지조차 확인할 수 없다.
    """
    from app.db import session_scope
    from app import settings_service

    _write(settings_file, {"permissions": {"allow": []}})
    with session_scope() as session:
        settings_service.apply_agy_allowlist(session, forced=False)

    # 사용자가 두 곳을 직접 지웠다.
    document = _read(settings_file)
    document["permissions"]["allow"] = [
        rule
        for rule in document["permissions"]["allow"]
        if "researchgate" not in rule and "arxiv" not in rule
    ]
    _write(settings_file, document)

    with session_scope() as session:
        state, added = settings_service.apply_agy_allowlist(session, forced=False)

    assert added == []
    rules = _read(settings_file)["permissions"]["allow"]
    assert not any("researchgate" in rule for rule in rules)
    assert not any("arxiv" in rule for rule in rules)
    # 화면은 "빠져 있다"는 사실만 보여 준다. 되돌리지는 않는다.
    assert "arxiv.org" in state.missing


def test_the_button_reapplies_even_after_the_migration_ran(
    settings_file, fresh_marker
):
    """다시 넣는 유일한 경로. 사용자가 눌렀을 때만 돈다."""
    from app.db import session_scope
    from app import settings_service

    _write(settings_file, {"permissions": {"allow": []}})
    with session_scope() as session:
        settings_service.apply_agy_allowlist(session, forced=False)

    document = _read(settings_file)
    document["permissions"]["allow"] = ["read_url(patents.google.com)"]
    _write(settings_file, document)

    with session_scope() as session:
        state, added = settings_service.apply_agy_allowlist(session, forced=True)

    assert added == list(agy_permissions.RECOMMENDED_HOSTS)
    assert state.missing == ()
    # 사용자가 직접 넣은 규칙은 그대로다.
    assert "patents.google.com" in state.allowed_hosts


def test_a_missing_file_leaves_the_migration_pending(settings_file, fresh_marker):
    """agy 설정 파일이 없으면 만들지 않고, 끝났다고 적지도 않는다.

    agy 를 쓰지도 않는 기계에 ~/.gemini 를 만들지 않기 위해서다. 대신 표시를
    남기지 않으므로, 사용자가 agy 를 처음 실행해 파일이 생긴 뒤에 적용된다.
    """
    from app.db import session_scope
    from app import settings_service

    with session_scope() as session:
        _, added = settings_service.apply_agy_allowlist(session, forced=False)

    assert added == []
    assert not settings_file.exists()
    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is False


def test_a_broken_file_leaves_the_migration_pending(settings_file, fresh_marker):
    """손상된 파일을 만난 자동 실행은 끝났다고 적지 않는다. 다음에 다시 시도한다."""
    from app.db import session_scope
    from app import settings_service

    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{", encoding="utf-8")

    with session_scope() as session:
        with pytest.raises(agy_permissions.AgyPermissionsError):
            settings_service.apply_agy_allowlist(session, forced=False)

    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is False
    # 시작 경로는 이 예외로 앱을 멈추지 않는다.
    settings_service.run_agy_allowlist_migration()
    assert settings_file.read_text(encoding="utf-8") == "{"


async def test_the_provider_probe_never_writes_the_file(settings_file):
    """Provider 검사는 읽기 전용이다.

    설정 화면을 여는 것만으로 검사가 돈다. 여기서 병합하면 사용자가 지운
    호스트가 되살아나고, 지운 사람은 그 사실조차 확인할 수 없다.
    """
    from app.providers.agy_cli import AgyCliProvider

    _write(settings_file, {"permissions": {"allow": []}})
    before = settings_file.read_bytes()

    result = await AgyCliProvider().probe()
    if not result.executable_ok:
        pytest.skip("이 환경에는 agy CLI 가 없어 허용 목록 경로까지 가지 않습니다.")

    assert settings_file.read_bytes() == before
    assert _backups(settings_file) == []
    # 읽기는 한다. 빠진 권장 출처를 알려 주는 것까지가 검사의 몫이다.
    assert any("권장 출처" in note for note in result.notes)


def test_the_apply_endpoint_merges_and_reports_the_new_state(
    client, settings_file, fresh_marker
):
    """설정 화면 버튼의 실제 경로."""
    _write(settings_file, {"permissions": {"allow": ["read_url(patents.google.com)"]}})

    response = client.post("/api/settings/agy-permissions/apply")

    assert response.status_code == 200, response.text
    payload = response.json()["agy_permissions"]
    assert payload["missing"] == []
    assert payload["applied"] == list(agy_permissions.RECOMMENDED_HOSTS)
    assert "patents.google.com" in payload["allowed_hosts"]


def test_the_apply_endpoint_refuses_to_overwrite_a_broken_file(
    client, settings_file, fresh_marker
):
    """손상된 파일은 버튼을 눌러도 덮어쓰지 않는다."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text('{"permissions": {', encoding="utf-8")

    response = client.post("/api/settings/agy-permissions/apply")

    assert response.status_code == 400
    assert "JSON" in response.json()["detail"]
    assert settings_file.read_text(encoding="utf-8") == '{"permissions": {'


# ------------------------------------------- 버전별 delta 자동 적용
#
# 버전을 올릴 때 **전체 목록을 다시 병합하면 안 된다.** 사용자가 v1 에서 지운
# 호스트가 v2 를 올리는 순간 되살아나기 때문이다. 지운 것은 그러기로 한 선택이고,
# 권장 목록에 새 줄이 생겼다는 것이 그 선택을 뒤집을 이유가 되지 않는다.

V1_HOSTS = ("arxiv.org", "dl.acm.org")
V2_HOSTS = ("www.biorxiv.org", "openreview.net")


@pytest.fixture()
def two_versions(monkeypatch):
    """v1 두 곳 + v2 두 곳으로 이뤄진 가상의 권장 목록."""
    versions = (("1", V1_HOSTS), ("2", V2_HOSTS))
    monkeypatch.setattr(agy_permissions, "RECOMMENDED_HOST_VERSIONS", versions)
    monkeypatch.setattr(
        agy_permissions, "RECOMMENDED_HOSTS", V1_HOSTS + V2_HOSTS
    )
    monkeypatch.setattr(agy_permissions, "MIGRATION_VERSION", "2")
    return versions


def test_hosts_since_returns_only_what_that_version_introduced(two_versions):
    # 아직 한 번도 적용하지 않은 설치.
    assert agy_permissions.hosts_since("") == V1_HOSTS + V2_HOSTS
    # v1 까지 끝난 설치에는 v2 가 새로 도입한 것만.
    assert agy_permissions.hosts_since("1") == V2_HOSTS
    # 최신까지 끝났으면 넣을 것이 없다.
    assert agy_permissions.hosts_since("2") == ()


def test_an_unknown_stored_version_yields_nothing(two_versions):
    """다운그레이드나 손으로 고친 값. 전부 넣는 쪽으로 기울지 않는다."""
    assert agy_permissions.hosts_since("99") is None
    assert agy_permissions.hosts_since("v1") is None


def test_apply_recommended_merges_only_the_hosts_it_was_given(
    settings_file, two_versions
):
    _write(settings_file, {"permissions": {"allow": []}})

    _, added = agy_permissions.apply_recommended(hosts=V2_HOSTS)

    assert added == list(V2_HOSTS)
    rules = _read(settings_file)["permissions"]["allow"]
    assert rules == [f"read_url({host})" for host in V2_HOSTS]


def test_a_version_bump_does_not_resurrect_a_host_deleted_in_the_old_version(
    settings_file, two_versions, fresh_marker
):
    """이 파일에서 가장 중요한 계약.

    v1 을 적용받은 사용자가 그중 하나를 지웠다. 그 뒤 ARIA 가 v2 로 올라간다.
    v2 가 새로 도입한 호스트는 들어와야 하고, 사용자가 지운 v1 호스트는 지워진
    채로 남아야 한다.
    """
    from app.db import session_scope
    from app import settings_service

    # --- v1 시점: 두 호스트가 들어간다 --------------------------------------
    _write(settings_file, {"permissions": {"allow": []}})
    monkey = pytest.MonkeyPatch()
    monkey.setattr(agy_permissions, "RECOMMENDED_HOST_VERSIONS", (("1", V1_HOSTS),))
    monkey.setattr(agy_permissions, "RECOMMENDED_HOSTS", V1_HOSTS)
    monkey.setattr(agy_permissions, "MIGRATION_VERSION", "1")
    try:
        with session_scope() as session:
            _, added = settings_service.apply_agy_allowlist(session, forced=False)
        assert added == list(V1_HOSTS)
    finally:
        monkey.undo()

    # --- 사용자가 dl.acm.org 를 직접 지운다 ---------------------------------
    document = _read(settings_file)
    document["permissions"]["allow"] = [
        rule for rule in document["permissions"]["allow"] if "dl.acm.org" not in rule
    ]
    _write(settings_file, document)

    # --- v2 로 올라간다(two_versions fixture 가 그 상태다) ------------------
    with session_scope() as session:
        state, added = settings_service.apply_agy_allowlist(session, forced=False)

    # v2 가 새로 도입한 것만 들어왔다.
    assert added == list(V2_HOSTS)
    hosts = set(state.allowed_hosts)
    assert V2_HOSTS[0] in hosts and V2_HOSTS[1] in hosts
    # 사용자가 지운 v1 호스트는 그대로 없다.
    assert "dl.acm.org" not in hosts
    assert "arxiv.org" in hosts
    # 화면에는 "빠져 있다"는 사실만 남는다. 되돌리지는 않는다.
    assert "dl.acm.org" in state.missing

    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is True


def test_the_button_still_merges_the_whole_recommended_list(
    settings_file, two_versions, fresh_marker
):
    """전체를 다시 넣는 유일한 경로. 지운 호스트도 이때는 돌아온다."""
    from app.db import session_scope
    from app import settings_service

    _write(settings_file, {"permissions": {"allow": [f"read_url({V1_HOSTS[0]})"]}})
    with session_scope() as session:
        state, added = settings_service.apply_agy_allowlist(session, forced=True)

    assert added == list(V1_HOSTS[1:] + V2_HOSTS)
    assert state.missing == ()


def test_an_unknown_stored_version_changes_nothing_automatically(
    settings_file, two_versions, fresh_marker
):
    """알 수 없는 표시에서는 파일도 표시도 건드리지 않는다."""
    from app.db import session_scope
    from app.models import AppSetting
    from app import settings_service

    _write(settings_file, {"permissions": {"allow": []}})
    before = settings_file.read_bytes()
    with session_scope() as session:
        session.add(
            AppSetting(key=settings_service.AGY_MIGRATION_KEY, value="99")
        )
        session.flush()

    with session_scope() as session:
        _, added = settings_service.apply_agy_allowlist(session, forced=False)

    assert added == []
    assert settings_file.read_bytes() == before
    with session_scope() as session:
        assert str(settings_service.get(session, settings_service.AGY_MIGRATION_KEY)) == "99"


def test_a_version_that_adds_no_host_only_advances_the_marker(
    settings_file, monkeypatch, fresh_marker
):
    """넣을 것이 없는 버전은 파일을 열지도 않는다."""
    from app.db import session_scope
    from app import settings_service

    monkeypatch.setattr(
        agy_permissions,
        "RECOMMENDED_HOST_VERSIONS",
        (("1", V1_HOSTS), ("2", ())),
    )
    monkeypatch.setattr(agy_permissions, "RECOMMENDED_HOSTS", V1_HOSTS)
    monkeypatch.setattr(agy_permissions, "MIGRATION_VERSION", "2")

    from app.models import AppSetting

    _write(settings_file, {"permissions": {"allow": []}})
    before = settings_file.read_bytes()
    # v1 까지 끝난 설치로 만든다.
    with session_scope() as session:
        session.add(AppSetting(key=settings_service.AGY_MIGRATION_KEY, value="1"))
        session.flush()

    with session_scope() as session:
        _, added = settings_service.apply_agy_allowlist(session, forced=False)

    assert added == []
    assert settings_file.read_bytes() == before
    assert _backups(settings_file) == []
    with session_scope() as session:
        assert settings_service.agy_allowlist_migration_done(session) is True
