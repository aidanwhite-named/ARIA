"""첨부 전처리: 텍스트 디코딩, PDF 추출, 스캔본 감지, 한도."""

from __future__ import annotations

import pytest

from app.enums import DeliveryMode, ExtractionMethod
from app.ingestion.security import UnsafeFilename
from app.ingestion.service import (
    IngestionLimits,
    extract_pdf,
    ingest_many,
    ingest_one,
    read_normalized,
)

from .pdf_fixture import build_pdf, build_scanned_like_pdf

LIMITS = IngestionLimits()


def test_text_file_inlined(work_dir) -> None:
    item = ingest_one("note.txt", b"hello\nworld", work_dir, True, LIMITS)
    assert item.read_ok
    assert item.delivery_mode == DeliveryMode.INLINE_CONTEXT
    assert item.extraction_method == ExtractionMethod.RAW_TEXT
    assert read_normalized(item) == "hello\nworld"
    assert len(item.sha256) == 64


def test_internal_filename_is_uuid_based(work_dir) -> None:
    """원본 파일명을 저장 경로로 그대로 쓰지 않는다."""
    item = ingest_one("보고서.txt", "내용".encode(), work_dir, True, LIMITS)
    assert "보고서" not in item.internal_filename
    assert item.internal_filename.startswith(item.attachment_id)
    assert item.original_filename == "보고서.txt"


def test_cp949_korean_text_decoded(work_dir) -> None:
    """Windows 에서 만든 cp949 텍스트도 깨지지 않아야 한다."""
    item = ingest_one("kr.txt", "한글 내용입니다".encode("cp949"), work_dir, True, LIMITS)
    assert item.read_ok
    assert read_normalized(item) == "한글 내용입니다"


def test_utf8_bom_stripped(work_dir) -> None:
    item = ingest_one("bom.txt", "﻿본문".encode("utf-8"), work_dir, True, LIMITS)
    assert read_normalized(item) == "본문"


def test_crlf_normalized(work_dir) -> None:
    item = ingest_one("crlf.txt", b"a\r\nb\r\nc", work_dir, True, LIMITS)
    assert read_normalized(item) == "a\nb\nc"


def test_empty_file_rejected(work_dir) -> None:
    item = ingest_one("empty.txt", b"   ", work_dir, True, LIMITS)
    assert not item.read_ok
    assert item.delivery_mode == DeliveryMode.UNSUPPORTED
    assert item.error


def test_invalid_json_reported(work_dir) -> None:
    item = ingest_one("bad.json", b"{not json", work_dir, True, LIMITS)
    assert not item.read_ok
    assert "JSON" in (item.error or "")


def test_pdf_page_boundaries_preserved(work_dir) -> None:
    pdf = build_pdf(
        [
            "Alpha page content here. This paragraph exists so the page has a real text layer.",
            "Beta page content here. The second page also carries a normal amount of text.",
        ]
    )
    item = ingest_one("doc.pdf", pdf, work_dir, True, LIMITS)
    assert item.read_ok
    assert item.page_count == 2
    assert item.extraction_method == ExtractionMethod.PDF_TEXT_LAYER

    text = read_normalized(item)
    assert "--- PAGE 1 ---" in text
    assert "--- PAGE 2 ---" in text
    assert text.index("--- PAGE 1 ---") < text.index("--- PAGE 2 ---")
    assert "Alpha page" in text
    assert "Beta page" in text


def test_scanned_pdf_detected_and_rejected(work_dir) -> None:
    item = ingest_one("scan.pdf", build_scanned_like_pdf(3), work_dir, True, LIMITS)
    assert not item.read_ok
    assert item.delivery_mode == DeliveryMode.UNSUPPORTED
    assert "OCR" in (item.error or "")


def test_scanned_pdf_keeps_partial_text(work_dir) -> None:
    """거부하더라도 뽑힌 텍스트는 버리지 않는다."""
    item = ingest_one("scan.pdf", build_scanned_like_pdf(2), work_dir, True, LIMITS)
    assert item.normalized_text_path is not None


def test_pdf_with_wrong_extension_rejected(work_dir) -> None:
    with pytest.raises(UnsafeFilename):
        ingest_one("fake.txt", build_pdf(["x"]), work_dir, True, LIMITS)


def test_non_pdf_with_pdf_extension_rejected(work_dir) -> None:
    with pytest.raises(UnsafeFilename):
        ingest_one("fake.pdf", b"just text, not a pdf", work_dir, True, LIMITS)


def test_executable_disguised_as_txt_rejected(work_dir) -> None:
    with pytest.raises(UnsafeFilename):
        ingest_one("payload.txt", b"MZ\x90\x00\x03binary", work_dir, True, LIMITS)


def test_corrupt_pdf_reports_error(work_dir) -> None:
    item = ingest_one("broken.pdf", b"%PDF-1.4\ngarbage", work_dir, True, LIMITS)
    assert not item.read_ok
    assert item.error


def test_file_size_limit(work_dir) -> None:
    limits = IngestionLimits(max_file_size_bytes=10)
    with pytest.raises(UnsafeFilename, match="너무 큽니다"):
        ingest_one("big.txt", b"x" * 100, work_dir, True, limits)


def test_file_count_limit(work_dir) -> None:
    limits = IngestionLimits(max_files=2)
    uploads = [(f"f{i}.txt", b"data", True) for i in range(3)]
    with pytest.raises(UnsafeFilename, match="개수"):
        ingest_many(uploads, work_dir, limits)


def test_total_size_limit(work_dir) -> None:
    limits = IngestionLimits(max_total_upload_bytes=10)
    uploads = [("a.txt", b"x" * 8, True), ("b.txt", b"y" * 8, True)]
    with pytest.raises(UnsafeFilename, match="총 업로드"):
        ingest_many(uploads, work_dir, limits)


def test_rejected_files_do_not_abort_batch(work_dir) -> None:
    uploads = [
        ("good.txt", b"fine", True),
        ("bad.exe", b"MZ\x00", True),
        ("also-good.md", b"# heading", True),
    ]
    result = ingest_many(uploads, work_dir, LIMITS)
    assert len(result.files) == 2
    assert len(result.rejected) == 1
    assert result.rejected[0]["filename"] == "bad.exe"


def test_extract_pdf_on_missing_file(tmp_path) -> None:
    text, pages, error = extract_pdf(tmp_path / "nope.pdf")
    assert text == ""
    assert pages == 0
    assert error
