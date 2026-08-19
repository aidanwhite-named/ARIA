"""Windows 실행 파일 해석.

shutil.which 결과만 믿지 않는다. npm 으로 설치된 CLI 는 Windows 에서
.cmd / .ps1 래퍼로 먼저 발견되는데, 래퍼를 subprocess 로 직접 호출하면
shell 이 끼어들고 인수 이스케이프 규칙이 달라진다. 가능하면 패키지 안의
네이티브 exe 를 직접 부른다.

확인되지 않은 임의의 cmd/bat/ps1 내용을 해석하거나 실행하지 않는다.
후보 경로는 여기 하드코딩된 공식 설치 구조에서만 만든다.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ExecutableKind:
    NATIVE_EXE = "native_exe"
    NODE_ENTRY = "node_entry"
    CMD_WRAPPER = "cmd_wrapper"
    POSIX_BIN = "posix_bin"


@dataclass
class ResolvedExecutable:
    path: str
    kind: str
    argv_prefix: list[str] = field(default_factory=list)
    source: str = ""

    def command(self, args: list[str]) -> list[str]:
        return [*self.argv_prefix, self.path, *args] if self.argv_prefix else [self.path, *args]


def _npm_roots() -> list[Path]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")
    for candidate in (
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules",
        Path("/usr/local/lib/node_modules"),
        Path("/usr/lib/node_modules"),
        Path.home() / ".npm-global" / "lib" / "node_modules",
    ):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _from_path_env(command: str) -> ResolvedExecutable | None:
    found = shutil.which(command)
    if not found:
        return None
    path = Path(found)
    suffix = path.suffix.lower()
    if sys.platform != "win32":
        return ResolvedExecutable(str(path), ExecutableKind.POSIX_BIN, source="PATH")
    if suffix == ".exe":
        return ResolvedExecutable(str(path), ExecutableKind.NATIVE_EXE, source="PATH")
    # .cmd / .ps1 래퍼는 네이티브 exe 를 못 찾았을 때만 쓴다.
    return None


def _cmd_wrapper_from_path(command: str) -> ResolvedExecutable | None:
    if sys.platform != "win32":
        return None
    for ext in (".cmd", ".bat"):
        found = shutil.which(command + ext) or shutil.which(command)
        if found and Path(found).suffix.lower() in (".cmd", ".bat"):
            return ResolvedExecutable(
                str(Path(found)), ExecutableKind.CMD_WRAPPER, source="PATH wrapper"
            )
    return None


def resolve_claude(override: str | None = None) -> ResolvedExecutable | None:
    """Claude Code CLI 실행 파일 해석.

    우선순위: 사용자 지정 → PATH 네이티브 exe → npm 패키지 내부 exe →
    node entrypoint → cmd 래퍼.
    """
    if override:
        path = Path(override)
        if _is_executable_file(path):
            kind = (
                ExecutableKind.NATIVE_EXE
                if path.suffix.lower() == ".exe"
                else ExecutableKind.CMD_WRAPPER
                if path.suffix.lower() in (".cmd", ".bat")
                else ExecutableKind.POSIX_BIN
            )
            return ResolvedExecutable(str(path), kind, source="사용자 지정")
        return None

    direct = _from_path_env("claude")
    if direct:
        return direct

    exe_name = "claude.exe" if sys.platform == "win32" else "claude"
    for root in _npm_roots():
        native = root / "@anthropic-ai" / "claude-code" / "bin" / exe_name
        if _is_executable_file(native):
            kind = (
                ExecutableKind.NATIVE_EXE
                if sys.platform == "win32"
                else ExecutableKind.POSIX_BIN
            )
            return ResolvedExecutable(str(native), kind, source="npm 패키지 내부")

    # 네이티브 바이너리 설치(~/.local/bin, ~/.claude/local)
    for candidate in (
        Path.home() / ".local" / "bin" / exe_name,
        Path.home() / ".claude" / "local" / exe_name,
    ):
        if _is_executable_file(candidate):
            kind = (
                ExecutableKind.NATIVE_EXE
                if sys.platform == "win32"
                else ExecutableKind.POSIX_BIN
            )
            return ResolvedExecutable(str(candidate), kind, source="네이티브 설치")

    node = shutil.which("node")
    if node:
        for root in _npm_roots():
            entry = root / "@anthropic-ai" / "claude-code" / "cli-wrapper.cjs"
            if _is_executable_file(entry):
                return ResolvedExecutable(
                    str(entry),
                    ExecutableKind.NODE_ENTRY,
                    argv_prefix=[node],
                    source="node entrypoint",
                )

    return _cmd_wrapper_from_path("claude")


def resolve_simple(command: str, override: str | None = None) -> ResolvedExecutable | None:
    """아직 설치가 확인되지 않은 CLI(codex, gemini)용 일반 해석기."""
    if override:
        path = Path(override)
        if _is_executable_file(path):
            kind = (
                ExecutableKind.NATIVE_EXE
                if path.suffix.lower() == ".exe"
                else ExecutableKind.POSIX_BIN
            )
            return ResolvedExecutable(str(path), kind, source="사용자 지정")
        return None
    return _from_path_env(command) or _cmd_wrapper_from_path(command)
