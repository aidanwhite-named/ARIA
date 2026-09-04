"""이월 회귀 — 고정 목록의 나머지 전달과 새 후보 탐색을 분리한다.

여기서 검증하는 성질은 하나다. **이월은 조회가 아니다.** 최초 검색이 만든
후보 목록은 그 자리에서 얼어붙고, 다음 라운드들은 그 목록의 미전달분만
돌려준다. 이월 도중에 인덱스를 다시 뒤지지 않으므로 후보가 늘어나지 않고,
목록을 다 전달하면 대기 작업이 스스로 끝난다.

실제 Provider 는 부르지 않는다. 로컬 인덱스와 실행기만으로 확인한다.
"""
import asyncio
import json
from dataclasses import replace

import pytest

from app import retrieval
from app.retrieval import agent as agent_module
from app.retrieval.actions import ReadPage, SearchDocument
from app.retrieval.agent import (
    CarryoverDelivery,
    ComponentState,
    RetrievalBudget,
    RetrievalRun,
)
from .fake_provider import DeterministicTestProvider
from .test_retrieval import _corpus, _pdf_attachment

# 후보가 여러 건 나오도록 같은 낱말을 여러 페이지에 넣는다.
PAGES_A = [
    f"[{index:04d}] 압력센서 {index} 는 하우징에 결합되어 제어부로 신호를 보낸다. "
    f"센서 신호는 {index}회 보정된다.\n- {index} -"
    for index in range(1, 9)
]
PAGES_B = [
    f"[{index:04d}] 별개 문헌의 센서 구조 {index} 를 설명한다. 제어부는 센서 "
    f"출력을 {index}단계로 처리한다.\n- {index} -"
    for index in range(1, 7)
]


def _make_agent(tmp_path, pages_by_name: dict, claim: str = "센서"):
    items = [
        _pdf_attachment(tmp_path, name, pages, sha=f"sha-{position}")
        for position, (name, pages) in enumerate(pages_by_name.items())
    ]
    corpus, _ = _corpus(tmp_path, items)
    agent = agent_module.RetrievalAgent(
        job_id="carryover", provider=DeterministicTestProvider(), model=None,
        timeout_seconds=60, work_dir=tmp_path, corpus=corpus, claim_text=claim,
        budget=RetrievalBudget(), trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
    )
    return agent, corpus


@pytest.fixture
def agent(tmp_path):
    result, corpus = _make_agent(tmp_path, {"a.pdf": PAGES_A})
    try:
        yield result
    finally:
        retrieval.close_documents(corpus)


@pytest.fixture
def two_document_agent(tmp_path):
    result, corpus = _make_agent(tmp_path, {"a.pdf": PAGES_A, "b.pdf": PAGES_B})
    try:
        yield result
    finally:
        retrieval.close_documents(corpus)


def _component(agent, component_id: str = "R001") -> ComponentState:
    state = ComponentState(component_id, "센서", "센서")
    agent._components[state.id] = state
    return state


def _delivered(entries: list[dict]) -> list[tuple[str, str]]:
    """이번 라운드에 실제로 전달된 (문헌, chunk_id).

    문헌이 다르면 같은 chunk_id 라도 다른 구간이므로 문헌과 함께 센다.
    """
    return [
        (document["attachment"], hit["chunk_id"])
        for entry in entries
        for document in entry.get("documents", [])
        for hit in document.get("hits", [])
    ]


def _count_searches(monkeypatch) -> list[int]:
    """search_corpus 호출 횟수. 이월 중에는 늘지 않아야 한다."""
    calls = [0]
    original = agent_module.search_module.search_corpus

    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_module.search_module, "search_corpus", counted)
    return calls


def _drain(agent, run, *, start_round: int = 2, max_rounds: int = 40) -> list[tuple[str, str]]:
    """대기 작업이 없어질 때까지 빈 요청으로 라운드를 돌린다."""
    delivered: list[tuple[str, str]] = []
    round_no = start_round
    while agent._deferred_actions and round_no < start_round + max_rounds:
        entries = asyncio.run(agent._execute_actions([], run, round_no))
        delivered.extend(_delivered(entries))
        round_no += 1
    return delivered


def test_carryover_delivers_the_frozen_list_once_and_then_stops(agent, monkeypatch):
    """작은 예산으로 나눠 전달해도 최초 후보 전부가 누락·중복 없이 나간다."""
    state = _component(agent)
    run = RetrievalRun()
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )

    # 먼저 넉넉한 예산으로 이 검색의 후보 전체를 확인해 둔다.
    reference_agent_hits = None
    calls = _count_searches(monkeypatch)

    agent.budget = replace(agent.budget, max_round_result_chars=1300)
    first = asyncio.run(agent._execute_actions([request], run, 1))
    delivered = _delivered(first)
    assert calls[0] == 1
    frozen = agent._carryovers[next(iter(agent._carryovers))]
    reference_agent_hits = [hit.row.chunk_id for hit in frozen.hits]
    assert len(delivered) < len(reference_agent_hits), "이 테스트는 부분 전달을 전제로 한다"
    assert agent._deferred_actions

    delivered.extend(_drain(agent, run))

    # 1) 이월 중에는 인덱스를 다시 뒤지지 않는다.
    assert calls[0] == 1
    # 2) 후보가 늘지도, 줄지도 않았다.
    assert [hit.row.chunk_id for hit in frozen.hits] == reference_agent_hits
    # 3) 최초 후보 전부가 정확히 한 번씩 전달됐다.
    assert [chunk_id for _alias, chunk_id in delivered] == reference_agent_hits
    assert len(set(delivered)) == len(delivered)
    # 4) 목록을 소진했으므로 대기 작업이 끝났다.
    assert not agent._deferred_actions
    assert not frozen.pending
    assert run.budget_limited is True
    assert run.budget_exhausted is False
    assert all(record.omitted == 0 for record in state.search_attempts.values())
    assert run.exposed_chunks == {
        (agent.corpus[0].attachment_id, chunk_id) for chunk_id in reference_agent_hits
    }


def test_carryover_survives_a_round_that_delivers_nothing(agent, monkeypatch):
    """예산이 모자라 0건인 라운드가 있어도 목록을 잃지 않고 이어서 전달한다."""
    state = _component(agent)
    run = RetrievalRun()
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )
    calls = _count_searches(monkeypatch)

    agent.budget = replace(agent.budget, max_round_result_chars=120)
    first = asyncio.run(agent._execute_actions([request], run, 1))
    assert not _delivered(first)
    assert agent._deferred_actions

    agent.budget = replace(agent.budget, max_round_result_chars=1500)
    delivered = _drain(agent, run)

    frozen = agent._carryovers[next(iter(agent._carryovers))]
    assert [chunk_id for _alias, chunk_id in delivered] == [
        hit.row.chunk_id for hit in frozen.hits
    ]
    assert len(set(delivered)) == len(delivered)
    assert not agent._deferred_actions
    assert calls[0] == 1


def test_carryover_keeps_documents_separate(two_document_agent, monkeypatch):
    """문헌이 둘이면 목록도 둘이다. 같은 chunk_id 를 하나로 합치지 않는다."""
    agent = two_document_agent
    state = _component(agent)
    run = RetrievalRun()
    request = SearchDocument(
        action="search_document", component_id=state.id, queries=["센서"], limit=8,
    )
    calls = _count_searches(monkeypatch)

    agent.budget = replace(agent.budget, max_round_result_chars=2500)
    first = asyncio.run(agent._execute_actions([request], run, 1))
    delivered = _delivered(first) + _drain(agent, run)

    assert calls[0] == 1
    assert len(agent._carryovers) == 2
    aliases = {alias for alias, _chunk in delivered}
    assert aliases == {document.alias for document in agent.corpus}
    # 문헌마다 고정 목록 전체가 전달됐고, 중복은 없다.
    expected = {
        state.alias: [hit.row.chunk_id for hit in state.hits]
        for state in agent._carryovers.values()
    }
    for alias, chunk_ids in expected.items():
        assert [chunk for row_alias, chunk in delivered if row_alias == alias] == chunk_ids
    assert len(set(delivered)) == len(delivered)
    # 같은 chunk_id 가 두 문헌에 있어도 각각 노출로 기록된다.
    shared = set(expected[agent.corpus[0].alias]) & set(expected[agent.corpus[1].alias])
    assert shared
    for chunk_id in shared:
        assert (agent.corpus[0].attachment_id, chunk_id) in run.exposed_chunks
        assert (agent.corpus[1].attachment_id, chunk_id) in run.exposed_chunks
    assert not agent._deferred_actions


def test_zero_hit_search_leaves_no_carryover(agent, monkeypatch):
    """0건 검색은 이월을 만들지 않는다. 검색했다는 기록은 남는다."""
    state = _component(agent)
    run = RetrievalRun()
    calls = _count_searches(monkeypatch)
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["존재하지않는낱말조합xyz"], limit=8,
    )
    entries = asyncio.run(agent._execute_actions([request], run, 1))
    assert not _delivered(entries)
    assert not agent._deferred_actions
    assert calls[0] == 1
    assert all(not carry.pending for carry in agent._carryovers.values())
    assert state.search_attempts


@pytest.mark.parametrize("partial_hits", [0, 2])
def test_failed_channel_is_retried_and_then_frozen(agent, monkeypatch, partial_hits):
    """실패한 조회는 캐시하지 않는다. 재시도로 얻은 목록부터 고정된다."""
    state = _component(agent)
    run = RetrievalRun()
    original = agent_module.search_module.search_corpus
    calls = [0]

    def flaky(*args, **kwargs):
        calls[0] += 1
        results = original(*args, **kwargs)
        if calls[0] == 1:
            for result in results:
                result.hits = result.hits[:partial_hits]
                result.channels = [
                    {"channel": "fts", "requested": True, "executed": False,
                     "error": "일시적 실패", "hits": 0}
                ]
        return results

    monkeypatch.setattr(agent_module.search_module, "search_corpus", flaky)
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )
    first = asyncio.run(agent._execute_actions([request], run, 1))
    assert len(_delivered(first)) == partial_hits

    # 실패는 캐시되지 않았으므로 같은 요청이 실제로 다시 조회된다.
    second = asyncio.run(agent._execute_actions([request], run, 2))
    assert calls[0] == 2
    delivered = _delivered(second) + _drain(agent, run, start_round=3)
    assert len(delivered) == 8
    assert calls[0] == 2
    assert not agent._deferred_actions
    assert "fts" not in state.search_attempts[agent.corpus[0].attachment_id].failed_channels
    assert "fts" not in state.failed_channels


def test_new_search_is_a_separate_list_not_a_carryover(agent, monkeypatch):
    """새 검색어는 별도의 조회다. 이월 목록에 후보를 보태지 않는다."""
    state = _component(agent)
    run = RetrievalRun()
    calls = _count_searches(monkeypatch)
    agent.budget = replace(agent.budget, max_round_result_chars=1300)

    first_request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )
    asyncio.run(agent._execute_actions([first_request], run, 1))
    frozen = agent._carryovers[next(iter(agent._carryovers))]
    before = [hit.row.chunk_id for hit in frozen.hits]
    pending_before = len(frozen.pending)
    assert pending_before

    second_request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["하우징 제어부"], limit=8,
    )
    agent.budget = replace(agent.budget, max_round_result_chars=56_000)
    asyncio.run(agent._execute_actions([second_request], run, 2))

    assert calls[0] == 2, "새 검색어는 실제로 인덱스를 조회한다"
    # 새 검색은 자기 목록을 만든다. 기존 고정 목록은 그대로다.
    assert len(agent._carryovers) == 2
    assert [hit.row.chunk_id for hit in frozen.hits] == before
    assert len(frozen.pending) <= pending_before

    _drain(agent, run, start_round=3)
    assert not agent._deferred_actions
    assert not frozen.pending


def test_carryover_does_not_mutate_shared_cached_results(agent):
    """제외 목록이 있어도 캐시된 결과의 후보를 지우지 않는다."""
    state = _component(agent)
    run = RetrievalRun()
    plain = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )
    asyncio.run(agent._execute_actions([plain], run, 1))
    cached = list(agent._search_cache.values())[0][1]
    before = [hit.row.chunk_id for hit in cached[0].hits]

    excluded = plain.model_copy(update={"exclude_chunk_ids": before[:2]})
    asyncio.run(agent._execute_actions([excluded], run, 2))
    assert [hit.row.chunk_id for hit in cached[0].hits] == before


def test_pending_reads_still_run_before_carryover(agent):
    """열람 우선 처리와 회차 내 본문 중복 제거는 그대로다."""
    state = _component(agent)
    run = RetrievalRun()
    agent.budget = replace(agent.budget, max_round_result_chars=1300)
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"], limit=8,
    )
    asyncio.run(agent._execute_actions([request], run, 1))
    assert any(
        isinstance(entry.item, CarryoverDelivery) for entry in agent._deferred_actions
    )

    agent.budget = replace(agent.budget, max_round_result_chars=56_000)
    read = ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1)
    entries = asyncio.run(agent._execute_actions([read], run, 2))
    assert entries[0]["action"] == "read_page"

    # 같은 라운드에서 열람으로 이미 실은 구간은 이월 결과에서 본문을 반복하지
    # 않고 참조만 남긴다. 후보 행 자체와 구성-근거 연결은 유지된다.
    reused = [
        hit
        for entry in entries
        for document in entry.get("documents", [])
        for hit in document.get("hits", [])
        if "text_shown_in_this_round" in hit
    ]
    assert all(hit.get("chunk_id") for hit in reused)
    assert not agent._deferred_actions


def test_partial_failure_keeps_old_pending_when_retry_succeeds(agent, monkeypatch):
    state = _component(agent)
    original = agent_module.search_module.search_corpus
    calls = []
    def flaky(*args, **kwargs):
        results = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            results[0].hits = results[0].hits[:2]
            results[0].channels.append({"channel": "semantic", "requested": True,
                                        "executed": False, "error": "temporary"})
        return results
    monkeypatch.setattr(agent_module.search_module, "search_corpus", flaky)
    request = SearchDocument(action="search_document", component_id=state.id,
                             attachment="ATT-01", queries=["센서"])
    run = RetrievalRun()
    agent.budget = replace(agent.budget, max_round_result_chars=120)
    asyncio.run(agent._execute_actions([request], run, 1))
    old = next(iter(agent._carryovers.values()))
    assert len(old.pending) == 2
    agent.budget = replace(agent.budget, max_round_result_chars=56000)
    entries = asyncio.run(agent._execute_actions([request], run, 2))
    assert len(calls) == 2
    assert len(agent._carryovers) == 2
    assert len(old.hits) == 2 and not old.pending
    assert len(set(_delivered(entries))) == 8
    assert not agent._deferred_actions
    assert "semantic" not in state.failed_channels


def test_pending_search_and_page_limit_remain_incomplete(agent):
    state = _component(agent)
    run = RetrievalRun()
    agent.budget = replace(agent.budget, max_round_result_chars=120)
    request = SearchDocument(action="search_document", component_id=state.id,
                             attachment="ATT-01", queries=["센서"])
    asyncio.run(agent._execute_actions([request], run, 1))
    assert run.budget_exhausted and run.deferred_pending
    agent.budget = replace(agent.budget, max_round_result_chars=56000, max_page_reads=0)
    page = ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1)
    asyncio.run(agent._execute_actions([page], run, 2))
    assert not agent._deferred_actions
    assert run.budget_exhausted  # 검색을 모두 전달했어도 요청한 페이지는 못 읽었다.
    asyncio.run(agent._execute_actions([], run, 3))
    assert run.budget_exhausted


def test_completed_carryover_publishes_history_without_partial_status(tmp_path):
    from app.providers.base import ExecutionOutcome
    class DrainProvider(DeterministicTestProvider):
        async def execute(self, request, emit):
            payload, _ = json.JSONDecoder().raw_decode(
                request.user_message[request.user_message.index("{"):]
            )
            if payload["round"] == 1:
                response = {"components": [{"label": "sensor", "feature": "센서"}],
                            "actions": [{"action": "search_document", "component_id": "R001",
                                         "queries": ["센서", "압력", "하우징"]}]}
            elif payload.get("deferred_actions", {}).get("count"):
                response = {"actions": []}
            else:
                response = {"actions": [{"action": "finalize_evidence", "components": [
                    {"component_id": "R001", "status_claim": "not_found"}]}]}
            return ExecutionOutcome(result_text=json.dumps(response), exit_code=0,
                                    terminal_reason="completed")
    document = _pdf_attachment(tmp_path, "a.pdf", PAGES_A)
    result = asyncio.run(retrieval.run_retrieval(
        job_id="drain", provider=DrainProvider(), model=None, timeout_seconds=60,
        work_dir=tmp_path, attachments=[document], claim_text="센서",
        budget=RetrievalBudget(max_round_result_chars=1300),
    ))
    try:
        assert result.ok
        assert result.manifest["deferred_actions"]
        assert result.manifest["deferred_pending"] == []
        assert result.manifest["budget_limited"] is True
        assert result.manifest["budget_exhausted"] is False
        assert result.bundle["budget_exhausted"] is False
        assert result.manifest["status"] == "complete"
    finally:
        retrieval.close_documents(result.documents)


@pytest.mark.parametrize("kind", ["partial_page", "dropped_page", "package"])
def test_final_package_loss_keeps_budget_warning(kind):
    from app.retrieval import evidence
    bundle = {"documents": [], "components": [], "budget_exhausted": False,
              "budget_limited": True, "evidence_pages": []}
    if kind == "partial_page":
        from app.retrieval import pages
        from .test_delivery_modes import _FakeDocument
        bundle["evidence_pages"] = pages.build(
            corpus=[_FakeDocument({1: "x" * 1000})], finding_pages={"doc": {1}},
            neighbours=0, char_budget=100,
        )
    elif kind == "dropped_page":
        bundle["page_reductions"] = ["ATT-01 p.1"]
    else:
        bundle["package_reductions"] = ["metadata omitted by budget"]
    evidence.fit(bundle, RetrievalBudget())
    assert bundle["budget_exhausted"] is True
