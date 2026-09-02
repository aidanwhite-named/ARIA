"""EPO 계획 턴의 도구 금지 — 세 Provider 에서 같은 결과가 나와야 한다.

계획 턴은 NO_TOOLS 다. 그 턴에서 모델이 외부 도구를 부르면 그 출력은 계획이
아니라 **이미 실행된 무언가**이고, 우리가 보지 못한 자료를 읽고 만들어진
지시다. 그래서 그 응답의 epo_search / epo_fetch_document 를 ARIA 가 대신
실행하지 않는다.

Provider 이름으로 갈라지지 않는다. Claude 는 --tools 로 도구를 실제로 끌 수
있고 agy·Codex 는 끌 수단이 없지만, **판정과 조치는 같다.** 다르게 남는 것은
격리 수준 기록뿐이다 — "막았다"와 "막을 수 없어 사후에 봤다"를 같은 값으로
적으면 감사 기록이 실제로 아는 것보다 강해진다.

네트워크를 열지 않는다. 도구 위반이 감지되면 OPS 전송 계층에 요청이 한 건도
도착하지 않아야 하고, 이 파일은 그것을 직접 센다.
"""

from __future__ import annotations

import pytest

from app.patent_search import epo_agent
from app.providers.agy_cli import AgyCliProvider
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider

from . import epo_fixtures as fx
from .test_epo_agent import FINISH, make_agent, run, say, search_action
from .test_epo_search import FakeTransport, ok, token_response


class _Outcome:
    """Provider 가 돌려주는 실행 결과의 도구 관련 부분만 흉내 낸다."""

    def __init__(self, text: str, *, tool_uses=(), tool_calls=(), flags=None) -> None:
        self.result_text = text
        self.cancelled = False
        self.timed_out = False
        self.usage = {}
        self.tool_uses = list(tool_uses)
        self.tool_calls = list(tool_calls)
        for name, value in (flags or {}).items():
            setattr(self, name, value)


class _Provider:
    """세 Provider 의 **선언된 성질**만 그대로 베낀 대역.

    id 와 supported_tool_policies 를 실제 클래스에서 읽는다. 값을 손으로 적어
    두면 제품 쪽이 바뀌어도 이 테스트는 옛 값으로 계속 통과한다.
    """

    def __init__(self, source, *, uncontrollable: bool, responses) -> None:
        self.id = source.id
        self.supported_tool_policies = source.supported_tool_policies
        self.uncontrollable = uncontrollable
        self.queue = list(responses)
        self.calls = 0

    async def execute(self, request, emit):
        self.calls += 1
        if not self.queue:
            raise AssertionError("준비된 응답보다 라운드가 많습니다.")
        text, tools = self.queue.pop(0)
        flags = (
            {"tools_uncontrollable": True, "tools_must_be_disabled": False}
            if self.uncontrollable
            else {
                "tools_uncontrollable": False,
                # Claude 는 이 실행의 정책을 그대로 돌려준다. NO_TOOLS 이므로 참.
                "tools_must_be_disabled": request.tool_policy.tools_disabled,
            }
        )
        return _Outcome(
            text,
            tool_uses=tools,
            tool_calls=[{"name": name, "ok": True} for name in tools],
            flags=flags,
        )


#: (이름, 실제 Provider 클래스, 도구를 끌 수 없는가, 기대하는 격리 수준)
PROVIDERS = [
    ("claude", ClaudeCliProvider, False, epo_agent.ISOLATION_ENFORCED),
    ("agy", AgyCliProvider, True, epo_agent.ISOLATION_POST_HOC),
    ("codex", CodexCliProvider, True, epo_agent.ISOLATION_POST_HOC),
]


@pytest.fixture()
def store(tmp_path):
    from app.patent_search import artifacts

    return artifacts.ArtifactStore(tmp_path / "evidence")


@pytest.mark.parametrize(
    "label,source,uncontrollable,expected", PROVIDERS, ids=[p[0] for p in PROVIDERS]
)
def test_tool_use_in_the_planning_turn_discards_the_output(
    label, source, uncontrollable, expected, store, tmp_path
) -> None:
    """도구를 부른 응답의 action 은 **하나도** 실행되지 않는다."""
    # 응답에는 검색과 상세 조회가 둘 다 들어 있다. 도구 검사가 없다면 이
    # 라운드에서 OPS 호출이 나갔을 것이다.
    reply = say(
        search_action(),
        {
            "action": "epo_fetch_document",
            "doc_number": "EP1000000A1",
            "constituent": "claims",
        },
        FINISH,
    )
    provider = _Provider(
        source, uncontrollable=uncontrollable, responses=[(reply, ["WebSearch"])]
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_UNAUTHORIZED_TOOL_USE
    # OPS 로 아무것도 나가지 않았다. 토큰 발급도 필요 없다.
    assert transport.requests == [], "도구 위반 응답의 action 이 실행됐습니다."
    assert result.search_calls == 0
    assert result.detail_fetches == 0
    assert result.candidates == {}


@pytest.mark.parametrize(
    "label,source,uncontrollable,expected", PROVIDERS, ids=[p[0] for p in PROVIDERS]
)
def test_violation_records_provider_tools_and_isolation_level(
    label, source, uncontrollable, expected, store, tmp_path
) -> None:
    """감사 기록에 사유·Provider·도구명·격리 수준이 모두 남는다."""
    provider = _Provider(
        source,
        uncontrollable=uncontrollable,
        responses=[(say(search_action(), FINISH), ["WebSearch", "WebFetch"])],
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert len(result.tool_violations) == 1
    violation = result.tool_violations[0]
    assert violation["provider"] == source.id
    assert violation["policy"] == "no_tools"
    assert violation["tools"] == ["WebSearch", "WebFetch"]
    assert violation["isolation"] == expected
    assert violation["detail"]

    # 라운드 기록에도 남아야 부분 실패를 추적할 수 있다.
    assert result.rounds[-1].status == epo_agent.TERM_UNAUTHORIZED_TOOL_USE
    assert result.rounds[-1].tool_uses == ["WebSearch", "WebFetch"]
    assert result.usage["tool_isolation"] == expected
    assert result.usage["unauthorized_tool_uses"] == 2
    assert result.usage["provider"] == source.id


@pytest.mark.parametrize(
    "label,source,uncontrollable,expected", PROVIDERS, ids=[p[0] for p in PROVIDERS]
)
def test_clean_turns_run_the_same_pipeline_on_every_provider(
    label, source, uncontrollable, expected, store, tmp_path
) -> None:
    """도구를 부르지 않으면 세 Provider 모두 같은 경로로 검색을 수행한다."""
    provider = _Provider(
        source, uncontrollable=uncontrollable, responses=[(say(search_action(), FINISH), [])]
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    assert result.tool_violations == []
    assert result.search_calls == 1
    assert set(result.candidates) == {"EP1000000A1", "US9876543B2"}
    # 격리 수준은 위반이 없어도 기록한다. "이 실행에서 도구를 무엇으로
    # 막았는가"는 위반 여부와 별개의 사실이다.
    assert result.usage["tool_isolation"] == expected


def test_tool_calls_without_tool_uses_are_still_caught(store, tmp_path) -> None:
    """이름 목록이 비어 있어도 호출 기록이 있으면 위반이다.

    Provider 마다 어느 칸을 채우는지가 다르다. 한쪽만 보면 다른 쪽만 채우는
    Provider 에서 검사가 통째로 조용해진다.
    """
    provider = _Provider(
        CodexCliProvider,
        uncontrollable=True,
        responses=[(say(search_action(), FINISH), [])],
    )

    original = provider.execute

    async def execute(request, emit):
        outcome = await original(request, emit)
        outcome.tool_calls = [{"name": "web_search", "ok": True}]
        return outcome

    provider.execute = execute
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_UNAUTHORIZED_TOOL_USE
    assert result.tool_violations[0]["tools"] == ["web_search"]
    assert transport.requests == []
