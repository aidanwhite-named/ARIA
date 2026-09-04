"""허용 목록이 실제로 프롬프트에 들어가는가, 그리고 논문 채널 기본값.

두 가지를 함께 지킨다.

  1. 비특허문헌(Crossref·Europe PMC) 채널은 기본으로 켜져 있다. 사용자가
     명시적으로 끈 선택은 그대로 보존된다.
  2. agy 검색 실행의 프롬프트는 **그 순간 파일에 있는** 허용 목록을 말한다.
     코드에 박힌 목록이 아니다 — 사용자가 파일을 고치면 다음 실행이 곧바로
     그것을 말해야 하고, 그러지 않으면 모델은 열 수 없는 주소를 열려다
     실행 전체를 날린다.
"""

from __future__ import annotations

import json

import pytest

from app import config, job_assembly, settings_service
from app.db import session_scope
from app.enums import JobKind
from app.models import AppSetting
from app.providers import agy_permissions


# ------------------------------------------------------- 논문 채널 기본값


def test_literature_channel_is_on_by_default() -> None:
    """새 설치와 이 키가 없는 기존 설치에서 기본 ON.

    유사문헌 검색 작업 자체가 외부 검색이다. 그 안에서 논문 채널만 따로 꺼
    두는 것은 보호가 아니라 결함이었다 — 웹 검색은 논문을 익명 링크와 요약문
    으로만 돌려주므로, 제목과 DOI 가 붙은 후보를 만드는 유일한 경로가 이쪽이다.
    """
    assert config.DEFAULTS["literature_integration_enabled"] is True


def test_installs_without_the_key_read_as_on(client) -> None:
    """DB 에 행이 없으면 기본값이 그대로 답이다."""
    with session_scope() as session:
        row = session.get(AppSetting, "literature_integration_enabled")
        if row is not None:
            session.delete(row)
        session.flush()
        assert (
            settings_service.get_all(session)["literature_integration_enabled"]
            is True
        )


def test_an_explicit_off_survives_the_new_default(client) -> None:
    """사용자가 화면에서 끄면 그 선택이 기본값을 덮는다.

    기본값을 바꾸는 변경에서 가장 위험한 것은 "껐는데 다시 켜지는" 경우다.
    저장은 사용자가 바꾼 키만 하므로 그 행이 남아 있는 한 기본값은 닿지 않는다.
    """
    with session_scope() as session:
        settings_service.update(session, {"literature_integration_enabled": False})
    try:
        with session_scope() as session:
            values = settings_service.get_all(session)
        assert values["literature_integration_enabled"] is False
    finally:
        with session_scope() as session:
            settings_service.update(
                session, {"literature_integration_enabled": True}
            )


# ------------------------------------------------- 허용 목록 → 프롬프트


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "antigravity-cli" / "settings.json"
    monkeypatch.setenv("ARIA_AGY_SETTINGS_PATH", str(path))
    return path


def _assemble(policy: str, hosts):
    return job_assembly.assemble_job(
        job_kind=JobKind.SIMILARITY_SEARCH,
        master_prompt=(
            "# 검색\n\n<CLAIM_TEXT>\n{{CLAIM_TEXT}}\n</CLAIM_TEXT>\n"
        ),
        attachments=[],
        runtime_context="",
        runtime_context_enabled=True,
        max_chars=None,
        claim_text="청구항 1. 압력센서를 포함하는 장치.",
        tool_policy_name=policy,
        agy_allowed_hosts=hosts,
    )


def _lane_text(assembly) -> str:
    lane = assembly.lanes["single"]
    return lane.system_prompt + lane.user_message


def test_the_agy_prompt_names_the_hosts_that_are_actually_open() -> None:
    """지금 열 수 있는 주소를 그대로 알려준다."""
    text = _lane_text(_assemble("agy_web_search", ["arxiv.org", "dl.acm.org"]))

    assert "arxiv.org" in text
    assert "dl.acm.org" in text
    # 목록에 없는 호스트를 프롬프트가 먼저 제안하지 않는다.
    assert "ieeexplore.ieee.org" not in text


def test_the_agy_prompt_carries_the_failure_rules() -> None:
    """열지 못한 문헌을 버리지도, 실행을 멈추지도 않게 하는 규칙."""
    text = _lane_text(_assemble("agy_web_search", ["arxiv.org"]))

    assert "미열람을 원문 확인으로 표현하지" in text
    assert "reported_title" in text
    assert "access_failures" in text
    # 접근 실패가 실행 중단 사유가 아니라는 것과, 블록은 반드시 나가야 한다는 것.
    assert "실행을 중단할 이유가 아닙니다" in text
    assert "[ARIA_SEARCH_LOG_V1]" in text
    # 허용이 열람 성공 보장이 아니라는 것.
    assert "유료벽" in text and "봇 차단" in text


def test_an_empty_allowlist_says_nothing_can_be_opened() -> None:
    """목록이 비면 절을 빼지 않는다. 빼면 모델이 '제한 없음'으로 읽는다."""
    text = _lane_text(_assemble("agy_web_search", []))

    assert "하나도 없습니다" in text
    assert "read_url_content 를 한 번도 호출하지 마십시오" in text


def test_other_policies_get_no_allowlist_section() -> None:
    """호스트 단위 승인 파일을 가진 Provider 는 agy 뿐이다."""
    text = _lane_text(_assemble("web_search", ["arxiv.org"]))

    assert "페이지 열람 허용 목록" not in text


def test_the_prompt_reads_the_file_not_a_hardcoded_list(settings_file) -> None:
    """사용자가 파일을 고치면 다음 실행의 프롬프트가 곧바로 그것을 말한다."""
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(
        json.dumps({"permissions": {"allow": ["read_url(직접넣은.example)"]}}),
        encoding="utf-8",
    )

    hosts = job_assembly.allowed_hosts_for("agy_web_search")
    assert hosts == ("직접넣은.example",)

    text = _lane_text(_assemble("agy_web_search", hosts))
    assert "직접넣은.example" in text


def test_a_policy_without_an_allowlist_file_reads_as_nothing_open(
    settings_file,
) -> None:
    """파일이 없으면 빈 목록이다. '제한 없음'이 아니다."""
    assert job_assembly.allowed_hosts_for("agy_web_search") == ()
    assert job_assembly.allowed_hosts_for("web_search") == ()


def test_recommended_hosts_are_the_ones_the_prompt_can_name(settings_file) -> None:
    """권장 목록을 적용하면 그 호스트들이 그대로 프롬프트로 간다."""
    agy_permissions.apply_recommended(create=True)

    text = _lane_text(
        _assemble("agy_web_search", job_assembly.allowed_hosts_for("agy_web_search"))
    )

    for host in agy_permissions.RECOMMENDED_HOSTS:
        assert host in text
