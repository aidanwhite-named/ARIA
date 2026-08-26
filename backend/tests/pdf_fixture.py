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


def build_korean_pdf(pages: list[str]) -> bytes:
    """한글 텍스트 레이어가 있는 PDF.

    build_pdf 는 Helvetica + latin-1 이라 한글을 넣을 수 없다. 실제 한국 공보는
    CID 폰트(Identity-H)에 ToUnicode CMap 이 붙어 있고, 추출기는 그 CMap 으로
    본문을 되살린다. 여기서도 같은 구조를 만든다 — 글리프 데이터는 없지만
    추출 경로에는 영향이 없고, ToUnicode 만 있으면 pypdf 가 원문을 돌려준다.

    글자마다 코드를 하나씩 배정하므로 임의의 한글·기호를 그대로 쓸 수 있다.
    """
    charset: list[str] = []
    for text in pages:
        for char in text:
            if char != "\n" and char not in charset:
                charset.append(char)
    code_of = {char: index + 1 for index, char in enumerate(charset)}

    bfchar = "\n".join(f"<{code_of[c]:04X}> <{ord(c):04X}>" for c in charset)
    tounicode = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /ARIA def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(charset)} beginbfchar\n{bfchar}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    )

    objects: list[bytes] = []
    page_count = len(pages)
    page_obj_nums = [3 + i * 2 for i in range(page_count)]
    font_num = 3 + page_count * 2
    descendant_num = font_num + 1
    descriptor_num = font_num + 2
    tounicode_num = font_num + 3

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1")
    )

    for index, text in enumerate(pages):
        page_num = page_obj_nums[index]
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {page_num + 1} 0 R "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
            ).encode("latin-1")
        )
        parts = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for line in text.split("\n"):
            hexed = "".join(f"{code_of[c]:04X}" for c in line)
            parts.append(f"<{hexed}> Tj")
            parts.append("T*")
        parts.append("ET")
        stream = "\n".join(parts).encode("latin-1")
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    objects.append(
        (
            "<< /Type /Font /Subtype /Type0 /BaseFont /ARIATest "
            f"/Encoding /Identity-H /DescendantFonts [{descendant_num} 0 R] "
            f"/ToUnicode {tounicode_num} 0 R >>"
        ).encode("latin-1")
    )
    objects.append(
        (
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ARIATest "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
            f"/Supplement 0 >> /FontDescriptor {descriptor_num} 0 R /DW 1000 >>"
        ).encode("latin-1")
    )
    objects.append(
        b"<< /Type /FontDescriptor /FontName /ARIATest /Flags 4 "
        b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 900 /Descent -200 "
        b"/CapHeight 700 /StemV 80 >>"
    )
    cmap = tounicode.encode("latin-1")
    objects.append(
        b"<< /Length "
        + str(len(cmap)).encode()
        + b" >>\nstream\n"
        + cmap
        + b"\nendstream"
    )

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
