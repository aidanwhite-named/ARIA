"""논문 후보가 검증 대상이 되고, 서지 검색이 새 후보를 데려오는가.

2026-09-01 실행에서 무너진 두 지점을 고정한다.

1. 논문 후보(DOI)는 :func:`search_verification.targets` 가 EPO 번호로 정규화하지
   못해 **조용히** 빠졌다. 그래서 공식 조회를 한 번도 시도하지 않았고, 웹에서
   발행사 사이트가 403 을 돌려주자 끝까지 "미확인 검색 단서"로 남았다.
2. 웹 검색이 문헌을 식별하지 못해도 ARIA 가 같은 검색어로 서지 DB 에 다시 물으면
   제목과 DOI 가 붙은 후보를 만들 수 있다.
"""

from __future__ import annotations

import json

from app import search_manifest, search_verification
from app.execution.runner import _literature_queries
from app.patent_search import (
    artifacts,
    literature_backend,
    literature_client,
)

from . import literature_fixtures as fx


def _paper_candidate(index: int, doi: str, *, origins=None) -> dict:
    return {
        "index": index,
        "doc_number": doi,
        "doi": doi,
        "group": None,
        "provisional_group": None,
        "classification_basis": search_manifest.CLASSIFICATION_NONE,
        "group_eligible": False,
        "provisional": True,
        "evidence_status": search_manifest.EVIDENCE_CANDIDATE,
        "official_evidence": {},
        "mapping": [],
        "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
        "origin_groups": {},
        "origin_provisional_groups": {},
        "discovery_origins": list(origins or [search_manifest.DISCOVERY_WEB]),
    }


def _transport(routes: dict):
    def send(request, timeout):
        for needle, (status, body) in routes.items():
            if needle in request.full_url:
                return literature_client.HttpResponse(
                    status=status, headers={}, body=body
                )
        raise AssertionError(f"예상하지 못한 요청입니다: {request.full_url}")

    return send


def _backend(routes: dict, tmp_path):
    return literature_backend.LiteratureBackend(
        client=literature_client.LiteratureClient(transport=_transport(routes)),
        store=artifacts.ArtifactStore(tmp_path / "evidence"),
    )


# --- 대상 선택 -------------------------------------------------------------
def test_epo_targets_still_drop_dois():
    """특허 채널의 규칙은 바뀌지 않았다. DOI 는 여전히 EPO 대상이 아니다."""
    reported = {"candidates": [_paper_candidate(1, fx.TARGET_DOI)]}
    assert search_verification.targets(reported) == []


def test_literature_targets_pick_up_what_epo_drops():
    reported = {
        "candidates": [
            _paper_candidate(1, fx.TARGET_DOI),
            {**_paper_candidate(2, ""), "doc_number": "EP1000000A1", "doi": ""},
        ]
    }
    found = search_verification.literature_targets(reported)
    assert [target.doc_key for target in found] == [fx.TARGET_DOI]


def test_literature_discovery_candidates_are_picked_first():
    """서지 검색이 데려온 후보는 이 채널이 유일하게 만든 것이라 먼저 고른다."""
    reported = {
        "candidates": [
            _paper_candidate(1, "10.1000/web-first"),
            _paper_candidate(
                2, fx.TARGET_DOI, origins=[search_manifest.DISCOVERY_LITERATURE]
            ),
        ]
    }
    order: list = []
    found = search_verification.literature_targets(reported, limit=1, order=order)
    assert [target.doc_key for target in found] == [fx.TARGET_DOI]
    assert order[0]["selection_reason"] == (
        search_verification.SELECT_LITERATURE_DISCOVERY
    )


def test_target_limit_records_what_it_dropped(tmp_path):
    reported = {
        "candidates": [
            _paper_candidate(1, "10.1000/first"),
            _paper_candidate(2, "10.1000/second"),
        ]
    }
    dropped: list = []
    found = search_verification.literature_targets(
        reported, limit=1, dropped=dropped
    )
    assert len(found) == 1
    assert dropped[0]["doc_number"] == "10.1000/second"
    assert dropped[0]["reason_code"] == "literature_verification_target_limit"


# --- 근거 확보 -------------------------------------------------------------
def test_fetch_literature_builds_a_verified_bundle(tmp_path):
    """웹에서 403 으로 막힌 문헌의 초록을 발행사 사이트 없이 확보한다."""
    backend = _backend({"europepmc": (200, fx.EUROPEPMC_DETAIL)}, tmp_path)
    found = [
        search_verification.Target(
            index=1, doc_number=fx.TARGET_DOI, doc_key=fx.TARGET_DOI
        )
    ]
    bundles = search_verification.fetch_literature(found, backend, max_fetches=4)
    bundle = bundles[fx.TARGET_DOI]
    assert bundle.verified
    assert bundle.backend_id == search_manifest.LITERATURE_BACKEND_ID
    assert "abstract" in bundle.texts
    assert bundle.artifact_ids
    # 초록 응답이 제목까지 함께 주므로 서지를 다시 받지 않는다.
    assert len(bundle.calls) == 1


def test_fetch_literature_marks_a_missing_record_as_failure(tmp_path):
    backend = _backend(
        {
            "europepmc": (200, fx.EUROPEPMC_EMPTY),
            "api.crossref.org": (404, b'{"status":"error"}'),
        },
        tmp_path,
    )
    found = [
        search_verification.Target(
            index=1, doc_number="10.1000/missing", doc_key="10.1000/missing"
        )
    ]
    bundles = search_verification.fetch_literature(found, backend, max_fetches=4)
    bundle = bundles["10.1000/missing"]
    assert not bundle.verified
    assert bundle.status == search_verification.STATUS_FETCH_FAILED
    assert bundle.reason


def test_bibliography_is_kept_when_the_publisher_deposits_no_abstract(tmp_path):
    """IEEE·Elsevier 는 초록을 등록하지 않는 일이 흔하다(2026-09-01 실측: 대상
    8건 중 7건). 그때도 제목·저널은 공식 응답에서 대조된 값이므로 버리지 않는다.

    다만 기술 내용을 대조할 근거는 없으므로 verified 는 거짓이다. 두 사실을 한
    값으로 뭉개면 "제목만 확인된 문헌"이 "초록까지 확인된 문헌"과 같아진다.
    """
    no_abstract = json.dumps(
        {
            "message": {
                "DOI": "10.1109/icce.2003.1218908",
                "title": ["A CMOS image sensor (CIS) with low power motion detection"],
                "container-title": ["ICCE"],
            }
        }
    ).encode("utf-8")
    backend = _backend(
        {
            "europepmc": (200, fx.EUROPEPMC_EMPTY),
            "api.crossref.org": (200, no_abstract),
        },
        tmp_path,
    )
    found = [
        search_verification.Target(
            index=1,
            doc_number="10.1109/icce.2003.1218908",
            doc_key="10.1109/icce.2003.1218908",
        )
    ]
    bundles = search_verification.fetch_literature(found, backend, max_fetches=4)
    bundle = bundles["10.1109/icce.2003.1218908"]
    assert not bundle.verified
    assert "title" in bundle.texts
    assert "abstract" not in bundle.texts
    assert "초록이 등록되어 있지 않아" in bundle.reason


def test_fetch_budget_leaves_the_rest_not_attempted(tmp_path):
    backend = _backend({"europepmc": (200, fx.EUROPEPMC_DETAIL)}, tmp_path)
    found = [
        search_verification.Target(index=1, doc_number=fx.TARGET_DOI,
                                   doc_key=fx.TARGET_DOI),
        search_verification.Target(index=2, doc_number="10.1000/second",
                                   doc_key="10.1000/second"),
    ]
    bundles = search_verification.fetch_literature(found, backend, max_fetches=1)
    assert bundles[fx.TARGET_DOI].verified
    second = bundles["10.1000/second"]
    assert second.status == search_verification.STATUS_NOT_ATTEMPTED
    # 조회하지 못한 것을 실패로 적으면 "없는 문헌"과 "안 본 문헌"이 같아진다.
    assert "상한" in second.reason


def test_annotated_candidate_reports_the_literature_backend(tmp_path):
    backend = _backend({"europepmc": (200, fx.EUROPEPMC_DETAIL)}, tmp_path)
    reported = {"candidates": [_paper_candidate(1, fx.TARGET_DOI)]}
    found = search_verification.literature_targets(reported)
    bundles = search_verification.fetch_literature(found, backend, max_fetches=4)
    updated = search_verification.annotate_bundles(reported, bundles)
    verification = updated["candidates"][0]["verification"]
    assert verification["status"] == search_manifest.VERIFY_RECORD_FETCHED
    assert verification["backend_id"] == search_manifest.LITERATURE_BACKEND_ID
    assert verification["artifact_ids"]


def test_unfetched_paper_is_not_blamed_on_the_patent_channel():
    """DOI 후보에게 'EPO OPS 번호가 아니다'라고 적으면 사유가 거짓이 된다."""
    reported = {"candidates": [_paper_candidate(1, fx.TARGET_DOI)]}
    updated = search_verification.annotate_bundles(reported, {})
    verification = updated["candidates"][0]["verification"]
    assert verification["backend_id"] == search_manifest.LITERATURE_BACKEND_ID
    assert "EPO OPS" not in verification["detail"]


# --- 후보 병합 -------------------------------------------------------------
def _discovery(doi: str, title: str = "논문 제목") -> dict:
    return {
        "candidates": [
            {
                "doi": doi,
                "doc_number": doi,
                "title": title,
                "authors": "저자",
                "container": "Sensors",
                "url": f"https://doi.org/{doi}",
                "artifact_ids": ["a" * 64],
                "evidence_fields": ["abstract", "title"],
                "sources": ["crossref"],
                "queries": ["edge detection image sensor"],
                "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
            }
        ]
    }


def test_merge_adds_an_identified_paper_the_web_never_named():
    reported = {"candidates": []}
    merged, notes = search_manifest.merge_literature_discoveries(
        reported, _discovery(fx.TARGET_DOI, fx.TARGET_TITLE)
    )
    candidate = merged["candidates"][0]
    assert candidate["doi"] == fx.TARGET_DOI
    assert candidate["title"] == fx.TARGET_TITLE
    assert candidate["doc_type"] == "paper"
    assert candidate["discovery_origins"] == [search_manifest.DISCOVERY_LITERATURE]
    # 열어 본 웹 페이지가 없다. 없는 관측을 지어내지 않는다.
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["group"] is None
    assert notes


def test_merge_keeps_one_candidate_when_both_channels_found_it():
    reported = {"candidates": [_paper_candidate(1, fx.TARGET_DOI)]}
    merged, _ = search_manifest.merge_literature_discoveries(
        reported, _discovery(fx.TARGET_DOI, fx.TARGET_TITLE)
    )
    assert len(merged["candidates"]) == 1
    candidate = merged["candidates"][0]
    assert candidate["discovery_origins"] == [
        search_manifest.DISCOVERY_WEB,
        search_manifest.DISCOVERY_LITERATURE,
    ]
    # 웹 후보가 제목을 못 적었으면 ARIA 가 받은 제목으로 빈 칸만 채운다.
    assert candidate["title"] == fx.TARGET_TITLE


def test_merge_does_not_join_different_dois():
    reported = {"candidates": [_paper_candidate(1, "10.3390/s20133649")]}
    merged, _ = search_manifest.merge_literature_discoveries(
        reported, _discovery(fx.TARGET_DOI)
    )
    assert len(merged["candidates"]) == 2


# --- 질의 선택 -------------------------------------------------------------
def test_queries_come_from_what_aria_observed():
    observed = {
        "search_queries_by_origin": {
            "claim_only": [
                '"CIS" "motion detection" "edge detection" "image sensor"',
                # 같은 뜻인데 따옴표만 다르다. 다시 묻지 않는다.
                'CIS "motion detection" edge detection image sensor',
                # 문헌번호 확인용 질의. 서지 DB 에서는 의미가 없다.
                '"US8773539" "Google Patents"',
            ]
        }
    }
    chosen = _literature_queries(observed, limit=6)
    assert len(chosen) == 1
    assert chosen[0]["search_origins"] == ["claim_only"]


def test_query_limit_is_respected():
    observed = {
        "search_queries": [f"low power image sensor variant {n}" for n in range(20)]
    }
    assert len(_literature_queries(observed, limit=3)) == 3


def test_no_observed_queries_means_no_search():
    assert _literature_queries({}, limit=6) == []
