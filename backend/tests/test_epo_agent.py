"""EPO 검색 루프 — 라운드·호출 상한, 취소, 종료 사유.

**네트워크를 열지 않는다.** Provider 와 OPS 전송 계층을 모두 가짜로 바꾸므로
실수로 실제 호출이 나가면 conftest 의 차단이 걸려 실패한다.

모델도 가짜다. 미리 정해 둔 응답을 순서대로 돌려주므로 라운드마다 무슨 일이
일어나는지 결정적으로 재현된다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.patent_search import (
    artifacts,
    epo_actions,
    epo_agent,
    epo_backend,
    epo_client,
    epo_cql,
    epo_prompts,
    epo_quota,
)

from . import epo_fixtures as fx
from .test_epo_search import TEST_KEY, TEST_SECRET, FakeTransport, ok, token_response


# ------------------------------------------------------------- 가짜 Provider


class FakeOutcome:
    def __init__(
        self,
        text: str,
        *,
        cancelled=False,
        timed_out=False,
        tool_uses=(),
        tool_calls=(),
        usage=None,
    ) -> None:
        self.result_text = text
        self.cancelled = cancelled
        self.timed_out = timed_out
        # Provider 가 알려준 사용량. 알려주지 않는 Provider 도 있으므로 기본은
        # 빈 dict 다.
        self.usage = dict(usage or {})
        self.tool_calls = list(tool_calls)
        # NO_TOOLS 턴이므로 보통은 비어 있다. 위반 경로를 시험할 때만 채운다.
        self.tool_uses = list(tool_uses)


#: 최소한의 청구항 분석. 검색 action 을 실행하려면 **반드시** 있어야 한다.
#: 계약이 여기 한 곳에만 적혀 있어야, 계약이 바뀌면 테스트가 같이 움직인다.
MINIMAL_ANALYSIS = {
    "elements": [{"id": "E1", "text": "로봇 팔", "essential": True}],
    "concept_combinations": [{"elements": ["E1"], "terms": ["robot arm"]}],
}

#: 최종 선택 턴의 기본 응답. 이 턴은 검색 뒤에 항상 한 번 오므로, 각 테스트가
#: 자기 관심사와 무관한 응답을 매번 큐에 넣지 않아도 되게 기본값을 둔다.
DEFAULT_SELECTION = json.dumps({"actions": [{"action": "finish", "notes": "선택 완료"}]})


class FakeProvider:
    """미리 정해 둔 응답을 라운드 순서대로 돌려준다.

    검색 라운드와 최종 선택 턴을 **시스템 프롬프트로** 구분한다. 두 턴은 계약이
    다르므로(선택 턴은 검색 금지) 같은 큐에서 꺼내면 테스트가 어느 턴을 보고
    있는지 알 수 없게 된다.
    """

    def __init__(self, *responses, selection: str | None = None) -> None:
        self.queue = list(responses)
        self.selection = selection
        #: 검색 라운드 요청만. 최종 선택 턴은 아래 목록에 따로 쌓인다.
        self.requests = []
        self.selection_requests = []

    async def execute(self, request, emit):
        if epo_prompts.SELECTION_MARKER in (request.system_prompt or ""):
            self.selection_requests.append(request)
            reply = self.selection if self.selection is not None else DEFAULT_SELECTION
            return reply if isinstance(reply, FakeOutcome) else FakeOutcome(reply)
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("준비된 모델 응답보다 라운드가 많습니다.")
        item = self.queue.pop(0)
        return item if isinstance(item, FakeOutcome) else FakeOutcome(item)


def say(*actions, strategy: str = "", analysis: dict | None = MINIMAL_ANALYSIS) -> str:
    """검색 라운드 응답 하나.

    claim_analysis 를 기본으로 넣는다. 그것이 계약이고, 넣지 않은 응답의 검색
    action 은 실행되지 않는다. 그 경로를 시험하는 테스트만 analysis=None 을
    넘긴다.
    """
    payload = {"strategy": strategy, "actions": list(actions)}
    if analysis is not None:
        payload["claim_analysis"] = analysis
    return json.dumps(payload, ensure_ascii=False)


def search_action(value: str = "robot arm", field: str = "ta", **extra) -> dict:
    return {
        "action": epo_actions.ACTION_SEARCH,
        "query": {"kind": "term", "field": field, "value": value},
        **extra,
    }


FINISH = {"action": epo_actions.ACTION_FINISH, "notes": "끝"}


@pytest.fixture()
def store(tmp_path) -> artifacts.ArtifactStore:
    return artifacts.ArtifactStore(tmp_path / "evidence")


def make_agent(provider, transport, store, tmp_path, **kwargs):
    backend = epo_backend.EpoOpsBackend(store=store)
    backend.configure(
        {
            epo_backend.SETTING_CONSUMER_KEY: TEST_KEY,
            epo_backend.SETTING_CONSUMER_SECRET: TEST_SECRET,
            **kwargs.pop("settings", {}),
        }
    )
    backend._client = epo_client.OpsClient(
        key=TEST_KEY,
        secret=TEST_SECRET,
        ledger=backend.ledger,
        transport=transport,
        sleep=lambda _s: None,
    )
    return epo_agent.EpoSearchAgent(
        job_id="job-1",
        provider=provider,
        model=None,
        timeout_seconds=60,
        work_dir=tmp_path / "work",
        claim_text="청구항 1. 로봇 팔과 힘 센서를 포함하는 장치.",
        backend=backend,
        **kwargs,
    )


def run(agent):
    return asyncio.run(agent.run())


# ------------------------------------------------------------------ 기본 흐름


def test_single_round_search_then_finish(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    assert result.search_calls == 1
    assert len(result.candidates) == 2
    assert "EP1000000A1" in result.candidates
    assert result.rounds[0].status == "ok"
    assert result.rounds[0].new_candidates == 2


def test_candidates_carry_evidence_references(store, tmp_path) -> None:
    """후보의 모든 필드가 보존된 아티팩트를 가리켜야 3단계가 대조할 수 있다."""
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    candidate = result.candidates["EP1000000A1"]
    assert candidate.artifact_ids
    for name, ref in candidate.evidence.items():
        assert ref["artifact_id"] in candidate.artifact_ids, name
        assert ref["field_path"].startswith("documents/EP.1000000.A1/")
        assert ref["profile_id"] == "epo_ops_exchange_xml_v1"


# ------------------------------------------------------------------ 상한


def test_the_planning_turn_runs_once(store, tmp_path) -> None:
    """검색계획 턴은 한 번뿐이다. 적응형 2라운드는 없다.

    모델이 계속 검색하려 해도 두 번째 계획 턴은 열리지 않는다. 검색 결과를
    읽는 판단은 검색하지 않는 최종 선택 턴 하나로 모았다.
    """
    provider = FakeProvider(
        say(search_action("a")),
        say(search_action("b")),   # 계획 턴으로는 불려서는 안 된다
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    planning = [
        request
        for request in provider.requests
        if epo_prompts.SELECTION_MARKER not in (request.system_prompt or "")
    ]
    assert len(planning) == 1
    assert len(result.rounds) == 1
    assert result.termination_reason == epo_agent.TERM_ROUND_LIMIT


def test_channel_search_call_limit_stops_the_turn(store, tmp_path) -> None:
    """채널 전체 상한에 닿으면 그 지점에서 멈춘다.

    채널 예산은 **작업당**이라 레인이 나눠 쓴다. 레인 예산만 보고 검색을
    내보내면 두 번째 레인이 쓸 몫까지 첫 레인이 먹는다.
    """
    channel = epo_agent.ChannelBudget(max_search_calls=2)
    channel.start()
    provider = FakeProvider(
        say(search_action("a"), search_action("b"), search_action("c")),
    )
    transport = FakeTransport(
        token_response(), *[ok(fx.SEARCH_BIBLIO) for _ in range(3)]
    )
    result = run(
        make_agent(provider, transport, store, tmp_path, channel=channel)
    )

    assert result.search_calls == 2
    assert result.termination_reason == epo_agent.TERM_SEARCH_CALL_LIMIT


def test_per_round_search_limit_rejects_extras_without_ending_the_loop(
    store, tmp_path
) -> None:
    """턴 상한은 그 턴의 나머지 검색만 거절한다. 루프를 끝내지 않는다."""
    provider = FakeProvider(
        say(
            search_action("a"),
            search_action("b"),
            search_action("c"),
            search_action("d"),   # 4번째 — 거절되어야 한다
            FINISH,
        ),
    )
    transport = FakeTransport(
        token_response(), *[ok(fx.SEARCH_BIBLIO) for _ in range(3)]
    )
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.rounds[0].search_calls == 3
    assert any("검색 호출 상한" in e for e in result.rounds[0].errors)
    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED


def test_the_search_stage_never_fetches_a_document(store, tmp_path) -> None:
    """검색 단계는 OPS 상세조회를 하지 않는다. 요청해도 실행되지 않는다.

    상세조회는 후보를 합친 뒤 공식 검증 단계에서만 한다. 검색 에이전트가 먼저
    써 버리면 같은 예산(epo_max_detail_fetches)이 검증에 남지 않는다.
    """
    fetch = {
        "action": "epo_fetch_document",
        "doc_number": "EP1000000A1",
        "constituent": "claims",
    }
    provider = FakeProvider(say(search_action("robot arm"), fetch, FINISH))
    # 검색 응답 하나만 준비한다. 상세조회가 나가면 FakeTransport 가 실패한다.
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.detail_fetches == 0
    assert result.search_calls == 1
    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    # 조용히 버리지 않는다. 요청했다는 사실이 기록에 남는다.
    retired = [
        item
        for item in result.excluded
        if item["reason_code"] == epo_agent.EXCLUDED_RETIRED_ACTION
    ]
    assert [item["value"] for item in retired] == ["epo_fetch_document"]


def test_a_retired_action_does_not_reject_the_whole_response(store, tmp_path) -> None:
    """옛 습관으로 상세조회를 보내도 그 응답의 검색은 실행된다.

    계획 턴이 한 번뿐이라, 그 한 번을 형식 오류로 날리면 레인이 빈손이 된다.
    """
    fetch = {"action": "epo_fetch_document", "doc_number": "EP1000000A1"}
    provider = FakeProvider(say(fetch, search_action("robot arm"), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.invalid_responses == 0
    assert result.search_calls == 1
    assert result.candidates


# --------------------------------------------------- 모델은 CQL 을 쓸 수 없다


def test_raw_cql_string_is_rejected(store, tmp_path) -> None:
    """모델이 CQL 문자열을 보내면 질의로 읽히지 않는다."""
    provider = FakeProvider(
        json.dumps(
            {
                "actions": [
                    {
                        "action": epo_actions.ACTION_SEARCH,
                        "query": 'ti all "robot" or pn any "EP1"',
                    }
                ]
            }
        ),
        say(search_action(), FINISH),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.rounds[0].status == "parse_error"
    assert result.search_calls == 1   # 첫 라운드는 호출을 만들지 못했다


@pytest.mark.parametrize(
    "value,field",
    [
        ('robot" or ti="x', "ta"),      # 인용부호로 구문 깨기
        ("wild*card", "ta"),            # 와일드카드
        ("자유 문장", "ipc"),            # 분류 필드에 자유 텍스트
    ],
)
def test_invalid_query_never_reaches_ops(store, tmp_path, value, field) -> None:
    provider = FakeProvider(say(search_action(value, field=field)), say(FINISH))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.search_calls == 0, "잘못된 질의가 OPS 로 나갔습니다."
    assert result.invalid_responses == 1
    assert any("검색식을 만들 수 없습니다" in e for e in result.rounds[0].errors)


def test_unknown_field_is_rejected(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action("x", field="evil")), say(FINISH))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))
    assert result.search_calls == 0


def test_prompt_field_list_matches_the_allowlist() -> None:
    """프롬프트가 안내하는 필드와 실제 허용 목록이 어긋나면 안 된다."""
    from app.patent_search import epo_prompts

    text = epo_prompts.system_prompt(epo_agent.EpoAgentBudget())
    for name in epo_cql.ALLOWED_FIELDS:
        assert name in text, name


# ------------------------------------------------- 무한 재생성 루프 방지


def test_repeated_parse_errors_stop_the_loop(store, tmp_path) -> None:
    """형식 오류가 반복되면 라운드를 태우지 않고 끝낸다."""
    provider = FakeProvider("설명만 하고 JSON 을 안 씁니다", "또 안 씁니다", "세 번째")
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_INVALID_RESPONSE_LIMIT
    assert result.invalid_responses == 3
    assert result.search_calls == 0
    # 형식 오류는 계획 턴을 소모하지 않으므로 세 번 물어봤다.
    assert len(provider.requests) == 3


def test_parse_error_is_reported_back_to_the_model(store, tmp_path) -> None:
    provider = FakeProvider("not json", say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    second = provider.requests[1].user_message
    assert "previous_error" in second
    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED


def test_repeated_invalid_queries_stop_the_loop(store, tmp_path) -> None:
    """잘못된 질의만 반복하면 호출 없이 끝난다."""
    budget = epo_agent.EpoAgentBudget(max_invalid_responses=2)
    bad = search_action('a" or b')
    provider = FakeProvider(say(bad), say(bad), say(bad))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path, budget=budget))

    assert result.termination_reason == epo_agent.TERM_INVALID_RESPONSE_LIMIT
    assert result.search_calls == 0


def test_empty_actions_do_not_loop_forever(store, tmp_path) -> None:
    budget = epo_agent.EpoAgentBudget(max_invalid_responses=2)
    provider = FakeProvider(say(), say(), say())
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path, budget=budget))
    assert result.termination_reason == epo_agent.TERM_INVALID_RESPONSE_LIMIT


# ------------------------------------------------------------------ 취소


def test_cancel_before_the_model_call(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response())
    agent = make_agent(
        provider, transport, store, tmp_path, is_cancelled=lambda: True
    )
    result = run(agent)

    assert result.cancelled is True
    assert result.termination_reason == epo_agent.TERM_CANCELLED
    assert provider.requests == [], "취소 뒤에도 모델을 불렀습니다."


def test_cancel_after_the_model_call_stops_before_ops(store, tmp_path) -> None:
    """모델 응답을 받은 뒤 취소되면 그 응답을 실행 계획으로 삼지 않는다.

    "OPS 호출이 없었다"만 보면 이 검사가 사라져도 통과한다 — action 루프의
    취소 확인이 대신 잡기 때문이다. 그래서 **actions 를 세지도 않았다**는 것을
    함께 본다. 그게 이 지점에서 멈췄다는 유일한 관측 가능한 증거다.
    """
    flag = {"cancelled": False}
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))

    original_execute = provider.execute

    async def execute_then_cancel(request, emit):
        outcome = await original_execute(request, emit)
        flag["cancelled"] = True
        return outcome

    provider.execute = execute_then_cancel
    agent = make_agent(
        provider, transport, store, tmp_path,
        is_cancelled=lambda: flag["cancelled"],
    )
    result = run(agent)

    assert result.termination_reason == epo_agent.TERM_CANCELLED
    assert transport.requests == [], "취소 뒤에도 OPS 를 불렀습니다."
    assert result.rounds[0].actions == 0, "취소 뒤에 응답을 실행 계획으로 읽었습니다."


def test_cancel_between_actions(store, tmp_path) -> None:
    """action 사이에서도 취소를 본다.

    앞 action 이 **호출을 만들지 않은** 경우가 이 검사가 유일하게 지키는
    자리다. OPS 호출 뒤의 확인은 호출이 있었을 때만 돌기 때문이다. 그래서 첫
    action 을 잘못된 질의(호출 없음)로 두고, 그 뒤에 취소가 들어오게 한다.
    """
    seen = {"n": 0}

    def is_cancelled() -> bool:
        seen["n"] += 1
        # 1: 라운드 시작 · 2: 모델 호출 후 · 3: 첫 action(잘못된 질의) 앞
        # 4: 두 번째 action 앞 — 이 시점에 취소가 들어온 것으로 본다.
        return seen["n"] >= 4

    provider = FakeProvider(
        say(search_action('bad" query'), search_action("robot arm"))
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(
        make_agent(
            provider, transport, store, tmp_path, is_cancelled=is_cancelled
        )
    )

    assert result.termination_reason == epo_agent.TERM_CANCELLED
    searches = [row for row in transport.requests if "search" in row["url"]]
    assert searches == [], "취소 뒤에도 검색이 나갔습니다."
    assert result.search_calls == 0


def test_cancel_during_retry_wait(store, tmp_path) -> None:
    """Retry-After 대기 중에도 취소가 반영된다.

    통째로 자면 그 시간 동안 취소가 반영되지 않고, 사용자가 멈춘 실행이 계속
    할당량을 쓴다.
    """
    slept: list[float] = []
    cancelled = {"value": False}

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        cancelled["value"] = True   # 대기 도중에 취소가 들어왔다

    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", headers={"Retry-After": "20"}, status=429),
        ok(fx.SEARCH_BIBLIO),
    )
    # 에이전트가 자기 취소 확인 sleep 을 끼워 넣는다. 그 바닥 함수만 바꿔서
    # 실제로 20초를 자지 않게 한다.
    agent = make_agent(
        provider, transport, store, tmp_path,
        is_cancelled=lambda: cancelled["value"],
        sleep=fake_sleep,
    )
    result = run(agent)

    assert result.termination_reason == epo_agent.TERM_CANCELLED
    # 20초를 통째로 자지 않았다.
    assert slept and max(slept) <= 1.0


def test_cancellable_sleep_stops_immediately_when_already_cancelled() -> None:
    slept: list[float] = []
    sleeper = epo_client.cancellable_sleep(
        lambda: True, sleep=slept.append
    )
    with pytest.raises(epo_client.OpsCancelled):
        sleeper(30.0)
    assert slept == []


# ------------------------------------------------- OPS 실패의 종료 사유


def test_quota_exhaustion_ends_the_channel(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    agent = make_agent(
        provider, transport, store, tmp_path,
        settings={
            epo_backend.SETTING_QUOTA_STATE: {
                "week": epo_quota.week_key(),
                "local_bytes": epo_quota.WEEKLY_QUOTA_BYTES,
            }
        },
    )
    result = run(agent)
    assert result.termination_reason == epo_agent.TERM_QUOTA_EXCEEDED


def test_throttling_ends_the_channel(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action("a"), search_action("b")))
    transport = FakeTransport(
        token_response(),
        ok(fx.SEARCH_BIBLIO, headers=fx.HEADERS_BLACK),
        ok(fx.SEARCH_BIBLIO),
    )
    result = run(make_agent(provider, transport, store, tmp_path))
    assert result.termination_reason == epo_agent.TERM_THROTTLED


def test_auth_failure_ends_the_channel(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.ERROR_403, status=403))
    result = run(make_agent(provider, transport, store, tmp_path))
    assert result.termination_reason == epo_agent.TERM_AUTH_FAILED


def test_zero_results_is_not_a_failure(store, tmp_path) -> None:
    """0건은 실패가 아니다. 검색은 정상으로 끝난다."""
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_EMPTY))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    assert result.candidates == {}
    assert result.rounds[0].status == "ok"


def test_provider_timeout_is_recorded(store, tmp_path) -> None:
    provider = FakeProvider(FakeOutcome("", timed_out=True))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))
    assert result.termination_reason == epo_agent.TERM_TIMEOUT


def test_provider_cancel_flag_is_honoured(store, tmp_path) -> None:
    provider = FakeProvider(FakeOutcome("", cancelled=True))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))
    assert result.termination_reason == epo_agent.TERM_CANCELLED
    assert result.cancelled is True


# ------------------------------------------------------------------ 기록


def test_usage_is_recorded(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    usage = result.usage
    assert usage["rounds_used"] == 1
    assert "max_rounds" not in usage, "라운드 개념은 사라졌다."
    assert usage["search_calls"] == 1
    assert usage["max_search_calls"] == 6
    assert usage["termination_reason"] == epo_agent.TERM_LLM_FINISHED
    assert usage["calls_by_kind"]["search"]["count"] == 1


def test_queries_are_recorded_as_built_cql(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action("robot arm"), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.queries == [
        {
            "round": 1,
            "cql": 'ta all "robot arm"',
            "normalized_classifications": [],
        }
    ]


def test_a_classification_query_records_both_the_input_and_what_ops_got(
    store, tmp_path
) -> None:
    """모델이 적은 분류코드와 OPS 로 나간 표기가 **둘 다** 기록에 남는다.

    바꾼 사실을 남기지 않으면, 모델이 적은 검색어와 실제로 나간 검색어가 다른
    채로 실행되고 아무도 모른다.
    """
    action = {
        "action": epo_actions.ACTION_SEARCH,
        "query": {
            "kind": "term",
            "field": "ipc",
            "value": "G08B 13/196",
            "match": "exact",
        },
    }
    provider = FakeProvider(say(action, FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.queries == [
        {
            "round": 1,
            "cql": 'ipc = "G08B13/196"',
            "normalized_classifications": [
                {"field": "ipc", "original": "G08B 13/196", "sent": "G08B13/196"}
            ],
        }
    ]
    # OPS 로 실제로 나간 질의에도 공백이 없다.
    search_url = [
        request["url"] for request in transport.requests if "search" in request["url"]
    ][0]
    assert "G08B13%2F196" in search_url or "G08B13/196" in search_url
    assert "G08B+13" not in search_url and "G08B%2013" not in search_url


def test_run_serializes_without_secrets(store, tmp_path) -> None:
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    assert TEST_SECRET not in dumped
    assert TEST_KEY not in dumped
    assert "FAKE-TOKEN-VALUE" not in dumped


def test_default_budget_matches_the_agreed_numbers() -> None:
    budget = epo_agent.EpoAgentBudget()
    assert not hasattr(budget, "max_rounds"), "라운드 상한은 더 이상 없다."
    assert budget.max_search_calls == 6
    assert budget.max_search_calls_per_round == 3
    assert budget.max_detail_fetches == 12


# =====================================================================
# 5차 리뷰: 서로 모순된 상한과 재귀 탈출.
# =====================================================================


def test_action_cap_allows_every_search_plus_finish() -> None:
    """상한이 서로 모순되면 큰 쪽은 장식이 된다.

    한 턴에 검색 3회가 실행 상한인데 action 상한이 그보다 작으면 그 3회에
    도달할 수 없고, finish 가 조용히 잘려 나가 모델은 자기가 끝냈다고 믿는다.
    """
    budget = epo_agent.EpoAgentBudget()
    needed = budget.max_search_calls_per_round + 1
    assert epo_actions.MAX_ACTIONS_PER_ROUND >= needed, (
        f"action 상한({epo_actions.MAX_ACTIONS_PER_ROUND})이 실행 상한 "
        f"({needed})보다 작습니다."
    )

    actions = [
        search_action(f"term {i}")
        for i in range(budget.max_search_calls_per_round)
    ] + [FINISH]
    parsed = epo_actions.parse_response(json.dumps({"actions": actions}))
    assert len(parsed.actions) == len(actions)
    assert parsed.actions[-1].action == epo_actions.ACTION_FINISH


def test_too_many_actions_are_rejected_not_truncated() -> None:
    """조용히 자르면 모델은 전부 실행됐다고 믿는다."""
    actions = [
        search_action("robot arm")
        for _ in range(epo_actions.MAX_ACTIONS_PER_ROUND + 1)
    ]
    with pytest.raises(epo_actions.ActionError, match="상한"):
        epo_actions.parse_response(json.dumps({"actions": actions}))


def test_over_cap_response_counts_as_invalid_not_silent(store, tmp_path) -> None:
    """루프에서도 거절로 다뤄져야 한다. 잘라서 실행하면 안 된다."""
    actions = [
        search_action("robot arm")
        for _ in range(epo_actions.MAX_ACTIONS_PER_ROUND + 1)
    ]
    provider = FakeProvider(json.dumps({"actions": actions}), say(FINISH))
    transport = FakeTransport(token_response())
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.invalid_responses == 1
    assert result.detail_fetches == 0
    assert result.rounds[0].status == "parse_error"


def test_search_default_result_count_matches_the_ops_ceiling() -> None:
    """기본값이 OPS 상한보다 작으면 모델이 지정하지 않을 때 조용히 좁아진다."""
    parsed = epo_actions.parse_response(json.dumps({"actions": [search_action()]}))
    assert parsed.actions[0].max_results == epo_client.MAX_RESULTS_PER_QUERY


def test_default_search_requests_the_full_range(store, tmp_path) -> None:
    """실제 요청 URL 에도 그 값이 나가야 한다."""
    provider = FakeProvider(say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    run(make_agent(provider, transport, store, tmp_path))

    search = [row for row in transport.requests if "search" in row["url"]][0]
    assert f"Range=1-{epo_client.MAX_RESULTS_PER_QUERY}" in search["url"]


@pytest.mark.parametrize("depth", [5_000, 20_000])
def test_deep_json_is_an_action_error_not_a_recursion_error(depth: int) -> None:
    """깊은 입력이 RecursionError 로 루프를 탈출하면 실행 전체가 끊긴다."""
    payload = '{"a":' * depth + "1" + "}" * depth
    with pytest.raises(epo_actions.ActionError, match="중첩"):
        epo_actions.parse_response(payload)


def test_deep_json_is_handled_as_an_invalid_response(store, tmp_path) -> None:
    """루프 안에서도 예외가 새어 나가지 않고 잘못된 응답으로 처리된다."""
    deep = '{"a":' * 8_000 + "1" + "}" * 8_000
    provider = FakeProvider(deep, say(search_action(), FINISH))
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.invalid_responses == 1
    assert result.termination_reason == epo_agent.TERM_LLM_FINISHED
    assert "중첩" in " ".join(result.rounds[0].errors)


def test_deep_nesting_inside_a_query_is_rejected() -> None:
    """질의 트리 자체가 깊은 경우도 같은 경로로 막힌다."""
    node = {"kind": "term", "field": "ta", "value": "x"}
    for _ in range(50):
        node = {"kind": "group", "op": "and", "items": [node]}
    with pytest.raises(epo_actions.ActionError, match="중첩"):
        epo_actions.parse_response(
            json.dumps(
                {"actions": [{"action": epo_actions.ACTION_SEARCH, "query": node}]}
            )
        )


def test_depth_counter_does_not_recurse() -> None:
    """깊이를 재려다 깊이 때문에 죽지 않는다."""
    value = 1
    for _ in range(50_000):
        value = {"a": value}
    assert epo_actions._depth(value) > epo_actions.DEPTH_LIMIT


def test_parse_response_single_action_dict_without_wrapper() -> None:
    """모델이 최상위에 단일 action dict 를 그대로 돌려주어도 actions 리스트로 감싸서 읽는다."""
    payload = json.dumps(search_action("radar surveillance", "ta"))
    parsed = epo_actions.parse_response(payload)
    assert len(parsed.actions) == 1
    assert parsed.actions[0].action == epo_actions.ACTION_SEARCH
    assert parsed.actions[0].query.value == "radar surveillance"


def test_parse_response_action_list_without_wrapper() -> None:
    """모델이 최상위에 action list 를 돌려주어도 정상 파싱한다."""
    payload = json.dumps([search_action("radar", "ta"), FINISH])
    parsed = epo_actions.parse_response(payload)
    assert len(parsed.actions) == 2
    assert parsed.actions[0].action == epo_actions.ACTION_SEARCH
    assert parsed.actions[1].action == epo_actions.ACTION_FINISH


def test_single_action_without_wrapper_executes_in_agent_loop(store, tmp_path) -> None:
    """실제 에이전트 루프에서도 단일 action dict 응답이 parse_error 나 no_actions 없이 동작한다."""
    # 짧은 형식에도 claim_analysis 를 함께 실을 수 있어야 한다. 감싸기가 형제
    # 필드를 버리면, 이 형식을 쓴 모델은 계약을 지킬 방법이 없어진다.
    provider = FakeProvider(
        json.dumps(
            {**search_action("radar surveillance", "ta"), "claim_analysis": MINIMAL_ANALYSIS},
            ensure_ascii=False,
        ),
    )
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    result = run(make_agent(provider, transport, store, tmp_path))

    assert result.termination_reason == epo_agent.TERM_ROUND_LIMIT
    assert result.search_calls == 1
    assert result.invalid_responses == 0
    assert len(result.candidates) > 0
