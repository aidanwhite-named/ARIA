"""페이지 텍스트를 검색 단위(chunk)로 나눈다.

임의의 고정 글자 수로 자르지 않는다. 그렇게 하면 "청구항 1의 (a) 구성"이
두 조각으로 갈려서 어느 쪽도 검색에 걸리지 않는 일이 생긴다. 우선순위는
다음과 같다.

  1. 특허 문단번호 경계 ([0032], 【0032】)
  2. 문단 경계 (빈 줄)
  3. 페이지 경계
  4. 위로도 너무 긴 문단만 제한된 overlap 으로 분할

청크는 **절대 페이지를 넘지 않는다.** 넘으면 검색 결과의 "몇 쪽"이 두 개가
되어 출처가 흐려진다. 문헌을 넘지 않는 것은 더 말할 것도 없다 — 인덱스 자체가
문헌마다 따로 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extraction import PARAGRAPH_RE, DocumentExtraction, detect_section

# 한 청크의 목표 상한. 넘으면 4번 규칙으로 자른다. 한글 특허 문단 하나가
# 보통 이 안에 들어간다.
MAX_CHUNK_CHARS = 1200

# 4번 규칙으로 자를 때만 쓰는 overlap. 경계에 걸친 문장이 어느 쪽에서도
# 검색되지 않는 것을 막는 최소한이며, 이 값 때문에 같은 문장이 두 청크에
# 나오면 검색 결과에서 한쪽만 남긴다(search 의 중복 제거).
CHUNK_OVERLAP_CHARS = 120

# 이보다 짧은 조각은 앞 청크에 붙인다. 표 머리글이나 도면 부호만 있는 줄이
# 독립 청크가 되면 검색 결과가 그런 조각으로 채워진다.
MIN_CHUNK_CHARS = 40

_BLANK_LINE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    """검색 단위 하나. 출처 메타데이터를 전부 들고 다닌다."""

    chunk_id: str
    page_number: int
    page_order: int
    text: str
    paragraph: str = ""
    section: str = ""
    printed_page: str = ""
    extraction_status: str = "ok"
    extraction_method: str = ""

    @property
    def char_count(self) -> int:
        return len(self.text)


def _paragraph_blocks(text: str) -> list[str]:
    """문단번호가 나오는 줄에서 새 블록을 시작한다."""
    lines = text.split("\n")
    blocks: list[list[str]] = []
    for line in lines:
        starts_paragraph = bool(PARAGRAPH_RE.search(line[:24]))
        if starts_paragraph or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return ["\n".join(block).strip() for block in blocks if "\n".join(block).strip()]


def _split_long(block: str) -> list[str]:
    """4번 규칙. 문단으로도 줄이지 못한 블록만 overlap 을 두고 자른다."""
    if len(block) <= MAX_CHUNK_CHARS:
        return [block]
    pieces: list[str] = []
    start = 0
    while start < len(block):
        end = min(len(block), start + MAX_CHUNK_CHARS)
        if end < len(block):
            # 문장 경계에서 자를 수 있으면 그렇게 한다.
            window = block.rfind("\n", start + MIN_CHUNK_CHARS, end)
            if window == -1:
                window = block.rfind(". ", start + MIN_CHUNK_CHARS, end)
            if window == -1:
                window = block.rfind("다. ", start + MIN_CHUNK_CHARS, end)
            if window > start:
                end = window + 1
        piece = block[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(block):
            break
        start = max(start + 1, end - CHUNK_OVERLAP_CHARS)
    return pieces


def split_page(text: str) -> list[str]:
    """페이지 텍스트 하나를 청크 본문 목록으로 나눈다."""
    stripped = text.strip()
    if not stripped:
        return []

    blocks = _paragraph_blocks(stripped)
    # 문단번호가 없는 문헌은 1번 규칙에서 블록이 하나로 남는다. 그때만 2번
    # 규칙(빈 줄)으로 내려간다.
    if len(blocks) <= 1:
        blocks = [b.strip() for b in _BLANK_LINE.split(stripped) if b.strip()]
    if not blocks:
        blocks = [stripped]

    pieces: list[str] = []
    for block in blocks:
        for piece in _split_long(block):
            if pieces and len(piece) < MIN_CHUNK_CHARS:
                # 너무 짧은 꼬리는 앞 청크에 붙인다. 페이지 안에서만 붙이므로
                # 페이지 경계는 그대로다.
                pieces[-1] = f"{pieces[-1]}\n{piece}"
            else:
                pieces.append(piece)
    return pieces


def chunk_document(extraction: DocumentExtraction) -> list[Chunk]:
    """문헌 전체를 청크로 나눈다. 추출 실패 페이지는 청크를 만들지 않는다.

    실패 페이지를 빈 청크로라도 남기지 않는 이유는, 검색 결과에 본문 없는
    행이 섞이면 "찾았지만 내용이 없다"와 "그 페이지를 못 읽었다"가 구분되지
    않기 때문이다. 못 읽은 페이지는 완전성 보고서와 문헌 상태로 따로 전달되고,
    그 기록이 not_found 판정을 막는다.
    """
    chunks: list[Chunk] = []
    section = ""
    for page in extraction.pages:
        section = detect_section(page.text, section)
        pieces = split_page(page.text)
        for order, piece in enumerate(pieces, start=1):
            match = PARAGRAPH_RE.search(piece[:24])
            chunks.append(
                Chunk(
                    chunk_id=f"P{page.page_number:04d}-{order:03d}",
                    page_number=page.page_number,
                    page_order=order,
                    text=piece,
                    paragraph=f"[{match.group(1)}]" if match else "",
                    section=section,
                    printed_page=page.printed_page or "",
                    extraction_status=page.status,
                    extraction_method=page.extraction_method,
                )
            )
    return chunks
