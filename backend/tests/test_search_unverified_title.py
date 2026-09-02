"""검증되지 않은 제목의 보존과 표시.

2026-09-02 실행이 이 파일의 이유다. 그 실행에서 후보의 명칭은 문헌번호-주소
대조를 통과하지 못해 **통째로 지워졌고**, 사용자에게 남은 것은 번호와 주소뿐
이었다. 그러면 "직접 확인해 보라"는 안내가 실행 불가능해진다 — 무엇을 확인해야
하는지가 사라지기 때문이다.

지우는 것과 등급을 낮춰 적는 것은 다르다. 이 파일이 지키는 것은 둘이다.

  - 미검증 제목은 사라지지 않고 reported_title 에 남아 보고서에 표시된다.
  - 그 값은 **어떤 게이트도 통과시키지 않는다.** 정식 A/B, 구성 대응표,
    직접 발췌 어디에도 쓰이지 않는다.
"""

from __future__ import annotations

import json

import pytest

from app import search_manifest, search_report

#: 후보 전용 페이지. 여기에 후보의 문헌번호가 들어 있어야 대조를 통과한다.
FETCHED_URL = "https://patents.example.com/patent/AB1234"
#: 열긴 열었는데 후보가 주장한 번호가 들어 있지 않은 주소.
MISMATCHED_URL = "https://arxiv.org/abs/2412.02317"

REPORTED = "HumanRig: Learning Automatic Rigging for Humanoid Characters"


def _block(candidates: list[dict]) -> str:
    return (
        "[ARIA_SEARCH_LOG_V1]\n"
        + json.dumps({"candidates": candidates}, ensure_ascii=False)
        + "\n[/ARIA_SEARCH_LOG_V1]"
    )


def _observed(urls=(FETCHED_URL,)):
    calls = [
        {
            "id": f"t{index}",
            "name": "WebFetch",
            "ts": "2026-09-02T00:00:00+00:00",
            "input": {"url": url},
            "ok": True,
            "error": None,
        }
        for index, url in enumerate(urls, start=1)
    ]
    return search_manifest.observed(calls, ["WebFetch"] * len(calls))


def _candidate(**overrides) -> dict:
    base = {
        "group": "A",
        "provisional": True,
        "channel": "web",
        "doc_type": "paper",
        "doc_number": "AB1234",
        "title": "",
        "reported_title": REPORTED,
        "applicant": "",
        "url": MISMATCHED_URL,
        "provenance": "search_snippet",
        "evidence_status": "candidate_only",
        "mapping": [
            {
                "feature": "제1 센서",
                "counterpart": "센서 모듈 110",
                "degree": "강한 대응",
                "support_source": "page_text",
                "support_text": "a sensor module 110 coupled to the housing",
                "support_scope": "abstract",
            }
        ],
    }
    base.update(overrides)
    return base


def _manifest(candidates, notes=None) -> dict:
    return search_manifest.build(
        claim_text="청구항 1. 테스트",
        prompt_id="search_prompt.md",
        prompt_sha256="a" * 64,
        claim_boundary_neutralized=False,
        started_at="2026-09-02T00:00:00+00:00",
        completed_at="2026-09-02T00:05:00+00:00",
        tool_calls=[],
        tool_uses=[],
        tool_policy_name="agy_web_search",
        allowed_tools=("search_web", "read_url_content"),
        reported={
            "rounds": [],
            "term_expansions": [],
            "candidates": candidates,
            "access_failures": [],
        },
        notes=notes or [],
        error=None,
    )


# ------------------------------------------------------------------ 보존


def test_the_reported_title_survives_when_the_page_was_never_opened() -> None:
    """페이지를 열지 못한 후보도 검색 결과에서 본 제목을 잃지 않는다."""
    reported, _ = search_manifest.parse(_block([_candidate()]), _observed(()))
    candidate = reported["candidates"][0]

    assert candidate["title"] == ""
    assert candidate["reported_title"] == REPORTED
    assert search_manifest.unverified_title(candidate) == REPORTED


def test_a_model_title_is_preserved_even_without_the_new_field() -> None:
    """모델이 옛 방식대로 title 에만 적어도 그 값은 남는다.

    새 칸을 쓰지 않는 모델 출력에서 제목이 사라지면, 이 변경은 프롬프트를 고친
    실행에서만 동작하는 셈이 된다.
    """
    entry = _candidate(title="검색 결과에서 본 제목")
    entry.pop("reported_title")
    reported, _ = search_manifest.parse(_block([entry]), _observed(()))
    candidate = reported["candidates"][0]

    assert candidate["title"] == ""
    assert candidate["reported_title"] == "검색 결과에서 본 제목"


def test_a_verified_title_wins_over_the_reported_one() -> None:
    """대조를 통과하면 검증된 명칭이 표시되고 미검증 제목은 물러난다.

    둘을 나란히 보여 주면 같은 위계로 읽히고, 그러면 칸을 나눈 의미가 없다.
    """
    entry = _candidate(
        url=FETCHED_URL,
        title="공식 페이지에서 확인한 명칭",
        evidence_status="source_page_reviewed",
        provenance="webfetch_summary",
    )
    reported, _ = search_manifest.parse(_block([entry]), _observed())
    candidate = reported["candidates"][0]

    assert candidate["title"] == "공식 페이지에서 확인한 명칭"
    assert candidate["identifier_url_matched"] is True
    # 승격에 별도 단계가 필요 없다. title 이 차는 순간 표시가 그쪽으로 넘어간다.
    assert search_manifest.unverified_title(candidate) == ""


# ------------------------------------------------------- 게이트를 열지 않는다


def test_an_unverified_title_alone_grants_no_group_or_mapping() -> None:
    """제목이 있다고 A/B 나 구성 대응표가 생기지 않는다.

    이 후보는 실재하는 번호에 확인되지 않은 제목이 붙어 있을 수 있는 상태다.
    바로 그 결합이 이 작업에서 가장 위험한 오류이므로, 미검증 제목은 어떤
    판단 근거도 되지 못한다.
    """
    reported, _ = search_manifest.parse(_block([_candidate()]), _observed(()))
    candidate = reported["candidates"][0]

    assert candidate["reported_title"] == REPORTED
    assert candidate["group"] is None
    assert candidate["group_eligible"] is False
    assert candidate["mapping"] == []
    assert candidate["evidence_status"] == "candidate_only"
    assert candidate["verbatim_excerpt"] == search_manifest.UNVERIFIED_EXCERPT


def test_the_unverified_title_does_not_lift_a_quarantine() -> None:
    """포털 주소를 적은 후보는 제목이 있어도 격리된다."""
    entry = _candidate(url="https://arxiv.org")
    reported, _ = search_manifest.parse(_block([entry]), _observed(()))
    candidate = reported["candidates"][0]

    assert candidate["quarantined"] is True
    assert candidate["reported_title"] == REPORTED
    assert candidate["mapping"] == []


# ------------------------------------------------------------------ 표시


def test_the_report_prints_the_title_link_and_status_together() -> None:
    """제목·링크·상태가 한 덩어리로 나간다. 셋이 흩어지면 확인할 수 없다."""
    reported, notes = search_manifest.parse(_block([_candidate()]), _observed(()))
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert "제목(검색 결과 기반·미검증): " + REPORTED in report
    assert f"링크: <{MISMATCHED_URL}>" in report
    assert "상태: 페이지 직접 확인 안 됨 — 사용자가 수동 확인 필요" in report
    # 검증된 명칭으로 오해될 수 있는 라벨은 붙이지 않는다.
    assert f"- 명칭: {REPORTED}" not in report


def test_the_report_does_not_label_a_verified_title_as_unverified() -> None:
    entry = _candidate(
        url=FETCHED_URL,
        title="공식 페이지에서 확인한 명칭",
        evidence_status="source_page_reviewed",
        provenance="webfetch_summary",
    )
    reported, notes = search_manifest.parse(_block([entry]), _observed())
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert "- 명칭: 공식 페이지에서 확인한 명칭" in report
    assert "검색 결과 기반·미검증" not in report


def test_the_report_still_hides_unverified_bibliographic_fields() -> None:
    """제목을 등급과 함께 적는다고 출원인·패밀리까지 여는 것은 아니다."""
    entry = _candidate(applicant="확인되지 않은 출원인", family="확인되지 않은 패밀리")
    reported, notes = search_manifest.parse(_block([entry]), _observed(()))
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert "확인되지 않은 출원인" not in report
    assert "확인되지 않은 패밀리" not in report


# ---------------------------------------------- 서지 API 확인으로의 승격


def test_a_bibliographic_hit_promotes_the_title_but_not_the_evidence() -> None:
    """Crossref·Europe PMC 가 제목을 확인하면 검증된 칸으로 올라간다.

    다만 그것은 "등록 서지가 그 제목을 말한다"이지 "논문 원문을 대조했다"가
    아니다. 그 구분은 증거 등급이 유지한다.
    """
    entry = _candidate(doi="10.1145/1", doc_number="")
    reported, _ = search_manifest.parse(_block([entry]), _observed(()))
    assert reported["candidates"][0]["title"] == ""

    merged, notes = search_manifest.merge_literature_discoveries(
        reported,
        {
            "candidates": [
                {
                    "doi": "10.1145/1",
                    "title": "등록 서지가 확인해 준 제목",
                    "authors": "저자",
                    "container": "학회",
                    "url": "https://doi.org/10.1145/1",
                    "sources": ["crossref"],
                }
            ]
        },
    )
    candidate = merged["candidates"][0]

    assert candidate["title"] == "등록 서지가 확인해 준 제목"
    assert search_manifest.unverified_title(candidate) == ""
    # 검색 결과가 뭐라고 했는지도 기록이다. 승격이 그것을 지우지 않는다.
    assert candidate["reported_title"] == REPORTED
    # 승격은 제목까지다. 원문 대조도 그룹 자격도 따라 오르지 않는다.
    assert candidate["original_verified"] is False
    assert candidate["group_eligible"] is False
    assert notes


# ------------------------------------------------- 링크로 만들 수 있는 주소
#
# 후보의 url 은 모델이 적은 값이고, 모델의 입력에는 검색 결과와 페이지 본문이
# 섞여 있다. 즉 비신뢰 데이터가 도달할 수 있는 자리다. 렌더러의 sanitize 는
# 마지막 방어선이지 유일한 방어선이 아니어야 하므로, 만들어 놓고 지우는 대신
# 애초에 링크로 만들지 않는다.

DANGEROUS_URLS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///C:/Windows/System32/drivers/etc/hosts",
    "vbscript:msgbox(1)",
]


@pytest.mark.parametrize("url", DANGEROUS_URLS)
def test_non_http_schemes_are_not_linkable(url: str) -> None:
    assert search_manifest.is_linkable_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "확인 필요",
        "patents.example.com/AB1234",
        "https://",
        "http:///path",
        "mailto:someone@example.com",
        "://broken",
    ],
)
def test_values_that_are_not_absolute_web_addresses_are_not_linkable(url: str) -> None:
    """상대 주소나 파싱할 수 없는 값도 링크로 만들지 않는다.

    base 를 붙여 절대 주소로 만들지 않는다 — 그러면 모델이 적은 값이 ARIA
    자신의 출처를 가리키게 된다.
    """
    assert search_manifest.is_linkable_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/2412.02317",
        "http://www.kipris.or.kr/AB1234",
        "HTTPS://PATENTS.EXAMPLE.COM/AB1234",
    ],
)
def test_http_and_https_are_linkable(url: str) -> None:
    assert search_manifest.is_linkable_url(url) is True


@pytest.mark.parametrize("url", DANGEROUS_URLS)
def test_the_report_prints_dangerous_urls_as_plain_text(url: str) -> None:
    """위험한 스킴은 평문으로만 나간다. 다만 지우지도 않는다.

    모델이 무엇을 적었는지는 그 자체로 기록이다. 평문이면 클릭되지 않는다.
    """
    reported, notes = search_manifest.parse(
        _block([_candidate(url=url)]), _observed(())
    )
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert url in report
    assert f"<{url}>" not in report
    assert f"]({url})" not in report


def test_the_report_still_links_http_addresses() -> None:
    reported, notes = search_manifest.parse(
        _block([_candidate(url="https://arxiv.org/abs/2412.02317")]), _observed(())
    )
    report = search_report.render(_manifest(reported["candidates"], notes))

    assert "<https://arxiv.org/abs/2412.02317>" in report
