"""AttachmentIngestionService.

브라우저 업로드는 CLI 에 파일을 첨부하는 것이 아니다. 흐름은 이렇다.

  업로드 → 실행별 격리 폴더에 UUID 이름으로 저장 → 검증 → 텍스트 정규화
  → manifest 기록 → 최종 프롬프트에 인라인 삽입

여기서는 추출/정규화만 한다. 요약, 청킹, 판단, 분석은 하지 않는다.
그건 Master Prompt 의 몫이고, ARIA 가 손대면 "분석 방법을 갖지 않는다"는
원칙을 어기게 된다.

너무 큰 문서는 조용히 자르지 않는다. INPUT_TOO_LARGE 로 정직하게 거절한다.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pypdf

from ..enums import AttachmentRole, DeliveryMode, ExtractionMethod
from .security import (
    UnsafeFilename,
    contains_executable_signature,
    looks_like_pdf,
    sniff_mime,
    validate_filename,
)

# 페이지당 이 글자 수보다 적게 나오면 텍스트 레이어가 없는 스캔본으로 본다.
# 스캔본은 추출 결과가 사실상 0자다. 값을 높이면 텍스트가 적은 정상 PDF
# (표지, 도면 설명 페이지 등)를 오탐한다.
_SCANNED_PDF_THRESHOLD = 10

PAGE_MARKER = "--- PAGE {page} ---"


@dataclass
class IngestedFile:
    attachment_id: str
    original_filename: str
    internal_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    required: bool
    stored_path: str
    role: str = AttachmentRole.SUPPLEMENTAL
    normalized_text_path: str | None = None
    page_count: int | None = None
    char_count: int = 0
    extraction_method: str = ExtractionMethod.NONE
    ocr_used: bool = False
    delivery_mode: str = DeliveryMode.UNSUPPORTED
    read_ok: bool = False
    error: str | None = None

    def manifest_entry(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "original_filename": self.original_filename,
            "internal_filename": self.internal_filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "required": self.required,
            "role": self.role,
            "page_count": self.page_count,
            "char_count": self.char_count,
            "extraction_method": self.extraction_method,
            "ocr_used": self.ocr_used,
            "delivery_mode": self.delivery_mode,
            "read_ok": self.read_ok,
            "error": self.error,
        }


@dataclass
class IngestionLimits:
    max_file_size_bytes: int = 25 * 1024 * 1024
    max_total_upload_bytes: int = 100 * 1024 * 1024
    max_files: int = 20


@dataclass
class IngestionResult:
    files: list[IngestedFile] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(f.char_count for f in self.files)

    @property
    def has_required_failure(self) -> bool:
        return any(f.required and not f.read_ok for f in self.files)


def preprocessing_versions() -> dict[str, str]:
    return {"pypdf": pypdf.__version__}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_pdf(path: Path) -> tuple[str, int, str | None]:
    """PDF 에서 페이지 경계를 보존한 텍스트를 뽑는다.

    반환: (텍스트, 페이지 수, 오류)
    """
    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as exc:  # pypdf 는 다양한 예외를 던진다
        return "", 0, f"PDF 를 열 수 없습니다: {type(exc).__name__}: {exc}"

    if getattr(reader, "is_encrypted", False):
        try:
            if reader.decrypt("") == 0:
                return "", 0, "암호로 보호된 PDF 입니다. 해제 후 다시 업로드하십시오."
        except Exception:
            return "", 0, "암호로 보호된 PDF 입니다. 해제 후 다시 업로드하십시오."

    pages: list[str] = []
    page_count = 0
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return "", 0, f"PDF 페이지를 읽을 수 없습니다: {exc}"

    for index in range(page_count):
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception as exc:
            raw = f"[ARIA: {index + 1}페이지 추출 실패: {type(exc).__name__}]"
        pages.append(f"{PAGE_MARKER.format(page=index + 1)}\n{_normalize_newlines(raw).strip()}")

    text = "\n\n".join(pages).strip()
    stripped = text.replace("\n", "").strip()
    marker_chars = sum(len(PAGE_MARKER.format(page=i + 1)) for i in range(page_count))

    if page_count and (len(stripped) - marker_chars) < _SCANNED_PDF_THRESHOLD * page_count:
        return (
            text,
            page_count,
            "텍스트 레이어가 거의 없습니다. 스캔 PDF 로 보이며 ARIA v0.1 은 OCR 을 "
            "지원하지 않습니다. 텍스트 PDF 로 변환한 뒤 업로드하십시오.",
        )

    return text, page_count, None


def ingest_one(
    raw_filename: str,
    data: bytes,
    work_dir: Path,
    required: bool,
    limits: IngestionLimits,
    role: str = AttachmentRole.SUPPLEMENTAL,
) -> IngestedFile:
    safe = validate_filename(raw_filename)

    attachment_id = str(uuid.uuid4())
    internal_filename = f"{attachment_id}{safe.extension}"
    stored_path = work_dir / "input" / internal_filename
    stored_path.parent.mkdir(parents=True, exist_ok=True)

    head = data[:16]
    if contains_executable_signature(head):
        raise UnsafeFilename(
            "파일 내용이 실행 파일 또는 압축 파일 형식입니다. 확장자와 무관하게 차단했습니다."
        )
    if safe.extension == ".pdf" and not looks_like_pdf(head):
        raise UnsafeFilename("확장자는 .pdf 이지만 PDF 형식이 아닙니다.")
    if safe.extension != ".pdf" and looks_like_pdf(head):
        raise UnsafeFilename("내용이 PDF 인데 확장자가 다릅니다.")

    if len(data) > limits.max_file_size_bytes:
        raise UnsafeFilename(
            f"파일이 너무 큽니다: {len(data):,} bytes "
            f"(제한 {limits.max_file_size_bytes:,} bytes)"
        )

    stored_path.write_bytes(data)

    item = IngestedFile(
        attachment_id=attachment_id,
        original_filename=safe.display,
        internal_filename=internal_filename,
        mime_type=sniff_mime(safe.extension, head),
        size_bytes=len(data),
        sha256=_sha256(data),
        required=required,
        stored_path=str(stored_path),
        role=role,
    )

    if safe.extension == ".pdf":
        text, page_count, error = extract_pdf(stored_path)
        item.page_count = page_count
        item.extraction_method = ExtractionMethod.PDF_TEXT_LAYER
        if error:
            item.error = error
            item.delivery_mode = DeliveryMode.UNSUPPORTED
            item.read_ok = False
            # 스캔본이어도 뽑힌 텍스트는 버리지 않고 남겨둔다.
            if text:
                item.normalized_text_path = _write_normalized(work_dir, attachment_id, text)
                item.char_count = len(text)
            return item
        normalized = text
    else:
        normalized = _normalize_newlines(_decode_text(data)).strip()
        item.extraction_method = ExtractionMethod.RAW_TEXT
        if safe.extension == ".json":
            try:
                json.loads(normalized)
            except json.JSONDecodeError as exc:
                item.error = f"JSON 형식이 아닙니다: {exc.msg} (line {exc.lineno})"
                item.delivery_mode = DeliveryMode.UNSUPPORTED
                return item

    if not normalized.strip():
        item.error = "내용이 비어 있습니다."
        item.delivery_mode = DeliveryMode.UNSUPPORTED
        item.read_ok = False
        return item

    item.normalized_text_path = _write_normalized(work_dir, attachment_id, normalized)
    item.char_count = len(normalized)
    item.delivery_mode = DeliveryMode.INLINE_CONTEXT
    item.read_ok = True
    return item


def _write_normalized(work_dir: Path, attachment_id: str, text: str) -> str:
    path = work_dir / "normalized" / f"{attachment_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def ingest_many(
    uploads: list[tuple[str, bytes, bool] | tuple[str, bytes, bool, str]],
    work_dir: Path,
    limits: IngestionLimits,
) -> IngestionResult:
    result = IngestionResult()

    if len(uploads) > limits.max_files:
        raise UnsafeFilename(
            f"파일 개수가 제한을 넘었습니다: {len(uploads)} (최대 {limits.max_files})"
        )

    total = sum(len(upload[1]) for upload in uploads)
    if total > limits.max_total_upload_bytes:
        raise UnsafeFilename(
            f"총 업로드 크기가 제한을 넘었습니다: {total:,} bytes "
            f"(제한 {limits.max_total_upload_bytes:,} bytes)"
        )

    for upload in uploads:
        filename, data, required = upload[:3]
        role = upload[3] if len(upload) == 4 else AttachmentRole.SUPPLEMENTAL
        try:
            result.files.append(
                ingest_one(filename, data, work_dir, required, limits, role=role)
            )
        except UnsafeFilename as exc:
            result.rejected.append({"filename": filename, "reason": str(exc)})
    return result


def read_normalized(item: IngestedFile) -> str:
    if not item.normalized_text_path:
        return ""
    path = Path(item.normalized_text_path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
