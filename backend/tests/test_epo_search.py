"""EPO OPS 검색 흐름 — CQL, 호출, 원본 보존, 파싱, 증거 검증.

**이 파일의 어떤 테스트도 네트워크를 열지 않는다.** OpsClient 의 transport 를
전부 가짜로 바꾸므로, 실수로 실제 호출이 나가면 fixture 가 떨어져 테스트가
실패한다(조용히 통과하지 않는다).

자격증명도 실물을 쓰지 않는다. 테스트 전용 문자열만 쓴다.
"""

from __future__ import annotations

import json

import pytest

from app.patent_search import (
    artifacts,
    epo_backend,
    epo_client,
    epo_cql,
    epo_parser,
    epo_quota,
    parsers,
    policy,
    provenance,
)

from . import epo_fixtures as fx

TEST_KEY = "TESTKEY000"
TEST_SECRET = "TESTSECRET111"


# --------------------------------------------------------------- 가짜 전송 계층


class FakeTransport:
    """미리 정해 둔 응답을 순서대로 돌려준다. 요청은 전부 기록한다."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.header_items()),
                "body": request.data,
                "timeout": timeout,
            }
        )
        if not self.queue:
            raise AssertionError(
                f"준비된 응답보다 요청이 많습니다: {request.full_url}"
            )
        item = self.queue.pop(0)
        return item(request) if callable(item) else item


def ok(body: bytes, headers=None, status: int = 200) -> epo_client.HttpResponse:
    return epo_client.HttpResponse(
        status=status, headers=dict(headers or fx.HEADERS_OK), body=body
    )


def token_response() -> epo_client.HttpResponse:
    return ok(fx.TOKEN_OK)


@pytest.fixture()
def store(tmp_path) -> artifacts.ArtifactStore:
    return artifacts.ArtifactStore(tmp_path / "evidence")


def make_backend(transport, store, **settings) -> epo_backend.EpoOpsBackend:
    backend = epo_backend.EpoOpsBackend(store=store)
    backend.configure(
        {
            epo_backend.SETTING_CONSUMER_KEY: TEST_KEY,
            epo_backend.SETTING_CONSUMER_SECRET: TEST_SECRET,
            **settings,
        }
    )
    backend._client = epo_client.OpsClient(
        key=TEST_KEY,
        secret=TEST_SECRET,
        ledger=backend.ledger,
        transport=transport,
        # 설정한 예산을 그대로 물려준다. 여기서 기본값을 쓰면 예산 테스트가
        # 설정과 무관한 값을 시험하게 된다.
        http_budget_seconds=backend._http_budget,
        sleep=lambda _seconds: None,
    )
    return backend


# ------------------------------------------------------------------- CQL 검증


def test_cql_builds_allowed_fields() -> None:
    query = epo_cql.Group(
        epo_cql.OP_AND,
        (
            epo_cql.Term(epo_cql.FIELD_TITLE_ABSTRACT, "robot arm"),
            epo_cql.Term(epo_cql.FIELD_IPC, "B25J 9/16"),
        ),
    )
    assert epo_cql.build(query) == '(ta all "robot arm" and ipc = "B25J 9/16")'


def test_cql_rejects_unknown_field() -> None:
    with pytest.raises(epo_cql.CqlError, match="허용되지 않은"):
        epo_cql.build(epo_cql.Term("evil", "x"))


@pytest.mark.parametrize("value", ['a" or ti="b', "wild*card", "q?mark", "back\\slash"])
def test_cql_rejects_injection_and_wildcards(value: str) -> None:
    """값을 고쳐서 통과시키지 않는다. 거절해서 사용자가 알게 한다."""
    with pytest.raises(epo_cql.CqlError, match="쓸 수 없는 문자"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_TITLE, value))


def test_cql_rejects_control_characters() -> None:
    with pytest.raises(epo_cql.CqlError, match="제어문자"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_TITLE, "robot\x00arm"))


def test_cql_enforces_size_limits() -> None:
    with pytest.raises(epo_cql.CqlError, match="자를 넘습니다"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_TITLE, "x" * 200))
    # 글자 수 상한에 먼저 걸리지 않도록 짧은 단어를 많이 쓴다.
    with pytest.raises(epo_cql.CqlError, match="단어가"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_TITLE, " ".join("w" for _ in range(15))))
    many = epo_cql.Group(
        epo_cql.OP_OR,
        tuple(epo_cql.Term(epo_cql.FIELD_TITLE, f"term{i}") for i in range(25)),
    )
    with pytest.raises(epo_cql.CqlError, match="검색항이"):
        epo_cql.build(many)


def test_cql_enforces_nesting_depth() -> None:
    node = epo_cql.Term(epo_cql.FIELD_TITLE, "deep")
    for _ in range(5):
        node = epo_cql.Group(epo_cql.OP_AND, (node, epo_cql.Term(epo_cql.FIELD_TITLE, "x")))
    with pytest.raises(epo_cql.CqlError, match="중첩"):
        epo_cql.build(node)


def test_cql_validates_classification_and_numbers() -> None:
    with pytest.raises(epo_cql.CqlError, match="분류코드 형식"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_IPC, "not a class!"))
    with pytest.raises(epo_cql.CqlError, match="문헌번호 형식"):
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_PUBLICATION_NUMBER, "12345"))
    assert (
        epo_cql.build(epo_cql.Term(epo_cql.FIELD_PUBLICATION_NUMBER, "EP1000000"))
        == 'pn = "EP1000000"'
    )


def test_cql_date_range() -> None:
    node = epo_cql.DateRange(epo_cql.FIELD_PUBLICATION_DATE, "20100101", "20201231")
    assert epo_cql.build(node) == 'pd within "20100101 20201231"'
    with pytest.raises(epo_cql.CqlError, match="YYYYMMDD"):
        epo_cql.build(epo_cql.DateRange(epo_cql.FIELD_PUBLICATION_DATE, "2010", "2020"))
    with pytest.raises(epo_cql.CqlError, match="늦습니다"):
        epo_cql.build(
            epo_cql.DateRange(epo_cql.FIELD_PUBLICATION_DATE, "20201231", "20100101")
        )


def test_cql_not_requires_two_operands() -> None:
    with pytest.raises(epo_cql.CqlError, match="not 은"):
        epo_cql.build(
            epo_cql.Group(epo_cql.OP_NOT, (epo_cql.Term(epo_cql.FIELD_TITLE, "a"),))
        )


def test_free_text_becomes_all_words_not_phrase() -> None:
    """긴 문장을 구로 검색하면 거의 늘 0건이고, 그것이 '없다'로 읽힌다."""
    term, dropped = epo_cql.from_free_text("robot arm with force feedback sensor")
    assert term.match == epo_cql.MATCH_ALL
    assert term.field == epo_cql.FIELD_TITLE_ABSTRACT
    assert dropped == ()


def test_free_text_reports_dropped_words() -> None:
    """자른 단어를 돌려준다. 안 돌려주면 검색어가 조용히 바뀐다."""
    words = [f"w{i}" for i in range(15)]
    term, dropped = epo_cql.from_free_text(" ".join(words))
    assert len(term.value.split()) == epo_cql.MAX_VALUE_WORDS
    assert dropped == tuple(words[epo_cql.MAX_VALUE_WORDS:])


# ------------------------------------------------------------------ 문헌번호


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("EP1000000A1", "EP.1000000.A1"),
        ("ep1000000a1", "EP.1000000.A1"),
        ("EP.1000000.A1", "EP.1000000.A1"),
        ("US9876543B2", "US.9876543.B2"),
        ("EP1000000", "EP.1000000"),
    ],
)
def test_doc_key_normalization(raw: str, expected: str) -> None:
    assert epo_client.normalize_doc_key(raw) == expected


@pytest.mark.parametrize("raw", ["../../etc/passwd", "EP", "", "E1000000A1", "EP/1000"])
def test_doc_key_rejects_path_traversal(raw: str) -> None:
    with pytest.raises(epo_client.OpsError):
        epo_client.normalize_doc_key(raw)


# --------------------------------------------------------------------- OAuth


def test_token_acquired_once_and_reused(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    backend.fetch_document("EP1000000A1", epo_client.CONSTITUENT_CLAIMS)
    kinds = [
        "token" if "accesstoken" in row["url"] else "call" for row in transport.requests
    ]
    assert kinds == ["token", "call", "call"]
    assert transport.requests[0]["headers"]["Authorization"].startswith("Basic ")
    assert transport.requests[1]["headers"]["Authorization"] == "Bearer FAKE-TOKEN-VALUE"


def test_token_rejected_is_not_retried(store) -> None:
    transport = FakeTransport(ok(b'{"error":"invalid_client"}', status=401))
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsAuthError):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot"))
    assert len(transport.requests) == 1


def test_expired_token_is_refreshed_once_on_401(store) -> None:
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", status=401),   # 토큰이 예상보다 먼저 죽었다
        token_response(),
        ok(fx.SEARCH_BIBLIO),
    )
    backend = make_backend(transport, store)
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert len(response.records) == 2
    assert len(transport.requests) == 4


def test_repeated_401_stops_instead_of_looping(store) -> None:
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", status=401),
        token_response(),
        ok(b"<error/>", status=401),
    )
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsAuthError):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert len(transport.requests) == 4


# ------------------------------------------------------- 비밀 비노출 / 가림


def test_secret_and_token_never_surface(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    exposed = json.dumps(backend.usage()) + json.dumps(backend.quota_state())
    assert TEST_SECRET not in exposed
    assert TEST_KEY not in exposed
    assert "FAKE-TOKEN-VALUE" not in exposed
    # repr 로도 새지 않는다.
    assert TEST_SECRET not in repr(backend._client)


def test_base64_basic_credentials_are_scrubbed() -> None:
    """중간 장비가 요청 헤더를 오류 페이지에 찍어도 base64 까지 가린다."""
    tokens = epo_client.credential_tokens(TEST_KEY, TEST_SECRET)
    basic = tokens[2]
    leaked = f"upstream rejected header Authorization: Basic {basic} for user"
    cleaned = epo_client.scrub(leaked, *tokens)
    assert basic not in cleaned
    assert TEST_KEY not in cleaned and TEST_SECRET not in cleaned


def test_error_body_is_scrubbed_before_raising(store) -> None:
    tokens = epo_client.credential_tokens(TEST_KEY, TEST_SECRET)
    body = f"denied for Basic {tokens[2]}".encode()
    transport = FakeTransport(token_response(), ok(body, status=403))
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsAuthError) as caught:
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert tokens[2] not in str(caught.value)


# ---------------------------------------------------- 재시도 / 상태 코드 구분


def test_429_honours_retry_after_and_retries_twice(store) -> None:
    slept: list[float] = []
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", headers={"Retry-After": "2"}, status=429),
        ok(b"<error/>", headers={"Retry-After": "2"}, status=429),
        ok(fx.SEARCH_BIBLIO),
    )
    backend = make_backend(transport, store)
    backend._client.sleep = slept.append
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert slept == [2.0, 2.0]
    assert len(response.records) == 2


def test_retry_stops_at_two(store) -> None:
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", status=503),
        ok(b"<error/>", status=503),
        ok(b"<error/>", status=503),
    )
    backend = make_backend(transport, store)
    backend._client.sleep = lambda _s: None
    with pytest.raises(epo_client.OpsUnavailable, match="재시도 2회"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    # 토큰 1 + 최초 1 + 재시도 2 = 4. 무한 재시도가 아니다.
    assert len(transport.requests) == 4


def test_long_retry_after_does_not_wait(store) -> None:
    transport = FakeTransport(
        token_response(), ok(b"<error/>", headers={"Retry-After": "3600"}, status=429)
    )
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsUnavailable, match="기다릴 수 없어"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))


def test_403_is_auth_error_not_retried(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.ERROR_403, status=403))
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsAuthError):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert len(transport.requests) == 2


def test_zero_results_is_not_a_failure(store) -> None:
    """0건과 호출 실패는 다른 사건이다. 섞으면 실패가 '그런 특허 없음'이 된다."""
    transport = FakeTransport(token_response(), ok(fx.SEARCH_EMPTY))
    backend = make_backend(transport, store)
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "nonexistent"))
    assert response.records == ()
    assert response.total_found == 0
    assert response.raw_artifact_id  # 0건 응답도 원본은 보존된다


class SteppingClock:
    """부를 때마다 step 초씩 흐르는 시계. 예산 소진을 결정적으로 재현한다."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_http_time_budget_is_enforced(store) -> None:
    """토큰 발급만으로 예산을 다 쓰면 검색은 시작조차 하지 않는다."""
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store, epo_http_budget_seconds=120)
    backend._client.clock = SteppingClock(200.0)
    with pytest.raises(epo_client.OpsBudgetExceeded):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    # 검색 요청은 나가지 않았다. 토큰 하나뿐이다.
    assert len(transport.requests) == 1


# ------------------------------------------------------------------ XML 파싱


def test_search_response_parses_bibliographic_data(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))

    assert response.total_found == 137          # 받은 2건이 아니라 전체 건수
    assert len(response.records) == 2
    first = response.records[0]
    assert first.doc_number == "EP1000000A1"
    assert first.title == "Articulated robot arm with force feedback"
    assert "espacenet.com" in first.source_url

    fields = first.fields
    assert fields["applicants"].value == "ACME ROBOTICS GMBH"   # 중복 제거됨
    assert fields["inventors"].value == "MUELLER, HANS"
    assert fields["publication_date"].value == "20000705"
    assert fields["application_number"].value == "EP99123456"
    assert fields["family_id"].value == "54321"
    assert "B25J" in fields["ipc"].value
    assert "force sensor" in fields["abstract:en"].value
    # 제목은 언어별로 따로 들어간다. 하나로 뭉개면 어느 언어를 인용했는지 잃는다.
    assert fields["title:de"].value.startswith("Gelenkarmroboter")


def test_claims_response_parses(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    response = backend.fetch_document("EP1000000A1", epo_client.CONSTITUENT_CLAIMS)
    claims = response.records[0].fields["claims:en"].value
    assert claims.startswith("1. A robot arm comprising a base")
    assert "six-axis load cell" in claims


def test_multilingual_field_requires_explicit_language() -> None:
    """언어를 골라 주지 않는다. 고르면 그 선택이 판정에 몰래 들어간다."""
    with pytest.raises(parsers.FieldPathMissing, match="언어를 지정"):
        epo_parser._extract(fx.SEARCH_BIBLIO, "documents/EP.1000000.A1/title")


def test_parser_rejects_entity_expansion() -> None:
    with pytest.raises(epo_parser.EpoXmlError, match="ENTITY"):
        epo_parser.read_documents(fx.BILLION_LAUGHS)


def test_parser_rejects_oversized_xml() -> None:
    with pytest.raises(epo_parser.EpoXmlError, match="상한"):
        epo_parser.read_documents(b"<a/>" + b"x" * (epo_parser.MAX_XML_BYTES + 1))


# --------------------------------------------------- 원본 보존과 증거 검증


def test_raw_response_is_preserved_before_records_exist(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    # 보존된 바이트가 응답 원본 그대로여야 한다. 파싱본이 아니다.
    assert store.read(response.raw_artifact_id) == fx.SEARCH_BIBLIO
    assert response.raw_artifact_id == artifacts.compute_id(fx.SEARCH_BIBLIO)


def test_every_field_points_at_the_artifact(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    response = backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    for record in response.records:
        for name, value in record.fields.items():
            assert value.evidence is not None and value.evidence.complete, name
            assert value.evidence.artifact_id == response.raw_artifact_id
            assert value.evidence.profile_id == epo_parser.PROFILE_EPO_OPS_XML


def test_excerpt_verified_against_preserved_bytes(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    response = backend.fetch_document("EP1000000A1", epo_client.CONSTITUENT_CLAIMS)
    record = response.records[0]

    verdict = provenance.verify_record_excerpt(
        excerpt="a force sensor arranged at the end effector",
        record=record,
        field_name="claims:en",
        store=store,
    )
    assert verdict.match_kind == provenance.MATCH_EXACT
    assert verdict.verified is True
    # EPO 라고 원문 등급이 되지는 않는다. 두 관문이 모두 닫혀 있다.
    assert verdict.original_verified is False
    assert verdict.profile_id == epo_parser.PROFILE_EPO_OPS_XML


def test_excerpt_not_in_source_is_rejected(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    record = backend.fetch_document("EP1000000A1").records[0]
    verdict = provenance.verify_record_excerpt(
        excerpt="a quantum entanglement drive",
        record=record,
        field_name="claims:en",
        store=store,
    )
    assert verdict.match_kind == provenance.MATCH_NONE
    assert verdict.verified is False


def test_adapter_value_is_not_trusted(store) -> None:
    """어댑터가 값을 바꿔치기해도 판정은 보존 바이트를 따른다."""
    from app.patent_search.base import FieldValue

    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    record = backend.fetch_document("EP1000000A1").records[0]
    forged = FieldValue(
        value="이 특허는 양자 구동기를 포함한다",
        evidence=record.fields["claims:en"].evidence,
    )
    verdict = provenance.verify_excerpt(
        excerpt="양자 구동기", field=forged, store=store
    )
    assert verdict.match_kind == provenance.MATCH_NONE


def test_raw_tier_stays_closed_even_if_policy_is_enabled(store) -> None:
    """정책을 켜도 프로필이 닫혀 있으면 원문 등급은 나오지 않는다."""
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    record = backend.fetch_document("EP1000000A1").records[0]
    verdict = provenance.verify_record_excerpt(
        excerpt="six-axis load cell",
        record=record,
        field_name="claims:en",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert verdict.match_kind == provenance.MATCH_EXACT
    assert verdict.original_verified is False
    assert "raw_capable=False" in verdict.reason


def test_epo_profile_is_not_raw_capable() -> None:
    assert epo_parser.PROFILE_EPO_OPS_XML not in parsers.raw_capable_profiles()


def test_corrupted_artifact_fails_verification(store, tmp_path) -> None:
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    response = backend.fetch_document("EP1000000A1")
    record = response.records[0]

    path = store._path(response.raw_artifact_id)
    path.write_bytes(fx.CLAIMS.replace(b"six-axis", b"nine-axis"))

    verdict = provenance.verify_record_excerpt(
        excerpt="six-axis load cell",
        record=record,
        field_name="claims:en",
        store=store,
    )
    assert verdict.match_kind == provenance.MATCH_NONE
    assert "무결성" in verdict.reason


# ------------------------------------------------------------ 상세 조회 상한


def test_detail_fetch_budget_is_enforced(store) -> None:
    transport = FakeTransport(token_response(), *[ok(fx.CLAIMS) for _ in range(4)])
    backend = make_backend(transport, store, epo_max_detail_fetches=2)
    backend.fetch_document("EP1000000A1")
    backend.fetch_document("EP1000000A1")
    with pytest.raises(epo_backend.DetailBudgetExceeded, match="2건"):
        backend.fetch_document("EP1000000A1")


def test_default_detail_budget_is_twelve() -> None:
    assert epo_backend.DEFAULT_MAX_DETAIL_FETCHES == 12


def test_constituent_allowlist() -> None:
    client = epo_client.OpsClient(key=TEST_KEY, secret=TEST_SECRET)
    with pytest.raises(epo_client.OpsError, match="허용되지 않은"):
        client.fetch("EP1000000A1", "everything")


def test_result_range_is_capped(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    backend.search_structured(
        epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"), max_results=500
    )
    assert "Range=1-20" in transport.requests[1]["url"]


# ------------------------------------------------------------------- 사용량


def test_quota_headers_are_observed(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    snapshot = backend.ledger.snapshot()
    assert snapshot["ops_weekly_bytes"] == 104857600
    assert snapshot["ops_hourly_bytes"] == 1048576
    assert snapshot["throttle"]["system_state"] == "idle"
    assert snapshot["throttle"]["dangerous"] is False
    # ARIA 로컬 카운터는 따로 센다. 두 숫자를 합치지 않는다.
    assert snapshot["local_bytes"] == len(fx.TOKEN_OK) + len(fx.SEARCH_BIBLIO)


def test_effective_usage_takes_the_larger_number() -> None:
    ledger = epo_quota.QuotaLedger(
        state=epo_quota.QuotaState(
            week=epo_quota.week_key(), local_bytes=10, ops_weekly_bytes=5000
        )
    )
    assert ledger.state.effective_weekly_bytes == 5000
    ledger.state = epo_quota.QuotaState(
        week=epo_quota.week_key(), local_bytes=9000, ops_weekly_bytes=5000
    )
    assert ledger.state.effective_weekly_bytes == 9000


def test_weekly_quota_blocks_next_call(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(
        transport,
        store,
        epo_quota_state={
            "week": epo_quota.week_key(),
            "local_bytes": epo_quota.WEEKLY_QUOTA_BYTES,
        },
    )
    with pytest.raises(epo_quota.QuotaExceeded, match="한도에 도달"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert transport.requests == []       # 예산을 태우기 전에 막는다


def test_weekly_quota_is_four_decimal_gigabytes() -> None:
    """EPO 는 "4GB" 라고만 적는다. 이진 단위로 읽으면 7.4% 넉넉해진다.

    한도를 모를 때는 작은 쪽으로 잡는다 — 틀렸을 때 일찍 멈추는 쪽이 낫다.
    """
    assert epo_quota.WEEKLY_QUOTA_BYTES == 4 * 1000 * 1000 * 1000
    assert epo_quota.WEEKLY_QUOTA_BYTES < 4 * 1024 * 1024 * 1024


def test_hourly_limit_is_observe_only_by_default(store) -> None:
    ledger = epo_quota.QuotaLedger(
        state=epo_quota.QuotaState(
            week=epo_quota.week_key(), ops_hourly_bytes=10**9
        )
    )
    ledger.check()      # 기본값 0 = 차단하지 않음
    ledger.hourly_limit = 1000
    with pytest.raises(epo_quota.QuotaExceeded, match="시간당"):
        ledger.check()


def test_dangerous_throttling_stops_the_channel(store) -> None:
    transport = FakeTransport(
        token_response(),
        ok(fx.SEARCH_BIBLIO, headers=fx.HEADERS_OVERLOADED),
        ok(fx.SEARCH_BIBLIO),
    )
    backend = make_backend(transport, store)
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    # 다음 호출은 시작되지 않는다.
    with pytest.raises(epo_quota.Throttled, match="과부하"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "gripper"))


def test_throttle_parser_keeps_raw_when_format_changes() -> None:
    reading = epo_quota.parse_throttle("something-new (search=unknown-value)")
    assert reading.raw == "something-new (search=unknown-value)"
    assert reading.services["search"] == "unknown-value"
    assert reading.dangerous is False


def test_quota_state_resets_on_new_week() -> None:
    ledger = epo_quota.QuotaLedger(
        state=epo_quota.QuotaState(week="1999-W01", local_bytes=10**9)
    )
    assert ledger.state.local_bytes == 0
    assert ledger.state.week == epo_quota.week_key()


def test_usage_separates_call_kinds(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    backend.fetch_document("EP1000000A1")
    usage = backend.usage()
    assert usage["calls_by_kind"]["token"]["count"] == 1
    assert usage["calls_by_kind"]["search"]["count"] == 1
    assert usage["calls_by_kind"]["detail"]["count"] == 1
    assert usage["detail_fetches"] == 1
    assert usage["max_detail_fetches"] == epo_backend.DEFAULT_MAX_DETAIL_FETCHES


# --------------------------------------------------------------- 상태와 배선


def test_status_is_configured_once_credentials_exist() -> None:
    backend = epo_backend.EpoOpsBackend()
    backend.configure({})
    assert backend.status().configured is False
    backend.configure(
        {
            epo_backend.SETTING_CONSUMER_KEY: TEST_KEY,
            epo_backend.SETTING_CONSUMER_SECRET: TEST_SECRET,
        }
    )
    status = backend.status()
    assert status.configured is True
    assert "raw" in status.detail or "원문" in status.detail


def test_search_without_credentials_never_touches_network() -> None:
    backend = epo_backend.EpoOpsBackend()
    backend.configure({})
    from app.patent_search.base import PatentSearchNotConfigured, PatentSearchQuery

    with pytest.raises(PatentSearchNotConfigured):
        backend.search(PatentSearchQuery(text="robot arm"))


# =====================================================================
# 외부 리뷰(2026-08-28)에서 지적된 결함들의 회귀 테스트.
#
# 다섯 건 모두 코드에서 재현을 확인한 뒤 고쳤다. 여기 있는 것은 "고쳤다"가
# 아니라 "다시 그렇게 되면 실패한다"를 보장하는 쪽이다.
# =====================================================================


# --- (3) XML 선언 차단이 앞 4KB 만 보던 문제 --------------------------------


def _padded_doctype(pad_bytes: int) -> bytes:
    """DOCTYPE 앞에 주석으로 pad_bytes 만큼 채운 XML."""
    pad = b"<!-- " + b"x" * pad_bytes + b" -->\n"
    return (
        b'<?xml version="1.0"?>\n'
        + pad
        + b'<!DOCTYPE r [<!ENTITY e "EXPANDED-PAYLOAD">]>\n<r>&e;</r>'
    )


@pytest.mark.parametrize("pad", [0, 4096, 5000, 100_000])
def test_doctype_blocked_regardless_of_offset(pad: int) -> None:
    """앞부분만 검사하면 그 길이만큼 채운 뒤 DOCTYPE 을 놓아 우회된다.

    실측으로 재현했던 결함이다 — 5KB 주석 뒤의 내부 엔티티가 그대로
    EXPANDED-PAYLOAD 로 확장됐다.
    """
    with pytest.raises(epo_parser.EpoXmlError, match="DOCTYPE 또는 ENTITY"):
        epo_parser.read_documents(_padded_doctype(pad))


def test_entity_declaration_blocked_without_doctype() -> None:
    body = b"<r>" + b"y" * 9000 + b'<!ENTITY x "boom">' + b"</r>"
    with pytest.raises(epo_parser.EpoXmlError, match="DOCTYPE 또는 ENTITY"):
        epo_parser.read_documents(body)


# --- (2) 4GB 가 실제로 하드 상한인가 ----------------------------------------


def test_quota_blocks_before_the_next_response_can_overshoot(store) -> None:
    """한도 1바이트 전에도 응답 상한만큼 더 받을 수 있으면 하드 상한이 아니다."""
    almost = epo_quota.WEEKLY_QUOTA_BYTES - 1
    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(
        transport,
        store,
        epo_quota_state={"week": epo_quota.week_key(), "local_bytes": almost},
    )
    with pytest.raises(epo_quota.QuotaExceeded):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert transport.requests == []


def test_quota_reserve_uses_the_response_ceiling() -> None:
    ledger = epo_quota.QuotaLedger(
        state=epo_quota.QuotaState(
            week=epo_quota.week_key(),
            local_bytes=epo_quota.WEEKLY_QUOTA_BYTES - epo_client.MAX_RESPONSE_BYTES,
        )
    )
    with pytest.raises(epo_quota.QuotaExceeded):
        ledger.check(reserve=epo_client.MAX_RESPONSE_BYTES)
    # 여유가 조금이라도 더 있으면 통과한다.
    ledger.state = epo_quota.QuotaState(
        week=epo_quota.week_key(),
        local_bytes=epo_quota.WEEKLY_QUOTA_BYTES
        - epo_client.MAX_RESPONSE_BYTES
        - 1,
    )
    ledger.check(reserve=epo_client.MAX_RESPONSE_BYTES)


def test_oversized_response_still_counts_bytes_and_headers(store) -> None:
    """버릴 응답이라도 바이트는 이미 내려받았고 EPO 는 그만큼 과금한다."""
    huge = b"<a>" + b"z" * (epo_client.MAX_RESPONSE_BYTES + 10) + b"</a>"
    transport = FakeTransport(token_response(), ok(huge))
    backend = make_backend(transport, store)
    with pytest.raises(epo_client.OpsError, match="상한"):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    snapshot = backend.ledger.snapshot()
    assert snapshot["local_bytes"] >= len(huge)
    # quota 헤더도 사라지지 않는다.
    assert snapshot["ops_weekly_bytes"] == 104857600


# --- (4) 재시도 대기가 예산을 소모하는가 ------------------------------------


def test_retry_wait_is_charged_to_the_time_budget(store) -> None:
    """기다린 시간을 안 깎으면 재시도를 반복하며 계약 시간을 조용히 넘긴다."""
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", headers={"Retry-After": "20"}, status=429),
        ok(b"<error/>", headers={"Retry-After": "20"}, status=429),
        ok(fx.SEARCH_BIBLIO),
    )
    backend = make_backend(transport, store, epo_http_budget_seconds=30)
    backend._client.sleep = lambda _s: None
    with pytest.raises(epo_client.OpsError):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    # 20초를 한 번 기다린 뒤 남은 예산(10초)으로는 두 번째 20초를 못 기다린다.
    assert backend._client._spent_seconds >= 20


# --- (5) 실패한 상세 조회도 예산을 쓰는가 ------------------------------------


def test_failed_detail_fetch_consumes_its_budget(store) -> None:
    """성공했을 때만 세면 빠르게 실패하는 조회가 상한을 소모하지 않는다."""
    transport = FakeTransport(
        token_response(),
        ok(b"<error/>", status=404),
        ok(b"<error/>", status=404),
        ok(fx.CLAIMS),
    )
    backend = make_backend(transport, store, epo_max_detail_fetches=2)
    for _ in range(2):
        with pytest.raises(epo_client.OpsError):
            backend.fetch_document("EP1000000A1")
    with pytest.raises(epo_backend.DetailBudgetExceeded):
        backend.fetch_document("EP1000000A1")
    assert backend.usage()["detail_fetches"] == 2


# --- (6) 번역 여부를 모른다고 정직하게 적는가 -------------------------------


def test_profile_records_translation_as_unknown() -> None:
    """False 로 적으면 확인하지 않은 사실을 확인한 것처럼 기록하게 된다."""
    profile = parsers.get_profile(epo_parser.PROFILE_EPO_OPS_XML)
    assert profile.resolved_translation_state == "unknown"
    assert profile.is_translation is False   # '번역이라고 확인됨'은 아니다


def test_unknown_translation_blocks_raw_even_with_everything_else_open(store) -> None:
    """unknown 은 no 가 아니다. 원문 등급 판정에서 yes 와 같이 막혀야 한다."""
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    record = backend.fetch_document("EP1000000A1").records[0]
    verdict = provenance.verify_record_excerpt(
        excerpt="six-axis load cell",
        record=record,
        field_name="claims:en",
        store=store,
        policy=policy.RAW_ENABLED,
    )
    assert verdict.translation_state == "unknown"
    assert verdict.original_verified is False


def test_translation_state_reaches_the_verification_record(store) -> None:
    transport = FakeTransport(token_response(), ok(fx.CLAIMS))
    backend = make_backend(transport, store)
    record = backend.fetch_document("EP1000000A1").records[0]
    verdict = provenance.verify_record_excerpt(
        excerpt="six-axis load cell",
        record=record,
        field_name="claims:en",
        store=store,
    )
    assert verdict.translation_state == "unknown"
    assert verdict.is_translation is False


# --- (7) 잘린 검색어가 기록되는가 -------------------------------------------


def test_truncated_free_text_is_reported_in_the_response(store) -> None:
    """조용히 바뀐 검색을 기록 없이 넘기지 않는다."""
    from app.patent_search.base import PatentSearchQuery

    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    long_query = " ".join(f"word{i}" for i in range(15))
    response = backend.search(PatentSearchQuery(text=long_query, max_results=5))
    assert response.notes
    assert "word10" in response.notes[0]


def test_short_free_text_has_no_notes(store) -> None:
    from app.patent_search.base import PatentSearchQuery

    transport = FakeTransport(token_response(), ok(fx.SEARCH_BIBLIO))
    backend = make_backend(transport, store)
    response = backend.search(PatentSearchQuery(text="robot arm", max_results=5))
    assert response.notes == ()


# =====================================================================
# 2차 외부 리뷰(2026-08-28)에서 지적된 하드 쿼터 우회 경로들.
# 세 건 모두 재현을 확인한 뒤 고쳤다.
# =====================================================================


def test_reservation_is_per_send_not_per_search(store) -> None:
    """토큰 응답과 검색 응답이 예약 하나를 나눠 쓰면 한도를 넘는다.

    실측으로 36바이트 초과를 재현했던 경로다. 예약 단위는 논리 검색이 아니라
    실제 HTTP 전송 하나여야 한다.
    """
    big = b"<a>" + b"z" * (epo_client.MAX_RESPONSE_BYTES - 10) + b"</a>"
    transport = FakeTransport(token_response(), ok(big))
    # 예약이 딱 하나만 통과하는 지점.
    start = epo_quota.WEEKLY_QUOTA_BYTES - epo_client.MAX_RESPONSE_BYTES - 1
    backend = make_backend(
        transport,
        store,
        epo_quota_state={"week": epo_quota.week_key(), "local_bytes": start},
    )
    with pytest.raises(epo_quota.QuotaExceeded):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))

    used = backend.ledger.state.effective_weekly_bytes
    assert used <= epo_quota.WEEKLY_QUOTA_BYTES
    # 예약이 남아 굳지 않는다.
    assert backend.ledger.reserved_bytes == 0


def test_reservation_is_released_when_transport_fails(store) -> None:
    """정산하지 않으면 쓰지도 않은 양이 한도를 차지한 채 굳는다."""

    def exploding(request, timeout):
        raise OSError("connection reset")

    backend = make_backend(exploding, store)
    with pytest.raises(epo_client.OpsUnavailable):
        backend.search_structured(epo_cql.Term(epo_cql.FIELD_TITLE, "robot arm"))
    assert backend.ledger.reserved_bytes == 0


def test_reservation_counts_toward_the_limit_while_in_flight() -> None:
    """날아가 있는 요청이 잡은 양도 한도 계산에 들어가야 한다."""
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=epo_quota.week_key()))
    room = epo_quota.WEEKLY_QUOTA_BYTES // epo_client.MAX_RESPONSE_BYTES
    for _ in range(room - 1):
        ledger.reserve(epo_client.MAX_RESPONSE_BYTES)
    assert ledger.reserved_bytes == (room - 1) * epo_client.MAX_RESPONSE_BYTES
    # 남은 자리를 다 쓰면 다음 예약은 거절된다.
    for _ in range(2):
        try:
            ledger.reserve(epo_client.MAX_RESPONSE_BYTES)
        except epo_quota.QuotaExceeded:
            break
    else:
        raise AssertionError("예약이 무한히 통과했습니다.")


def test_peek_does_not_move_the_marker() -> None:
    """저장 전에 눈금을 옮기면 저장 실패 시 증분을 다시 낼 수 없다."""
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=epo_quota.week_key()))
    ledger.record(body_bytes=123, headers={})
    first = ledger.peek_delta()
    second = ledger.peek_delta()
    assert first["local_bytes"] == second["local_bytes"] == 123
    assert ledger.pending_bytes == 123
    ledger.ack(first)
    assert ledger.pending_bytes == 0
    assert ledger.peek_delta()["local_bytes"] == 0


def test_ack_only_clears_what_was_saved() -> None:
    """저장 도중 새 응답이 오면 그 증분까지 지워 버리면 안 된다."""
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=epo_quota.week_key()))
    ledger.record(body_bytes=100, headers={})
    delta = ledger.peek_delta()
    ledger.record(body_bytes=50, headers={})   # 저장하는 사이에 하나 더 왔다
    ledger.ack(delta)
    assert ledger.pending_bytes == 50


def test_sync_from_stored_keeps_unsaved_increments() -> None:
    """저장 못 한 증분은 저장소와 맞추는 과정에서도 잃지 않는다."""
    week = epo_quota.week_key()
    ledger = epo_quota.QuotaLedger(state=epo_quota.QuotaState(week=week, local_bytes=100))
    ledger.record(body_bytes=25, headers={})     # 저장 실패했다고 치자
    assert ledger.pending_bytes == 25
    ledger.sync_from_stored(epo_quota.QuotaState(week=week, local_bytes=500))
    assert ledger.state.local_bytes == 500
    assert ledger.pending_bytes == 25
