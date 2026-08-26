"""검색 감사 기록 파싱과 증거 등급.

핵심 규칙: WebFetch 요약이 원문 직접 인용으로 승격되지 않는다.
"""

from __future__ import annotations

import json

import pytest

from app import search_manifest


def _block(payload: dict) -> str:
    return (
        "# 보고서\n\n본문\n\n[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]\n"
    )


# 후보 전용 페이지 주소여야 하고, 그 안에 후보의 문헌번호가 들어 있어야 한다.
# 둘 중 하나라도 아니면 ARIA 가 후보를 격리하거나 서지정보를 지운다.
CANDIDATE_URL = "https://patents.example.com/patent/US20190123456A1"


def _candidate(**overrides) -> dict:
    base = {
        "group": "A",
        "provisional": True,
        "channel": "web",
        "doc_type": "patent",
        "doc_number": "US2019/0123456A1",
        "title": "테스트 특허",
        "applicant": "테스트 주식회사",
        "url": CANDIDATE_URL,
        "provenance": "webfetch_summary",
        "evidence_status": "source_page_reviewed",
        # 페이지 관측에 근거한 행이 하나도 없으면 후보는 A/B/C 에 들어가지
        # 못한다. 기본 후보는 그 하한을 만족하는 형태로 둔다.
        "mapping": [
            {
                "feature": "제1 센서",
                "counterpart": "센서 모듈 110",
                "degree": "부분 대응",
                "support_source": "page_text",
                "support_text": "a sensor module 110 disposed on",
                "support_scope": "abstract",
            }
        ],
    }
    base.update(overrides)
    return base


def _observed(succeeded=(CANDIDATE_URL,), attempted=None):
    """성공한 WebFetch 호출을 흉내 낸 관측 기록."""
    attempted = list(attempted if attempted is not None else succeeded)
    calls = [
        {
            "id": f"t{i}",
            "name": "WebFetch",
            "ts": "2026-08-21T00:00:00+00:00",
            "input": {"url": url},
            "ok": url in succeeded,
            "error": None if url in succeeded else "403",
        }
        for i, url in enumerate(attempted, start=1)
    ]
    return search_manifest.observed(calls, ["WebFetch"] * len(calls))


def test_parses_rounds_candidates_and_failures() -> None:
    reported, notes = search_manifest.parse(
        _block(
            {
                "rounds": [
                    {"round": 1, "channel": "web", "queries": ["a", "b"], "note": "1차"}
                ],
                "candidates": [_candidate()],
                "access_failures": [{"url": "https://p/x", "reason": "유료"}],
            }
        ),
        _observed(),
    )
    assert reported["rounds"][0]["queries"] == ["a", "b"]
    assert reported["candidates"][0]["doc_number"] == "US2019/0123456A1"
    assert reported["access_failures"][0]["reason"] == "유료"
    assert notes == []


def test_missing_block_raises() -> None:
    with pytest.raises(search_manifest.SearchLogError, match="찾지 못했"):
        search_manifest.parse("# 보고서\n블록 없음\n")


def test_duplicate_block_raises() -> None:
    text = _block({"candidates": []}) + _block({"candidates": []})
    with pytest.raises(search_manifest.SearchLogError, match="하나만"):
        search_manifest.parse(text)


def test_broken_json_raises() -> None:
    text = "[ARIA_SEARCH_LOG_V1]\n{not json}\n[/ARIA_SEARCH_LOG_V1]"
    with pytest.raises(search_manifest.SearchLogError, match="JSON"):
        search_manifest.parse(text)


def test_block_is_stripped_from_user_facing_report() -> None:
    text = _block({"candidates": []})
    stripped = search_manifest.strip_block(text)
    assert "ARIA_SEARCH_LOG_V1" not in stripped
    assert "# 보고서" in stripped


def test_fenced_block_is_also_stripped() -> None:
    payload = json.dumps({"candidates": []}, ensure_ascii=False)
    text = (
        "# 보고서\n\n```json\n[ARIA_SEARCH_LOG_V1]\n"
        + payload
        + "\n[/ARIA_SEARCH_LOG_V1]\n```\n"
    )
    assert "ARIA_SEARCH_LOG_V1" not in search_manifest.strip_block(text)
    reported, _ = search_manifest.parse(text, _observed())
    assert reported["candidates"] == []


# ----------------------------------------------------------- 증거 등급 강등


def test_raw_original_claim_on_web_channel_is_downgraded() -> None:
    """WebFetch 는 원문을 주지 않는다. 원문 대조 주장은 그대로 둘 수 없다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        provenance="raw_original_verified",
                        verbatim_excerpt="The apparatus comprises a first unit",
                        source_location="claim 1, line 3",
                        provisional=False,
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["provenance"] == search_manifest.PROV_WEBFETCH
    assert candidate["original_verified"] is False
    assert any("내렸습니다" in note for note in notes)


def test_downgraded_candidate_loses_its_verbatim_excerpt() -> None:
    """요약문이 인용문 칸에 남으면 그 자체가 거짓 인용이 된다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        provenance="raw_original_verified",
                        verbatim_excerpt="The apparatus comprises a first unit",
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["verbatim_excerpt"] == "원문에서 확인되지 않음"
    assert "The apparatus comprises" not in json.dumps(reported, ensure_ascii=False)
    assert any("직접 발췌" in note for note in notes)


def test_webfetch_summary_excerpt_is_never_promoted() -> None:
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        provenance="webfetch_summary",
                        verbatim_excerpt="요약 모델이 쓴 문장",
                        source_location="",
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["verbatim_excerpt"] == "원문에서 확인되지 않음"
    assert candidate["source_location"] == "확인 필요"
    assert candidate["original_verified"] is False


# ------------------------------------- 원문 미확인 시 위치는 무조건 '확인 필요'


def test_source_location_is_replaced_not_preserved_when_unverified() -> None:
    """위치는 원문을 본 사람만 쓸 수 있는 진술이다. 있어도 보존하지 않는다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        provenance="webfetch_summary",
                        source_location="청구항 1, 3컬럼 12행",
                        verbatim_excerpt="",
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["original_verified"] is False
    assert candidate["source_location"] == "확인 필요"
    assert "3컬럼 12행" not in json.dumps(reported, ensure_ascii=False)
    assert any("원문 위치" in note for note in notes)


def test_mapping_rows_lose_location_excerpt_and_translation_when_unverified() -> None:
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        provenance="webfetch_summary",
                        mapping=[
                            {
                                "feature": "제1 센서",
                                "counterpart": "센서 모듈 110",
                                "degree": "강한 대응",
                                "support_source": "page_text",
                                "support_text": "a sensor module 110 disposed on",
                                "support_scope": "abstract",
                                "source_location": "문단 [0032]",
                                "verbatim_excerpt": "the first sensor comprises",
                                "translation": "제1 센서는 …를 포함하고",
                                "similar": "구조가 같다",
                                "different": "제어부가 다르다",
                            }
                        ],
                    )
                ]
            }
        ),
        _observed(),
    )
    row = reported["candidates"][0]["mapping"][0]
    assert row["verified"] is False
    assert row["source_location"] == "확인 필요"
    assert row["verbatim_excerpt"] == "원문에서 확인되지 않음"
    # 확보하지 못한 원문의 번역문은 원문보다 더 확인할 수 없는 진술이다.
    assert row["translation"] == "원문에서 확인되지 않음"
    # 대응 관계 설명 자체는 남는다. 지우는 것은 원문을 주장하는 칸뿐이다.
    assert row["feature"] == "제1 센서"
    assert row["counterpart"] == "센서 모듈 110"
    assert row["similar"] == "구조가 같다"
    dumped = json.dumps(reported, ensure_ascii=False)
    assert "the first sensor comprises" not in dumped
    assert "문단 [0032]" not in dumped
    assert any("구성 대응표" in note for note in notes)


def test_unverified_candidate_is_forced_to_provisional() -> None:
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(provisional=False)]}), _observed()
    )
    assert reported["candidates"][0]["provisional"] is True
    assert any("잠정 분류" in note for note in notes)


# ----------------------------------------------- 관측한 열람 기록과의 대조


def test_reviewed_claim_without_any_fetch_is_downgraded() -> None:
    """열어 본 적 없는 주소에 '페이지 열람함'을 붙일 수 없다."""
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(url="https://never.example.com/z")]}),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_CANDIDATE
    assert candidate["provenance"] == search_manifest.PROV_SNIPPET
    assert candidate["page_fetch_succeeded"] is False
    assert any("대조되지 않아" in note for note in notes)


def test_failed_fetch_does_not_count_as_reviewed() -> None:
    """403 이나 유료 장벽으로 실패한 호출은 열람이 아니다."""
    paywalled = "https://paywall.example.com/x"
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(url=paywalled)]}),
        _observed(succeeded=(), attempted=(paywalled,)),
    )
    candidate = reported["candidates"][0]
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_CANDIDATE
    assert candidate["provenance"] == search_manifest.PROV_SNIPPET
    assert candidate["page_fetch_succeeded"] is False
    assert any("대조되지 않아" in note for note in notes)


def test_successful_fetch_confirms_reviewed_status() -> None:
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate()]}), _observed()
    )
    candidate = reported["candidates"][0]
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_REVIEWED
    assert candidate["provenance"] == search_manifest.PROV_WEBFETCH
    assert candidate["page_fetch_succeeded"] is True
    # 확인해 준 것이지 올려 준 것이 아니다. 원문 대조는 여전히 아니다.
    assert candidate["original_verified"] is False
    assert not any("내렸습니다" in note for note in notes), notes


def test_url_matching_ignores_case_trailing_slash_and_www() -> None:
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        url="http://WWW.Patents.Example.com/patent/US20190123456A1/#a"
                    )
                ]
            }
        ),
        _observed(),
    )
    assert reported["candidates"][0]["page_fetch_succeeded"] is True


def test_url_matching_does_not_ignore_the_path() -> None:
    reported, _ = search_manifest.parse(
        _block({"candidates": [_candidate(url="https://patents.example.com/other")]}),
        _observed(),
    )
    assert reported["candidates"][0]["page_fetch_succeeded"] is False


def test_missing_observed_section_means_nothing_is_confirmed() -> None:
    """대조할 근거가 없으면 인정하지 않는다(fail-closed)."""
    reported, _ = search_manifest.parse(_block({"candidates": [_candidate()]}))
    candidate = reported["candidates"][0]
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_CANDIDATE


def test_model_cannot_claim_patent_db_channel() -> None:
    """모델이 channel: patent_db 를 보고해도 web 으로 강제된다.

    patent_db 는 ARIA 가 백엔드를 직접 호출해 만든 후보에만 붙는 라벨이다.
    모델 보고에 허용하면, 웹에서 찾은 후보에 patent_db 라고 적어 채널별
    집계를 오염시킬 수 있다. 증거 등급 강등만으로는 라벨이 남기 때문에
    막지 못한다.
    """
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        channel=search_manifest.CHANNEL_PATENT_DB,
                        provenance=search_manifest.PROV_RAW,
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["channel"] == search_manifest.CHANNEL_WEB
    assert candidate["original_verified"] is False
    assert search_manifest.CHANNEL_PATENT_DB not in search_manifest.MODEL_REPORTED_CHANNELS


def test_false_patent_db_claim_is_recorded_in_notes() -> None:
    """조용히 바꾸지 않는다. 모델이 ARIA 전용 채널을 주장한 사실을 남긴다."""
    _, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(channel=search_manifest.CHANNEL_PATENT_DB)
                ]
            }
        ),
        _observed(),
    )
    assert any("patent_db" in note for note in notes)


def test_channels_used_is_empty_for_web_only_run() -> None:
    """지원 채널 목록이 '이번에 특허 DB 도 봤다'로 읽히면 안 된다."""
    manifest = search_manifest.build(
        claim_text="청구항",
        prompt_id="search_prompt.md",
        prompt_version=1,
        prompt_sha256="a" * 64,
        claim_boundary_neutralized=False,
        started_at=None,
        completed_at=None,
        tool_calls=[],
        tool_uses=[],
        tool_policy_name="web_search",
        allowed_tools=("WebSearch", "WebFetch"),
        reported=None,
        notes=None,
        error=None,
    )
    assert search_manifest.CHANNEL_PATENT_DB in manifest["channels"]
    assert manifest["channels_used"] == []


def test_channel_allowlists_are_disjoint() -> None:
    """두 목록이 겹치면 분리한 의미가 없다."""
    assert not set(search_manifest.MODEL_REPORTED_CHANNELS) & set(
        search_manifest.ARIA_PRODUCED_CHANNELS
    )
    assert set(search_manifest.KNOWN_CHANNELS) == set(
        search_manifest.MODEL_REPORTED_CHANNELS
    ) | set(search_manifest.ARIA_PRODUCED_CHANNELS)


def test_unknown_enum_values_fall_back_instead_of_crashing() -> None:
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        group="S",
                        channel="epo",
                        doc_type="blogpost",
                        provenance="i_am_sure",
                        evidence_status="totally_verified",
                        mapping=[{"degree": "아주 강한 대응"}],
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["group"] == "C"
    assert candidate["channel"] == search_manifest.CHANNEL_WEB
    assert candidate["doc_type"] == "other"
    assert candidate["provenance"] == search_manifest.PROV_SNIPPET
    assert candidate["evidence_status"] == search_manifest.EVIDENCE_CANDIDATE
    assert candidate["mapping"][0]["degree"] == search_manifest.DEGREE_UNKNOWN


def test_missing_candidates_array_raises() -> None:
    with pytest.raises(search_manifest.SearchLogError, match="candidates"):
        search_manifest.parse(_block({"rounds": []}), _observed())


def test_excess_rounds_are_noted() -> None:
    _, notes = search_manifest.parse(
        _block(
            {
                "rounds": [
                    {"round": n, "queries": [f"q{n}"]} for n in range(1, 5)
                ],
                "candidates": [],
            }
        ),
        _observed(),
    )
    assert any("라운드" in note for note in notes)


# --------------------------------------------------- ARIA 가 관측한 기록


def test_observed_section_separates_attempted_from_succeeded_fetches() -> None:
    calls = [
        {
            "id": "t1",
            "name": "WebSearch",
            "ts": "2026-08-21T00:00:00+00:00",
            "input": {"query": "실제로 나간 검색식"},
            "ok": True,
            "error": None,
        },
        {
            "id": "t2",
            "name": "WebFetch",
            "ts": "2026-08-21T00:00:01+00:00",
            "input": {"url": "https://paywall/x"},
            "ok": False,
            "error": "403",
        },
        {
            "id": "t3",
            "name": "WebFetch",
            "ts": "2026-08-21T00:00:02+00:00",
            "input": {"url": "https://open/y"},
            "ok": True,
            "error": None,
        },
    ]
    observed = search_manifest.observed(calls, ["WebSearch", "WebFetch", "WebFetch"])
    assert observed["search_queries"] == ["실제로 나간 검색식"]
    assert observed["attempted_fetch_urls"] == ["https://paywall/x", "https://open/y"]
    assert observed["succeeded_fetch_urls"] == ["https://open/y"]
    assert observed["tool_call_counts"] == {"WebSearch": 1, "WebFetch": 2}
    assert observed["tool_failures"][0]["error"] == "403"


def test_observed_section_accepts_agy_search_and_fetch_tool_names() -> None:
    calls = [
        {
            "name": "search_web",
            "input": {"query": "sensor patent"},
            "ok": True,
            "ts": "t1",
        },
        {
            "name": "read_url_content",
            "input": {"url": "https://example.com/paper"},
            "ok": True,
            "ts": "t2",
        },
    ]
    result = search_manifest.observed(
        calls, ["search_web", "read_url_content"]
    )
    assert result["search_queries"] == ["sensor patent"]
    assert result["attempted_fetch_urls"] == ["https://example.com/paper"]
    assert result["succeeded_fetch_urls"] == ["https://example.com/paper"]
    assert result["tool_call_counts"] == {
        "search_web": 1,
        "read_url_content": 1,
    }


def test_pending_fetch_result_is_not_counted_as_succeeded() -> None:
    """결과가 도착하지 않은 호출(ok=None)은 성공으로 세지 않는다."""
    calls = [
        {
            "id": "t1",
            "name": "WebFetch",
            "ts": "2026-08-21T00:00:00+00:00",
            "input": {"url": "https://pending/z"},
            "ok": None,
            "error": None,
        }
    ]
    observed = search_manifest.observed(calls, ["WebFetch"])
    assert observed["attempted_fetch_urls"] == ["https://pending/z"]
    assert observed["succeeded_fetch_urls"] == []


def test_build_keeps_record_even_when_the_model_block_is_unreadable() -> None:
    manifest = search_manifest.build(
        claim_text="청구항 1.",
        prompt_id="search_prompt.md",
        prompt_version=1,
        prompt_sha256="abc",
        claim_boundary_neutralized=False,
        started_at="2026-08-21T00:00:00+00:00",
        completed_at="2026-08-21T00:01:00+00:00",
        tool_calls=[
            {"name": "WebSearch", "input": {"query": "q"}, "ok": True, "ts": "t"}
        ],
        tool_uses=["WebSearch"],
        tool_policy_name="web_search",
        allowed_tools=("WebSearch", "WebFetch"),
        reported=None,
        notes=[],
        error="블록을 찾지 못했습니다.",
    )
    assert manifest["version"] == search_manifest.MANIFEST_VERSION
    assert manifest["reported"] is None
    assert manifest["error"]
    # 모델 보고가 없어도 실제로 무엇을 검색했는지는 남는다.
    assert manifest["observed"]["search_queries"] == ["q"]
    assert manifest["prompt"]["sha256"] == "abc"
    assert manifest["policy"]["allowed_tools"] == ["WebSearch", "WebFetch"]
    # 강제하지 못하는 보증을 참으로 기록하지 않는다.
    assert manifest["policy"]["search_domain_restriction"] is False
    # 향후 EPO/업로드 채널이 붙을 자리.
    assert "channels" in manifest


# ------------------------------------------------ 명세서를 어떻게 반영했는가

def _expansion_payload(rows) -> dict:
    return {"rounds": [], "term_expansions": rows, "candidates": []}


def test_term_expansion_is_kept_as_reported() -> None:
    reported, notes = search_manifest.parse(
        _block(
            _expansion_payload(
                [
                    {
                        "claim_term": "제어부",
                        "alternative_meanings": ["일반 제어 회로", "FPGA 신호 처리 회로"],
                        "expanded_terms": ["controller", "FPGA"],
                        "basis": "문단 [0021]",
                        "excluded_limitations": ["특정 FPGA 모델"],
                    }
                ]
            )
        ),
        _observed(succeeded=()),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )
    assert reported["term_expansions"][0] == {
        "claim_term": "제어부",
        "alternative_meanings": ["일반 제어 회로", "FPGA 신호 처리 회로"],
        "expanded_terms": ["controller", "FPGA"],
        "basis": "문단 [0021]",
        "excluded_limitations": ["특정 FPGA 모델"],
    }
    assert not any("용어 확장" in note for note in notes)


def test_legacy_interpretation_drops_narrowing_claim() -> None:
    """이전 출력이 와도 좁힘 주장을 새 계약으로 가져오지 않는다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "rounds": [],
                "claim_interpretation": [
                    {"term": "부재", "reading": "지지대", "narrowed": True}
                ],
                "candidates": [],
            }
        ),
        _observed(succeeded=()),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )
    assert reported["term_expansions"][0]["alternative_meanings"] == ["지지대"]
    assert "narrowed" not in reported["term_expansions"][0]
    assert any("이전 claim_interpretation" in note for note in notes)


def test_spec_without_any_term_expansion_is_noted() -> None:
    reported, notes = search_manifest.parse(
        _block(_expansion_payload([])),
        _observed(succeeded=()),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )
    assert reported["term_expansions"] == []
    assert any("용어 확장 기록이 비어" in note for note in notes)


def test_term_expansion_without_a_spec_is_noted() -> None:
    """명세서를 넣지 않았는데 명세서 근거를 말하면 대조할 자료가 없다."""
    _reported, notes = search_manifest.parse(
        _block(
            _expansion_payload(
                [
                    {
                        "claim_term": "제어부",
                        "alternative_meanings": ["회로"],
                        "basis": "문단 [0021]",
                    }
                ]
            )
        ),
        _observed(succeeded=()),
        spec_provided=False,
    )
    assert any("넣지 않은 실행" in note for note in notes)


def test_nameless_term_expansion_rows_are_dropped() -> None:
    reported, _notes = search_manifest.parse(
        _block(_expansion_payload([{"alternative_meanings": ["무언가"]}, "문자열"])),
        _observed(succeeded=()),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )
    assert reported["term_expansions"] == []


def test_isolated_results_are_merged_as_union_and_preserve_baseline_group() -> None:
    base, _ = search_manifest.parse(
        _block({"candidates": [_candidate(group="C")]}),
        _observed(),
        search_origin=search_manifest.ORIGIN_CLAIM_ONLY,
    )
    added_url = "https://patents.example.com/new"
    assisted, _ = search_manifest.parse(
        _block(
            {
                "term_expansions": [],
                "candidates": [
                    _candidate(group="A"),
                    _candidate(
                        group="B",
                        doc_number="US2020/9999999A1",
                        url=added_url,
                    ),
                ],
            }
        ),
        _observed(succeeded=(CANDIDATE_URL, added_url)),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )

    merged = search_manifest.merge_reported(base, assisted)
    assert merged is not None
    assert [row["doc_number"] for row in merged["candidates"]] == [
        "US2019/0123456A1",
        "US2020/9999999A1",
    ]
    duplicate = merged["candidates"][0]
    assert duplicate["group"] == "C"
    assert duplicate["search_origins"] == ["claim_only", "spec_assisted"]
    assert merged["candidates"][1]["search_origins"] == ["spec_assisted"]


def test_unknown_document_numbers_do_not_collapse_unrelated_candidates() -> None:
    base, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        doc_number="확인 필요",
                        url="https://patents.example.com/first",
                    )
                ]
            }
        ),
        _observed(succeeded=()),
    )
    assisted, _ = search_manifest.parse(
        _block(
            {
                "term_expansions": [],
                "candidates": [
                    _candidate(
                        doc_number="확인 필요",
                        url="https://patents.example.com/second",
                    )
                ],
            }
        ),
        _observed(succeeded=()),
        spec_provided=True,
        search_origin=search_manifest.ORIGIN_SPEC_ASSISTED,
    )
    merged = search_manifest.merge_reported(base, assisted)
    assert merged is not None
    assert len(merged["candidates"]) == 2


def test_spec_document_is_recorded_in_the_input_section() -> None:
    manifest = search_manifest.build(
        claim_text="청구항 1.",
        prompt_id="search_prompt.md",
        prompt_version=2,
        prompt_sha256="b" * 64,
        claim_boundary_neutralized=False,
        spec_document={
            "attachment_id": "att-1",
            "filename": "spec.pdf",
            "sha256": "c" * 64,
            "page_count": 30,
            "char_count": 42000,
        },
        spec_boundary_neutralized=True,
        started_at=None,
        completed_at=None,
        tool_calls=None,
        tool_uses=None,
        tool_policy_name="web_search",
        allowed_tools=("WebSearch",),
        reported=None,
        notes=None,
        error=None,
    )
    assert manifest["input"]["spec_document"]["filename"] == "spec.pdf"
    assert manifest["input"]["spec_boundary_neutralized"] is True


def test_manifest_without_a_spec_says_so_explicitly() -> None:
    manifest = search_manifest.build(
        claim_text="청구항 1.",
        prompt_id="search_prompt.md",
        prompt_version=2,
        prompt_sha256="b" * 64,
        claim_boundary_neutralized=False,
        started_at=None,
        completed_at=None,
        tool_calls=None,
        tool_uses=None,
        tool_policy_name="web_search",
        allowed_tools=("WebSearch",),
        reported=None,
        notes=None,
        error=None,
    )
    assert manifest["input"]["spec_document"] is None
    assert manifest["input"]["spec_boundary_neutralized"] is False


# --------------------------------------------------- 포인터와 본문 읽기의 구분


def _pointer_calls(content_read):
    """agy 식 열람 호출. content_read 가 본문을 읽었는지를 가른다."""
    call = {
        "id": "t1",
        "name": "read_url_content",
        "ts": "2026-08-25T00:00:00+00:00",
        "input": {"url": CANDIDATE_URL},
        "ok": True,
        "error": None,
    }
    if content_read is not None:
        call["content_read"] = content_read
    return [call]


def test_pointer_only_fetch_is_not_a_page_read() -> None:
    """agy 의 read_url_content 는 성공해도 본문을 돌려주지 않는다.

    이것을 열람으로 세면 아무도 읽지 않은 페이지가 근거가 된다. 2026-08-25
    이전 실행이 전부 그랬다 — 열람 성공은 참인데 원문 확인 0건, 근거 문장 0건.
    """
    section = search_manifest.observed(_pointer_calls(False), ["read_url_content"])
    assert section["attempted_fetch_urls"] == [CANDIDATE_URL]
    assert section["succeeded_fetch_urls"] == []


def test_fetch_whose_content_was_read_counts_as_a_page_read() -> None:
    section = search_manifest.observed(_pointer_calls(True), ["read_url_content"])
    assert section["succeeded_fetch_urls"] == [CANDIDATE_URL]


def test_provider_that_returns_content_inline_keeps_the_old_contract() -> None:
    """WebFetch 처럼 본문을 그대로 돌려주는 Provider 는 표시를 붙이지 않는다."""
    section = search_manifest.observed(_pointer_calls(None), ["read_url_content"])
    assert section["succeeded_fetch_urls"] == [CANDIDATE_URL]


def test_candidate_from_a_pointer_only_fetch_is_quarantined() -> None:
    """본문을 읽지 않았으면 그 후보는 그룹에도 대응표에도 들어가지 못한다."""
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate()]}),
        search_manifest.observed(_pointer_calls(False), ["read_url_content"]),
    )
    candidate = reported["candidates"][0]
    assert candidate["page_fetch_succeeded"] is False
    assert candidate["quarantined"] is True
    assert candidate["group_eligible"] is False
    assert candidate["mapping"] == []
    assert any("열람 기록이 없습니다" in note for note in notes)


def test_candidate_whose_page_was_read_keeps_its_page_supported_row() -> None:
    reported, _ = search_manifest.parse(
        _block({"candidates": [_candidate()]}),
        search_manifest.observed(_pointer_calls(True), ["read_url_content"]),
    )
    candidate = reported["candidates"][0]
    assert candidate["page_fetch_succeeded"] is True
    assert candidate["mapping"][0]["support_source"] == "page_text"
    # 본문을 읽었다고 해서 공식 원문 대조로 올라가지는 않는다.
    assert candidate["provenance"] == "webfetch_summary"
    assert candidate["original_verified"] is False
