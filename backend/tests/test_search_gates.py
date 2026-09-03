"""후보 식별 게이트와 행별 근거 강등.

2026-08-25 10:14 실행의 회귀 시험이다. 그 실행은 후보 3건 중 2건의 페이지를 연
적이 없었다. 한 건은 url 이 "확인 필요" 였고 다른 한 건은 KIPRIS 첫 화면이었다.
그런데도 둘 다 A/B/C 분류와 구성 대응표를 받았고, 실재하는 출원번호에 지어낸
명칭과 기술 내용이 결합된 채로 사용자에게 인쇄됐다.

그때 ARIA 가 한 조치는 발췌·위치·번역을 상수로 덮은 것뿐이었다. web 채널에서는
원문 대조가 성립하지 않아 그 세 칸이 늘 같은 문구가 되고, 검증되지 않는
counterpart·degree·similar·different 만 내용을 갖는다. 근거 있는 행과 지어낸
행이 화면에서 똑같이 보였다.

이 파일이 지키는 것은 둘이다.
  - 문헌 식별이 확인되지 않은 후보는 그룹과 대응표에 들어가지 않는다.
  - 관측 근거가 없는 행은 등급도 서술도 남기지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import search_manifest


def _block(payload: dict) -> str:
    return (
        "# 보고서\n\n본문\n\n[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]\n"
    )


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


def _observed(succeeded=(CANDIDATE_URL,)):
    calls = [
        {
            "id": "t1",
            "name": "WebFetch",
            "ts": "2026-08-25T00:00:00+00:00",
            "input": {"url": url},
            "ok": True,
            "error": None,
        }
        for url in succeeded
    ]
    return search_manifest.observed(calls, ["WebFetch"] * len(calls))


# ---------------------------------------------------------------- URL 판정


def test_portal_root_url_is_not_a_document_url() -> None:
    assert search_manifest.is_document_url("http://www.kipris.or.kr") is False
    assert search_manifest.is_document_url("https://patents.google.com/") is False
    assert search_manifest.is_document_url("확인 필요") is False
    assert search_manifest.is_document_url("") is False


def test_search_result_url_is_not_a_document_url() -> None:
    assert (
        search_manifest.is_document_url("https://patents.google.com/?q=sensor") is False
    )
    assert search_manifest.is_document_url("https://www.google.com/search?q=x") is False


def test_document_specific_url_is_accepted() -> None:
    assert search_manifest.is_document_url(
        "https://patents.google.com/patent/US11268651B2/en"
    )
    assert search_manifest.is_document_url(
        "https://worldwide.espacenet.com/publicationDetails/biblio?CC=US&NR=11268651B2"
    )


def test_identity_in_url_tolerates_country_and_kind_code_variants() -> None:
    # KIPRIS 는 URL 에 숫자만 싣는다.
    assert search_manifest.identity_in_url(
        "KR1020220054763A", "", "https://kipris.or.kr/detail?applNumber=1020220054763"
    )
    # Google Patents 는 통째로 싣는다.
    assert search_manifest.identity_in_url(
        "US11268651B2", "", "https://patents.google.com/patent/US11268651B2/en"
    )
    # 다른 문헌의 주소는 대조되지 않는다.
    assert not search_manifest.identity_in_url(
        "KR1020220054763A", "", "https://patents.google.com/patent/US11268651B2/en"
    )


# ------------------------------------------------------------ 후보 격리


def test_candidate_with_portal_root_url_is_quarantined() -> None:
    """검색 포털 첫 화면만 있는 후보는 그룹과 대응표에서 빠진다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        group="C",
                        doc_number="KR1020220054763A",
                        title="레이더 및 EO/IR 센서를 이용한 감시 장치",
                        url="http://www.kipris.or.kr",
                        provenance="search_snippet",
                        evidence_status="candidate_only",
                        note="모델이 쓴 비고",
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["quarantined"] is True
    assert candidate["group_eligible"] is False
    assert candidate["url_is_document"] is False
    # 문헌번호는 단서로 남기되 지어낸 서지정보와 대응표는 남기지 않는다.
    assert candidate["doc_number"] == "KR1020220054763A"
    assert candidate["title"] == ""
    assert candidate["applicant"] == ""
    assert candidate["note"] == ""
    assert candidate["mapping"] == []
    assert any("미확인 검색 단서로 격리" in note for note in notes)


def test_candidate_without_a_url_is_quarantined() -> None:
    reported, _ = search_manifest.parse(
        _block({"candidates": [_candidate(url="확인 필요")]}),
        _observed(),
    )
    assert reported["candidates"][0]["quarantined"] is True


def test_number_that_does_not_appear_in_the_url_loses_bibliographic_fields() -> None:
    """번호와 페이지가 서로를 가리키지 않으면 명칭·출원인을 인쇄하지 않는다.

    실재하는 번호에 다른 발명의 명칭을 붙이는 것이 이번 사고의 핵심이었다.
    후보 자체는 남긴다 — 번호는 다시 확인해 볼 단서로 쓸모가 있다.
    """
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(doc_number="KR1020229999999A")]}),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["identifier_url_matched"] is False
    # 페이지는 실제로 열렸으므로 격리 대상은 아니다.
    assert candidate["quarantined"] is False
    assert candidate["doc_number"] == "KR1020229999999A"
    assert candidate["title"] == ""
    assert candidate["applicant"] == ""
    # 검증된 칸에서는 빠지지만 통째로 사라지지는 않는다. 사용자가 직접 확인할
    # 단서가 없으면 "다시 확인해 보라"가 실행 불가능한 안내가 된다.
    assert candidate["reported_title"] == "테스트 특허"
    assert any("명칭·출원인·패밀리를 검증된 값에서 제외" in note for note in notes)
    assert any("검색 결과 기반·미검증" in note for note in notes)


# --------------------------------------------------------- 행별 근거 강등


def test_row_without_observed_support_loses_degree_and_prose() -> None:
    """근거 없는 행은 등급만 내리지 않고 서술 칸도 비운다.

    등급을 내려도 산문이 남아 있으면 사용자는 그 산문을 읽는다. 2026-08-25
    실행에서 사용자가 본 것이 정확히 그 산문이었다.
    """
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        mapping=[
                            {
                                "feature": "(E) 레이더와 EO/IR 융합 처리",
                                "counterpart": "온디바이스 AI 프레임워크로 융합",
                                "degree": "강한 대응",
                                "similar": "융합 처리가 같다",
                                "different": "외부 서버로 전송",
                            }
                        ]
                    )
                ]
            }
        ),
        _observed(),
    )
    row = reported["candidates"][0]["mapping"][0]
    assert row["support_source"] == "none"
    assert row["degree"] == search_manifest.DEGREE_UNKNOWN
    assert row["counterpart"] == ""
    assert row["similar"] == ""
    assert row["different"] == ""
    assert row["page_supported"] is False
    assert any("대응 내용·유사점·차이점을 비웠습니다" in note for note in notes)


def test_page_text_claim_without_a_successful_fetch_is_demoted() -> None:
    """열람 기록이 없는데 페이지 본문을 읽었다는 주장은 성립하지 않는다."""
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate()]}),
        _observed(succeeded=()),
    )
    candidate = reported["candidates"][0]
    # 페이지를 열지 못했으므로 후보 자체가 격리되고 대응표는 남지 않는다.
    assert candidate["quarantined"] is True
    assert candidate["mapping"] == []
    assert any("미확인 검색 단서로 격리" in note for note in notes)


def test_snippet_supported_row_keeps_prose_but_blocks_group_entry() -> None:
    """스니펫 근거는 서술을 남기되 A/B/C 진입 자격은 주지 않는다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        mapping=[
                            {
                                "feature": "제1 센서",
                                "counterpart": "센서 모듈 110",
                                "degree": "부분 대응",
                                "support_source": "snippet",
                                "support_text": "sensor module 110",
                                "support_scope": "abstract",
                            }
                        ]
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    row = candidate["mapping"][0]
    assert row["support_source"] == "snippet"
    assert row["degree"] == "부분 대응"
    assert row["counterpart"] == "센서 모듈 110"
    assert row["page_supported"] is False
    assert candidate["quarantined"] is False
    assert candidate["group_eligible"] is False
    assert any("그룹 분류에서 제외" in note for note in notes)


def test_page_supported_row_makes_the_candidate_group_eligible() -> None:
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate()]}),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["group_eligible"] is True
    assert candidate["page_supported_rows"] == 1
    assert candidate["identifier_url_matched"] is True
    # 게이트가 아무것도 강등하지 않았다. (라운드 미보고 같은 다른 메모는 무관하다)
    assert not [
        note
        for note in notes
        if "격리" in note or "제외" in note or "비웠습니다" in note
    ]


# ------------------------------------------------------ 그룹 정의 단일 출처


def test_manifest_carries_the_group_definitions_it_used() -> None:
    """렌더러가 자기 표를 들고 있으면 정의를 고친 뒤 조용히 어긋난다."""
    manifest = search_manifest.build(
        claim_text="청구항 1.",
        prompt_id="search_prompt.md",
        prompt_sha256="c" * 64,
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
    assert manifest["group_schema_version"] == search_manifest.GROUP_SCHEMA_VERSION
    assert manifest["group_definitions"] == search_manifest.GROUP_DEFINITIONS


def test_group_definitions_come_from_the_program_contract() -> None:
    """그룹 정의는 프로그램이 소유하고 매 실행에 그대로 나간다.

    모델은 감사 블록에 "A"/"B" 라는 글자만 넘기고 그 글자의 뜻은 프롬프트가
    정한다. 예전에는 그 문장이 사용자 프롬프트 본문에 있었고, 그래서 두 곳이
    갈라질 수 있었다 — 2026-08-25 실행에서 실제로 B 와 C 가 뒤집혀 나갔다.

    이제 정의는 search_contract 가 코드의 정의에서 만들어 붙인다. 사용자가 검색
    전략을 어떻게 고쳐도 이 문장은 바뀌지 않는다.
    """
    from app import search_contract, search_prompt

    contract = search_contract.group_definitions_section()
    for group, definition in search_manifest.GROUP_DEFINITIONS.items():
        assert (
            f"{group}. {definition}" in contract
        ), f"프로그램 계약에 그룹 {group} 정의가 코드와 같은 문장으로 없습니다."

    # 전략에 그룹 이야기가 한 글자도 없어도 조립된 본문에는 들어간다.
    rendered = search_prompt.compose("아무 전략이나 적는다.", "청구항 1. 장치.").body
    for group, definition in search_manifest.GROUP_DEFINITIONS.items():
        assert f"{group}. {definition}" in rendered


def test_the_frontend_keeps_no_copy_of_the_group_definitions() -> None:
    """그룹 정의는 백엔드가 소유하고 프런트는 자기 사본을 갖지 않는다.

    예전 게이트는 정반대를 요구했다 — 프런트가 같은 정의를 fallback 표로
    들고 있는지 대조했다. 그 표가 실제로 사라진 뒤에는 그 게이트가 **없어진
    중복을 되살리라고 요구하는** 셈이 되어, 방향을 뒤집었다. 막으려는 사고는
    처음부터 하나였다: 두 곳에 적힌 정의가 갈라지는 것(2026-08-25 실행에서
    B 와 C 가 뒤집혀 나갔다).

    정의를 화면에 표시해야 하면 프런트는 ``manifest.group_definitions`` 를
    읽는다. 그 값이 매 실행 매니페스트에 실린다는 사실과 프로그램 계약에
    주입된다는 사실은 바로 위 두 게이트가 지킨다. 정의를 싣지 않는 옛
    매니페스트는 백엔드 렌더러가 그리므로(test_search_report 의
    ``test_a_past_manifest_without_group_definitions_still_renders``) 프런트에
    fallback 표를 새로 만들 이유가 없다.

    테스트 파일은 보지 않는다. 매니페스트를 흉내 내는 픽스처가 정의 문장을
    담는 것은 사본이 아니라 입력 데이터다.
    """
    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    sources = [path for path in root.rglob("*.ts*") if ".test." not in path.name]
    assert sources, "프런트엔드 소스를 찾지 못했습니다."

    # 옛 C 정의까지 본다. 되살아나는 중복이 현행 A/B 라는 보장이 없다.
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for group, definition in search_manifest.LEGACY_GROUP_DEFINITIONS.items():
            assert definition not in text, (
                f"{path.relative_to(root)} 에 그룹 {group} 의 정의 문장이 "
                "복제되어 있습니다. 정의는 백엔드가 소유하고 매니페스트의 "
                "group_definitions 로 전달합니다."
            )


def test_paper_without_an_identifier_keeps_its_title_when_the_page_was_opened() -> None:
    """대조할 번호도 DOI 도 없으면 번호-주소 대조를 요구할 수 없다.

    막으려는 실패는 '실재하는 번호에 다른 발명의 명칭 붙이기'다. 번호가 없으면
    그 실패가 성립하지 않는다. 실제로 연 전용 페이지라는 사실을 근거로 쓴다.
    """
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(doc_type="paper", doc_number="", doi="", title="어떤 논문")
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["identifier_url_matched"] is True
    assert candidate["title"] == "어떤 논문"


def test_paper_with_a_doi_still_requires_the_doi_to_match_the_url() -> None:
    reported, _ = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        doc_type="paper",
                        doc_number="",
                        doi="10.1234/other.9999",
                        title="어떤 논문",
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["identifier_url_matched"] is False
    assert candidate["title"] == ""


# ------------------------------------------------- 우회 사례 (2차 검토에서 발견)
#
# 1차 게이트를 넣은 뒤에도 세 가지가 그룹 진입에 성공했다. 모두 "ARIA 가 확인할
# 수 있는데 확인하지 않은" 항목이다.


def test_number_mismatch_blocks_group_entry_not_just_bibliography() -> None:
    """번호가 URL 과 대조되지 않으면 그룹에도 들어갈 수 없다.

    서지정보만 지우는 것으로는 부족하다. 번호는 A 라고 적고 B 의 페이지를 연
    후보의 대응 행은 주장한 문헌이 아니라 다른 문헌에서 온 것이다.
    """
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(doc_number="KR9999999999A")]}),
        _observed(),
    )
    candidate = reported["candidates"][0]
    assert candidate["identifier_url_matched"] is False
    assert candidate["group_eligible"] is False
    assert any("실제로 연 주소에서 확인되지 않았습니다" in note for note in notes)


def test_page_text_without_support_text_is_not_page_supported() -> None:
    """근거 텍스트 없이 page_text 만 적는 것으로 그룹에 들어갈 수 없다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        mapping=[
                            {
                                "feature": "f",
                                "counterpart": "c",
                                "degree": "강한 대응",
                                "support_source": "page_text",
                                "support_text": "",
                                "support_url": CANDIDATE_URL,
                            }
                        ]
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    row = candidate["mapping"][0]
    assert row["support_source"] == "none"
    assert row["page_supported"] is False
    assert row["degree"] == search_manifest.DEGREE_UNKNOWN
    assert row["counterpart"] == ""
    assert candidate["group_eligible"] is False
    assert any("근거 텍스트 없이" in note for note in notes)


def test_support_url_pointing_elsewhere_is_downgraded() -> None:
    """근거 주소가 이 후보의 전용 페이지가 아니면 페이지 근거로 인정하지 않는다."""
    reported, notes = search_manifest.parse(
        _block(
            {
                "candidates": [
                    _candidate(
                        mapping=[
                            {
                                "feature": "f",
                                "counterpart": "c",
                                "degree": "강한 대응",
                                "support_source": "page_text",
                                "support_text": "무언가 읽은 문장",
                                "support_url": "https://elsewhere.example.com/zzz",
                            }
                        ]
                    )
                ]
            }
        ),
        _observed(),
    )
    candidate = reported["candidates"][0]
    row = candidate["mapping"][0]
    # 읽은 문장이 있다고 했으므로 서술은 남기되 페이지 근거로는 세지 않는다.
    assert row["support_source"] == "snippet"
    assert row["page_supported"] is False
    assert row["counterpart"] == "c"
    assert candidate["group_eligible"] is False
    assert any("이 후보의 전용 페이지가 아니어서" in note for note in notes)


def test_support_url_that_was_never_opened_is_downgraded() -> None:
    """후보 URL 과 같아도 그 주소를 실제로 열지 않았으면 인정하지 않는다."""
    reported, notes = search_manifest.parse(
        _block({"candidates": [_candidate(url=CANDIDATE_URL)]}),
        _observed(succeeded=("https://patents.example.com/patent/OTHER123456",)),
    )
    candidate = reported["candidates"][0]
    # 이 후보의 페이지를 연 적이 없으므로 후보 자체가 격리된다.
    assert candidate["quarantined"] is True
    assert candidate["group_eligible"] is False


def test_legacy_manifest_is_labelled_in_the_report() -> None:
    """게이트 이전 기록은 덮어쓰지 않고 그 사실을 밝힌다."""
    from app import search_report

    legacy = {
        "version": 3,
        "reported": {"candidates": [], "rounds": [], "access_failures": []},
        "observed": {},
        "input": {},
        "normalization_notes": [],
    }
    report = search_report.render(legacy)
    assert "게이트가 적용되기 전에 생성되었습니다" in report
