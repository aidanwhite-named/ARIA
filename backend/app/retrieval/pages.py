"""근거 패키지의 페이지 확장.

근거 패키지는 찾은 청크만 담지 않는다. **그 청크가 있는 페이지 전문**과 앞뒤
페이지를 예산이 허락하는 만큼 함께 담는다.

왜인가. 짧은 발췌 몇 줄로는 「이 문헌에 대응 구성이 없다」를 단정할 수 없다.
특허 문언에서 한 구성의 설명은 문단 여럿에 걸치고 페이지 경계에서 끊긴다. 넣을
자리가 있는데 발췌만 넣으면, 넣을 수 있었던 문맥을 버린 채 판단하게 된다.

이것은 **전달 방식이 아니라 근거 패키지의 확장 방식**이다. 한때 「페이지 단위」를
독립 전달 모드로 두었는데, 같은 검색을 돌리고 담는 단위만 다른 것이라 사용자가
고를 축이 하나 늘어날 뿐이었고 "검색은 했는데 어느 폭으로 담겼나"를 두 군데서
설명하게 됐다.

**예산이 모자라면 중요도가 낮은 것부터 줄인다.**

    주변 페이지(후보에서 먼 것부터) → 후보 페이지 → (evidence 의 기존 축약)

페이지 확장은 덧붙임이므로 압박이 오면 가장 먼저 사라진다. 다 사라지면 예전의
청크 단위 근거 패키지와 같아진다. 뺀 페이지는 미확인으로 기록된다 — 조용히
빠지면 사용자는 그 페이지를 검토한 결과라고 믿게 된다.
"""

from __future__ import annotations

# 페이지 전문을 담을 때 한 페이지가 예산에서 차지할 수 있는 최대 비율.
# 한 페이지가 예산을 통째로 먹으면 다른 문헌이 한 페이지도 못 들어간다.
MAX_PAGE_SHARE = 0.25


def widen(pages: set[int], last_page: int, neighbours: int) -> list[int]:
    """후보 페이지를 앞뒤로 넓힌다. 문헌 범위를 벗어나지 않는다."""
    widened: set[int] = set()
    for page in pages:
        for offset in range(-neighbours, neighbours + 1):
            candidate = page + offset
            if 1 <= candidate <= last_page:
                widened.add(candidate)
    return sorted(widened)


def page_list(pages) -> str:
    """페이지 번호를 구간으로 접는다. 300페이지를 하나씩 적으면 예산을 먹는다."""
    ordered = sorted({int(value) for value in pages})
    if not ordered:
        return ""
    spans: list[str] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        spans.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    spans.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(spans)


def _page_text(document, page: int) -> str:
    rows = document.index.page_rows(page)
    return "\n".join(row.text for row in rows if row.text)


def build(
    *,
    corpus,
    finding_pages: dict[str, set[int]],
    neighbours: int,
    char_budget: int,
) -> list[dict]:
    """문헌별 페이지 전문 묶음.

    finding_pages 는 {attachment_id: {페이지 번호}} — 이번 실행에서 **근거로
    확정된 구간이 있는** 페이지다. 인덱스에 있는 페이지가 아니라 근거가 나온
    페이지만 중심으로 삼는다. 그 구분이 없으면 검색과 무관한 페이지가 문맥이라는
    이름으로 딸려 온다.

    char_budget 은 여기서 쓰는 거친 상한이다. 정확한 맞춤은 evidence.fit() 이
    완성된 문자열을 직접 재서 한다 — 여기서는 300페이지 문헌을 통째로 만들었다가
    하나씩 빼는 O(n²) 렌더링을 피하려고 미리 자를 뿐이다.
    """
    if neighbours < 0 or char_budget <= 0:
        return []

    per_page_cap = max(1, int(char_budget * MAX_PAGE_SHARE))
    used = 0
    documents: list[dict] = []
    for document in corpus:
        found = {int(page) for page in finding_pages.get(document.attachment_id, set())}
        if not found:
            continue
        last_page = int(getattr(document.index, "page_count", 0) or 0)
        wanted = widen(found, last_page, neighbours)
        entry_pages: list[dict] = []
        for page in wanted:
            text = _page_text(document, page)
            if not text:
                continue
            if len(text) > per_page_cap or used + len(text) > char_budget:
                # 거친 상한. 남은 자리에 안 들어가면 여기서 멈춘다.
                continue
            status = document.index.page_status(page) or {}
            entry_pages.append(
                {
                    "pdf_page": page,
                    "printed_page": status.get("printed_page") or None,
                    "candidate": page in found,
                    "extraction_status": status.get("status", ""),
                    "text": text,
                }
            )
            used += len(text)
        if not entry_pages:
            continue
        included = {page["pdf_page"] for page in entry_pages}
        documents.append(
            {
                "attachment": document.alias,
                "attachment_id": document.attachment_id,
                "filename": document.filename,
                "pdf_pages": last_page,
                "candidate_pages": sorted(found),
                "included_pages": sorted(included),
                "pages": entry_pages,
            }
        )
    return documents


def unverified_pages(document: dict) -> list[int]:
    """이번 실행이 페이지 전문으로 확인하지 않은 페이지.

    「찾지 못했다」가 아니라 「보지 않았다」이며, 둘을 섞으면 보고서가 거짓이 된다.
    """
    included = {int(page["pdf_page"]) for page in document.get("pages", [])}
    last = int(document.get("pdf_pages") or 0)
    return [page for page in range(1, last + 1) if page not in included]


# 예산 때문에 뺀 페이지를 몇 개까지 이름으로 적을 것인가. 나머지는 개수로만
# 적는다 — 목록이 길어지면 그 목록이 다시 예산을 먹고, 줄이려는 fit() 이 수렴을
# 못 한다.
MAX_LISTED_DROPS = 8


def render(documents: list[dict], dropped: list[str] | None = None) -> list[str]:
    """근거 패키지 안에 들어갈 페이지 절. 담을 것도 뺀 것도 없으면 빈 목록.

    dropped 는 예산 때문에 뺀 페이지의 짧은 이름들이다. 이 목록을
    package_reductions 에 넣지 않는 것은 의도다 — 그쪽에 넣으면
    evidence._apply_reductions 가 **모든 구성의 상태 사유**에 같은 문장을 붙이고
    not_found 를 coverage 로 내린다. 페이지를 뺀 것은 근거를 뺀 것이 아니다.
    근거 구간과 그 발췌는 그대로 남아 있고, 빠진 것은 앞뒤 문맥뿐이다. 구성
    판정을 흔들면 사실과 달라진다.

    빠진 페이지는 아래 「미확인 페이지」에 자동으로 나타난다 — 그 목록은 지금
    담고 있는 페이지에서 계산하기 때문이다.
    """
    dropped = dropped or []
    if not documents and not dropped:
        return []
    if not documents:
        return ["", "[근거 구간이 있는 페이지 전문]", "", _dropped_line(dropped)]
    lines = [
        "",
        "[근거 구간이 있는 페이지 전문]",
        "",
        "아래는 위 근거 구간이 실린 페이지의 **전문**과 그 앞뒤 페이지입니다.",
        "발췌만으로는 앞뒤 문맥이 끊기므로, 예산이 허락하는 만큼 페이지를 통째로",
        "담았습니다. 여기 없는 페이지는 이번 검토 범위 밖입니다 — 검토하지 않은",
        "것과 문헌에 없는 것은 다릅니다.",
    ]
    for document in documents:
        missing = page_list(unverified_pages(document))
        lines += [
            "",
            f"[{document['attachment']} · {document['filename']}]",
            f"- 전체 {document['pdf_pages']}페이지 중 "
            f"{len(document['pages'])}페이지를 전문으로 담았습니다.",
            f"- 담은 페이지: {page_list(document['included_pages']) or '(없음)'}",
            f"- **미확인 페이지**: {missing or '(없음)'}",
        ]
        for page in document["pages"]:
            mark = "근거 페이지" if page["candidate"] else "앞뒤 문맥"
            printed = (
                f" (인쇄면 {page['printed_page']})" if page["printed_page"] else ""
            )
            lines += [
                "",
                f"--- {document['attachment']} p.{page['pdf_page']}{printed} · "
                f"{mark} · 추출 {page['extraction_status']} ---",
                page["text"],
            ]
    if dropped:
        lines += ["", _dropped_line(dropped)]
    return lines


def _dropped_line(dropped: list[str]) -> str:
    listed = dropped[:MAX_LISTED_DROPS]
    rest = len(dropped) - len(listed)
    tail = f" 외 {rest}쪽" if rest > 0 else ""
    return (
        f"- 예산 때문에 뺀 페이지: {', '.join(listed)}{tail}. 위 미확인 페이지에 "
        "포함됩니다. 근거 구간과 그 발췌는 그대로입니다."
    )


def drop_one(documents: list[dict], *, only_context: bool) -> dict | None:
    """페이지 하나를 뺀다. 뺐으면 그 페이지 정보, 없으면 None.

    후보에서 **먼 것부터** 뺀다. 같은 문헌 안에서는 후보 페이지와의 거리가 먼
    페이지가 먼저 나간다 — 앞뒤 한 칸은 붙어 있는 문맥이고, 두 칸 밖은 그보다
    약한 문맥이다.
    """
    best: tuple[int, int, dict, dict] | None = None
    for document in documents:
        candidates = {
            int(page["pdf_page"]) for page in document["pages"] if page["candidate"]
        }
        for position, page in enumerate(document["pages"]):
            if only_context and page["candidate"]:
                continue
            number = int(page["pdf_page"])
            distance = (
                min((abs(number - c) for c in candidates), default=0)
                if candidates
                else 0
            )
            key = (distance, number)
            if best is None or key > (best[0], best[1]):
                best = (distance, number, document, page)
    if best is None:
        return None
    _distance, number, document, page = best
    document["pages"] = [
        item for item in document["pages"] if int(item["pdf_page"]) != number
    ]
    document["included_pages"] = [
        value for value in document["included_pages"] if value != number
    ]
    return {
        "attachment": document["attachment"],
        "pdf_page": number,
        "candidate": page["candidate"],
        "label": f"{document['attachment']} p.{number}",
    }
