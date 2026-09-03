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

import re
from html import unescape

from . import search_channels, search_manifest
from .search_manifest import (
    CLASSIFICATION_LEGACY,
    CLASSIFICATION_NONE,
    CLASSIFICATION_OFFICIAL,
    CLASSIFICATION_ORIGINAL,
    CLASSIFICATION_PAGE,
    CLASSIFICATION_SEARCH,
    EVIDENCE_OFFICIAL,
    EVIDENCE_REVIEWED,
    GROUP_DEFINITIONS,
    LEGACY_GROUP_DEFINITIONS,
    GROUPS,
    PROV_OFFICIAL_RESPONSE,
    PROV_RAW,
    PROV_SNIPPET,
    PROV_WEBFETCH,
    SCOPE_ABSTRACT,
    SCOPE_CLAIMS,
    SCOPE_FULL_TEXT,
    SCOPE_UNKNOWN,
    SUPPORT_NONE,
    SUPPORT_OFFICIAL,
    SUPPORT_PAGE,
    SUPPORT_SNIPPET,
    classification_view,
)

# 후보 식별 게이트와 행별 근거 게이트가 들어간 매니페스트 버전. 이보다 낮은
# 기록은 그 검사를 통과한 적이 없으므로 화면에서 그 사실을 밝힌다.
LEGACY_GATE_VERSION = 4
CLASSIFICATION_SCHEMA_VERSION = 8

def _group_titles(manifest: dict) -> dict[str, str]:
    """이 실행이 실제로 쓴 그룹 정의에서 제목을 만든다.

    정의는 search_manifest 가 소유하고 매니페스트에 실려 온다. 렌더러가 자기
    표를 들고 있으면 정의를 고친 뒤 갱신되지 않은 렌더러가 옛 제목으로 인쇄한다.
    2026-08-25 실행에서 실제로 그렇게 어긋나 B 와 C 가 뒤집혀 나갔다.

    정의를 싣지 않는 옛 매니페스트에서만 현재 정의로 채운다.
    """
    stored = manifest.get("group_definitions")
    definitions = stored if isinstance(stored, dict) and stored else GROUP_DEFINITIONS
    # C 는 새 실행이 만들지 않지만 과거 기록에는 남아 있다. fallback 은 그 정의를
    # 아는 표를 써야 한다 — 없으면 옛 보고서를 여는 순간 KeyError 다.
    return {
        group: (
            f"{group}. "
            + (definitions.get(group) or LEGACY_GROUP_DEFINITIONS[group])
        )
        for group in GROUPS
    }


_SUPPORT_LABEL = {
    SUPPORT_PAGE: "페이지 관측",
    SUPPORT_OFFICIAL: "공식 문헌 대조",
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
    PROV_OFFICIAL_RESPONSE: "EPO 공식 응답에서 발견 (원문 인용 아님)",
}

# 이 후보를 데려온 검색 경로. search_origins(무엇을 입력으로 검색했나)와
# 다른 축이다.
_DISCOVERY_LABEL = {
    "web": "웹 검색",
    "epo": "EPO 독립 검색",
    "literature": "ARIA 서지 검색(Crossref·Europe PMC)",
}

# 웹 페이지를 열지 않는 발견 경로. 이 경로로만 온 후보에게 "페이지를 열지
# 못했다"고 적으면 안 된다 — 애초에 열 페이지가 없었다.
_NO_WEB_PAGE_ORIGINS = ("epo", "literature")

# 계획 턴에서 도구를 막은 수단. "막았다"와 "막을 수 없어 사후에 봤다"를 같은
# 말로 적으면 기록이 실제로 아는 것보다 강해진다.
_ISOLATION_LABEL = {
    "provider_enforced": "CLI 단계에서 도구를 차단(그런데도 호출이 관측됨)",
    # **사후 탐지는 차단이 아니다.** 이 값이 붙은 실행에서 외부 호출은 이미
    # 나갔고, ARIA 가 한 일은 그 응답을 쓰지 않기로 한 것뿐이다. 문구에서 그
    # 경계를 흐리면 기록이 실제로 보증하는 것보다 강해진다.
    "post_hoc_detection": (
        "사후 탐지 — 도구를 끌 수단이 없어 호출을 막지 못했고, "
        "이미 나간 외부 호출은 되돌릴 수 없음"
    ),
    "unknown": "도구 통제 수준 확인 불가",
}

_EVIDENCE_LABEL = {
    "candidate_only": "후보 단계",
    EVIDENCE_REVIEWED: "페이지 열람 성공",
    EVIDENCE_OFFICIAL: "EPO 공식 기록 대조",
}

_CLASSIFICATION_LABEL = {
    CLASSIFICATION_NONE: "분류 없음",
    CLASSIFICATION_LEGACY: "과거 분류(검증 근거 미기록)",
    CLASSIFICATION_SEARCH: "검색 결과 기반 AI 잠정 분류",
    CLASSIFICATION_PAGE: "페이지 관측 근거가 있는 AI 분류",
    CLASSIFICATION_OFFICIAL: "공식 기록 대조가 있는 AI 분류",
    CLASSIFICATION_ORIGINAL: "원문 직접 대조가 있는 AI 분류",
}

_VERIFICATION_LABEL = {
    "not_attempted": "공식 검증 미시도",
    "fetch_failed": "공식 문헌 확보 실패",
    "record_fetched": "공식 문헌 확보 완료",
    "classification_failed": "2차 분류 실패",
    "evidence_mismatch": "근거 문장 대조 실패",
    "promoted": "공식 근거 분류 완료",
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


#: 표 한 칸을 다듬는 함수. 채널 상태 사유도 같은 규칙으로 다듬어야 하므로
#: search_channels 가 소유하고 여기서는 이름만 빌린다.
_cell = search_channels.cell


def _link(url) -> str:
    """클릭 가능한 마크다운 자동 링크. 아니면 평문 주소 그대로.

    링크로 만들지 않는 경우가 둘이다.

      - http/https 가 아닌 값. 이 칸은 모델이 적으며 그 입력에는 검색 결과와
        페이지 본문이 섞여 있다. javascript:·data:·file: 을 링크로 만들면
        보고서를 여는 것만으로 사용자가 한 번 클릭할 수 있는 자리에 놓인다.
        판정은 search_manifest.is_linkable_url 한 곳에서 하고 화면도 같은
        규칙을 쓴다.
      - 자동 링크 문법을 깨뜨리는 문자가 든 값. 깨진 링크보다 평문이 낫다.

    어느 쪽이든 **값을 지우지는 않는다.** 모델이 무엇을 적었는지는 그 자체로
    기록이고, 평문이면 클릭되지 않는다.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if any(ch.isspace() or ch in "<>" for ch in text):
        return text
    if not search_manifest.is_linkable_url(text):
        return text
    return f"<{text}>"


def _unverified_title_lines(item: dict, *, indent: str = "") -> list[str]:
    """검증되지 않은 제목을 사용자가 직접 확인할 수 있게 적는다.

    왜 지우지 않고 적는가
    ---------------------
    이 후보의 명칭은 검증되지 않았다. 그래서 title 칸에서는 빠진다. 그런데
    사용자에게 번호와 주소만 남기면 "직접 확인해 보라"는 말이 실행 불가능해진다 —
    무엇을 확인해야 하는지가 사라지기 때문이다.

    그래서 값을 감추는 대신 **등급을 붙여** 적는다. 라벨과 상태 줄이 항상 함께
    나가므로 검증된 명칭과 같은 위계로 읽힐 수 없고, 이 값은 A/B 등급·구성
    대응표·직접 발췌 어디에도 쓰이지 않는다(search_manifest 의 게이트는 이
    칸을 보지 않는다).
    """
    title = search_manifest.unverified_title(item)
    if not title:
        return []
    lines = [f"{indent}- 제목(검색 결과 기반·미검증): {_cell(title)}"]
    if item.get("url"):
        lines.append(f"{indent}- 링크: {_link(item['url'])}")
    lines.append(
        f"{indent}- 상태: 페이지 직접 확인 안 됨 — 사용자가 수동 확인 필요"
    )
    return lines


def _verification_detail(value) -> str:
    """옛 기록에 저장된 EPO XML 오류가 보고서 본문을 밀어내지 않게 줄인다."""
    text = str(value or "").strip()
    if "<fault" in text.lower() or "<?xml" in text.lower():
        status = re.search(r"HTTP\s+(\d+)", text, re.IGNORECASE)
        codes = [
            " ".join(unescape(part).split())
            for part in re.findall(r"<code>(.*?)</code>", text, re.DOTALL | re.IGNORECASE)
        ]
        messages = [
            " ".join(unescape(part).split())[:240]
            for part in re.findall(
                r"<message>(.*?)</message>", text, re.DOTALL | re.IGNORECASE
            )
        ]
        details = list(dict.fromkeys([*codes, *messages]))
        head = f"EPO OPS HTTP {status.group(1)}" if status else "EPO OPS 조회 오류"
        return " · ".join([head, *details])[:600]
    text = " ".join(text.split())
    return text[:600] + ("…" if len(text) > 600 else "")


def _no_web_channel_label(discovered) -> str:
    """웹 페이지를 열지 않는 발견 경로의 이름. 둘 다면 둘 다 적는다.

    예전에는 이 자리에 "EPO 독립 검색"이 박혀 있었다. 서지 검색이 데려온 논문
    후보에게도 그 문구가 나가면, 보고서가 실제와 다른 경로를 말하게 된다.
    """
    names = [
        _DISCOVERY_LABEL.get(origin, origin)
        for origin in discovered
        if origin in _NO_WEB_PAGE_ORIGINS
    ]
    return " + ".join(names) or "ARIA 직접 검색"


def _candidate_section(item: dict) -> list[str]:
    identity = _cell(item["doc_number"] or item["doi"] or "문헌번호 확인 필요")
    lines = [f"#### {identity}"]
    if item["title"]:
        lines.append(f"- 명칭: {_cell(item['title'])}")
    # 미검증 제목은 링크·상태와 한 덩어리로 나간다. 셋이 떨어져 있으면 "무엇을
    # 어디서 확인해야 하는가"가 다시 흩어진다. 그래서 이 블록이 나가면 아래의
    # 원문 링크 줄은 생략한다 — 같은 주소를 두 번 적지 않는다.
    unverified_lines = _unverified_title_lines(item)
    lines += unverified_lines
    if item["applicant"]:
        lines.append(f"- 출원인·저자: {_cell(item['applicant'])}")
    if item["doi"] and item["doc_number"]:
        lines.append(f"- DOI: {item['doi']}")
    lines.append(f"- 패밀리: {_cell(item['family'] or '확인 필요')}")
    if item["url"] and not unverified_lines:
        lines.append(f"- 원문 링크: {_link(item['url'])}")
    lines.append(
        "- 증거 등급: "
        + _PROVENANCE_LABEL.get(item["provenance"], item["provenance"])
        + " / "
        + _EVIDENCE_LABEL.get(item["evidence_status"], item["evidence_status"])
    )
    lines.append(
        "- 분류 근거: "
        + _CLASSIFICATION_LABEL.get(
            item.get("classification_basis"),
            str(item.get("classification_basis") or "확인 필요"),
        )
    )
    # 공식 응답을 받아 봤지만 A/B 근거를 더 찾지 못한 후보. 분류는 내리지 않되
    # 그 사실을 숨기지도 않는다 — OPS 는 초록·청구항만 주므로 명세서에만 있는
    # 구성은 여기서 대조될 수 없고, 읽는 사람은 그 한계를 알아야 한다.
    if item.get("official_ab_confirmation") == "not_confirmed":
        detail = _cell(str(item.get("official_ab_confirmation_detail") or ""))
        lines.append(f"- 공식 문헌 추가 확인: {detail}")

    # 공식 대조로 덮이기 전의 1차 분류. 두 분류를 같은 줄에 나란히 쓰지 않는다 —
    # 나란히 두면 같은 위계로 읽히고, 그러면 등급을 나눈 의미가 없다. 이 후보의
    # 분류는 위의 '분류 근거' 줄이고, 여기는 대체되기 전의 기록이다.
    page_prior = item.get("page_classification")
    if isinstance(page_prior, dict) and page_prior.get("group"):
        agreed = page_prior.get("group") == item.get("group")
        lines.append(
            f"- 대체된 1차 분류: {page_prior['group']} "
            + _CLASSIFICATION_LABEL.get(
                page_prior.get("classification_basis"),
                str(page_prior.get("classification_basis") or "확인 필요"),
            )
            + f" · 페이지 근거 행 {int(page_prior.get('page_supported_rows') or 0)}개"
            + (
                " (공식 대조 결과와 같음)"
                if agreed
                else " — 공식 대조 결과와 달라 공식 분류를 채택했습니다"
            )
        )
    if item.get("matched_feature_rows") is not None:
        lines.append(
            f"- 공식 기록에서 대조된 구성 행: "
            f"{int(item.get('matched_feature_rows') or 0)}개"
        )
    verification = item.get("verification") or {}
    if verification:
        lines.append(
            "- 후보별 공식 검증: "
            + _VERIFICATION_LABEL.get(
                verification.get("status"), verification.get("status") or "기록 없음"
            )
            + (
                f" — {_verification_detail(verification.get('detail'))}"
                if _verification_detail(verification.get("detail"))
                else ""
            )
        )
    origins = item.get("search_origins") or []
    if origins:
        lines.append(
            "- 검색 입력: "
            + " + ".join(_ORIGIN_LABEL.get(origin, origin) for origin in origins)
        )
    # 어느 검색 경로가 이 문헌을 데려왔는가. 둘 다면 둘 다 적는다 — 한쪽만
    # 적으면 "EPO 를 켜서 무엇을 더 얻었는가"에 답할 수 없다.
    discovered = search_manifest.discovery_origins(item)
    lines.append(
        "- 발견 경로: "
        + " + ".join(_DISCOVERY_LABEL.get(origin, origin) for origin in discovered)
    )
    epo_discovery = item.get("epo_discovery")
    if isinstance(epo_discovery, dict) and epo_discovery:
        lanes = ", ".join(epo_discovery.get("lanes") or []) or "레인 미기록"
        lines.append(f"  - EPO 검색 레인: {lanes}")
        for entry in epo_discovery.get("shortlist") or []:
            reason = _cell(entry.get("reason") or "이유 미기록")
            lines.append(f"  - EPO 선정 이유: {reason}")
        artifact_ids = epo_discovery.get("artifact_ids") or []
        if artifact_ids:
            lines.append(
                "  - 재사용한 EPO 응답 아티팩트: "
                + ", ".join(str(value)[:12] + "…" for value in artifact_ids[:5])
            )
    literature_discovery = item.get("literature_discovery")
    if isinstance(literature_discovery, dict) and literature_discovery:
        sources = ", ".join(literature_discovery.get("sources") or []) or "미기록"
        lines.append(f"  - 서지 출처: {sources}")
        container = _cell(literature_discovery.get("container") or "")
        if container:
            lines.append(f"  - 게재지: {container}")
        for query in (literature_discovery.get("queries") or [])[:3]:
            lines.append(f"  - ARIA 가 보낸 질의: `{_cell(query)}`")
        artifact_ids = literature_discovery.get("artifact_ids") or []
        if artifact_ids:
            lines.append(
                "  - 보존한 서지 응답 아티팩트: "
                + ", ".join(str(value)[:12] + "…" for value in artifact_ids[:5])
            )
    epo_only = search_manifest.DISCOVERY_WEB not in discovered
    lines.append(
        "- ARIA 관측: 페이지 본문 "
        + ("읽음" if item["page_fetch_succeeded"] else "읽은 기록 없음")
        + " · 원문 대조 "
        + ("완료" if item["original_verified"] else "안 됨")
        + " · 문헌번호-주소 대조 "
        + (
            "해당 없음(웹 페이지를 열지 않는 경로)"
            if epo_only
            else "완료"
            if item.get("identifier_url_matched")
            else "안 됨"
        )
    )
    if epo_only:
        # 웹 게이트의 문구를 그대로 쓰면 "확인 실패"로 읽힌다. 이 후보에는
        # 애초에 열어 볼 웹 페이지가 없었고, 존재하지 않는 페이지 관측을
        # 지어내지 않는 것이 규칙이다.
        lines.append(
            f"- 이 후보는 {_no_web_channel_label(discovered)}이 데려왔습니다. 웹 "
            "페이지 관측이 없으므로 페이지 근거 분류는 만들지 않으며, 정식 A/B "
            "는 공식 응답에 구성 대응이 대조된 경우에만 붙습니다."
        )
    elif not item.get("identifier_url_matched", True):
        lines.append(
            "- 이 후보의 문헌번호가 위 주소에서 확인되지 않아 명칭·출원인·"
            "패밀리를 표시하지 않았습니다."
        )
    if not item["original_verified"]:
        lines.append("- 원문 직접 발췌 상태: 검증되지 않았습니다.")
    lines.append(f"- 원문 위치: {item['source_location']}")
    lines.append(f"- 직접 발췌: {item['verbatim_excerpt']}")
    if item["note"]:
        lines.append(f"- 비고: {_cell(item['note'])}")

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
            location = (
                f"공식 응답 필드: {row.get('support_field')}"
                if support == SUPPORT_OFFICIAL and row.get("support_field")
                else row["source_location"]
            )
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
                        location,
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


def _provisional_candidates(items: list[tuple[dict, dict]]) -> list[str]:
    """한 A/B 그룹 안에서 검증되지 않은 후보를 잠정 하위 절로 표시한다."""
    if not items:
        return []
    lines = [
        "### 잠정 분류",
        "",
    ]
    for item, view in items:
        identity = item.get("doc_number") or item.get("doi") or "문헌번호 확인 필요"
        discovered = search_manifest.discovery_origins(item)
        epo_only = search_manifest.DISCOVERY_WEB not in discovered
        lines.append(f"- `{identity}`")
        lines.append(
            "  - 발견 경로: "
            + " + ".join(_DISCOVERY_LABEL.get(origin, origin) for origin in discovered)
        )
        unverified_lines = _unverified_title_lines(item, indent="  ")
        lines += unverified_lines
        if item.get("url") and not unverified_lines:
            lines.append(
                ("  - 공식 응답의 주소: " if epo_only else "  - 모델이 제시한 주소: ")
                + _link(item["url"])
            )
        lines.append(
            "  - 분류 근거: "
            + _CLASSIFICATION_LABEL.get(view["basis"], view["basis"])
        )
        verification = item.get("verification") or {}
        if verification:
            detail = _verification_detail(verification.get("detail")) or (
                "세부 사유가 기록되지 않았습니다."
            )
            lines.append(
                "  - 정식 승격되지 않은 이유: "
                + _VERIFICATION_LABEL.get(
                    verification.get("status"),
                    verification.get("status") or "검증 기록 없음",
                )
                + f" — {detail}"
            )
        elif item.get("quarantine_reason"):
            lines.append(f"  - 정식 승격되지 않은 이유: {item['quarantine_reason']}")
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


def _below_threshold_section(items: list[dict]) -> list[str]:
    """공식 검증했으나 A/B 기준에 못 미친 후보.

    긴 구성 대응표를 만들지 않는다. 중요하지 않은 후보에 60행짜리 표를 붙이는
    것이 이 보고서를 읽기 어렵게 만든 주된 이유였다. 대신 **왜 아닌지**를 한
    줄로 남긴다 — 후보를 조용히 지우지 않는 것이 목적이다.
    """
    if not items:
        return []
    lines = [
        "## 공식 검증했으나 A/B 기준 미달",
        "",
        "공식 문헌을 확보해 대조했지만 A 에도 B 에도 해당하지 않는다고 판단한 "
        "후보입니다. 상세 구성 대응표는 만들지 않고 사유만 남깁니다.",
        "",
    ]
    for item in items:
        identity = item.get("doc_number") or item.get("doi") or "문헌번호 확인 필요"
        reason = (item.get("verification") or {}).get("detail") or item.get("note")
        lines.append(f"- `{identity}`")
        if reason:
            lines.append(f"  - {_cell(str(reason))}")
    lines.append("")
    return lines


def _isolated_section(items: list[dict]) -> list[str]:
    """미확인 검색 단서.

    문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 하나도 없는
    후보다. 버리지 않는 이유는 문헌번호 자체가 다시 확인해 볼 단서이기
    때문이고, 그룹에 넣지 않는 이유는 검증된 후보와 같은 위계로 읽히면 안 되기
    때문이다.

    검증된 명칭·출원인·비고·구성 대응표는 인쇄하지 않는다. 정규화 단계에서 이미
    비워지지만, 값이 남아 있더라도 이 자리에서 다시 인쇄하지 않는다. 격리
    영역에 그럴듯한 명칭이 검증된 것처럼 찍히면 사용자는 번호와 명칭의 결합을
    믿게 되고, 그것이 애초에 막으려던 실패다.

    다만 **검색 결과에서 본 제목은 등급을 붙여 적는다.** 예전에는 그것마저
    지웠는데, 그러면 남는 것이 번호와 주소뿐이라 "직접 확인해 보라"는 안내가
    실행 불가능해진다. 지우는 것과 등급을 낮춰 적는 것은 다르다 — 라벨과
    "수동 확인 필요" 상태가 항상 함께 나가므로 검증된 명칭과 같은 위계로 읽힐
    수 없고, 이 값은 어떤 게이트도 통과시키지 않는다.
    """
    lines = [
        "## 미검증 참고 후보",
        "",
        "아래 후보는 아직 공식 검증을 받지 못했습니다. 웹 후보는 "
        "문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 없어서이고, "
        "EPO 독립 검색과 ARIA 서지 검색 후보는 공식 응답에 구성 대응이 아직 "
        "대조되지 않아서입니다. "
        "각 항목의 사유를 함께 적었습니다. 문헌번호는 다시 확인해 볼 단서로 "
        "남기며, 검증되지 않은 대응 내용은 표시하지 않습니다.",
        "",
    ]
    if not items:
        lines += ["해당 후보가 없습니다.", ""]
        return lines
    for item in items:
        identity = item["doc_number"] or item["doi"] or "문헌번호 확인 필요"
        discovered = search_manifest.discovery_origins(item)
        epo_only = search_manifest.DISCOVERY_WEB not in discovered
        lines.append(f"- `{identity}`")
        lines.append(
            "  - 발견 경로: "
            + " + ".join(_DISCOVERY_LABEL.get(origin, origin) for origin in discovered)
        )
        verification = item.get("verification") or {}
        if epo_only:
            # 웹 게이트 문구를 쓰지 않는다. 이 후보는 격리된 것이 아니라 아직
            # 공식 근거 대조를 통과하지 못한 EPO 독립 검색 후보다.
            detail = _verification_detail(verification.get("detail"))
            lines.append(
                f"  - 상태: {_no_web_channel_label(discovered)} 후보 — "
                + _VERIFICATION_LABEL.get(
                    verification.get("status"),
                    verification.get("status") or "공식 근거 대조 전",
                )
                + (f" ({detail})" if detail else "")
            )
        else:
            lines.append(
                "  - 제외 사유: "
                + (
                    item.get("quarantine_reason")
                    or "페이지 관측에 근거한 대응표 행이 없습니다."
                )
            )
        unverified_lines = _unverified_title_lines(item, indent="  ")
        lines += unverified_lines
        if item["url"] and not unverified_lines:
            lines.append(
                ("  - 공식 응답의 주소: " if epo_only else "  - 모델이 제시한 주소: ")
                + _link(item["url"])
            )
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


#: 채널 상태 표에 쓰는 표시. 값이 늘어나면 프런트도 같은 말을 써야 한다.
#: 저장되는 값은 search_channels 의 canonical 상태이고, 여기 있는 것은 그 값을
#: 사람이 읽는 말로 옮기는 표뿐이다. 두 축을 한 문자열로 합치면 옛 기록을 다시
#: 읽을 때 표시 문구가 바뀔 때마다 상태 판정이 함께 흔들린다.
_STATUS_OK = "성공"
_STATUS_PARTIAL = "부분 성공"
_STATUS_FAILED = "실패"
_STATUS_SKIPPED = "미실행"

_STATUS_LABEL = {
    search_channels.STATUS_SUCCEEDED: _STATUS_OK,
    search_channels.STATUS_PARTIAL: _STATUS_PARTIAL,
    search_channels.STATUS_FAILED: _STATUS_FAILED,
    search_channels.STATUS_SKIPPED: _STATUS_SKIPPED,
}


def _channel_status_section(manifest: dict) -> list[str]:
    """보고서 맨 위의 채널별 성공·실패.

    판정은 search_channels 하나가 한다. 실행 시점에는 그 결과를 매니페스트에
    저장하고(channel_status), 보고서는 **같은 함수로 다시 계산해서** 그린다.
    렌더러가 자기 판정을 들고 있으면 저장된 기록과 화면이 갈라지는데, 같은
    순수 함수를 부르면 그럴 여지가 없다.

    저장된 행을 그대로 쓰지 않는 이유는 하나다. 매니페스트를 만든 뒤에 그
    안의 채널 절을 고치는 경로가 있으면(테스트·이관·후처리) 저장된 행만 옛
    상태로 남아, 보고서가 바로 아래에 인쇄하는 내용과 표의 상태가 어긋난다.
    이 기록에서 계산하면 표는 언제나 이 기록을 설명한다.

    상태를 저장하지 않던 옛 기록도 같은 경로로 그려진다.
    """
    rows = search_channels.status_rows(manifest)

    overall = _STATUS_LABEL.get(
        search_channels.overall_status(rows), _STATUS_FAILED
    )

    lines = [
        "## 채널별 실행 결과",
        "",
        "| 채널 | 상태 | 내용 |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        label = str(row.get("label") or row.get("id") or "채널")
        state = _STATUS_LABEL.get(str(row.get("status") or ""), _STATUS_SKIPPED)
        lines.append(f"| {label} | {state} | {_cell(row.get('detail'))} |")
    lines += ["", f"**전체 실행 상태: {overall}**", ""]

    epo = manifest.get("epo") or {}
    lanes = [lane for lane in (epo.get("lanes") or []) if isinstance(lane, dict)]
    epo_queries = [
        str(query.get("cql") or "")
        for lane in lanes
        for query in (lane.get("queries") or [])
        if isinstance(query, dict) and query.get("cql")
    ]
    if epo_queries:
        lines += ["실제로 실행된 EPO 검색식", ""]
        lines += [f"- `{_cell(query)}`" for query in epo_queries]
        lines.append("")
    # 모델이 적은 분류코드와 OPS 로 실제 나간 표기가 다르면 그 사실을 밝힌다.
    normalized = [
        row
        for lane in lanes
        for query in (lane.get("queries") or [])
        if isinstance(query, dict)
        for row in (query.get("normalized_classifications") or [])
        if isinstance(row, dict)
    ]
    if normalized:
        lines += ["분류코드 형식 변환 (모델 입력 → OPS 전송)", ""]
        lines += [
            f"- `{_cell(row.get('original'))}` → `{_cell(row.get('sent'))}`"
            for row in normalized
        ]
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

    # 과거 매니페스트에는 없던 키가 있다. 보고서를 여는 것이 실패하면 그
    # 실행의 기록 전체를 못 읽게 되므로, 읽기 경로에서는 없는 키를 거짓으로
    # 읽는다. 저장된 값은 고치지 않는다.
    verified = sum(1 for item in candidates if item.get("original_verified"))
    reviewed = sum(1 for item in candidates if item.get("page_fetch_succeeded"))
    classified = [
        (item, classification_view(item, manifest.get("version")))
        for item in candidates
    ]
    # 과거 group_eligible 누락을 True로 간주하지 않는다. 저장된 검증 흔적이
    # 없으면 잠정 그룹으로 보이게 해 과거 검색 제안을 정식 분류로 승격하지 않는다.
    grouped = [(item, view) for item, view in classified if view["group"]]
    provisional = [
        (item, view) for item, view in classified if view["provisional_group"]
    ]
    # 공식 검증했는데 A/B 기준에 못 미친 후보. 아직 검증하지 못한 후보와 다른
    # 사실이므로 같은 칸에 두지 않는다 — 앞은 결론이고 뒤는 아직 보지 않은 것이다.
    below_threshold = [
        item
        for item, view in classified
        if not view["group"]
        and view.get("outcome") == search_manifest.OUTCOME_BELOW_THRESHOLD
    ]
    below_ids = {id(item) for item in below_threshold}
    provisional = [
        (item, view) for item, view in provisional if id(item) not in below_ids
    ]
    isolated = [
        item
        for item, view in classified
        if not view["group"]
        and not view["provisional_group"]
        and id(item) not in below_ids
    ]
    official = sum(
        1 for _item, view in classified if view["basis"] == CLASSIFICATION_OFFICIAL
    )
    base_candidates = sum(
        1 for item in candidates if "claim_only" in (item.get("search_origins") or [])
    )
    assisted_only = sum(
        1
        for item in candidates
        if item.get("search_origins") == ["spec_assisted"]
    )
    both = sum(1 for item in candidates if len(item.get("search_origins") or []) > 1)
    # 발견 경로별 집계. search_origins 와 다른 축이라 따로 센다.
    epo_discovered = [
        item
        for item in candidates
        if search_manifest.DISCOVERY_EPO in search_manifest.discovery_origins(item)
    ]
    epo_only_count = sum(
        1
        for item in epo_discovered
        if search_manifest.DISCOVERY_WEB
        not in search_manifest.discovery_origins(item)
    )
    literature_discovered = [
        item
        for item in candidates
        if search_manifest.DISCOVERY_LITERATURE
        in search_manifest.discovery_origins(item)
    ]
    # 웹 검색이 식별하지 못한 문헌을 서지 검색이 몇 건이나 데려왔는가. 이 채널을
    # 켠 효과를 한 숫자로 읽을 수 있는 자리다.
    literature_only_count = sum(
        1
        for item in literature_discovered
        if search_manifest.DISCOVERY_WEB
        not in search_manifest.discovery_origins(item)
    )

    lines = [
        *_channel_status_section(manifest),
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
        *(
            [
                f"- EPO 독립 검색이 데려온 후보 {len(epo_discovered)}건 "
                f"(그중 웹 검색에 없던 후보 {epo_only_count}건)",
            ]
            if epo_discovered
            else []
        ),
        *(
            [
                f"- ARIA 서지 검색이 데려온 후보 {len(literature_discovered)}건 "
                f"(그중 웹 검색에 없던 후보 {literature_only_count}건)",
            ]
            if literature_discovered
            else []
        ),
        f"- 정식 그룹 분류 {len(grouped)}건 · 잠정 그룹 분류 "
        f"{len(provisional)}건 · 미분류 검색 단서 {len(isolated)}건",
        f"- 공식 문헌 근거로 정식 분류된 후보 {official}건",
        f"- 페이지 본문을 읽은 것이 확인된 후보 {reviewed}건",
        f"- 원문 대조가 확인된 후보 {verified}건",
        # 옛 기록에는 search_call_count 가 없다. 그때는 호출당 질의 하나였으므로
        # 질의 수가 곧 호출 수였다.
        f"- 실제 검색 호출 "
        f"{observed.get('search_call_count', len(observed.get('search_queries') or []))}회 "
        f"(실제 질의 {len(observed.get('search_queries') or [])}개)",
        # URL 조회는 검색이 아니고 페이지 열람도 아니다. 이 Provider 는 열람
        # 성공을 알려주지 않으므로 "시도"까지만 적는다.
        f"- URL 조회 시도 {len(observed.get('url_lookup_attempts') or [])}건 "
        "(열람 성공 여부는 관측되지 않음)",
        f"- 페이지 열람 시도 {len(observed.get('attempted_fetch_urls') or [])}건 "
        f"(본문 읽음 {len(observed.get('succeeded_fetch_urls') or [])}건)",
        # "확인된 실패 0건"이 "전부 성공"으로 읽히지 않게 둘을 나란히 적는다.
        f"- 확인된 도구 실패 {len(observed.get('tool_failures') or [])}건 · "
        f"결과 확인 불가 {len(observed.get('unknown_tool_outcomes') or [])}건",
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

    if int(manifest.get("version") or 0) < CLASSIFICATION_SCHEMA_VERSION:
        lines += [
            "> **과거 분류 안전 해석:** 이 기록에 정식 분류 근거가 저장되어 있지 "
            "않은 A/B는 잠정 등급으로 표시합니다. 원본 매니페스트 값은 수정하지 "
            "않았습니다.",
            "",
        ]

    verification = manifest.get("verification") or {}
    if verification.get("attempted") or verification.get("reason"):
        counts = verification.get("counts") or {}
        lines += ["## 공식 문헌 2차 검증", ""]
        if verification.get("reason"):
            lines.append(f"- 상태: {verification['reason']}")
        lines.append(
            "- 대상/결과: "
            f"{int(counts.get('targets') or 0)}건 · 공식 문헌 확보 "
            f"{int(counts.get('verified') or 0)}건 · 확보 실패 "
            f"{int(counts.get('fetch_failed') or 0)}건 · 미시도 "
            f"{int(counts.get('not_attempted') or 0)}건"
        )
        usage = verification.get("usage") or {}
        if int(usage.get("reused_artifact_calls") or 0):
            lines.append(
                "- EPO 응답 재사용: 계획상 완전 재사용 "
                f"{int(usage.get('fully_reused_documents') or 0)}건 · 부분 재사용 "
                f"{int(usage.get('partially_reused_documents') or 0)}건 · 실제 추가 "
                "호출 없이 종료 "
                f"{int(usage.get('reused_without_fresh_fetch_documents') or 0)}건 · "
                "추가 호출 발생 "
                f"{int(usage.get('reused_with_fresh_fetch_documents') or 0)}건"
            )
        lines += [
            "- A/B는 AI 분류이며, ARIA는 보존된 공식 응답에서 그대로 대조된 "
            "구성 행 수를 공개합니다. 안정적인 특징 분모가 없어 임의의 커버리지 "
            "백분율은 계산하지 않습니다.",
            "",
        ]

    if verified == 0 and candidates:
        lines += [
            "이번 실행에서 원문 대조가 확인된 문헌은 없습니다. 모든 발췌 칸은 "
            "`원문에서 확인되지 않음`, 위치 칸은 `확인 필요` 입니다. "
            "대응 관계를 인용하려면 각 문헌의 원문을 직접 확인하십시오.",
            "",
        ]

    # 웹 채널의 출력을 읽지 못한 실행. 후보가 EPO 하나에서만 나왔다는 사실은
    # 결과를 읽기 전에 알아야 하므로 접지 않고 맨 앞에 둔다.
    web_error = str(reported.get("web_report_error") or "")
    if web_error:
        lines += [
            "> **웹 채널의 검색 결과를 읽지 못했습니다.** 아래 후보는 EPO 독립 "
            "검색만으로 만들어졌으며, 웹 검색이 찾은 문헌은 하나도 들어 있지 "
            f"않습니다. 사유: {web_error}",
            "",
        ]

    lines += _focus_section(focus)
    lines += _expansion_section(manifest, reported)

    # 사용자가 가장 먼저 읽어야 하는 것은 실행 과정이 아니라 분류 결과다.
    # 지금까지 만든 채널 상태·요약·검증 설명은 잠시 보관하고, A/B/C와 잠정
    # 분류 및 기준 미달 후보를 먼저 만든 뒤 그 아래에 붙인다.
    context_lines = lines
    lines = []

    if not candidates:
        lines += ["## 후보", "", "제시된 후보가 없습니다.", ""]

    if provisional:
        lines += [
            "> **잠정 분류 안내:** 검색 결과를 바탕으로 모델이 제안했지만 "
            "페이지 본문 또는 공식 문헌 근거로 정식 승격되지 않은 후보입니다. "
            "문헌번호와 주소는 재검토 단서로만 사용하십시오.",
            "",
        ]

    titles = _group_titles(manifest)
    for group in GROUPS:
        formal_rows = [
            (item, view) for item, view in grouped if view["group"] == group
        ]
        provisional_rows = [
            (item, view)
            for item, view in provisional
            if view["provisional_group"] == group
        ]
        if not formal_rows and not provisional_rows:
            continue
        heading = titles[group]
        if group not in search_manifest.WRITE_GROUPS:
            heading += " — 과거 분류 (새 실행은 만들지 않습니다)"
        lines += [f"## {heading}", ""]
        if formal_rows:
            lines += ["### 정식 분류", ""]
            for item, view in formal_rows:
                item = {**item, "classification_basis": view["basis"]}
                lines += _candidate_section(item)
        lines += _provisional_candidates(provisional_rows)

    lines += _below_threshold_section(below_threshold)
    lines += _isolated_section(isolated)
    lines += context_lines
    # 웹/EPO 교차 발견표, EPO 청구항 분해, 도구 격리 내부 상태, 최종 선택 턴의
    # 진행 과정은 사용자 보고서에서 뺐다. 전부 매니페스트에 그대로 남아 있고
    # 화면의 감사 패널에서 볼 수 있다 — 이 보고서는 "무엇을 찾았나"를 읽는
    # 자리이지 ARIA 의 내부 상태를 읽는 자리가 아니다.

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
