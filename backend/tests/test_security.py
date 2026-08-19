"""파일명 검증과 업로드 차단."""

from __future__ import annotations

import pytest

from app.ingestion.security import (
    UnsafeFilename,
    contains_executable_signature,
    looks_like_pdf,
    validate_filename,
)


@pytest.mark.parametrize(
    "name",
    [
        "report.txt",
        "명세서.pdf",
        "notes.md",
        "data.csv",
        "payload.json",
        "a.b.c.txt",
    ],
)
def test_allowed_filenames(name: str) -> None:
    assert validate_filename(name).display == name


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "..\\escape.txt",
        "dir/../../etc/passwd.txt",
        "/absolute.txt",
        "\\\\server\\share.txt",
        "C:\\Windows\\system.txt",
    ],
)
def test_path_traversal_blocked(name: str) -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename(name)


@pytest.mark.parametrize(
    "name",
    ["run.exe", "script.ps1", "install.cmd", "go.sh", "app.js", "macro.vbs", "lib.dll"],
)
def test_executable_extensions_blocked(name: str) -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename(name)


@pytest.mark.parametrize(
    "name",
    ["CLAUDE.md", "claude.md", "AGENTS.md", "GEMINI.md", ".mcp.json", "settings.json"],
)
def test_agent_config_files_blocked(name: str) -> None:
    """작업 폴더에 들어가면 CLI 가 지시문으로 읽을 수 있는 파일."""
    with pytest.raises(UnsafeFilename):
        validate_filename(name)


@pytest.mark.parametrize("name", [".env", ".npmrc", ".hidden.txt"])
def test_dotfiles_blocked(name: str) -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename(name)


@pytest.mark.parametrize("name", ["CON.txt", "nul.txt", "COM1.md", "lpt9.txt"])
def test_windows_reserved_names_blocked(name: str) -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename(name)


def test_control_characters_blocked() -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename("bad\x00name.txt")


def test_no_extension_blocked() -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename("README")


def test_empty_blocked() -> None:
    with pytest.raises(UnsafeFilename):
        validate_filename("   ")


def test_executable_signature_detection() -> None:
    assert contains_executable_signature(b"MZ\x90\x00")
    assert contains_executable_signature(b"\x7fELF\x02")
    assert contains_executable_signature(b"PK\x03\x04zip")
    assert not contains_executable_signature(b"plain text")


def test_pdf_signature_detection() -> None:
    assert looks_like_pdf(b"%PDF-1.7 rest")
    assert not looks_like_pdf(b"not a pdf")
