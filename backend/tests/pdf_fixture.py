"""테스트용 최소 PDF 생성기.

reportlab 같은 추가 의존성 없이 텍스트 레이어가 있는 PDF 를 만든다.
페이지 경계 추출과 스캔본 감지를 검증하는 데 쓴다.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: list[str]) -> bytes:
    """각 문자열을 한 페이지로 하는 PDF 바이트를 만든다."""
    objects: list[bytes] = []

    page_count = len(pages)
    # 1: Catalog, 2: Pages, 3..: Page/Contents 쌍, 마지막: Font
    font_obj_num = 3 + page_count * 2
    page_obj_nums = [3 + i * 2 for i in range(page_count)]

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1")
    )

    for i, text in enumerate(pages):
        page_num = page_obj_nums[i]
        contents_num = page_num + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {contents_num} 0 R "
                f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> >>"
            ).encode("latin-1")
        )
        lines = text.split("\n")
        parts = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for line in lines:
            parts.append(f"({_escape(line)}) Tj")
            parts.append("T*")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1")
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def build_scanned_like_pdf(page_count: int = 2) -> bytes:
    """텍스트 레이어가 거의 없는 PDF (스캔본 감지 테스트용)."""
    return build_pdf([" "] * page_count)
