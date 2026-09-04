"""Token efficiency regressions; no live provider calls."""
import asyncio
import json
from dataclasses import replace

import pytest

from app import config, retrieval
from app.providers.agy_cli import AgyCliProvider
from app.retrieval import agent as agent_module, pages
from app.retrieval.actions import ReadPage, SearchDocument, parse_response
from app.retrieval.agent import ComponentState, DeferredAction, RetrievalBudget, RetrievalRun
from app.retrieval.prompts import AGENT_SYSTEM_PROMPT, dump_round_json, render_round
from .fake_provider import DeterministicTestProvider
from .test_retrieval import _corpus, _pdf_attachment, KOREAN_PAGES
from .test_delivery_modes import _FakeDocument


@pytest.fixture
def agent(tmp_path):
    document = _pdf_attachment(tmp_path, "doc.pdf", KOREAN_PAGES)
    corpus, _ = _corpus(tmp_path, [document])
    result = agent_module.RetrievalAgent(
        job_id="efficiency", provider=DeterministicTestProvider(), model=None,
        timeout_seconds=60, work_dir=tmp_path, corpus=corpus, claim_text="센서",
        budget=RetrievalBudget(), trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
    )
    try:
        yield result
    finally:
        retrieval.close_documents(corpus)


def test_efficiency_defaults_and_explicit_overrides():
    budget = retrieval.budget_from_settings({})
    assert (budget.max_rounds, budget.max_round_result_chars, budget.max_evidence_chars) == (5, 56000, 100000)
    assert config.DEFAULTS["retrieval_max_rounds"] == 5
    assert config.DEFAULTS["retrieval_evidence_chars"] == 100000
    assert retrieval.budget_from_settings({"retrieval_max_rounds": 9}).max_rounds == 9


def test_round_json_is_compact_and_preserves_every_value():
    """전송 JSON 에서 지우는 것은 구조 사이의 공백뿐이다."""
    payload = {
        "round": 2,
        "claim_text": "청구항",
        "documents": [
            {
                "attachment": "ATT-01",
                "hits": [
                    {"chunk_id": "P0001-002", "text": "첫 줄\n  들여쓴 줄  끝공백 "}
                ],
            }
        ],
    }
    message = render_round(payload)
    body = message.split("\n")[1]
    assert ", " not in body and ": " not in body
    # 값은 하나도 잃지 않는다 — 문자열 안의 개행·들여쓰기·끝공백까지 그대로다.
    assert json.loads(body) == {
        key: value for key, value in payload.items() if key != "claim_text"
    }


def test_budget_is_measured_in_the_format_actually_sent():
    """json_size 와 실제 전송 직렬화가 어긋나면 예산이 조용히 새거나 넘친다."""
    entry = {"action": "search_document", "documents": [{"hits": [{"text": "가" * 50}]}]}
    assert agent_module.json_size(entry) == len(dump_round_json(entry))
    assert agent_module.json_size(entry) < len(json.dumps(entry, ensure_ascii=False, indent=2))


def test_56k_english_result_fits_serialized_agy_transport(agent):
    result = [{"text": "sample text\n" * 4300}]
    assert sum(agent_module.json_size(row) for row in result) <= 56000
    message = render_round(agent._round_payload(2, result, ""))
    provider = AgyCliProvider()
    assert provider.payload_bytes(AGENT_SYSTEM_PROMPT, message) < provider.max_input_bytes


def test_round_gate_uses_provider_envelope_and_records_bytes(agent):
    class EnvelopeProvider(DeterministicTestProvider):
        max_input_bytes = 20000
        calls = 0
        def payload_bytes(self, system_prompt, user_message):
            assert len((system_prompt + user_message).encode("utf-8")) < self.max_input_bytes
            return self.max_input_bytes + 1
        async def execute(self, request, emit):
            self.calls += 1
            raise AssertionError("Over-budget input reached provider")
    agent.provider = EnvelopeProvider()
    run = asyncio.run(agent.run())
    assert run.rounds[0].status == "input_too_large"
    assert run.rounds[0].input_bytes == 20001
    assert agent.provider.calls == 0


def test_hit_payload_is_minimal_but_audit_and_candidates_keep_provenance(agent):
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    request = SearchDocument(action="search_document", component_id=state.id, queries=["센서"])
    run = RetrievalRun()
    result = asyncio.run(agent._execute_actions([request], run, 1))
    hits = [hit for entry in result for doc in entry["documents"] for hit in doc["hits"]]
    assert hits
    required = {"alias", "chunk_id", "pdf_page", "section", "extraction_status", "text"}
    allowed = required | {"context_before", "context_after"}
    assert all(required <= set(hit) <= allowed for hit in hits)
    assert state.hit_chunks and run.exposed_chunks
    assert all(hit["channels"] and hit["ranks"] and hit["score"] for hit in state.hit_chunks.values())
    trace = [json.loads(line) for line in agent.trace.path.read_text(encoding="utf-8").splitlines()]
    audited = next(row for row in trace if row["type"] == "search_candidates")["payload"]["documents"][0]["hits"][0]
    assert {"attachment_id", "filename", "extraction_method", "channel_ranks", "page_order", "score"} <= audited.keys()


def test_first_round_labels_and_already_deferred_ids_are_canonicalized(agent):
    label = "청구항 1 (A)"
    request = SearchDocument(action="search_document", component_id=label, queries=["센서"])
    agent._deferred_actions = [
        DeferredAction(request, first_round=1, reason="old", attempts=9),
        DeferredAction(request.model_copy(update={"component_id": "R001"}), first_round=2, reason="new", attempts=3),
    ]
    response = parse_response(json.dumps({"components": [{"label": label, "feature": "센서"}], "actions": [request.model_dump()]}))
    run = RetrievalRun()
    agent._declare_components(response, run)
    assert agent._component(label) is agent._components["R001"]
    assert len(agent._deferred_actions) == 1
    pending = agent._deferred_actions[0]
    assert (pending.item.component_id, pending.first_round, pending.attempts) == ("R001", 1, 9)
    result = asyncio.run(agent._execute_actions(response.actions, run, 1))
    assert result[0]["component_id"] == "R001"
    assert agent._components["R001"].hit_chunks
    assert all(row.item.component_id == "R001" for row in agent._deferred_actions)
    finalize = parse_response(json.dumps({"actions": [{"action": "finalize_evidence", "components": [{"component_id": label}]}]})).actions[0]
    assert agent._canonical_action(finalize).components[0].component_id == "R001"


def test_ambiguous_labels_are_not_assigned_to_an_arbitrary_component(agent):
    response = parse_response(json.dumps({"components": [{"label": "same"}, {"label": "same"}], "actions": []}))
    agent._declare_components(response, RetrievalRun())
    assert agent._component("same") is None
    assert agent._component("R001") is not None


def test_zero_hit_search_keeps_first_candidate_priority_and_audit(agent):
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    document = agent.corpus[0]
    request = SearchDocument(action="search_document", component_id=state.id, attachment=document.alias, queries=["센서"])
    before = agent._action_priority(request)
    state.record_search(attachment_id=document.attachment_id, alias=document.alias,
                        queries=["a", "b", "c"], channels_used=["fts_bm25"],
                        failed_channels=[], hits=0, omitted=3)
    assert not state.searched
    assert state.search_attempts[document.attachment_id].omitted == 3
    assert agent._action_priority(request) == before
    state.record_search(attachment_id=document.attachment_id, alias=document.alias,
                        queries=["d"], channels_used=["fts_bm25"],
                        failed_channels=[], hits=1, omitted=0)
    assert state.searched[document.attachment_id].hits == 1
    assert state.search_attempts[document.attachment_id].queries == ["a", "b", "c", "d"]


def test_unseeded_components_share_reserved_budget_before_busy_reads(agent, monkeypatch):
    for ident in ["R001", "R002", "R003"]:
        agent._components[ident] = ComponentState(ident, ident, "sensor")
    agent._components["R003"].hit_chunks["known"] = {"chunk_id": "known"}
    read = ReadPage(action="read_page", component_id="R003", attachment="ATT-01", page=1)
    agent._deferred_actions = [DeferredAction(read, 1, "old read", attempts=30)]
    searches = [SearchDocument(action="search_document", component_id=c, queries=["sensor"]) for c in ["R001", "R002"]]
    grants = []
    async def consume(item, run, round_no, budget_left):
        grants.append((item.component_id, budget_left))
        entry = {"action": item.action, "text": ""}
        entry["text"] = "x" * (budget_left - agent_module.json_size(entry))
        return entry, agent_module.json_size(entry)
    monkeypatch.setattr(agent, "_execute_one", consume)
    result = asyncio.run(agent._execute_actions(searches, RetrievalRun(), 2))
    assert [c for c, _ in grants] == ["R001", "R002", "R003"]
    assert grants[0][1] == grants[1][1] == 8400
    assert grants[2][1] == 39200
    assert sum(agent_module.json_size(row) for row in result) == 56000


def test_overlarge_page_is_explicit_prefix_not_a_full_page():
    original = "한글 원문과 text\n" * 200
    built = pages.build(corpus=[_FakeDocument({1: original})], finding_pages={"doc": {1}}, neighbours=0, char_budget=1000)
    page = built[0]["pages"][0]
    assert page["text"] == original[:250]
    assert page["truncated"] is True
    assert page["source_chars"] == len(original)
    assert page["included_chars"] + page["omitted_chars"] == len(original)
    assert pages.unverified_pages(built[0]) == [1]
    assert pages.truncations(built)[0]["source_end"] == 250
    rendered = "\n".join(pages.render(built))
    assert "부분 수록" in rendered and "뒤" in rendered and "검토 범위 밖" in rendered
    assert "0페이지를 전문으로" in rendered


def test_full_page_and_dropped_partial_are_distinguished():
    built = pages.build(corpus=[_FakeDocument({1: "x" * 10, 2: "y" * 500})], finding_pages={"doc": {1, 2}}, neighbours=0, char_budget=100)
    assert pages.unverified_pages(built[0]) == [2]
    assert len(pages.truncations(built)) == 1
    assert pages.drop_one(built, only_context=False)["pdf_page"] == 2
    assert not pages.truncations(built)


def test_repeated_search_is_not_executed_twice(agent, monkeypatch):
    """인덱스는 다시 뒤지지 않지만 새 호출에 원문을 다시 전달한다."""
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    calls = []
    original = agent_module.search_module.search_corpus

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_module.search_module, "search_corpus", counted)
    run = RetrievalRun()
    request = SearchDocument(action="search_document", component_id=state.id, queries=["센서"])
    first = asyncio.run(agent._execute_actions([request], run, 1))
    repeat = SearchDocument(action="search_document", component_id=state.id, queries=[" 센서 "])
    second = asyncio.run(agent._execute_actions([repeat], run, 2))
    assert len(calls) == 1
    assert first[0]["documents"]
    assert second[0]["repeated_search"] is True
    assert second[0]["first_executed_round"] == 1
    first_hits = first[0]["documents"][0]["hits"]
    second_hits = second[0]["documents"][0]["hits"]
    assert first_hits and second_hits
    assert [row["text"] for row in second_hits] == [row["text"] for row in first_hits]


@pytest.mark.parametrize("first_budget", [20, 300])
def test_undelivered_search_cache_is_replayed_instead_of_disappearing(agent, first_budget):
    """첫 반환 예산이 0건이어도 다음 라운드는 캐시 후보를 실제로 전달한다."""
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    run = RetrievalRun()
    request = SearchDocument(
        action="search_document", component_id=state.id,
        attachment="ATT-01", queries=["센서"],
    )
    original_budget = agent.budget
    agent.budget = replace(original_budget, max_round_result_chars=first_budget)
    first = asyncio.run(agent._execute_actions([request], run, 1))
    first_hits = [
        hit for entry in first for document in entry.get("documents", [])
        for hit in document.get("hits", [])
    ]
    assert not first_hits
    assert not run.exposed_chunks
    assert agent._deferred_actions

    agent.budget = replace(original_budget, max_round_result_chars=56000)
    second = asyncio.run(agent._execute_actions([], run, 2))
    hits = [hit for entry in second for doc in entry["documents"] for hit in doc["hits"]]
    assert hits
    # 0건 전달이면 재조회(repeated_search)든 고정 목록 이월(carryover)이든
    # 후보를 잃지 않고 다음 라운드에 이어서 전달한다.
    assert second[0].get("carryover") or second[0].get("repeated_search")
    assert run.exposed_chunks
    assert not agent._deferred_actions


def test_search_hits_carry_neighbouring_context(agent, monkeypatch):
    """후보 청크의 같은 페이지 앞뒤 구간을 함께 싣고, 길이는 상한으로 자른다."""
    document = agent.corpus[0]
    monkeypatch.setattr(
        document.index, "neighbours",
        lambda chunk_id, before=1, after=1: ("앞" * 900, "뒤" * 900),
    )
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    request = SearchDocument(action="search_document", component_id=state.id, queries=["센서"])
    result = asyncio.run(agent._execute_actions([request], RetrievalRun(), 1))
    hits = [hit for entry in result for doc in entry["documents"] for hit in doc["hits"]]
    assert hits
    for hit in hits:
        assert hit["context_before"] == "앞" * agent_module.CONTEXT_CHARS
        assert hit["context_after"] == "뒤" * agent_module.CONTEXT_CHARS


def test_already_served_page_reuses_cache_and_resends_text(agent, monkeypatch):
    """새 호출에는 캐시 본문을 다시 싣되 페이지 읽기 예산은 다시 쓰지 않는다."""
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    run = RetrievalRun()
    request = ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1)
    first = asyncio.run(agent._execute_actions([request], run, 1))
    original_text = first[0]["pages"][0]["text"]
    assert run.pages_read == 1
    document = agent.corpus[0]
    monkeypatch.setattr(
        document.index, "page_rows",
        lambda page: (_ for _ in ()).throw(AssertionError("page was queried again")),
    )
    second = asyncio.run(agent._execute_actions([request], run, 2))
    page = second[0]["pages"][0]
    assert page["text"] == original_text
    assert page["already_read"] is True
    assert page["first_served_round"] == 1
    assert page["chunks"]
    assert run.pages_read == 1
    assert run.repeat_page_reads == 1
    assert second[0]["pages_read_total"] == 1
    next_input = agent._round_payload(3, second, "")
    assert next_input["results"][0]["pages"][0]["text"] == original_text


def test_duplicate_span_is_sent_once_per_call_with_links_preserved(agent):
    """한 호출 안에서 같은 구간은 본문 한 번. 구성별 후보 연결은 모두 남는다."""
    first = ComponentState("R001", "센서 A", "센서")
    second = ComponentState("R002", "센서 B", "센서")
    agent._components[first.id] = first
    agent._components[second.id] = second
    run = RetrievalRun()
    requests = [
        SearchDocument(action="search_document", component_id="R001", queries=["센서"]),
        SearchDocument(action="search_document", component_id="R002", queries=["센서"]),
    ]
    results = asyncio.run(agent._execute_actions(requests, run, 1))
    by_component = {entry["component_id"]: entry for entry in results}
    rows = {
        key: [hit for doc in entry["documents"] for hit in doc["hits"]]
        for key, entry in by_component.items()
    }
    assert rows["R001"] and rows["R002"]
    shared = set(hit["chunk_id"] for hit in rows["R001"]) & set(
        hit["chunk_id"] for hit in rows["R002"]
    )
    assert shared
    for chunk_id in shared:
        carrying = [
            hit
            for component_rows in rows.values()
            for hit in component_rows
            if hit["chunk_id"] == chunk_id and hit.get("text")
        ]
        assert len(carrying) == 1
        repeated = [
            hit
            for component_rows in rows.values()
            for hit in component_rows
            if hit["chunk_id"] == chunk_id and "text" not in hit
        ]
        assert all(hit["text_shown_in_this_round"]["component_id"] for hit in repeated)
    # 두 구성 모두 근거 연결과 노출 기록을 유지한다.
    def recorded(state):
        return {key.split(":")[-1] for key in state.hit_chunks}

    assert shared <= recorded(first) and shared <= recorded(second)
    assert all(entry["snippet"] for entry in second.hit_chunks.values())
    assert {chunk for _attachment, chunk in run.exposed_chunks} >= shared


def test_next_round_resends_the_text_it_deduplicated(agent):
    """중복 제거는 호출 안에서만. 다음 라운드에는 원문이 다시 실린다."""
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    run = RetrievalRun()
    request = ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1)
    same_round = asyncio.run(agent._execute_actions([request, request], run, 1))
    pages = [page for entry in same_round for page in entry["pages"]]
    assert len([page for page in pages if page.get("text")]) == 1
    next_round = asyncio.run(agent._execute_actions([request], run, 2))
    assert next_round[0]["pages"][0]["text"]


def test_ledger_drops_only_the_snippet_that_is_already_in_the_message(agent):
    """후보 장부는 같은 메시지에 원문이 있으면 발췌를 빼고 출처는 남긴다."""
    state = ComponentState("R001", "센서", "센서")
    agent._components[state.id] = state
    agent._order.append(state.id)
    run = RetrievalRun()
    request = SearchDocument(action="search_document", component_id=state.id, queries=["센서"])
    results = asyncio.run(agent._execute_actions([request], run, 1))
    payload = agent._round_payload(2, results, "")
    ledger = payload["components"][0]["candidate_ledger"]
    assert ledger
    for row in ledger:
        assert row["chunk_id"] and row["attachment"] and row["channels"]
        assert ("snippet" in row) != bool(row.get("text_shown_in_this_round"))
    # 원문이 실리지 않은 라운드에서는 발췌가 그대로 남는다.
    assert all("snippet" in row for row in agent._round_payload(3, [], "")["components"][0]["candidate_ledger"])


def test_status_and_deferred_lists_state_shared_text_once(agent):
    """상태·대기 목록의 반복 문장을 한 번으로 줄이되 출처와 미처리는 남긴다."""
    entry, _spent = agent._document_status(agent.corpus, "get_document_status", 50_000)
    assert entry["note"]
    assert all("note" not in row and "filename" not in row for row in entry["documents"])
    assert all(row["attachment"] and row["pdf_sha256"] for row in entry["documents"])

    run = RetrievalRun()
    for page in (1, 2):
        agent._enqueue_deferred(
            ReadPage(action="read_page", component_id="R001", attachment="ATT-01", page=page),
            run=run, round_no=1, reason="반환 예산으로 누락됨",
        )
    preview = agent._deferred_preview()
    assert preview["count"] == 2
    assert len(preview["deferred"]) == 1
    assert preview["deferred"][0]["reason"] == "반환 예산으로 누락됨"
    assert len(preview["deferred"][0]["items"]) == 2
    assert all("reason" not in row for row in preview["deferred"][0]["items"])
    assert all(row["attachment"] == "ATT-01" for row in preview["deferred"][0]["items"])


def test_same_chunk_id_in_two_documents_is_not_treated_as_one_span(tmp_path):
    """chunk_id 는 문헌 안에서만 고유하다. 문헌이 다르면 본문도 장부도 섞이지 않는다."""
    first = _pdf_attachment(tmp_path, "a.pdf", ["[0001] 알파 센서 고유문구 AAA" + chr(10) + "- 1 -"], sha="sha-a")
    second = _pdf_attachment(tmp_path, "b.pdf", ["[0001] 베타 센서 고유문구 BBB" + chr(10) + "- 1 -"], sha="sha-b")
    corpus, _ = _corpus(tmp_path, [first, second])
    agent = agent_module.RetrievalAgent(
        job_id="two-docs", provider=DeterministicTestProvider(), model=None,
        timeout_seconds=60, work_dir=tmp_path, corpus=corpus, claim_text="센서",
        budget=RetrievalBudget(), trace=agent_module.TraceWriter(tmp_path / "trace.jsonl"),
    )
    try:
        # 전제: 두 문헌이 같은 chunk_id 를 쓴다.
        ids = [{row.chunk_id for row in document.index.all_chunks()} for document in corpus]
        assert ids[0] & ids[1]

        state = ComponentState("R001", "센서", "센서")
        agent._components[state.id] = state
        agent._order.append(state.id)
        run = RetrievalRun()
        actions = [
            SearchDocument(action="search_document", component_id=state.id, queries=["센서"]),
            ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1),
            ReadPage(action="read_page", component_id=state.id, attachment="ATT-02", page=1),
        ]
        results = asyncio.run(agent._execute_actions(actions, run, 1))

        hits = [hit for entry in results for doc in entry.get("documents") or [] for hit in doc["hits"]]
        by_alias = {hit["alias"]: hit for hit in hits}
        assert {"ATT-01", "ATT-02"} <= set(by_alias)
        # 같은 chunk_id 를 쓰지만 각자의 본문을 받는다.
        assert by_alias["ATT-01"]["chunk_id"] == by_alias["ATT-02"]["chunk_id"]
        assert "AAA" in by_alias["ATT-01"]["text"] and "BBB" not in by_alias["ATT-01"]["text"]
        assert "BBB" in by_alias["ATT-02"]["text"] and "AAA" not in by_alias["ATT-02"]["text"]

        # 페이지 읽기도 문헌별로 각자의 본문을 싣는다.
        pages = {
            entry["attachment"]: entry["pages"][0]
            for entry in results
            if entry.get("action") == "read_page" and entry.get("pages")
        }
        assert "AAA" in pages["ATT-01"].get("text", "")
        assert "BBB" in pages["ATT-02"].get("text", "")

        # 후보 장부도 문헌별로 갈린다.
        ledger = agent._round_payload(2, results, "")["components"][0]["candidate_ledger"]
        assert {row["attachment"] for row in ledger} == {"ATT-01", "ATT-02"}
        snippets = {
            row["attachment"]: (row.get("snippet") or "")
            for row in ledger
        }
        for alias, marker in (("ATT-01", "AAA"), ("ATT-02", "BBB")):
            other = "BBB" if marker == "AAA" else "AAA"
            candidate = next(
                value for key, value in state.hit_chunks.items() if value["alias"] == alias
            )
            assert marker in candidate["snippet"] and other not in candidate["snippet"]
            assert other not in snippets[alias]
    finally:
        retrieval.close_documents(corpus)


def test_pending_read_precedes_search_and_supplies_shared_text(agent):
    state = ComponentState("R001", "sensor", "센서")
    agent._components[state.id] = state
    state.hit_chunks["known"] = {"chunk_id": "known"}
    read = ReadPage(action="read_page", component_id=state.id, attachment="ATT-01", page=1)
    agent._deferred_actions = [DeferredAction(read, 1, "pending", attempts=0)]
    search = SearchDocument(action="search_document", component_id=state.id, queries=["센서"])
    run = RetrievalRun()
    results = asyncio.run(agent._execute_actions([search], run, 2))
    assert results[0]["action"] == "read_page"
    assert results[0]["pages"][0]["text"]
    assert run.pages_read == 1
    hits = [hit for entry in results[1:] for doc in entry.get("documents", []) for hit in doc["hits"]]
    shared = [hit for hit in hits if hit["pdf_page"] == 1]
    assert shared
    assert all("text" not in hit and hit["text_shown_in_this_round"]["pdf_page"] == 1 for hit in shared)
    assert sum(agent_module.json_size(row) for row in results) <= agent.budget.max_round_result_chars


def test_large_search_cannot_consume_other_pending_search_shares(agent, monkeypatch):
    state = ComponentState("R001", "sensor", "sensor")
    state.hit_chunks["known"] = {"chunk_id": "known"}
    agent._components[state.id] = state
    requests = [SearchDocument(action="search_document", component_id=state.id, queries=[query]) for query in ["a", "b", "c"]]
    agent._deferred_actions = [DeferredAction(requests[0], 1, "pending")]
    grants = []
    async def consume(item, run, round_no, budget_left):
        grants.append(budget_left)
        entry = {"action": item.action, "text": ""}
        entry["text"] = "x" * (budget_left - agent_module.json_size(entry))
        return entry, agent_module.json_size(entry)
    monkeypatch.setattr(agent, "_execute_one", consume)
    results = asyncio.run(agent._execute_actions(requests[1:], RetrievalRun(), 2))
    assert len(results) == 3
    assert max(grants) - min(grants) <= 1
    assert sum(agent_module.json_size(row) for row in results) == 56000
