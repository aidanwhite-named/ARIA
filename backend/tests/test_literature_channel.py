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
    base,
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


# --- 페이지 근거가 있는 후보를 먼저 검증한다 ------------------------------
#
# 2026-09-02 실행에서 무너진 지점이다. 서지 검색이 데려온 관련성 미판정 후보
# 8건이 상한을 통째로 가져가, 웹에서 페이지까지 열고 대응 행 4개를 얻은 잠정 B
# 후보(arXiv:2409.17106)가 공식 검증 대상에서 빠졌다. 그리고 그 8건 중 4건은
# 초록이 없어 실패했는데도 자리가 넘어가지 않았다.
def _page_backed(index: int, doi: str, *, group: str = "B", rows: int = 4) -> dict:
    """페이지를 열어 본문 근거로 잠정 A/B 를 받은 웹 후보."""
    candidate = _paper_candidate(index, doi)
    candidate.update(
        {
            "provisional_group": group,
            "classification_basis": search_manifest.CLASSIFICATION_SEARCH,
            "page_fetch_succeeded": True,
            "page_supported_rows": rows,
            "evidence_status": search_manifest.EVIDENCE_REVIEWED,
        }
    )
    return candidate


def test_page_backed_paper_outranks_plain_bibliographic_candidates():
    """일반 서지 후보가 배열 앞에 있어도 페이지 근거가 있는 후보를 먼저 고른다."""
    reported = {
        "candidates": [
            _paper_candidate(
                index, f"10.1000/noise-{index}",
                origins=[search_manifest.DISCOVERY_LITERATURE],
            )
            for index in range(1, 9)
        ]
        + [_page_backed(9, fx.TARGET_DOI)]
    }
    order: list = []
    found = search_verification.literature_targets(reported, limit=8, order=order)
    assert found[0].doc_key == fx.TARGET_DOI
    assert order[0]["selection_reason"] == search_verification.SELECT_PAGE_EVIDENCE
    # 상한이 8이어도 자리를 받는다. 예전에는 9번째로 밀려 잘렸다.
    assert fx.TARGET_DOI in {target.doc_key for target in found[:8]}


def test_page_backed_priority_applies_to_a_and_b_alike():
    """B 만 특별 취급하지 않는다. 같은 근거를 가진 A 도 같은 자리를 받는다."""
    for group in ("A", "B"):
        reported = {
            "candidates": [
                _paper_candidate(
                    1, "10.1000/noise",
                    origins=[search_manifest.DISCOVERY_LITERATURE],
                ),
                _page_backed(2, fx.TARGET_DOI, group=group),
            ]
        }
        found = search_verification.literature_targets(reported, limit=1)
        assert [target.doc_key for target in found] == [fx.TARGET_DOI], group


def test_snippet_only_provisional_group_gets_no_special_priority():
    """잠정 분류만 있고 페이지를 열지 못한 후보는 우선순위를 받지 않는다.

    검색 스니펫만 보고 적은 등급이 페이지 본문 대조와 같은 자리를 받으면, 이
    정책은 "모델이 A/B 라고 적었는가"를 다시 신뢰하는 것이 된다.
    """
    unopened = _page_backed(1, "10.1000/snippet-only")
    unopened["page_fetch_succeeded"] = False
    no_rows = _page_backed(2, "10.1000/no-rows")
    no_rows["page_supported_rows"] = 0
    reported = {
        "candidates": [
            unopened,
            no_rows,
            _paper_candidate(
                3, fx.TARGET_DOI, origins=[search_manifest.DISCOVERY_LITERATURE]
            ),
        ]
    }
    order: list = []
    found = search_verification.literature_targets(reported, limit=1, order=order)
    assert [target.doc_key for target in found] == [fx.TARGET_DOI]
    assert order[0]["selection_reason"] == (
        search_verification.SELECT_LITERATURE_DISCOVERY
    )


def test_same_rank_keeps_candidate_order():
    """같은 순위 안에서는 후보 목록 순서를 그대로 지킨다(안정 정렬)."""
    reported = {
        "candidates": [
            _page_backed(1, "10.1000/page-first"),
            _page_backed(2, "10.1000/page-second"),
            _paper_candidate(
                3, "10.1000/lit", origins=[search_manifest.DISCOVERY_LITERATURE]
            ),
            _paper_candidate(4, "10.1000/plain"),
        ]
    }
    found = search_verification.literature_targets(reported, limit=4)
    assert [target.doc_key for target in found] == [
        "10.1000/page-first",
        "10.1000/page-second",
        "10.1000/lit",
        "10.1000/plain",
    ]


# --- 조회 실패 시 다음 후보로 이월 ----------------------------------------
class _StubBackend:
    """DOI 별로 성공·실패를 정해 두는 서지 백엔드.

    실제 백엔드로는 "이 후보만 초록이 없다"를 한 실행 안에서 만들기 어렵다.
    여기서 고정하려는 것은 파서가 아니라 **선택·이월 정책**이므로, 호출 횟수와
    HTTP 예산을 셀 수 있는 최소 스텁을 쓴다.
    """

    def __init__(self, abstracts: dict, *, http_budget: float = 0.0):
        self.abstracts = abstracts
        self.calls: list[tuple[str, str]] = []
        self.http_seconds = 0.0
        self.http_budget = http_budget

    def usage(self) -> dict:
        return {
            "detail_fetches": len(self.calls),
            "http_seconds": self.http_seconds,
            "http_budget_seconds": self.http_budget,
        }

    def fetch_document(self, doi, constituent="abstract", *, agent_budget=True):
        self.calls.append((doi, constituent))
        self.http_seconds += 1.0
        abstract = self.abstracts.get(doi)
        fields = {
            "title": base.FieldValue(
                value=f"{doi} 제목",
                evidence=base.EvidenceRef(
                    artifact_id="a" * 64, field_path="title", profile_id="crossref"
                ),
            )
        }
        if abstract and constituent == "abstract":
            fields["abstract"] = base.FieldValue(
                value=abstract,
                evidence=base.EvidenceRef(
                    artifact_id="b" * 64,
                    field_path="abstract",
                    profile_id="crossref",
                ),
            )
        return base.PatentSearchResponse(
            records=(base.PatentRecord(doc_number=doi, title="", fields=fields),),
            total_found=1,
            raw_artifact_id="c" * 64,
            fetched_at="2026-09-02T00:00:00+00:00",
            http_status=200,
            request_url=f"https://example.invalid/{doi}",
        )


def _shortlist(*dois: str) -> list:
    """앞 두 건이 정규 선택, 나머지가 예비인 대상 목록."""
    return [
        search_verification.Target(
            index=position + 1,
            doc_number=doi,
            doc_key=doi,
            selection_reason=search_verification.SELECT_LITERATURE_DISCOVERY,
            selection_role=(
                search_verification.ROLE_PRIMARY
                if position < 2
                else search_verification.ROLE_BACKFILL
            ),
        )
        for position, doi in enumerate(dois)
    ]


def test_failed_abstract_hands_the_slot_to_the_next_candidate():
    """앞 후보가 초록을 못 얻으면 뒤 후보가 그 자리에서 검증된다."""
    backend = _StubBackend({"10.1000/c": "초록 본문", "10.1000/b": "초록 본문"})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=12, verification_targets=2
    )
    assert bundles["10.1000/a"].status == search_verification.STATUS_FETCH_FAILED
    assert bundles["10.1000/b"].verified
    # 예비였던 c 가 a 의 실패로 자리를 받아 실제로 검증됐다.
    assert bundles["10.1000/c"].verified
    assert bundles["10.1000/c"].selection_role == search_verification.ROLE_BACKFILL


def test_backfill_records_whose_failure_freed_the_slot():
    """실패는 fetch_failed 로 보존하고, 대체 후보에는 누구의 자리인지 적는다."""
    backend = _StubBackend({"10.1000/c": "초록 본문"})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    reported = {
        "candidates": [
            _paper_candidate(1, "10.1000/a"),
            _paper_candidate(2, "10.1000/b"),
            _paper_candidate(3, "10.1000/c"),
        ]
    }
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=12, verification_targets=2
    )
    updated = search_verification.annotate_bundles(reported, bundles)
    first, second, third = updated["candidates"]
    # 실패한 후보를 지우지 않는다. 상태와 사유가 그대로 남아 미검증 참고 후보로
    # 인쇄된다.
    assert first["verification"]["status"] == search_manifest.VERIFY_FETCH_FAILED
    assert first["verification"]["detail"]
    assert second["verification"]["status"] == search_manifest.VERIFY_FETCH_FAILED
    assert third["verification"]["status"] == search_manifest.VERIFY_RECORD_FETCHED
    assert third["verification"]["selection_role"] == (
        search_verification.ROLE_BACKFILL
    )
    # 첫 번째 이월은 첫 번째 실패로 빈 자리를 받은 것이다.
    assert third["verification"]["backfill_for"] == "10.1000/a"
    assert "10.1000/a" in third["verification"]["detail"]
    assert third["official_evidence"]["backfill_for"] == "10.1000/a"


def test_backfill_stops_once_the_goal_is_met():
    """목표를 채우면 남은 예비는 부르지 않는다. 미시도는 실패가 아니다."""
    backend = _StubBackend(
        {"10.1000/a": "초록", "10.1000/b": "초록", "10.1000/c": "초록"}
    )
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=12, verification_targets=2
    )
    untried = bundles["10.1000/c"]
    assert untried.status == search_verification.STATUS_NOT_ATTEMPTED
    assert untried.reason_code == "literature_verification_goal_met"
    assert "10.1000/c" not in {doi for doi, _ in backend.calls}


def test_backfill_never_walks_past_the_shortlist():
    """shortlist 밖은 없다. 전부 실패해도 목록을 넘어 조회하지 않는다."""
    backend = _StubBackend({})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=99, verification_targets=2
    )
    assert len(bundles) == 3
    assert {doi for doi, _ in backend.calls} == {
        "10.1000/a",
        "10.1000/b",
        "10.1000/c",
    }
    assert all(
        bundle.status == search_verification.STATUS_FETCH_FAILED
        for bundle in bundles.values()
    )


def test_backfill_respects_the_explicit_fetch_budget():
    """호출 예산이 끝나면 이월도 끝난다. 남은 후보는 미시도로 적는다."""
    backend = _StubBackend({"10.1000/d": "초록"})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c", "10.1000/d")
    # 앞 세 건이 예산을 다 쓴다. 초록을 가진 d 가 뒤에 있어도 부르지 않는다.
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=3, verification_targets=2
    )
    assert len(backend.calls) == 3
    last = bundles["10.1000/d"]
    assert last.status == search_verification.STATUS_NOT_ATTEMPTED
    assert last.reason_code == "literature_fetch_budget_exhausted"


def test_backfill_stops_when_the_http_time_budget_is_spent():
    """HTTP 시간 예산이 바닥나면 부르지 않는다. 실패인 줄 알면서 부르지 않는다."""
    backend = _StubBackend({}, http_budget=2.0)
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=99, verification_targets=3
    )
    assert len(backend.calls) == 2
    stopped = bundles["10.1000/c"]
    assert stopped.status == search_verification.STATUS_NOT_ATTEMPTED
    assert stopped.reason_code == "literature_http_budget_exhausted"


def test_shortlist_limit_caps_what_can_be_attempted():
    """예비는 shortlist 상한까지만 만든다. 그 밖은 상한 제외로 기록한다."""
    reported = {
        "candidates": [
            _paper_candidate(index, f"10.1000/p-{index}")
            for index in range(1, 7)
        ]
    }
    dropped: list = []
    found = search_verification.literature_targets(
        reported, limit=2, shortlist_limit=4, dropped=dropped
    )
    assert len(found) == 4
    roles = [target.selection_role for target in found]
    assert roles == [
        search_verification.ROLE_PRIMARY,
        search_verification.ROLE_PRIMARY,
        search_verification.ROLE_BACKFILL,
        search_verification.ROLE_BACKFILL,
    ]
    assert [row["doc_number"] for row in dropped] == [
        "10.1000/p-5",
        "10.1000/p-6",
    ]
    assert dropped[0]["reason_code"] == "literature_verification_target_limit"


def test_a_shortlisted_out_candidate_says_which_cap_dropped_it():
    """상한 제외와 미시도는 다른 말이다. 후보마다 자기 사유를 받는다."""
    reported = {
        "candidates": [
            _paper_candidate(1, "10.1000/a"),
            _paper_candidate(2, "10.1000/b"),
        ]
    }
    dropped: list = []
    found = search_verification.literature_targets(
        reported, limit=1, shortlist_limit=1, dropped=dropped
    )
    backend = _StubBackend({"10.1000/a": "초록"})
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=4, verification_targets=1
    )
    updated = search_verification.annotate_bundles(reported, bundles, dropped)
    excluded = updated["candidates"][1]["verification"]
    assert excluded["status"] == search_manifest.VERIFY_NOT_ATTEMPTED
    assert excluded["reason_code"] == "literature_verification_target_limit"
    # 특허 채널의 사유로 설명하지 않는다. 이 후보를 자른 것은 논문 상한이다.
    assert "EPO OPS" not in excluded["detail"]
    assert "논문 후보 상한" in excluded["detail"]


def test_the_shortlist_wins_when_the_goal_is_set_higher():
    """확보 목표가 shortlist 보다 크게 설정돼도 시도 상한을 넘지 않는다."""
    reported = {
        "candidates": [
            _paper_candidate(index, f"10.1000/p-{index}") for index in range(1, 8)
        ]
    }
    found = search_verification.literature_targets(
        reported, limit=6, shortlist_limit=3
    )
    assert len(found) == 3
    # 예비가 없다. 세 건 전부가 정규 선택이고, 그 밖은 부르지 않는다.
    assert all(
        target.selection_role == search_verification.ROLE_PRIMARY
        for target in found
    )
    backend = _StubBackend({})
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=99, verification_targets=6
    )
    assert len(backend.calls) == 3
    assert len(bundles) == 3


# --- 통계 -----------------------------------------------------------------
def test_summary_separates_selected_attempted_and_verified():
    """'대상 8건'이 고른 8건인지 부른 8건인지 알 수 있어야 한다."""
    backend = _StubBackend({"10.1000/c": "초록"})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c", "10.1000/d")
    order = [
        {"position": position + 1, "doc_number": target.doc_key}
        for position, target in enumerate(found)
    ]
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=12, verification_targets=2
    )
    summary = search_verification.literature_verification_summary(
        found,
        bundles,
        verification_targets=2,
        shortlist_limit=4,
        max_fetches=12,
        order=order,
    )
    assert summary["selected"] == 2
    assert summary["shortlisted"] == 4
    # a·b 가 실패해 c 를 불렀고, c 로 목표를 채우지 못해 d 까지 불렀다.
    assert summary["attempted"] == 4
    assert summary["verified"] == 1
    assert summary["fetch_failed"] == 3
    assert summary["backfill_attempted"] == 2
    assert summary["backfill_verified"] == 1
    assert summary["not_attempted"] == 0
    assert summary["limits"]["verification_targets"] == 2
    # 계획만이 아니라 실제 결과가 감사 기록에 붙는다.
    assert order[2]["attempted"] is True
    assert order[2]["outcome"] == search_verification.STATUS_VERIFIED
    assert order[2]["backfill_for"] == "10.1000/a"


def test_summary_counts_untried_reserves_as_not_attempted():
    backend = _StubBackend({"10.1000/a": "초록", "10.1000/b": "초록"})
    found = _shortlist("10.1000/a", "10.1000/b", "10.1000/c")
    bundles = search_verification.fetch_literature(
        found, backend, max_fetches=12, verification_targets=2
    )
    summary = search_verification.literature_verification_summary(
        found, bundles, verification_targets=2, shortlist_limit=3, max_fetches=12
    )
    assert (summary["attempted"], summary["verified"]) == (2, 2)
    assert summary["not_attempted"] == 1
    assert summary["backfill_attempted"] == 0


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
