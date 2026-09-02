"""비특허문헌 채널 — 클라이언트·파서·백엔드.

이 테스트가 지키는 계약은 하나다. **논문 후보의 근거는 보존된 응답에서 다시
뽑은 값이어야 한다.** 어댑터가 보고한 값을 그대로 믿으면 2026-09-01 실행에서
났던 사고(모델이 엉뚱한 문서를 열고 열람 성공으로 세어진 일)가 백엔드 안에서
되풀이된다.
"""

from __future__ import annotations

import json

import pytest

from app.patent_search import (
    artifacts,
    literature_backend,
    literature_client,
    literature_parser,
    parsers,
)
from app.patent_search.base import PatentSearchQuery

from . import literature_fixtures as fx


@pytest.fixture()
def store(tmp_path):
    return artifacts.ArtifactStore(tmp_path / "evidence")


def _transport(routes: dict, calls: list | None = None):
    """URL 조각 -> 응답 바이트. 맞는 것이 없으면 실패로 만든다."""

    def send(request, timeout):
        url = request.full_url
        if calls is not None:
            calls.append(url)
        for needle, (status, body) in routes.items():
            if needle in url:
                return literature_client.HttpResponse(
                    status=status, headers={}, body=body
                )
        raise AssertionError(f"예상하지 못한 요청입니다: {url}")

    return send


# --- DOI 정규화 ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "10.3390/s25103219",
        "10.3390/S25103219",
        "https://doi.org/10.3390/s25103219",
        "doi:10.3390/s25103219",
        "DOI 10.3390/s25103219.",
        "(10.3390/s25103219)",
    ],
)
def test_doi_normalizes_to_one_key(raw):
    assert literature_client.normalize_doi(raw) == fx.TARGET_DOI


@pytest.mark.parametrize("raw", ["", "US8773539B2", "EP1000000A1", "확인 필요"])
def test_patent_numbers_are_not_dois(raw):
    """특허번호를 DOI 로 통과시키면 Crossref 에 없는 값으로 조회가 나가고,
    그 404 가 '논문이 없다'로 읽힌다."""
    assert not literature_client.looks_like_doi(raw)
    with pytest.raises(literature_client.LiteratureError):
        literature_client.normalize_doi(raw)


def test_engine_operators_are_removed_from_queries():
    """모델은 구글 문법으로 검색어를 쓴다. 서지 API 는 그 문법을 모른다."""
    query = (
        '"CIS" "motion detection" ("edge detection" OR "edge") '
        "site:patents.google.com"
    )
    assert literature_client.plain_query(query) == (
        "CIS motion detection edge detection edge"
    )


# --- 파서 ------------------------------------------------------------------
def test_crossref_work_is_read_with_jats_stripped():
    work = literature_parser.read_crossref_work(fx.CROSSREF_WORK)
    assert work is not None
    assert work.doi == fx.TARGET_DOI
    assert work.title == fx.TARGET_TITLE
    # 실제 응답의 초록은 <jats:p> 로 감싸여 있다. 값에 태그가 남으면 안 된다.
    assert "<jats:" not in work.abstract
    assert work.abstract.startswith("We propose a complementary")
    # 저자는 given/family 로 쪼개져 있어 한 경로로 뽑을 수 없다. 표시용으로만
    # 쓰고 근거 필드에는 넣지 않는다.
    assert "Minkyu Song" in work.authors
    assert "authors" not in work.paths


def test_europepmc_detail_is_read_as_plain_text():
    works = literature_parser.read_europepmc_results(fx.EUROPEPMC_DETAIL)
    assert len(works) == 1
    work = works[0]
    assert work.doi == fx.TARGET_DOI
    assert work.abstract.startswith("We propose a complementary")
    assert work.paths["abstract"] == "resultList/result/0/abstractText"


def test_search_responses_rank_the_target_first():
    """두 DB 를 함께 쓰는 근거. 색인 방식이 달라 서로 다른 질의에서 잡힌다."""
    crossref = literature_parser.read_crossref_items(fx.CROSSREF_SEARCH)
    europepmc = literature_parser.read_europepmc_results(fx.EUROPEPMC_SEARCH)
    assert crossref[0].doi == fx.TARGET_DOI
    assert europepmc[0].doi == fx.TARGET_DOI


def test_records_without_doi_are_dropped():
    """DOI 가 없으면 웹 후보와 맞출 키가 없어 같은 문헌이 둘로 남는다."""
    body = json.dumps(
        {"resultList": {"result": [{"title": "제목만 있는 레코드"}]}}
    ).encode("utf-8")
    assert literature_parser.read_europepmc_results(body) == []


# --- 아티팩트 재추출 --------------------------------------------------------
def test_every_field_is_reextractable_from_the_artifact(store):
    """어댑터가 보고한 값과 아티팩트에서 다시 뽑은 값이 같아야 한다.

    이 성질이 깨지면 support_text 대조가 통째로 무의미해진다.
    """
    literature_parser.register()
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {"europepmc": (200, fx.EUROPEPMC_DETAIL)},
            )
        ),
        store=store,
    )
    response = backend.fetch_document(fx.TARGET_DOI, "abstract")
    assert len(response.records) == 1
    record = response.records[0]
    assert record.fields

    for name, value in record.fields.items():
        ref = value.evidence
        assert ref is not None and ref.complete, name
        extracted = parsers.extract(
            store.read(ref.artifact_id), ref.field_path, ref.profile_id
        )
        assert extracted.text == value.value, name
        # 이 경로로는 원문 등급이 나오지 않는다. 발행사 메타데이터이지 논문
        # 원문이 아니다.
        assert extracted.raw_capable is False


# --- 백엔드 --------------------------------------------------------------
def test_abstract_falls_back_to_crossref_when_not_in_europepmc(store):
    """Europe PMC 는 생의학 색인이라 범위 밖 논문이 많다. 그때 Crossref 로 넘어간다."""
    calls: list = []
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {
                    "europepmc": (200, fx.EUROPEPMC_EMPTY),
                    "api.crossref.org": (200, fx.CROSSREF_WORK),
                },
                calls,
            )
        ),
        store=store,
    )
    response = backend.fetch_document(fx.TARGET_DOI, "abstract")
    assert len(response.records) == 1
    assert response.records[0].doc_number == fx.TARGET_DOI
    assert "abstract" in response.records[0].fields
    assert len(calls) == 2  # EPMC 먼저, 그 다음 Crossref


def test_a_different_document_is_never_used_as_evidence(store):
    """요청한 DOI 와 다른 문헌이 오면 근거로 쓰지 않는다.

    2026-09-01 실행에서 모델이 신경섬유종증 논문을 열고도 '열람 성공 1건'으로
    세어졌다. 같은 사고를 백엔드에서 막는다.
    """
    other = json.dumps(
        {
            "resultList": {
                "result": [
                    {
                        "doi": "10.1000/other",
                        "title": "전혀 다른 논문",
                        "abstractText": "무관한 초록",
                    }
                ]
            }
        }
    ).encode("utf-8")
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {
                    "europepmc": (200, other),
                    "api.crossref.org": (200, fx.CROSSREF_EMPTY),
                }
            )
        ),
        store=store,
    )
    response = backend.fetch_document(fx.TARGET_DOI, "abstract")
    assert response.records == ()
    assert any("DOI 가 요청한 값과 다릅니다" in note for note in response.notes)


def test_search_keeps_both_databases_hits_for_the_same_doi(store):
    """두 DB 가 같은 문헌을 찾은 사실을 접지 않는다.

    이것이 후보 순위의 교차 확인 신호다. 여기서 하나로 접으면 신호가 언제나
    1 이 되어 아무 일도 하지 않는다(2026-09-01 실측에서 실제로 그랬다).
    한 DB 안에서의 중복은 그대로 없앤다.
    """
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {
                    "api.crossref.org": (200, fx.CROSSREF_SEARCH),
                    "europepmc": (200, fx.EUROPEPMC_SEARCH),
                }
            )
        ),
        store=store,
    )
    response = backend.search(PatentSearchQuery(text="edge detection", max_results=5))
    numbers = [record.doc_number for record in response.records]
    assert numbers.count(fx.TARGET_DOI) == 2
    sources = {
        record.fields["title"].evidence.profile_id
        for record in response.records
        if record.doc_number == fx.TARGET_DOI
    }
    assert len(sources) == 2


def test_one_database_failing_does_not_lose_the_other(store):
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {
                    "api.crossref.org": (500, b"boom"),
                    "europepmc": (200, fx.EUROPEPMC_SEARCH),
                }
            )
        ),
        store=store,
    )
    response = backend.search(PatentSearchQuery(text="edge detection", max_results=5))
    assert any(record.doc_number == fx.TARGET_DOI for record in response.records)
    assert any("crossref 검색 실패" in note for note in response.notes)


def test_raw_response_is_preserved_before_parsing(store):
    """파싱에 실패해도 원본은 남아 있어야 한다. 근거 없는 후보를 만들지 않는다."""
    backend = literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(
            transport=_transport(
                {
                    "api.crossref.org": (200, b"{not json"),
                    "europepmc": (200, fx.EUROPEPMC_EMPTY),
                }
            )
        ),
        store=store,
    )
    response = backend.search(PatentSearchQuery(text="edge detection"))
    assert response.records == ()
    assert response.raw_artifact_id
    assert store.read(response.raw_artifact_id) == b"{not json"


def test_network_time_budget_stops_further_calls():
    """호출 수가 아니라 **네트워크 시간**으로 막는다. 느린 응답 하나가 실행
    전체를 잡아 두는 것을 호출 수 상한으로는 막을 수 없다."""
    import time

    def slow(request, timeout):
        time.sleep(0.02)
        return literature_client.HttpResponse(
            status=200, headers={}, body=fx.CROSSREF_EMPTY
        )

    client = literature_client.LiteratureClient(
        http_budget_seconds=0.01, transport=slow
    )
    client.search_crossref("first")
    assert client.usage()["http_seconds"] >= 0.01
    with pytest.raises(literature_client.LiteratureBudgetExceeded):
        client.search_crossref("second")
