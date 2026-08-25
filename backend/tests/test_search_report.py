"""사용자 보고서 생성.

검색 작업의 보고서는 모델이 쓴 산문이 아니라 ARIA 가 검증한 구조화 필드에서
만든다. 이 파일이 지키는 것은 하나다 — WebFetch 요약에서 온 문장이 보고서의
'원문 직접 발췌' 칸에 절대 들어가지 않는다.
"""

from __future__ import annotations

import json

from app import search_manifest, search_report

FABRICATED = "상기 제1 센서는 제2 센서와 직렬로 연결되며"
# 후보 전용 페이지이고 주소 안에 후보의 문헌번호가 들어 있다. 둘 중 하나라도
# 아니면 후보가 격리되거나 서지정보가 지워진다.
FETCHED_URL = "https://patents.example.com/patent/AB1234"


def _manifest(candidates, notes=None, observed=None, spec=None, expansions=None):
    return search_manifest.build(
        claim_text="청구항 1. 테스트",
        prompt_id="search_prompt.md",
        prompt_version=1,
        prompt_sha256="a" * 64,
        claim_boundary_neutralized=False,
        spec_document=spec,
        started_at="2026-08-21T00:00:00+00:00",
        completed_at="2026-08-21T00:05:00+00:00",
        tool_calls=[
            {
                "id": "t1",
                "name": "WebSearch",
                "ts": "2026-08-21T00:00:01+00:00",
                "input": {"query": "테스트 검색식"},
                "ok": True,
                "error": None,
            },
            {
                "id": "t2",
                "name": "WebFetch",
                "ts": "2026-08-21T00:00:02+00:00",
                "input": {"url": FETCHED_URL},
                "ok": True,
                "error": None,
            },
        ],
        tool_uses=["WebSearch", "WebFetch"],
        tool_policy_name="web_search",
        allowed_tools=("WebSearch", "WebFetch"),
        reported={
            "rounds": [],
            "term_expansions": expansions or [],
            "candidates": candidates,
            "access_failures": [],
        },
        notes=notes or [],
        error=None,
    )


def _parsed(entry: dict):
    """모델이 보고한 항목을 실제 정규화 경로에 통과시킨다."""
    block = (
        "[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps({"candidates": [entry]}, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]"
    )
    observed = search_manifest.observed(
        [
            {
                "id": "t2",
                "name": "WebFetch",
                "ts": "2026-08-21T00:00:02+00:00",
                "input": {"url": FETCHED_URL},
                "ok": True,
                "error": None,
            }
        ],
        ["WebFetch"],
    )
    return search_manifest.parse(block, observed)


def _hostile_candidate(**overrides) -> dict:
    base = {
        "group": "A",
        "provisional": False,
        "channel": "web",
        "doc_type": "patent",
        "doc_number": "AB1234",
        "title": "테스트 특허",
        "applicant": "테스트 주식회사",
        "url": FETCHED_URL,
        "provenance": "raw_original_verified",
        "evidence_status": "source_page_reviewed",
        "verbatim_excerpt": FABRICATED,
        "source_location": "청구항 1, 3컬럼 12행",
        "mapping": [
            {
                "feature": "제1 센서",
                "counterpart": "센서 모듈 110",
                "degree": "강한 대응",
                "support_source": "page_text",
                "support_text": "a sensor module 110 coupled to the housing",
                "support_scope": "abstract",
                "source_location": "문단 [0032]",
                "verbatim_excerpt": FABRICATED,
                "translation": "the first sensor is connected in series",
                "similar": "직렬 연결 구조가 같다",
                "different": "제어부 구성이 다르다",
            }
        ],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------- 위반 사례


def test_fabricated_excerpt_never_reaches_the_user_report() -> None:
    """모델이 원문 대조를 주장해도 web 채널에서는 발췌가 통과하지 못한다."""
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert FABRICATED not in report
    assert "3컬럼 12행" not in report
    assert "문단 [0032]" not in report
    assert "the first sensor is connected in series" not in report
    assert "원문에서 확인되지 않음" in report
    assert "확인 필요" in report


def test_report_keeps_the_analysis_while_dropping_the_quote() -> None:
    """대응 설명은 남는다. 지우는 것은 원문을 주장하는 칸뿐이다."""
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert "제1 센서" in report
    assert "센서 모듈 110" in report
    assert "직렬 연결 구조가 같다" in report
    assert "제어부 구성이 다르다" in report
    assert "강한 대응" in report


def test_report_marks_unverified_candidates_as_provisional() -> None:
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert "잠정 분류" in report
    assert "원문 대조 안 됨" in report


def test_report_states_the_disclaimer() -> None:
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert search_report.DISCLAIMER in report
    assert "선행기술" not in report.split("## 요약")[0].replace(
        search_report.DISCLAIMER, ""
    )


def test_report_shows_normalization_notes() -> None:
    reported, notes = _parsed(_hostile_candidate())
    assert notes, "정규화 메모가 비어 있으면 사용자가 무엇이 조정됐는지 모른다."
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert "ARIA 가 조정한 증거 등급" in report
    for note in notes:
        assert note in report


def test_report_distinguishes_attempted_from_succeeded_fetches() -> None:
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert "페이지 열람 시도" in report
    assert "성공" in report


def test_report_isolates_a_candidate_that_was_never_fetched() -> None:
    """열람 기록이 없는 후보는 그룹과 대응표에서 빠지고 단서로만 남는다."""
    reported, notes = _parsed(
        _hostile_candidate(url="https://never-fetched.example.com/AB1234")
    )
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert "## 미확인 검색 단서" in report
    assert "성공한 페이지 열람 기록이 없습니다" in report
    # 그룹 제목 아래에는 아무것도 없다.
    assert "전체 구조와 핵심 특징이 모두 강하게 유사" not in report
    # 검증되지 않은 서지정보와 대응 서술은 인쇄하지 않는다.
    assert "테스트 특허" not in report
    assert "테스트 주식회사" not in report
    assert "센서 모듈 110" not in report


def test_pipes_and_newlines_cannot_break_the_evidence_table() -> None:
    """모델이 셀 안에 파이프나 줄바꿈을 넣어 표 구조를 깨뜨리지 못한다."""
    reported, notes = _parsed(
        _hostile_candidate(
            mapping=[
                {
                    "feature": "제1 센서 | 추가열 | 또 다른 열",
                    "counterpart": "여러\n줄로\n쓴 값",
                    "degree": "부분 대응",
                    "support_source": "page_text",
                    "support_text": "관측 | 근거\n텍스트",
                    "support_scope": "abstract",
                    "similar": "-",
                    "different": "-",
                }
            ]
        )
    )
    report = search_report.render(_manifest(reported["candidates"], notes))
    table_rows = [
        line
        for line in report.splitlines()
        if line.startswith("| ") and "---" not in line
    ]
    # 헤더 1줄 + 데이터 1줄. 모든 줄의 칸 수가 같아야 한다.
    widths = {line.count(" | ") for line in table_rows}
    assert len(widths) == 1, table_rows
    assert "\\|" in report
    assert "여러 줄로 쓴 값" in report


def test_empty_candidate_list_still_renders() -> None:
    report = search_report.render(_manifest([]))
    assert "제시된 후보가 없습니다" in report
    assert search_report.DISCLAIMER in report


def test_report_says_when_nothing_was_verified() -> None:
    reported, notes = _parsed(_hostile_candidate())
    report = search_report.render(_manifest(reported["candidates"], notes))
    assert "원문 대조가 확인된 문헌은 없습니다" in report


# ------------------------------------------------ 명세서를 어떻게 반영했는가

_SPEC = {
    "attachment_id": "att-1",
    "filename": "spec.pdf",
    "sha256": "c" * 64,
    "page_count": 30,
    "char_count": 42000,
}


def test_report_says_nothing_about_a_spec_that_was_not_used() -> None:
    report = search_report.render(_manifest([]))
    assert "출원발명 문서" not in report


def test_report_shows_the_spec_and_term_expansion() -> None:
    report = search_report.render(
        _manifest(
            [],
            spec=_SPEC,
            expansions=[
                {
                    "claim_term": "제어부",
                    "alternative_meanings": ["일반 제어 회로", "FPGA 신호 처리 회로"],
                    "expanded_terms": ["controller", "FPGA"],
                    "basis": "문단 [0021]",
                    "excluded_limitations": ["특정 FPGA 모델"],
                }
            ],
        )
    )
    assert "출원발명 문서를 이용한 별도 검색 확장" in report
    assert "spec.pdf" in report
    assert "문단 [0021]" in report
    assert "controller" in report
    assert "특정 FPGA 모델" in report
    assert "합집합 병합 단계에서 금지" in report


def test_report_flags_a_spec_with_no_expansion_record() -> None:
    report = search_report.render(_manifest([], spec=_SPEC))
    assert "어떻게 반영했는지는 확인할 수 없습니다" in report


def test_expansion_table_cannot_be_broken_by_pipes() -> None:
    report = search_report.render(
        _manifest(
            [],
            spec=_SPEC,
            expansions=[
                {
                    "claim_term": "제어부 | 파이프",
                    "alternative_meanings": ["줄바꿈\n포함"],
                    "expanded_terms": [],
                    "basis": "문단 [0021]",
                    "excluded_limitations": [],
                }
            ],
        )
    )
    row = next(line for line in report.splitlines() if "파이프" in line)
    # 이스케이프된 파이프는 칸을 나누지 않는다.
    assert row.replace(chr(92) + "|", "").count("|") == 6
    assert "줄바꿈 포함" in row
