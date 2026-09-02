"""Codex 후보의 공식 문헌 2차 검증과 후보별 감사 상태."""

from __future__ import annotations

from types import SimpleNamespace

from app import search_manifest, search_verification
from app.patent_search import epo_client
from app.patent_search.base import PatentSearchError


def _candidate(index: int, number: str, group: str = "A") -> dict:
    return {
        "index": index,
        "doc_number": number,
        "doi": "",
        "group": None,
        "provisional_group": group,
        "classification_basis": search_manifest.CLASSIFICATION_SEARCH,
        "group_eligible": False,
        "provisional": True,
        "evidence_status": search_manifest.EVIDENCE_CANDIDATE,
        "official_evidence": {},
        "mapping": [],
        "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
        "origin_groups": {search_manifest.ORIGIN_CLAIM_ONLY: None},
        "origin_provisional_groups": {
            search_manifest.ORIGIN_CLAIM_ONLY: group
        },
    }


def _page_candidate(index: int, number: str, group: str = "A") -> dict:
    """1차에서 페이지 관측 근거로 **정식** 분류를 받은 후보.

    agy·Claude 실행에서만 나올 수 있는 모양이다. Codex 후보는 웹 게이트를
    통과할 수 없어 언제나 잠정으로 도착한다.
    """
    return {
        "index": index,
        "doc_number": number,
        "doi": "",
        "group": group,
        "provisional_group": None,
        "classification_basis": search_manifest.CLASSIFICATION_PAGE,
        "group_eligible": True,
        "provisional": False,
        "evidence_status": search_manifest.EVIDENCE_REVIEWED,
        "identifier_url_matched": True,
        "page_fetch_succeeded": True,
        "page_supported_rows": 2,
        "url": f"https://patents.example.test/{number}",
        "official_evidence": {},
        "mapping": [
            {
                "feature": "레이더 탐지부",
                "page_supported": True,
                "support_source": search_manifest.SUPPORT_PAGE,
                "degree": "강한 대응",
            },
            {
                "feature": "EO/IR 융합 처리",
                "page_supported": True,
                "support_source": search_manifest.SUPPORT_PAGE,
                "degree": "부분 대응",
            },
        ],
        "search_origins": [search_manifest.ORIGIN_CLAIM_ONLY],
        "origin_groups": {search_manifest.ORIGIN_CLAIM_ONLY: group},
        "origin_provisional_groups": {search_manifest.ORIGIN_CLAIM_ONLY: None},
    }


def _bundle(number: str, *, verified: bool, reason: str = ""):
    key = epo_client.normalize_doc_key(number)
    bundle = search_verification.EvidenceBundle(
        doc_number=number,
        doc_key=key,
        reason=reason,
        calls=[{"artifact_id": "a" * 64}],
    )
    if verified:
        bundle.status = search_verification.STATUS_VERIFIED
        bundle.texts = {"claims:en": "a verified claim sentence"}
        bundle.record = SimpleNamespace(
            title="Official title", fields={}, source_url="https://example.test/patent"
        )
    else:
        bundle.status = search_verification.STATUS_FETCH_FAILED
    return key, bundle


def test_partial_fetch_results_are_recorded_per_candidate() -> None:
    reported = {
        "candidates": [
            _candidate(1, "EP1234567A1"),
            _candidate(2, "EP7654321A1", "B"),
            _candidate(3, "paper-without-ops-id", "C"),
        ]
    }
    key1, ok = _bundle("EP1234567A1", verified=True)
    key2, failed = _bundle(
        "EP7654321A1", verified=False, reason="OPS returned 404"
    )

    search_verification.annotate_bundles(reported, {key1: ok, key2: failed})

    statuses = [item["verification"]["status"] for item in reported["candidates"]]
    assert statuses == [
        search_manifest.VERIFY_RECORD_FETCHED,
        search_manifest.VERIFY_FETCH_FAILED,
        search_manifest.VERIFY_NOT_ATTEMPTED,
    ]
    assert reported["candidates"][1]["verification"]["detail"] == "OPS returned 404"
    assert reported["candidates"][0]["verification"]["artifact_ids"] == [
        "a" * 64
    ]


def test_supported_official_row_promotes_and_records_basis(monkeypatch) -> None:
    reported = {"candidates": [_candidate(1, "EP1234567A1")]}
    key, bundle = _bundle("EP1234567A1", verified=True)

    def supported_row(*_args, **_kwargs):
        return {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
        }

    monkeypatch.setattr(search_verification, "_verify_row", supported_row)
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "A",
                "mapping": [{"feature": "x", "support_text": "y"}],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )
    candidate = updated["candidates"][0]
    assert candidate["group"] == "A"
    assert candidate["provisional_group"] is None
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_OFFICIAL
    assert candidate["matched_feature_rows"] == 1
    assert candidate["verification"]["status"] == search_manifest.VERIFY_PROMOTED


def test_unmatched_official_rows_keep_the_ai_group_provisional(monkeypatch) -> None:
    reported = {"candidates": [_candidate(1, "EP1234567A1", "B")]}
    key, bundle = _bundle("EP1234567A1", verified=True)

    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": False,
            "support_source": search_manifest.SUPPORT_NONE,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "A",
                "mapping": [{"feature": "x", "support_text": "not present"}],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )
    candidate = updated["candidates"][0]
    assert candidate["group"] is None
    # 2차 턴의 제안도 검증되지 않았으므로 잠정 칸에만 남는다.
    assert candidate["provisional_group"] == "A"
    assert candidate["verification"]["status"] == (
        search_manifest.VERIFY_EVIDENCE_MISMATCH
    )


def test_classification_failure_does_not_hide_fetch_failures() -> None:
    reported = {
        "candidates": [
            _candidate(1, "EP1234567A1"),
            _candidate(2, "EP7654321A1", "B"),
        ]
    }
    key1, ok = _bundle("EP1234567A1", verified=True)
    key2, failed = _bundle("EP7654321A1", verified=False, reason="quota")

    search_verification.annotate_classification_failure(
        reported, {key1: ok, key2: failed}, detail="invalid JSON"
    )

    assert reported["candidates"][0]["verification"]["status"] == (
        search_manifest.VERIFY_CLASSIFICATION_FAILED
    )
    assert reported["candidates"][1]["verification"]["status"] == (
        search_manifest.VERIFY_FETCH_FAILED
    )


def test_no_group_means_below_the_ab_threshold_not_a_failure() -> None:
    """group 을 비운 것은 실패가 아니라 결론이다. 후보는 남고 표만 안 만든다."""
    reported = {"candidates": [_candidate(1, "EP1234567A1", "B")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "note": "초록에 유사 서술이 있으나 핵심 관계가 다릅니다.",
                "mapping": [{"support_text": "x"}],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    assert candidate["group"] is None
    assert candidate["classification_outcome"] == (
        search_manifest.OUTCOME_BELOW_THRESHOLD
    )
    # 긴 대응표를 만들지 않는다. 짧은 사유만 남는다.
    assert candidate["mapping"] == []
    assert "핵심 관계가 다릅니다" in candidate["note"]
    assert candidate["verification"]["reason_code"] == "below_ab_threshold"
    assert candidate["verification"]["status"] == (
        search_manifest.VERIFY_RECORD_FETCHED
    )


# --- 충돌 해소 규칙 -------------------------------------------------------
#
# 규칙 1·5 : 공식 응답에 대조된 분류가 최우선이고, 페이지 분류와 어긋나면
#            공식 분류가 이 후보의 분류가 된다.
# 규칙 3   : 그때 덮인 페이지 분류와 대응표는 버리지 않고 보존한다.
# 규칙 4   : 공식 조회 실패나 근거 불일치를 문헌 부재로 읽지 않는다. 페이지
#            관측으로 이미 정식이던 후보는 강등하지 않는다.


def test_official_classification_overrides_the_page_group(monkeypatch) -> None:
    reported = {"candidates": [_page_candidate(1, "EP1234567A1", "A")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "B",
                "mapping": [{"feature": "x", "support_text": "y"}],
            }
        ]
    }

    updated, notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    # 규칙 1·5: 공식 분류가 primary 다.
    assert candidate["group"] == "B"
    assert candidate["provisional_group"] is None
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_OFFICIAL
    # 규칙 3: 덮인 페이지 분류와 대응표는 보존된다.
    preserved = candidate[search_verification.PAGE_CLASSIFICATION_FIELD]
    assert preserved["group"] == "A"
    assert preserved["classification_basis"] == search_manifest.CLASSIFICATION_PAGE
    assert [row["feature"] for row in preserved["mapping"]] == [
        "레이더 탐지부",
        "EO/IR 융합 처리",
    ]
    assert any("어긋나" in note for note in notes)


def test_agreeing_official_classification_still_preserves_the_page_rows(
    monkeypatch,
) -> None:
    reported = {"candidates": [_page_candidate(1, "EP1234567A1", "B")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "B",
                "mapping": [{"feature": "x", "support_text": "y"}],
            }
        ]
    }

    updated, notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    assert candidate["group"] == "B"
    assert candidate[search_verification.PAGE_CLASSIFICATION_FIELD]["group"] == "B"
    # 값이 같으면 충돌 메모는 남기지 않는다.
    assert not any("어긋나" in note for note in notes)


def test_failed_official_fetch_does_not_demote_a_page_backed_group() -> None:
    reported = {"candidates": [_page_candidate(1, "EP1234567A1", "B")]}
    key, failed = _bundle("EP1234567A1", verified=False, reason="OPS 404")

    updated, _notes = search_verification.apply_classification(
        reported, {"candidates": []}, {key: failed}, store=object()
    )

    candidate = updated["candidates"][0]
    # 규칙 4: EPO 에 없다는 것은 문헌 부재가 아니다.
    assert candidate["group"] == "B"
    assert candidate["provisional_group"] is None
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_PAGE
    assert len(candidate["mapping"]) == 2
    assert candidate["verification"]["status"] == search_manifest.VERIFY_FETCH_FAILED
    # 공식 근거는 하나도 없었다는 사실은 그대로 남는다.
    assert candidate["official_supported_rows"] == 0


def test_unsupported_official_rows_do_not_demote_a_page_backed_group(
    monkeypatch,
) -> None:
    reported = {"candidates": [_page_candidate(1, "EP1234567A1", "A")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": False,
            "support_source": search_manifest.SUPPORT_NONE,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "B",
                "mapping": [{"feature": "x", "support_text": "초록에 없는 문장"}],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    # 초록·청구항에서 못 찾았다고 페이지 관측 분류를 내리지 않는다.
    assert candidate["group"] == "A"
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_PAGE
    # 검증되지 않은 2차 제안이 정식 칸도 잠정 칸도 차지하지 못한다.
    assert candidate["provisional_group"] is None
    assert candidate["verification"]["status"] == (
        search_manifest.VERIFY_EVIDENCE_MISMATCH
    )


def test_provisional_candidates_keep_the_existing_demotion_path() -> None:
    """페이지 근거가 없는 후보(Codex 경로)는 종전대로 잠정에 남는다."""
    reported = {"candidates": [_candidate(1, "EP1234567A1", "B")]}
    key, failed = _bundle("EP1234567A1", verified=False, reason="OPS 404")

    updated, _notes = search_verification.apply_classification(
        reported, {"candidates": []}, {key: failed}, store=object()
    )

    candidate = updated["candidates"][0]
    assert candidate["group"] is None
    assert candidate["provisional_group"] == "B"
    assert search_verification.PAGE_CLASSIFICATION_FIELD not in candidate


def test_vendor_xml_error_is_compacted_for_the_user_report() -> None:
    class OpsError(Exception):
        status = 404

    error = OpsError(
        'EPO OPS 오류(HTTP 404). <?xml version="1.0"?>'
        "<fault><code>SERVER.EntityNotFound</code>"
        "<message>No results found</message></fault>"
    )

    compact = search_verification._compact_fetch_error(error)

    assert compact == (
        "EPO OPS HTTP 404 · SERVER.EntityNotFound · No results found"
    )
    assert "<fault>" not in compact


def test_official_support_scope_comes_from_the_matched_field() -> None:
    assert search_verification._scope_for_field("claims:en") == "claims"
    assert search_verification._scope_for_field("abstract:ko") == "abstract"
    assert search_verification._scope_for_field("description:en") == "full_text"
    assert search_verification._scope_for_field("title:en") == "unknown"


# --- EPO 독립 검색 후보의 공식 근거 대조 -----------------------------------
#
# EPO 레인이 데려온 후보는 열어 본 웹 페이지가 없다. 그래서 페이지 근거로는
# 절대 정식 분류를 받을 수 없고, 공식 응답에 구성 대응이 실제로 대조된 경우에만
# 주 A/B/C 대응표에 들어간다. 못 들어간 후보는 지우지 않고 잠정으로 남긴다.


def _epo_candidate(index: int, number: str) -> dict:
    """merge_epo_discoveries 가 만드는 모양 그대로."""
    return search_manifest.epo_candidate(
        index=index,
        doc_number=number,
        title=f"{number} 제목",
        source_url=f"https://ops.example.test/{number}",
        lanes=["epo:claim_only"],
        shortlist_reasons=[{"lane": "epo:claim_only", "reason": "힘 센서 개시"}],
        artifact_ids=["a" * 64],
        evidence_fields=["claims:en"],
    )


def test_epo_only_candidate_enters_the_main_table_after_official_support(
    monkeypatch,
) -> None:
    reported = {"candidates": [_epo_candidate(1, "EP1234567A1")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "B",
                "mapping": [{"feature": "힘 센서", "support_text": "force sensor"}],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    assert candidate["group"] == "B"
    assert candidate["group_eligible"] is True
    assert candidate["classification_basis"] == search_manifest.CLASSIFICATION_OFFICIAL
    assert candidate["verification"]["status"] == search_manifest.VERIFY_PROMOTED
    # 발견 경로는 승격 뒤에도 남는다.
    assert search_manifest.discovery_origins(candidate) == [
        search_manifest.DISCOVERY_EPO
    ]
    # 존재하지 않는 페이지 분류를 만들지 않는다.
    assert search_verification.PAGE_CLASSIFICATION_FIELD not in candidate
    assert candidate["page_fetch_succeeded"] is False


def test_epo_only_candidate_without_official_support_stays_provisional(
    monkeypatch,
) -> None:
    reported = {"candidates": [_epo_candidate(1, "EP1234567A1")]}
    key, bundle = _bundle("EP1234567A1", verified=True)
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": False,
            "support_source": search_manifest.SUPPORT_NONE,
        },
    )
    payload = {
        "candidates": [
            {
                "doc_number": "EP1234567A1",
                "group": "A",
                "mapping": [{"feature": "x", "support_text": "보존 응답에 없는 문장"}],
            }
        ]
    }

    updated, notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )

    candidate = updated["candidates"][0]
    # 삭제하지 않는다. 정식 칸만 비우고 잠정으로 남긴다.
    assert candidate["group"] is None
    assert candidate["provisional_group"] == "A"
    assert candidate["verification"]["status"] == (
        search_manifest.VERIFY_EVIDENCE_MISMATCH
    )
    assert search_verification.PAGE_CLASSIFICATION_FIELD not in candidate
    assert any("잠정" in note for note in notes)


def test_target_limit_is_recorded_per_candidate() -> None:
    """상한 밖의 후보는 조용히 사라지지 않고 사유가 남는다."""
    reported = {
        "candidates": [
            _candidate(1, "EP1111111A1"),
            _candidate(2, "EP2222222A1"),
            _candidate(3, "EP3333333A1"),
        ]
    }
    dropped: list = []
    found = search_verification.targets(reported, limit=1, dropped=dropped)

    assert [item.doc_number for item in found] == ["EP1111111A1"]
    assert [row["doc_number"] for row in dropped] == ["EP2222222A1", "EP3333333A1"]
    assert all(row["reason_code"] == "verification_target_limit" for row in dropped)

    key, bundle = _bundle("EP1111111A1", verified=True)
    search_verification.annotate_bundles(reported, {key: bundle}, dropped)
    second = reported["candidates"][1]["verification"]
    assert second["status"] == search_manifest.VERIFY_NOT_ATTEMPTED
    assert second["reason_code"] == "verification_target_limit"
    assert "상한" in second["detail"]

    audit = search_verification.section(
        attempted=True, bundles={key: bundle}, dropped=dropped
    )
    assert len(audit["excluded_candidates"]) == 2


# --- 이미 받은 EPO 자료의 재사용 -------------------------------------------


class _LaneCandidate:
    """EpoSearchRun.candidates 의 값이 드는 것만 흉내 낸다."""

    def __init__(self, number, fields, evidence, artifact_ids) -> None:
        self.doc_number = number
        self.title = f"{number} 제목"
        self.source_url = f"https://ops.example.test/{number}"
        self.fields = fields
        self.evidence = evidence
        self.artifact_ids = artifact_ids


class _LaneRun:
    def __init__(self, candidates) -> None:
        self.candidates = {item.doc_number: item for item in candidates}


def _lane_run(number: str, *constituents: str) -> _LaneRun:
    artifact = "b" * 64
    fields = {f"{name}:en": f"{name} text for {number}" for name in constituents}
    evidence = {
        name: {
            "artifact_id": artifact,
            "field_path": f"documents/{number}/{name}",
            "profile_id": "epo_ops_exchange_xml_v1",
        }
        for name in fields
    }
    return _LaneRun([_LaneCandidate(number, fields, evidence, [artifact])])


class _CountingBackend:
    """fetch_document 호출을 세는 대역. 실제 OPS 를 부르지 않는다."""

    def __init__(self) -> None:
        self.calls: list = []

    def fetch_document(self, doc_key, constituent, *, agent_budget=True):
        self.calls.append(constituent)
        raise PatentSearchError("이 테스트에서는 새 조회가 일어나면 안 됩니다.")


def _target(number: str) -> "search_verification.Target":
    return search_verification.Target(
        index=1,
        doc_number=number,
        doc_key=epo_client.normalize_doc_key(number),
    )


def test_reuse_bundles_carry_the_lane_artifacts() -> None:
    bundles = search_verification.reuse_bundles([_lane_run("EP1234567A1", "claims")])
    key = epo_client.normalize_doc_key("EP1234567A1")

    bundle = bundles[key]
    assert bundle.verified
    assert bundle.texts["claims:en"].startswith("claims text")
    assert bundle.artifact_ids == ["b" * 64]
    # 이 호출은 이번 검증 단계가 낸 것이 아니다.
    assert all(call["reused"] for call in bundle.calls)


def test_reused_artifacts_prevent_a_second_api_call() -> None:
    """청구항·초록·서지가 이미 있으면 OPS 를 한 번도 다시 부르지 않는다."""
    prefetched = search_verification.reuse_bundles(
        [_lane_run("EP1234567A1", "claims", "abstract", "title")]
    )
    backend = _CountingBackend()

    bundles = search_verification.fetch_official(
        [_target("EP1234567A1")], backend, max_fetches=12, prefetched=prefetched
    )

    assert backend.calls == [], "이미 받은 자료를 다시 내려받았습니다."
    assert bundles[epo_client.normalize_doc_key("EP1234567A1")].verified
    audit = search_verification.section(attempted=True, bundles=bundles)
    assert audit["usage"]["official_fetch_calls"] == 0
    assert audit["usage"]["reused_artifact_calls"] == 1


def test_only_the_missing_constituents_are_fetched() -> None:
    """청구항만 있으면 초록·서지만 더 받는다. 청구항은 다시 부르지 않는다."""
    prefetched = search_verification.reuse_bundles(
        [_lane_run("EP1234567A1", "claims")]
    )
    backend = _CountingBackend()

    bundles = search_verification.fetch_official(
        [_target("EP1234567A1")], backend, max_fetches=12, prefetched=prefetched
    )

    assert "claims" not in backend.calls
    assert set(backend.calls) == {"abstract", "biblio"}
    # 새 조회가 실패해도 재사용한 청구항 덕분에 근거는 남아 있다.
    assert bundles[epo_client.normalize_doc_key("EP1234567A1")].verified


def test_a_fetch_budget_of_zero_still_uses_what_the_lane_already_had() -> None:
    """예산이 말라도 이미 손에 있는 자료를 버리지 않는다."""
    prefetched = search_verification.reuse_bundles(
        [_lane_run("EP1234567A1", "claims")]
    )
    backend = _CountingBackend()

    bundles = search_verification.fetch_official(
        [_target("EP1234567A1")], backend, max_fetches=0, prefetched=prefetched
    )

    assert backend.calls == []
    bundle = bundles[epo_client.normalize_doc_key("EP1234567A1")]
    assert bundle.verified
    assert "상한" in bundle.reason


# --- 경계 사례 2: 검증 대상 선택 정책 ---------------------------------------
#
# 후보 배열 순서로 자르면 웹 후보가 앞자리를 다 차지해 EPO 후보가 통째로 빠진다.
# 상한이 후보 수보다 작은 실행에서 그 일이 매번 일어난다.


def _reusable_key(number: str) -> str:
    return epo_client.normalize_doc_key(number)


def _plan(*runs) -> dict:
    """레인이 받아 둔 것으로 재사용 계획을 만든다. 실행 경로와 같은 순서다."""
    return search_verification.reuse_plan(search_verification.reuse_bundles(runs))


def test_reusable_candidates_are_selected_before_the_rest() -> None:
    """필요한 구성요소가 다 있는 후보를 먼저 고른다. OPS 호출이 0회다."""
    reported = {
        "candidates": [
            _candidate(1, "EP1111111A1"),
            _candidate(2, "EP2222222A1"),
            _candidate(3, "EP3333333A1"),
        ]
    }
    order: list = []
    found = search_verification.targets(
        reported,
        limit=1,
        order=order,
        reuse=_plan(_lane_run("EP3333333A1", "claims", "abstract", "title")),
    )

    assert [item.doc_number for item in found] == ["EP3333333A1"]
    assert order[0]["selection_reason"] == search_verification.SELECT_REUSABLE
    assert "추가 조회 없이" in order[0]["detail"]
    assert order[0]["expected_fetches"] == 0


def test_a_partial_reuse_is_not_recorded_as_a_free_candidate() -> None:
    """청구항만 손에 있는 후보를 '추가 조회 없음'으로 적지 않는다.

    재사용 묶음이 있다는 것과 더 받을 것이 없다는 것은 다른 말이다. 검색만 하고
    끝난 문헌은 서지·초록만 있고, 그것을 공짜로 세면 예산 계획이 그 자리에서
    틀린다.
    """
    plan = _plan(_lane_run("EP2222222A1", "claims"))
    key = _reusable_key("EP2222222A1")

    assert plan[key]["complete"] is False
    assert plan[key]["missing"] == ["abstract", "biblio"]
    assert plan[key]["expected_fetches"] == 2

    order: list = []
    search_verification.targets(
        {"candidates": [_candidate(1, "EP2222222A1")]},
        limit=1,
        order=order,
        reuse=plan,
    )
    assert (
        order[0]["selection_reason"]
        == search_verification.SELECT_REUSABLE_PARTIAL
    )
    assert order[0]["expected_fetches"] == 2
    assert "abstract" in order[0]["detail"]


def test_full_reuse_is_selected_before_partial_reuse() -> None:
    """같은 재사용이라도 공짜인 쪽을 먼저 고른다. 그 다음이 부분 재사용이다."""
    reported = {
        "candidates": [
            _candidate(1, "EP1111111A1"),
            _candidate(2, "EP2222222A1"),
            _candidate(3, "EP3333333A1"),
        ]
    }
    order: list = []
    found = search_verification.targets(
        reported,
        limit=3,
        order=order,
        reuse=_plan(
            _lane_run("EP3333333A1", "claims", "abstract", "title"),
            _lane_run("EP2222222A1", "claims"),
        ),
    )

    assert [item.doc_number for item in found] == [
        "EP3333333A1",
        "EP2222222A1",
        "EP1111111A1",
    ]
    assert [row["selection_reason"] for row in order] == [
        search_verification.SELECT_REUSABLE,
        search_verification.SELECT_REUSABLE_PARTIAL,
        search_verification.SELECT_CANDIDATE_ORDER,
    ]
    # 아무것도 없는 후보는 구성요소를 전부 받아야 한다.
    assert [row["expected_fetches"] for row in order] == [0, 2, 3]


def test_the_audit_record_separates_full_and_partial_reuse() -> None:
    """감사 기록이 예상 추가 조회 횟수와 재사용의 종류를 함께 남긴다."""
    prefetched = search_verification.reuse_bundles(
        [
            _lane_run("EP1234567A1", "claims", "abstract", "title"),
            _lane_run("EP7654321A1", "claims"),
        ]
    )
    order: list = []
    found = search_verification.targets(
        {
            "candidates": [
                _candidate(1, "EP1234567A1"),
                _candidate(2, "EP7654321A1"),
            ]
        },
        limit=2,
        order=order,
        reuse=search_verification.reuse_plan(prefetched),
    )
    backend = _CountingBackend()
    bundles = search_verification.fetch_official(
        found, backend, max_fetches=12, prefetched=prefetched
    )
    audit = search_verification.section(
        attempted=True, bundles=bundles, order=order
    )

    # 완전 재사용은 한 번도 부르지 않았고, 부분 재사용은 모자란 둘만 불렀다.
    assert backend.calls == ["abstract", "biblio"]
    assert audit["selection_policy"]["planned_fetch_calls"] == 2
    assert audit["selection_policy"]["ranking"][:2] == [
        search_verification.SELECT_REUSABLE,
        search_verification.SELECT_REUSABLE_PARTIAL,
    ]
    assert audit["usage"]["planned_fetch_calls"] == 2
    assert audit["usage"]["official_fetch_calls"] == 2
    assert audit["usage"]["fully_reused_documents"] == 1
    assert audit["usage"]["partially_reused_documents"] == 1
    assert audit["usage"]["reuse_plan_unknown_documents"] == 0
    assert audit["usage"]["reused_without_fresh_fetch_documents"] == 1
    assert audit["usage"]["reused_with_fresh_fetch_documents"] == 1


def test_partial_reuse_stays_partial_when_zero_budget_prevents_fetches() -> None:
    """호출이 0회였다는 이유로 부분 재사용을 완전 재사용으로 적지 않는다."""
    prefetched = search_verification.reuse_bundles(
        [_lane_run("EP7654321A1", "claims")]
    )
    order: list = []
    found = search_verification.targets(
        {"candidates": [_candidate(1, "EP7654321A1")]},
        limit=1,
        order=order,
        reuse=search_verification.reuse_plan(prefetched),
    )
    bundles = search_verification.fetch_official(
        found, _CountingBackend(), max_fetches=0, prefetched=prefetched
    )
    audit = search_verification.section(
        attempted=True, bundles=bundles, order=order
    )

    assert order[0]["selection_reason"] == search_verification.SELECT_REUSABLE_PARTIAL
    assert audit["usage"]["official_fetch_calls"] == 0
    assert audit["usage"]["fully_reused_documents"] == 0
    assert audit["usage"]["partially_reused_documents"] == 1
    assert audit["usage"]["reused_without_fresh_fetch_documents"] == 1
    assert audit["usage"]["reused_with_fresh_fetch_documents"] == 0


def test_a_key_only_reuse_list_is_never_promoted_to_free() -> None:
    """구성요소를 세지 않았으면 모르는 것이다. 0 으로 적지 않는다."""
    order: list = []
    search_verification.targets(
        {"candidates": [_candidate(1, "EP1111111A1")]},
        limit=1,
        order=order,
        reuse={_reusable_key("EP1111111A1")},
    )

    assert (
        order[0]["selection_reason"]
        == search_verification.SELECT_REUSABLE_PARTIAL
    )
    assert order[0]["expected_fetches"] is None
    assert "미상" in order[0]["detail"]


def test_epo_candidates_are_not_starved_by_web_candidates() -> None:
    """웹 후보가 앞에 있어도 EPO 후보가 상한 밖으로 밀려나지 않는다."""
    web = [_candidate(index, f"EP{index}111111A1") for index in range(1, 4)]
    epo = search_manifest.epo_candidate(
        index=4, doc_number="EP9999999A1", lanes=["epo:claim_only"]
    )
    order: list = []
    dropped: list = []
    found = search_verification.targets(
        {"candidates": [*web, epo]}, limit=1, order=order, dropped=dropped
    )

    assert [item.doc_number for item in found] == ["EP9999999A1"]
    assert order[0]["selection_reason"] == search_verification.SELECT_EPO_DISCOVERY
    # 밀려난 웹 후보는 사유와 함께 남는다.
    assert len(dropped) == 3
    assert all(
        row["selection_reason"] == search_verification.SELECT_CANDIDATE_ORDER
        for row in dropped
    )


def test_the_policy_keeps_candidate_order_inside_one_rank() -> None:
    """정책이 건드리지 않는 후보들의 상대 순서는 그대로다(안정 정렬)."""
    reported = {
        "candidates": [
            _candidate(1, "EP1111111A1"),
            _candidate(2, "EP2222222A1"),
            _candidate(3, "EP3333333A1"),
        ]
    }
    found = search_verification.targets(reported, limit=3)

    assert [item.doc_number for item in found] == [
        "EP1111111A1",
        "EP2222222A1",
        "EP3333333A1",
    ]


def test_the_selection_order_is_recorded_in_the_audit_section() -> None:
    """무엇을 왜 골랐는지가 감사 기록에 남는다."""
    epo = search_manifest.epo_candidate(
        index=1, doc_number="EP1000000A1", lanes=["epo:claim_only"]
    )
    order: list = []
    dropped: list = []
    search_verification.targets(
        {"candidates": [epo, _candidate(2, "EP2222222A1")]},
        limit=1,
        order=order,
        dropped=dropped,
    )
    audit = search_verification.section(
        attempted=True, order=order, dropped=dropped
    )

    assert audit["selection_order"][0]["doc_number"] == "EP1000000A1"
    assert audit["selection_policy"]["ranking"][0] == (
        search_verification.SELECT_REUSABLE
    )
    assert audit["excluded_candidates"][0]["doc_number"] == "EP2222222A1"


# ----------------------------------------------- 예산과 조회 범위가 서로 맞는가


def test_the_verification_target_cap_matches_the_detail_fetch_budget() -> None:
    """상한 둘이 어긋나면 뒤쪽 후보는 언제나 예산이 말라 검증되지 않는다.

    후보 하나에 claims/abstract/biblio 세 번을 부른다. 예전에는 대상 8건
    (= 24회 필요)에 예산이 12회였고, 그래서 절반은 늘 조회조차 못 했다.
    """
    from app import config

    targets = int(config.DEFAULTS["epo_verification_targets"])
    budget = int(config.DEFAULTS["epo_max_detail_fetches"])
    per_target = len(search_verification.DEFAULT_CONSTITUENTS)

    assert targets * per_target == budget, (
        f"검증 대상 {targets}건 × 구성요소 {per_target}개 = "
        f"{targets * per_target}회인데 조회 예산은 {budget}회입니다."
    )


def test_the_verification_target_default_matches_the_setting_default() -> None:
    """설정이 누락된 경로에서도 옛 상한(8)으로 돌아가지 않는다.

    러너는 설정에서 읽지만 targets() 의 기본값은 설정을 주지 않은 경로에서
    쓰인다. 두 수가 어긋나면 그 경로만 조용히 옛 상한으로 돈다.
    """
    import inspect

    from app import config

    default = inspect.signature(search_verification.targets).parameters["limit"].default
    assert default == int(config.DEFAULTS["epo_verification_targets"])
    assert default != 8, "옛 기본값이 남아 있습니다."


def test_only_ab_candidates_get_a_detailed_mapping(monkeypatch) -> None:
    """상세 구성 대응표는 A/B 로 승격된 문헌에만 만든다."""
    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
            "feature": "x",
        },
    )
    reported = {
        "candidates": [
            _candidate(1, "EP1111111A1", "A"),
            _candidate(2, "EP2222222A1", "A"),
        ]
    }
    key_a, bundle_a = _bundle("EP1111111A1", verified=True)
    key_b, bundle_b = _bundle("EP2222222A1", verified=True)
    payload = {
        "candidates": [
            {
                "doc_number": "EP1111111A1",
                "group": "B",
                "mapping": [{"feature": "x", "support_text": "y"}],
            },
            {
                "doc_number": "EP2222222A1",
                "note": "핵심 관계가 다릅니다.",
                "mapping": [{"feature": "x", "support_text": "y"}],
            },
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key_a: bundle_a, key_b: bundle_b}, store=object()
    )

    promoted, below = updated["candidates"]
    assert promoted["group"] == "B"
    assert promoted["mapping"]
    assert below["group"] is None
    assert below["mapping"] == []


def test_the_mapping_row_cap_is_ten(monkeypatch) -> None:
    """후보당 60행은 과도했다. 핵심 구성·관계만 남긴다."""
    assert search_verification.MAX_MAPPING_ROWS == 10

    monkeypatch.setattr(
        search_verification,
        "_verify_row",
        lambda *_args, **_kwargs: {
            "official_supported": True,
            "support_source": search_manifest.SUPPORT_OFFICIAL,
            "feature": "x",
        },
    )
    reported = {"candidates": [_candidate(1, "EP1111111A1", "A")]}
    key, bundle = _bundle("EP1111111A1", verified=True)
    payload = {
        "candidates": [
            {
                "doc_number": "EP1111111A1",
                "group": "A",
                "mapping": [
                    {"feature": f"f{i}", "support_text": "y"} for i in range(25)
                ],
            }
        ]
    }

    updated, _notes = search_verification.apply_classification(
        reported, payload, {key: bundle}, store=object()
    )
    assert len(updated["candidates"][0]["mapping"]) == 10
