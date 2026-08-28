"""검색 결과 사용자 보고서 생성.

분석 작업과 다른 점이 하나 있다. 분석 작업의 보고서는 모델이 쓴 Markdown 을
그대로 사용자에게 준다. 검색 작업은 그렇게 하지 않는다.

이유는 증거 등급이다. WebFetch 는 페이지 원문이 아니라 다른 모델이 추출한
요약을 돌려주므로, 그 문장은 특허·논문의 직접 인용문이 될 수 없다. 그런데
모델이 자유 형식 Markdown 안에서 그 문장을 따옴표로 묶어 "청구항 1에 이렇게
적혀 있다"고 쓰면, ARIA 는 그것을 알아볼 방법이 없다. 임의의 산문에서 어떤
따옴표가 원문 인용 주장인지 판별하는 것은 정규식으로 풀 문제가 아니다.

그래서 방향을 뒤집었다. 사용자가 보는 보고서를 ARIA 가 검증된 구조화 필드에서
직접 만든다. 발췌 칸에는 search_manifest 가 원문 확인 여부를 대조한 값만
들어가며, 확인되지 않은 후보에서는 어떤 문자열도 그 칸에 들어갈 수 없다.
모델의 원문 산문은 버리지 않고 model_report.md 와 stdout.log 에 남기지만,
보고서 본문으로 승격시키지는 않는다.

결과적으로 감사 블록이 이 작업의 필수 출력이 된다. 블록이 없으면 만들 보고서가
없고, 검증되지 않은 산문을 대신 내보내면 이 설계의 목적 자체가 사라진다.
"""

from __future__ import annotations

from .search_manifest import (
    EVIDENCE_REVIEWED,
    GROUP_DEFINITIONS,
    GROUPS,
    PROV_RAW,
    PROV_SNIPPET,
    PROV_WEBFETCH,
    SCOPE_ABSTRACT,
    SCOPE_CLAIMS,
    SCOPE_FULL_TEXT,
    SCOPE_UNKNOWN,
    SUPPORT_NONE,
    SUPPORT_PAGE,
    SUPPORT_SNIPPET,
)

DISCLAIMER = (
    "현재 검색 결과는 웹 기반 검토 후보 탐색 자료입니다. "
    "원문 직접 인용과 법적 판단을 보장하지 않습니다."
)

# 후보 식별 게이트와 행별 근거 게이트가 들어간 매니페스트 버전. 이보다 낮은
# 기록은 그 검사를 통과한 적이 없으므로 화면에서 그 사실을 밝힌다.
LEGACY_GATE_VERSION = 4

def _group_titles(manifest: dict) -> dict[str, str]:
    """이 실행이 실제로 쓴 그룹 정의에서 제목을 만든다.

    정의는 search_manifest 가 소유하고 매니페스트에 실려 온다. 렌더러가 자기
    표를 들고 있으면 정의를 고친 뒤 갱신되지 않은 렌더러가 옛 제목으로 인쇄한다.
    2026-08-25 실행에서 실제로 그렇게 어긋나 B 와 C 가 뒤집혀 나갔다.

    정의를 싣지 않는 옛 매니페스트에서만 현재 정의로 채운다.
    """
    stored = manifest.get("group_definitions")
    definitions = stored if isinstance(stored, dict) and stored else GROUP_DEFINITIONS
    return {
        group: f"{group}. {definitions.get(group) or GROUP_DEFINITIONS[group]}"
        for group in GROUPS
    }


_SUPPORT_LABEL = {
    SUPPORT_PAGE: "페이지 관측",
    SUPPORT_SNIPPET: "검색 스니펫",
    SUPPORT_NONE: "근거 없음",
}

_SCOPE_LABEL = {
    SCOPE_CLAIMS: "청구항",
    SCOPE_FULL_TEXT: "전문",
    SCOPE_ABSTRACT: "초록",
    SCOPE_UNKNOWN: "확인 필요",
}

_PROVENANCE_LABEL = {
    PROV_SNIPPET: "검색 스니펫만 확인 (페이지 미열람)",
    PROV_WEBFETCH: "페이지 요약 확인 (원문 아님)",
    PROV_RAW: "원문 대조 완료",
}

_EVIDENCE_LABEL = {
    "candidate_only": "후보 단계",
    EVIDENCE_REVIEWED: "페이지 열람 성공",
}

_ORIGIN_LABEL = {
    "claim_only": "청구항 단독 검색",
    "spec_assisted": "명세서 보조 확장 검색",
}

_EXPANSION_HEADERS = (
    "청구항 문언",
    "가능한 의미",
    "추가 검색어",
    "명세서 근거",
    "검색 제한에 쓰지 않은 한정",
)

# 근거 칸을 결론 칸보다 왼쪽에 둔다. 사용자가 대응 정도를 읽기 전에 그 판단이
# 무엇에 기대고 있는지를 먼저 보게 하려는 것이다. 근거가 "근거 없음"인 행은
# 대응 정도·대응 내용·유사점·차이점이 이미 비워져 있다.
_MAPPING_HEADERS = (
    "청구항의 기술적 특징",
    "근거 출처",
    "관측 근거 텍스트",
    "검토 범위",
    "대응 정도",
    "검색 문헌의 대응 내용",
    "원문 위치",
    "원문 직접 발췌",
    "한국어 번역",
    "유사한 점",
    "차이가 있는 점",
)


def _cell(value: str) -> str:
    """표 한 칸. 파이프와 줄바꿈이 표를 깨지 않게 한다."""
    text = str(value or "").replace("|", "\\|")
    return " ".join(text.split()) or "-"


def _candidate_section(item: dict) -> list[str]:
    identity = item["doc_number"] or item["doi"] or "문헌번호 확인 필요"
    lines = [f"#### {identity}"]
    if item["title"]:
        lines.append(f"- 명칭: {item['title']}")
    if item["applicant"]:
        lines.append(f"- 출원인·저자: {item['applicant']}")
    if item["doi"] and item["doc_number"]:
        lines.append(f"- DOI: {item['doi']}")
    lines.append(f"- 패밀리: {item['family'] or '확인 필요'}")
    if item["url"]:
        lines.append(f"- 원문 링크: {item['url']}")
    lines.append(
        "- 증거 등급: "
        + _PROVENANCE_LABEL.get(item["provenance"], item["provenance"])
        + " / "
        + _EVIDENCE_LABEL.get(item["evidence_status"], item["evidence_status"])
    )
    origins = item.get("search_origins") or []
    if origins:
        lines.append(
            "- 발견 경로: "
            + " + ".join(_ORIGIN_LABEL.get(origin, origin) for origin in origins)
        )
    lines.append(
        "- ARIA 관측: 페이지 본문 "
        + ("읽음" if item["page_fetch_succeeded"] else "읽은 기록 없음")
        + " · 원문 대조 "
        + ("완료" if item["original_verified"] else "안 됨")
        + " · 문헌번호-주소 대조 "
        + ("완료" if item.get("identifier_url_matched") else "안 됨")
    )
    if not item.get("identifier_url_matched", True):
        lines.append(
            "- 이 후보의 문헌번호가 위 주소에서 확인되지 않아 명칭·출원인·"
            "패밀리를 표시하지 않았습니다."
        )
    if item["provisional"]:
        lines.append("- **잠정 분류** — 원문으로 검증되지 않았습니다.")
    lines.append(f"- 원문 위치: {item['source_location']}")
    lines.append(f"- 직접 발췌: {item['verbatim_excerpt']}")
    if item["note"]:
        lines.append(f"- 비고: {item['note']}")

    if item["mapping"]:
        lines += [
            "",
            "청구항 구성 대응표",
            "",
            "| " + " | ".join(_MAPPING_HEADERS) + " |",
            "| " + " | ".join(["---"] * len(_MAPPING_HEADERS)) + " |",
        ]
        for row in item["mapping"]:
            support = row.get("support_source", SUPPORT_NONE)
            lines.append(
                "| "
                + " | ".join(
                    _cell(value)
                    for value in (
                        row["feature"],
                        _SUPPORT_LABEL.get(support, support),
                        row.get("support_text") or "-",
                        _SCOPE_LABEL.get(
                            row.get("support_scope", SCOPE_UNKNOWN), SCOPE_UNKNOWN
                        ),
                        row["degree"],
                        row["counterpart"],
                        row["source_location"],
                        row["verbatim_excerpt"],
                        row["translation"],
                        row["similar"],
                        row["different"],
                    )
                )
                + " |"
            )
    lines.append("")
    return lines


def _expansion_section(manifest: dict, reported: dict) -> list[str]:
    """명세서가 독립 보조 검색의 검색어를 어떻게 확장했는지."""
    spec = (manifest.get("input") or {}).get("spec_document") or None
    rows = reported.get("term_expansions") or []
    if not spec and not rows:
        return []

    lines = ["## 출원발명 문서를 이용한 별도 검색 확장", ""]
    if spec:
        lines.append(
            f"- 참고 자료: {spec.get('filename') or '이름 없음'} · "
            f"{int(spec.get('char_count') or 0):,}자"
        )
        lines.append(
            "- 청구항 단독 검색에는 이 문서가 전달되지 않았습니다. 아래 용어는 "
            "별도의 명세서 보조 검색에만 사용했고, 두 결과는 합집합으로 병합했습니다."
        )
    else:
        lines.append(
            "- 이 실행에는 출원발명 문서를 넣지 않았습니다. 아래 확장 기록에는 "
            "대조할 명세서가 없습니다."
        )

    if not rows:
        lines += [
            "- 모델이 보고한 용어 확장 기록이 없습니다. 명세서를 검색어에 "
            "어떻게 반영했는지는 확인할 수 없습니다.",
            "",
        ]
        return lines

    lines += [
        "",
        "| " + " | ".join(_EXPANSION_HEADERS) + " |",
        "| " + " | ".join(["---"] * len(_EXPANSION_HEADERS)) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    row["claim_term"],
                    ", ".join(row["alternative_meanings"]),
                    ", ".join(row["expanded_terms"]),
                    row["basis"],
                    ", ".join(row["excluded_limitations"]),
                )
            )
            + " |"
        )
    lines.append("")
    lines += [
        "위 기록은 모델의 자기보고이며 명세서 근거 위치는 자동 대조되지 "
        "않습니다. 다만 명세서 보조 결과가 청구항 단독 후보를 삭제하는 것은 "
        "ARIA의 합집합 병합 단계에서 금지됩니다.",
        "",
    ]
    return lines


def _isolated_section(items: list[dict]) -> list[str]:
    """미확인 검색 단서.

    문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 하나도 없는
    후보다. 버리지 않는 이유는 문헌번호 자체가 다시 확인해 볼 단서이기
    때문이고, 그룹에 넣지 않는 이유는 검증된 후보와 같은 위계로 읽히면 안 되기
    때문이다.

    명칭·출원인·비고·구성 대응표는 인쇄하지 않는다. 정규화 단계에서 이미
    비워지지만, 값이 남아 있더라도 이 자리에서 다시 인쇄하지 않는다. 격리
    영역에 그럴듯한 명칭이 찍히면 사용자는 번호와 명칭의 결합을 믿게 되고,
    그것이 애초에 막으려던 실패다.
    """
    if not items:
        return []
    lines = [
        "## 미확인 검색 단서",
        "",
        "아래 후보는 문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 "
        "없어 그룹 분류와 구성 대응표에서 제외했습니다. 문헌번호는 다시 확인해 "
        "볼 단서로만 남깁니다. 명칭·출원인·대응 내용은 검증되지 않았으므로 "
        "표시하지 않습니다.",
        "",
    ]
    for item in items:
        identity = item["doc_number"] or item["doi"] or "문헌번호 확인 필요"
        lines.append(f"- `{identity}`")
        lines.append(
            "  - 제외 사유: "
            + (
                item.get("quarantine_reason")
                or "페이지 관측에 근거한 대응표 행이 없습니다."
            )
        )
        if item["url"]:
            lines.append(f"  - 모델이 제시한 주소: {item['url']}")
        lines.append(
            "  - 증거 등급: "
            + _PROVENANCE_LABEL.get(item["provenance"], item["provenance"])
        )
    lines.append("")
    return lines


def _focus_section(focus: dict | None) -> list[str]:
    """구성대비 결과에서 선택한 보완 검색 범위를 표시한다."""
    if not focus:
        return []
    lines = [
        "## 검색 대상 미대응 구성",
        "",
        f"- 원본 분석: {focus.get('source_job_label') or focus.get('source_job_id') or '-'}",
        f"- 선택 기준: 유사도 {focus.get('threshold', 80)}% 미만 또는 대응 문헌 미발견",
        "- 검색 순서: **1차 조합 검색 → 2차 개별 검색**",
        "",
    ]
    for item in focus.get("components") or []:
        similarity = item.get("similarity")
        score = f"{similarity}%" if similarity is not None else "유사도 없음"
        lines.append(
            f"- {item.get('claim') or '-'} {item.get('symbol') or ''} · {score} · "
            f"{item.get('feature') or '-'}"
        )
        if item.get("difference"):
            lines.append(f"  - 검색할 차이: {item['difference']}")
    lines.append("")
    return lines


def _channel_comparison_section(manifest: dict) -> list[str]:
    """웹과 EPO의 공개번호 교집합·차집합을 사용자에게 보여 준다.

    이것은 후보 발견 경로의 대조이지 A/B/C 분류나 패밀리 판정이 아니다. 그
    경계를 문구와 표에 함께 남겨, EPO 단독 후보가 곧 신규성 근거인 것처럼
    읽히지 않게 한다.
    """
    comparison = manifest.get("channel_comparison")
    if not isinstance(comparison, dict):
        return []

    epo = manifest.get("epo") or {}
    if not comparison.get("compared"):
        if not epo.get("enabled"):
            return []
        return [
            "## 웹/EPO 채널 교차 발견",
            "",
            "두 채널을 대조하지 못했습니다: "
            + str(comparison.get("reason") or "비교할 기록이 없습니다."),
            "",
        ]

    counts = comparison.get("counts") or {}
    web = comparison.get("web") or {}
    epo_stats = comparison.get("epo") or {}
    lines = [
        "## 웹/EPO 채널 교차 발견",
        "",
        "이 표는 **국가코드를 보존한 공개번호 표기**만 맞춘 발견 경로 비교입니다. "
        "특허 패밀리 판정이나 A/B/C 유사도 분류가 아니며, `EPO에서만`은 이번 "
        "웹 검색 기록에 같은 공개번호가 없었다는 뜻일 뿐입니다.",
        "",
        f"- 양쪽 채널에서 발견: {int(counts.get('both') or 0)}건",
        f"- EPO에서만 발견: {int(counts.get('epo_only') or 0)}건",
        f"- 웹에서만 발견: {int(counts.get('web_only') or 0)}건",
        "- 식별 가능한 고유 공개번호: "
        f"웹 {int(web.get('unique_identified') or 0)}건 · "
        f"EPO {int(epo_stats.get('unique_identified') or 0)}건",
    ]
    if web.get("unidentified") or epo_stats.get("unidentified"):
        lines.append(
            "- 문헌번호가 없어 대조에서 제외: "
            f"웹 {int(web.get('unidentified') or 0)}건 · "
            f"EPO {int(epo_stats.get('unidentified') or 0)}건"
        )
    if web.get("quarantined"):
        lines.append(
            f"- 웹 후보 중 식별·근거 격리 상태: {int(web['quarantined'])}건 "
            "(교차 발견 여부가 격리를 자동 해제하지 않습니다.)"
        )
    excluded_lanes = epo_stats.get("excluded_lanes") or []
    if excluded_lanes:
        lines.append(
            "- 완료되지 않아 비교에서 제외한 EPO 레인: "
            + ", ".join(str(value) for value in excluded_lanes)
        )

    rows: list[tuple[str, dict]] = []
    for key, label in (
        ("both", "양쪽"),
        ("epo_only", "EPO에서만"),
        ("web_only", "웹에서만"),
    ):
        rows.extend((label, item) for item in comparison.get(key) or [])

    if not rows:
        lines += ["", "식별 가능한 공개번호 후보가 없습니다.", ""]
        return lines

    lines += [
        "",
        "| 구분 | 대표 문헌번호 | 명칭 | 웹 기록 | EPO 기록 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, item in rows:
        web_records = []
        for record in item.get("web") or []:
            origins = ", ".join(record.get("origins") or []) or "경로 미기록"
            suffix = " · 격리" if record.get("quarantined") else ""
            web_records.append(
                f"{record.get('doc_number') or '-'} ({origins}{suffix})"
            )
        epo_records = []
        for record in item.get("epo") or []:
            lanes = ", ".join(record.get("lanes") or []) or "레인 미기록"
            epo_records.append(f"{record.get('doc_number') or '-'} ({lanes})")
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    label,
                    item.get("doc_number") or "-",
                    item.get("title") or "-",
                    "; ".join(web_records) or "-",
                    "; ".join(epo_records) or "-",
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def render(manifest: dict) -> str:
    """감사 기록에서 사용자용 Markdown 보고서를 만든다.

    입력은 search_manifest.build() 가 만든 기록이다. 모델의 산문은 여기에
    들어오지 않는다.
    """
    reported = manifest.get("reported") or {}
    candidates = reported.get("candidates") or []
    observed = manifest.get("observed") or {}
    focus = (manifest.get("input") or {}).get("search_focus")

    verified = sum(1 for item in candidates if item["original_verified"])
    reviewed = sum(1 for item in candidates if item["page_fetch_succeeded"])
    # group_eligible 이 없는 옛 매니페스트는 그대로 그룹에 둔다. 지난 기록을
    # 다시 그릴 때 화면이 비어 버리면 안 된다.
    grouped = [item for item in candidates if item.get("group_eligible", True)]
    isolated = [item for item in candidates if not item.get("group_eligible", True)]
    base_candidates = sum(
        1 for item in candidates if "claim_only" in (item.get("search_origins") or [])
    )
    assisted_only = sum(
        1
        for item in candidates
        if item.get("search_origins") == ["spec_assisted"]
    )
    both = sum(1 for item in candidates if len(item.get("search_origins") or []) > 1)

    lines = [
        (
            "# 미대응 구성 보완 검색 후보"
            if focus
            else "# 유사 특허·논문 검토 후보"
        ),
        "",
        f"> {DISCLAIMER}",
        "",
        "이 보고서는 ARIA 가 검증한 구조화 기록에서 생성했습니다. 모델이 작성한 "
        "산문은 보고서 본문으로 사용하지 않으며, 실행 기록에 원문 그대로 "
        "보관합니다.",
        "",
        "## 요약",
        "",
        f"- 후보 {len(candidates)}건",
        *(
            [
                f"- 검색 대상 미대응 구성 {len(focus.get('components') or [])}개",
                "- 검색 순서: 1차 조합 검색 → 2차 개별 검색",
            ]
            if focus
            else [
                f"- 청구항 단독 검색 후보 {base_candidates}건",
                f"- 명세서 보조 검색으로 새로 추가된 후보 {assisted_only}건",
                f"- 두 검색에서 모두 발견된 후보 {both}건",
            ]
        ),
        f"- 그룹 분류된 후보 {len(grouped)}건 · 미확인 검색 단서 {len(isolated)}건",
        f"- 페이지 본문을 읽은 것이 확인된 후보 {reviewed}건",
        f"- 원문 대조가 확인된 후보 {verified}건",
        f"- 실제 검색 호출 {len(observed.get('search_queries') or [])}회",
        f"- 페이지 열람 시도 {len(observed.get('attempted_fetch_urls') or [])}건 "
        f"(본문 읽음 {len(observed.get('succeeded_fetch_urls') or [])}건)",
        "",
    ]

    # 식별·근거 게이트 이전에 만들어진 기록은 그 게이트를 통과한 적이 없다.
    # 데이터를 덮어쓰지 않고 그 사실만 알린다 — 과거 기록을 고쳐 쓰면 무엇이
    # 원본이었는지 알 수 없게 된다.
    if int(manifest.get("version") or 0) < LEGACY_GATE_VERSION:
        lines += [
            "> **이 보고서는 후보 식별·행별 근거 게이트가 적용되기 전에 "
            "생성되었습니다.** 아래 후보의 문헌번호·명칭·출원인이 같은 페이지에서 "
            "확인되었는지, 각 대응 행이 실제 관측에 근거하는지는 검증되지 "
            "않았습니다. 사용하기 전에 각 문헌을 직접 확인하십시오.",
            "",
        ]

    if verified == 0 and candidates:
        lines += [
            "이번 실행에서 원문 대조가 확인된 문헌은 없습니다. 모든 발췌 칸은 "
            "`원문에서 확인되지 않음`, 위치 칸은 `확인 필요` 입니다. "
            "대응 관계를 인용하려면 각 문헌의 원문을 직접 확인하십시오.",
            "",
        ]

    lines += _focus_section(focus)
    lines += _expansion_section(manifest, reported)
    lines += _channel_comparison_section(manifest)

    if not candidates:
        lines += ["## 후보", "", "제시된 후보가 없습니다.", ""]

    titles = _group_titles(manifest)
    for group in GROUPS:
        rows = [item for item in grouped if item["group"] == group]
        if not rows:
            continue
        lines += [f"## {titles[group]}", ""]
        for item in rows:
            lines += _candidate_section(item)

    lines += _isolated_section(isolated)

    failures = (reported.get("access_failures") or [])
    if failures:
        lines += ["## 원문 확보 필요", ""]
        for failure in failures:
            lines.append(f"- {failure['url'] or '주소 없음'} — {failure['reason']}")
        lines.append("")

    rounds = reported.get("rounds") or []
    if rounds:
        lines += ["## 검색 라운드 (모델 보고)", ""]
        for entry in rounds:
            origin = _ORIGIN_LABEL.get(entry.get("search_origin"), entry.get("search_origin", ""))
            lines.append(f"- {origin} · {entry['round']}라운드 · {entry['channel']}")
            if entry["note"]:
                lines.append(f"  - {entry['note']}")
            for query in entry["queries"]:
                lines.append(f"  - `{query}`")
        lines.append("")

    queries_by_origin = observed.get("search_queries_by_origin") or {}
    queries = observed.get("search_queries") or []
    if queries_by_origin:
        lines += ["## 실제로 실행된 검색어 (ARIA 관측)", ""]
        for origin, origin_queries in queries_by_origin.items():
            lines.append(f"- {_ORIGIN_LABEL.get(origin, origin)}")
            lines += [f"  - `{query}`" for query in origin_queries]
        lines.append("")
    elif queries:
        lines += ["## 실제로 실행된 검색어 (ARIA 관측)", ""]
        lines += [f"- `{query}`" for query in queries]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
