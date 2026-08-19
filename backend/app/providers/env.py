"""자식 CLI 프로세스에 넘길 환경변수 구성.

이건 이론적인 하드닝이 아니라 실측으로 확인된 버그 방지책이다.

ARIA 를 Claude Code 세션 안에서 실행하면(개발 중에는 거의 항상 그렇다)
부모 프로세스가 ANTHROPIC_BASE_URL, CLAUDECODE, CLAUDE_CODE_ENTRYPOINT,
CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH 같은 변수를 환경에 심어둔다. 이걸
그대로 상속하면 자식 claude.exe 가 호스트 전용 엔드포인트를 바라보면서
"Not logged in" 으로 실패한다. 원인을 찾기 매우 어려운 실패다.

그래서 상속 대신 allowlist 로 환경을 새로 만든다.

CLAUDE_CONFIG_DIR 은 일부러 설정하지 않는다. 실행별 전용 config 디렉터리를
주면 격리는 강해지지만 사용자가 `claude setup-token` 으로 저장한 기본
위치(~/.claude)의 자격증명을 못 찾게 된다. 인증을 지키려고 격리를 조금
양보하는 쪽을 택했다.
"""

from __future__ import annotations

import os
import sys

# 프로세스가 정상 동작하는 데 실제로 필요한 것만 통과시킨다.
_WINDOWS_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMPUTERNAME",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)

_POSIX_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)

# allowlist 를 통과하더라도 이 접두사는 무조건 제거한다. 방어적 이중 처리.
_BLOCKED_PREFIXES = (
    "ANTHROPIC",
    "CLAUDE",
    "CODEX",
    "GEMINI",
    "GOOGLE_API",
    "GOOGLE_CLOUD",
    "OPENAI",
    "VERTEX",
    "AWS_",
    "BEDROCK",
)


def _allowlist() -> frozenset[str]:
    return _WINDOWS_ALLOWLIST if sys.platform == "win32" else _POSIX_ALLOWLIST


def is_blocked(name: str) -> bool:
    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def build_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """CLI 자식 프로세스용 환경변수를 새로 만든다.

    extra 는 allowlist 적용 후에 얹으므로, ARIA 가 의도적으로 지정하는
    CLAUDE_CODE_DISABLE_AUTO_MEMORY 같은 값은 살아남는다.
    """
    allowed = _allowlist()
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if is_blocked(key):
            continue
        if key.upper() in allowed:
            env[key] = value

    # 한글 경로/출력이 cp949 로 깨지는 것을 막는다.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    if extra:
        env.update(extra)
    return env


def describe_filtering() -> dict[str, object]:
    """Settings 화면에서 무엇이 제거됐는지 보여주기 위한 요약."""
    removed = sorted(
        key
        for key in os.environ
        if is_blocked(key) or key.upper() not in _allowlist()
    )
    return {
        "allowlist": sorted(_allowlist()),
        "blocked_prefixes": list(_BLOCKED_PREFIXES),
        "removed_count": len(removed),
        "removed_sample": removed[:40],
    }
