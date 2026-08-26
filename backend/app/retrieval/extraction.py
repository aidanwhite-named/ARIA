"""PDF 페이지 단위 추출과 완전성 보고서.

기존 ingestion.service.extract_pdf 는 "프롬프트에 넣을 한 덩어리 텍스트"를
만든다. 여기서는 같은 pypdf 추출을 **페이지 레코드**로 받는다. 검색 결과가
"몇 쪽 몇 문단"인지 말하려면 페이지가 합쳐지기 전에 잡아야 한다.

OCR 은 하지 않는다. 텍스트 레이어가 없는 페이지는 그렇다고 기록할 뿐이고,
그 기록이 뒤에서 "문헌에 없음" 판정을 막는 근거가 된다.

추출 방식을 두 가지로 돌린다.

  plain  : 기존 경로와 같은 pypdf 기본 추출. 정규화 텍스트와 같은 결과다.
  layout : pypdf 4.0 부터 있는 extraction_mode="layout".

두 결과가 크게 어긋나는 페이지는 경고로 남긴다. plain 이 사실상 비어 있는데
layout 이 본문을 뽑아낸 페이지는 layout 결과를 색인에 쓴다 — 페이지를 통째로
잃는 것보다 낫고, 어느 방식으로 뽑았는지는 청크마다 기록되므로 출처가 흐려지지
않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

from .versions import EXTRACTOR_VERSION, INDEX_VERSION

# 페이지당 이 글자 수 미만이면 텍스트 레이어가 없다고 본다.
# ingestion.service._SCANNED_PDF_THRESHOLD 와 같은 값을 쓴다 — 두 곳이 다른
# 기준을 쓰면 "업로드는 통과했는데 색인은 전부 빈 페이지" 같은 상태가 된다.
LOW_TEXT_THRESHOLD = 10

# 두 추출 방식의 글자 수가 이 비율 이상 어긋나면 의심 페이지로 표시한다.
DIVERGENCE_RATIO = 0.35

# 페이지 추출 상태.
STATUS_OK = "ok"
STATUS_EMPTY = "empty_or_low_text"
STATUS_FAILED = "extraction_failed"
STATUS_VISUAL = "visual_review_required"
PAGE_STATUSES = (STATUS_OK, STATUS_EMPTY, STATUS_FAILED, STATUS_VISUAL)

# 사람이 원문을 봐야 하는 상태. 이 페이지가 하나라도 있으면 그 문헌에 대한
# not_found 판정을 확정할 수 없다.
UNREADABLE_STATUSES = frozenset({STATUS_EMPTY, STATUS_FAILED, STATUS_VISUAL})

WARN_DIVERGENCE = "extraction_divergence"

# 문헌 전체 상태.
DOC_COMPLETE = "complete"
DOC_REVIEW = "review_required"
DOC_UNUSABLE = "unusable"

METHOD_PLAIN = "pdf_text_layer"
METHOD_LAYOUT = "pdf_layout"
METHOD_RAW = "raw_text"

# 특허 문단번호. 한국 공보는 【0032】, 영문·KIPO 텍스트본은 [0032] 를 쓴다.
PARAGRAPH_RE = re.compile(r"[\[【]\s*(\d{3,5})\s*[\]】]")

# 페이지 머리말/꼬리말의 인쇄 페이지 번호. "- 12 -", "12", "12/47", "Page 12".
_PRINTED_PATTERNS = (
    re.compile(r"^-\s*(\d{1,4})\s*-$"),
    re.compile(r"^(\d{1,4})\s*/\s*\d{1,4}$"),
    re.compile(r"^(?:page|쪽|페이지)\s*[.:]?\s*(\d{1,4})$", re.IGNORECASE),
    re.compile(r"^(\d{1,4})$"),
)

# 공보의 절 제목. 검색 결과에 "어디 절인지"를 붙이는 용도이며, 없는 문헌도
# 많으므로 못 찾는 것이 정상이다.
_SECTION_PATTERNS = (
    (re.compile(r"[\[【]\s*발명의?\s*명칭\s*[\]】]"), "발명의 명칭"),
    (re.compile(r"[\[【]\s*기술\s*분야\s*[\]】]"), "기술분야"),
    (re.compile(r"[\[【]\s*배경\s*기술\s*[\]】]"), "배경기술"),
    (re.compile(r"[\[【]\s*해결하?려?는?\s*과제\s*[\]】]"), "해결하려는 과제"),
    (re.compile(r"[\[【]\s*과제의?\s*해결\s*수단\s*[\]】]"), "과제의 해결 수단"),
    (re.compile(r"[\[【]\s*발명의?\s*효과\s*[\]】]"), "발명의 효과"),
    (re.compile(r"[\[【]\s*도면의?\s*간단한?\s*설명\s*[\]】]"), "도면의 간단한 설명"),
    (
        re.compile(r"[\[【]\s*발명을?\s*실시하기?\s*위한"),
        "발명을 실시하기 위한 구체적인 내용",
    ),
    (re.compile(r"[\[【]\s*청구\s*범위\s*[\]】]"), "청구범위"),
    (re.compile(r"^\s*특허\s*청구\s*범위\s*$"), "청구범위"),
    (re.compile(r"[\[【]\s*요\s*약\s*[\]】]"), "요약"),
    (re.compile(r"^\s*ABSTRACT\s*$", re.IGNORECASE), "ABSTRACT"),
    (re.compile(r"^\s*(?:CLAIMS?|WHAT IS CLAIMED)\b", re.IGNORECASE), "CLAIMS"),
    (
        re.compile(r"^\s*(?:DETAILED DESCRIPTION|BACKGROUND|SUMMARY)\b", re.IGNORECASE),
        "DESCRIPTION",
    ),
)


@dataclass
class PageRecord:
    """PDF 한 페이지의 추출 결과."""

    page_number: int
    text: str = ""
    status: str = STATUS_OK
    extraction_method: str = METHOD_PLAIN
    extraction_error: str | None = None
    printed_page: str | None = None
    plain_char_count: int = 0
    layout_char_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict:
        return {
            "page_number": self.page_number,
            "printed_page": self.printed_page,
            "status": self.status,
            "extraction_method": self.extraction_method,
            "extraction_error": self.extraction_error,
            "char_count": self.char_count,
            "plain_char_count": self.plain_char_count,
            "layout_char_count": self.layout_char_count,
            "warnings": list(self.warnings),
        }


@dataclass
class DocumentExtraction:
    """문헌 하나의 페이지 레코드와 완전성 보고서 재료."""

    attachment_id: str
    filename: str
    sha256: str
    source_page_count: int = 0
    pages: list[PageRecord] = field(default_factory=list)
    open_error: str | None = None

    @property
    def processed_page_count(self) -> int:
        return len(self.pages)

    def pages_with_status(self, status: str) -> list[int]:
        return [p.page_number for p in self.pages if p.status == status]

    def pages_with_warning(self, warning: str) -> list[int]:
        return [p.page_number for p in self.pages if warning in p.warnings]

    @property
    def unreadable_pages(self) -> list[int]:
        return [p.page_number for p in self.pages if p.status in UNREADABLE_STATUSES]

    @property
    def page_count_mismatch(self) -> bool:
        return self.source_page_count != self.processed_page_count

    def status(self) -> str:
        if self.open_error or not self.pages:
            return DOC_UNUSABLE
        if not any(p.status == STATUS_OK for p in self.pages):
            return DOC_UNUSABLE
        if self.page_count_mismatch or self.unreadable_pages:
            return DOC_REVIEW
        if any(WARN_DIVERGENCE in p.warnings for p in self.pages):
            return DOC_REVIEW
        return DOC_COMPLETE

    def report(self, *, chunk_count: int = 0, chunk_failures: int = 0) -> dict:
        """완전성 보고서. extraction_report.json 에 그대로 들어간다."""
        return {
            "version": 1,
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "pdf_sha256": self.sha256,
            "source_page_count": self.source_page_count,
            "processed_page_count": self.processed_page_count,
            "page_count_mismatch": self.page_count_mismatch,
            "ok_pages": len(self.pages_with_status(STATUS_OK)),
            "empty_or_low_text_pages": self.pages_with_status(STATUS_EMPTY),
            "extraction_failed_pages": self.pages_with_status(STATUS_FAILED),
            "visual_review_required_pages": self.pages_with_status(STATUS_VISUAL),
            "extraction_divergence_pages": self.pages_with_warning(WARN_DIVERGENCE),
            "chunk_count": chunk_count,
            "chunk_failures": chunk_failures,
            "index_version": INDEX_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "open_error": self.open_error,
            "status": self.status(),
        }


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _visible_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def detect_printed_page(text: str) -> str | None:
    """페이지의 머리말/꼬리말에서 인쇄 페이지 번호를 찾는다. 없으면 None."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None
    for line in (*lines[:2], *lines[-2:]):
        if len(line) > 20:
            continue
        for pattern in _PRINTED_PATTERNS:
            match = pattern.match(line)
            if match:
                return match.group(1)
    return None


def detect_section(text: str, current: str = "") -> str:
    """이 텍스트가 새 절을 시작하면 그 이름, 아니면 이어지는 절 이름."""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern, label in _SECTION_PATTERNS:
            if pattern.search(stripped):
                current = label
    return current


def _page_has_images(page) -> bool:
    """이 페이지에 이미지 XObject 가 있는가.

    텍스트가 없는 페이지가 '빈 페이지'인지 '스캔 이미지'인지 가른다. 후자는
    사람이 원문을 봐야 하는 페이지이므로 상태를 나눠서 기록한다.
    """
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return False
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
    except Exception:
        # 자원 딕셔너리를 읽지 못하는 것은 판정 실패이지 이미지의 증거가
        # 아니다. 없다고 단정하지 않고 False 로 두되, 텍스트가 없으면 어차피
        # empty_or_low_text 로 검토 대상이 된다.
        return False
    return False


def _extract_one(page, index: int, layout_check: bool) -> PageRecord:
    record = PageRecord(page_number=index + 1)

    try:
        plain = _normalize(page.extract_text() or "").strip()
    except Exception as exc:
        record.status = STATUS_FAILED
        record.extraction_error = f"{type(exc).__name__}: {exc}"
        return record

    record.plain_char_count = len(plain)
    layout = ""
    if layout_check:
        try:
            layout = _normalize(
                page.extract_text(extraction_mode="layout") or ""
            ).strip()
            record.layout_char_count = len(layout)
        except Exception as exc:  # layout 은 보조 확인이므로 실패해도 계속 간다
            record.warnings.append(f"layout_extraction_failed:{type(exc).__name__}")

    plain_visible = _visible_chars(plain)
    layout_visible = _visible_chars(layout)

    # plain 이 사실상 비었는데 layout 이 본문을 뽑았으면 layout 을 쓴다.
    # 페이지를 통째로 잃는 것보다 낫고, 어느 방식이었는지는 기록에 남는다.
    if plain_visible < LOW_TEXT_THRESHOLD <= layout_visible:
        record.text = layout
        record.extraction_method = METHOD_LAYOUT
        record.warnings.append(WARN_DIVERGENCE)
    else:
        record.text = plain
        record.extraction_method = METHOD_PLAIN
        if plain_visible and layout_visible:
            largest = max(plain_visible, layout_visible)
            if abs(plain_visible - layout_visible) / largest > DIVERGENCE_RATIO:
                record.warnings.append(WARN_DIVERGENCE)

    if _visible_chars(record.text) < LOW_TEXT_THRESHOLD:
        record.status = STATUS_VISUAL if _page_has_images(page) else STATUS_EMPTY
    else:
        record.status = STATUS_OK
        record.printed_page = detect_printed_page(record.text)
    return record


def extract_document(
    path: Path,
    *,
    attachment_id: str,
    filename: str,
    sha256: str,
    layout_check_max_pages: int = 400,
) -> DocumentExtraction:
    """PDF 를 페이지 레코드로 추출한다. 예외를 밖으로 던지지 않는다.

    열지 못한 PDF 도 보고서를 남긴다. 조용히 빈 결과를 돌려주면 "검색 결과가
    없다"와 구분되지 않는다.
    """
    result = DocumentExtraction(
        attachment_id=attachment_id, filename=filename, sha256=sha256
    )
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:
        result.open_error = f"PDF 를 열 수 없습니다: {type(exc).__name__}: {exc}"
        return result

    if getattr(reader, "is_encrypted", False):
        try:
            if reader.decrypt("") == 0:
                result.open_error = "암호로 보호된 PDF 입니다."
                return result
        except Exception:
            result.open_error = "암호로 보호된 PDF 입니다."
            return result

    try:
        result.source_page_count = len(reader.pages)
    except Exception as exc:
        result.open_error = f"PDF 페이지를 읽을 수 없습니다: {exc}"
        return result

    # layout 비교는 페이지마다 한 번 더 파싱한다. 아주 긴 문헌에서 업로드가
    # 눈에 띄게 느려지지 않도록 상한을 둔다. 상한을 넘긴 페이지는 비교하지
    # 않았다는 사실이 layout_char_count = None 으로 남는다.
    layout_check = result.source_page_count <= layout_check_max_pages

    for index in range(result.source_page_count):
        try:
            page = reader.pages[index]
        except Exception as exc:
            result.pages.append(
                PageRecord(
                    page_number=index + 1,
                    status=STATUS_FAILED,
                    extraction_error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        result.pages.append(_extract_one(page, index, layout_check))
    return result


def extract_text_document(
    text: str, *, attachment_id: str, filename: str, sha256: str
) -> DocumentExtraction:
    """PDF 가 아닌 첨부(.txt/.md/.json/.csv)를 한 페이지짜리 문헌으로 만든다.

    페이지 개념이 없는 형식이므로 1페이지로 두고 extraction_method 를 달리
    기록한다. 없는 페이지 번호를 지어내지 않는다.
    """
    result = DocumentExtraction(
        attachment_id=attachment_id, filename=filename, sha256=sha256
    )
    body = _normalize(text).strip()
    result.source_page_count = 1
    record = PageRecord(
        page_number=1,
        text=body,
        extraction_method=METHOD_RAW,
        plain_char_count=len(body),
        status=STATUS_OK if _visible_chars(body) >= LOW_TEXT_THRESHOLD else STATUS_EMPTY,
    )
    result.pages.append(record)
    return result
