"""업로드 파일명 검증.

업로드된 파일명을 저장 경로로 그대로 쓰지 않는다. 저장은 UUID 기반 내부
파일명으로 하고, 원본 이름은 manifest 에만 기록한다. 그래도 파일명 자체를
검증하는 이유는 manifest/UI/다운로드 파일명으로 흘러나가기 때문이다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# v0.1 지원 형식. 모두 ARIA 가 직접 텍스트로 정규화할 수 있는 것만 넣었다.
ALLOWED_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".json", ".csv", ".pdf"})

# 실행 환경 설정 파일. 작업 폴더에 들어가면 CLI 가 지시문으로 읽을 수 있다.
BLOCKED_NAMES = frozenset(
    {
        "claude.md",
        "agents.md",
        "gemini.md",
        "mcp.json",
        ".mcp.json",
        "settings.json",
        "settings.local.json",
        ".env",
        ".npmrc",
        "package.json",
    }
)

BLOCKED_DIR_MARKERS = frozenset({".claude", ".codex", ".gemini", ".git", "node_modules"})

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_CHARS = re.compile(r'[<>:"|?*\\/]')


class UnsafeFilename(ValueError):
    """업로드를 거부해야 하는 파일명."""


@dataclass
class SafeName:
    original: str
    display: str
    extension: str


def validate_filename(raw: str) -> SafeName:
    if not raw or not raw.strip():
        raise UnsafeFilename("파일명이 비어 있습니다.")

    name = unicodedata.normalize("NFC", raw.strip())

    if _CONTROL_CHARS.search(name):
        raise UnsafeFilename("파일명에 제어 문자가 포함되어 있습니다.")

    # 경로 탐색 및 절대 경로 차단. 브라우저는 보통 basename 만 보내지만
    # 요청은 직접 만들어질 수 있다.
    if ".." in name:
        raise UnsafeFilename("파일명에 상위 경로 참조(..)가 포함되어 있습니다.")
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise UnsafeFilename("절대 경로는 허용되지 않습니다.")

    lowered_parts = [part.lower() for part in re.split(r"[\\/]", name) if part]
    for part in lowered_parts[:-1]:
        if part in BLOCKED_DIR_MARKERS:
            raise UnsafeFilename(f"허용되지 않는 디렉터리가 포함되어 있습니다: {part}")

    base = lowered_parts[-1] if lowered_parts else ""
    if not base:
        raise UnsafeFilename("파일명을 확인할 수 없습니다.")

    display = re.split(r"[\\/]", name)[-1]
    if _UNSAFE_CHARS.search(display):
        raise UnsafeFilename("파일명에 사용할 수 없는 문자가 포함되어 있습니다.")

    if display.startswith("."):
        raise UnsafeFilename("점으로 시작하는 숨김 파일은 허용되지 않습니다.")

    if base in BLOCKED_NAMES:
        raise UnsafeFilename(
            f"실행 환경 설정 파일로 인식되어 차단했습니다: {display}"
        )

    stem, _, ext = base.rpartition(".")
    if not stem:
        raise UnsafeFilename("확장자가 없는 파일은 허용되지 않습니다.")
    if stem in _WINDOWS_RESERVED:
        raise UnsafeFilename(f"Windows 예약어는 사용할 수 없습니다: {display}")

    extension = f".{ext}"
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsafeFilename(
            f"지원하지 않는 형식입니다: {extension} (허용: {allowed})"
        )

    return SafeName(original=display, display=display, extension=extension)


def sniff_mime(extension: str, head: bytes) -> str:
    if extension == ".pdf":
        return "application/pdf"
    if extension == ".json":
        return "application/json"
    if extension == ".csv":
        return "text/csv"
    if extension in (".md", ".markdown"):
        return "text/markdown"
    del head
    return "text/plain"


def looks_like_pdf(head: bytes) -> bool:
    return head[:5] == b"%PDF-"


def contains_executable_signature(head: bytes) -> bool:
    """실행 파일이 허용 확장자로 위장해 들어오는 것을 막는다."""
    signatures = (
        b"MZ",  # PE / DOS
        b"\x7fELF",
        b"\xca\xfe\xba\xbe",  # Mach-O fat
        b"\xfe\xed\xfa",  # Mach-O
        b"PK\x03\x04",  # zip 계열 (docx/xlsx/jar) - v0.1 미지원
    )
    return any(head.startswith(sig) for sig in signatures)
