"""agy 전역 설정의 페이지 열람 허용 목록(permissions.allow).

왜 ARIA 가 남의 도구 설정 파일을 만지는가
------------------------------------------
agy 는 headless 실행에서 승인 창을 띄울 수 없다. 그래서 허용 목록에 없는
호스트로 ``read_url_content`` 를 부르면 자동 거부되는데, **거부된 호출 하나만
실패하는 것이 아니라 그 턴 전체가 취소된다.** 2026-09-02 실행에서 실측했다 —
성공한 ``search_web`` 5건을 마친 뒤 arxiv.org 를 열려다 거부됐고, agy 는
종료 코드 0 · ``status: CANCELED`` · 빈 응답으로 끝냈다. 이미 끝난 검색 결과도
감사 블록도 함께 사라졌다.

즉 이 파일 한 줄이 유사문헌 검색 채널 전체의 성패를 가른다. 그래서 ARIA 가
논문 출처로 자주 필요한 호스트만 권장 목록으로 병합한다.

지키는 선
---------
- **기존 항목을 덮어쓰지 않는다.** 병합만 한다. 사용자가 직접 넣은 규칙은
  ARIA 가 모르는 이유로 거기 있는 것이다.
- ``read_url(*)`` 를 쓰지 않는다. ``--dangerously-skip-permissions`` 도 쓰지
  않는다. 범위를 넓히는 것은 문제를 없애는 것이 아니라 감사할 수 없게 만드는
  것이다.
- **JSON 이 깨져 있으면 손대지 않고 오류를 낸다.** 새 파일로 덮어쓰면 사용자가
  거기 넣어 둔 다른 설정(trustedWorkspaces 등)이 조용히 사라진다.
- 쓰기 전에 백업을 만들고, 원자적으로 바꾼다.
- 바뀔 것이 없으면 아무것도 쓰지 않는다. 설정 화면을 열 때마다 백업 파일이
  쌓이면 그건 백업이 아니라 쓰레기다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ARIA 가 권장하는 논문 출처. 특허 쪽은 patents.google.com 이 이미 관례적으로
# 들어가 있고, EPO 는 웹 페이지가 아니라 OPS API 로 가므로 여기 없다.
#
# 호스트는 **정확히** 적는다. agy 의 규칙은 호스트 문자열 일치라 www 유무가
# 다르면 다른 호스트다. 검색 결과가 실제로 내놓는 표기를 그대로 쓴다.
#
# 버전별로 나눠 적는 이유
# -----------------------
# 자동 적용은 "저장된 버전 **이후에 새로 도입된** 호스트"만 넣는다. 전체 목록을
# 다시 병합하면, 사용자가 v1 에서 지운 호스트가 v2 를 올리는 순간 되살아난다.
# 지운 것은 그러기로 한 선택이고, 권장 목록에 새 줄이 생겼다는 것이 그 선택을
# 뒤집을 이유가 되지는 않는다.
#
# 그래서 새 호스트를 추가할 때 **기존 항목에 끼워 넣지 말고** 새 버전 줄을
# 만든다. 순서가 곧 시간 축이다.
#
#     ("2", ("www.biorxiv.org",)),   ← v2 에서 새로 도입한 것만
#
# 전체 목록을 다시 넣는 유일한 경로는 사용자가 설정 화면의 버튼을 눌렀을 때다.
RECOMMENDED_HOST_VERSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "1",
        (
            "arxiv.org",
            "www.mdpi.com",
            "ieeexplore.ieee.org",
            "dl.acm.org",
            "www.researchgate.net",
            "www.semanticscholar.org",
        ),
    ),
)

#: 지금 권장하는 호스트 전부. 버전 줄을 도입 순서대로 편 것이다.
RECOMMENDED_HOSTS: tuple[str, ...] = tuple(
    host for _, hosts in RECOMMENDED_HOST_VERSIONS for host in hosts
)

# 규칙 문법: read_url(<host>).
_RULE = re.compile(r"^\s*read_url\s*\(\s*([^)\s]+)\s*\)\s*$", re.IGNORECASE)

# 절대 만들지 않는 값. 사용자가 직접 넣었으면 지우지 않지만 ARIA 는 쓰지 않는다.
WILDCARD = "*"

#: 지금 코드가 아는 마지막 버전. 저장된 표시가 이 값이면 자동 적용은 끝났다.
MIGRATION_VERSION = RECOMMENDED_HOST_VERSIONS[-1][0]


def hosts_since(stored_version: str) -> tuple[str, ...] | None:
    """저장된 버전 **이후** 버전들이 새로 도입한 호스트.

    - 빈 문자열: 아직 한 번도 적용하지 않은 설치다. 전부 돌려준다.
    - 아는 버전: 그 다음 줄부터 끝까지 이어 붙여 돌려준다. 이미 지나간 버전의
      호스트는 들어가지 않는다 — 사용자가 지웠다면 지운 채로 둔다.
    - 그 밖의 값: **None.** 코드보다 새 버전(다운그레이드)이거나 손으로 고친
      값이다. 무엇이 적용됐는지 알 수 없으므로 아무것도 넣지 않는다. 전부 넣는
      쪽으로 기울면 바로 그 "지운 호스트가 되살아난다"가 일어난다.
    """
    stored = str(stored_version or "")
    if not stored:
        return RECOMMENDED_HOSTS
    known = [version for version, _ in RECOMMENDED_HOST_VERSIONS]
    if stored not in known:
        return None
    start = known.index(stored) + 1
    return tuple(
        host for _, hosts in RECOMMENDED_HOST_VERSIONS[start:] for host in hosts
    )


_ENV_OVERRIDE = "ARIA_AGY_SETTINGS_PATH"


class AgyPermissionsError(Exception):
    """설정 파일을 안전하게 읽거나 쓸 수 없다. 이때는 손대지 않는다."""


def settings_path() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def rule_for(host: str) -> str:
    return f"read_url({host})"


def _host_of(rule: object) -> str:
    match = _RULE.match(str(rule or ""))
    return match.group(1).lower() if match else ""


@dataclass(frozen=True)
class AgyPermissionState:
    """지금 이 기계의 허용 목록 상태. 설정 화면과 프롬프트가 같은 값을 본다."""

    path: str
    exists: bool
    #: read_url 규칙에서 뽑은 호스트 전부. 사용자가 직접 넣은 것을 포함한다.
    allowed_hosts: tuple[str, ...] = ()
    #: 권장 목록 중 실제로 들어가 있는 것.
    applied: tuple[str, ...] = ()
    #: 권장 목록 중 아직 없는 것.
    missing: tuple[str, ...] = ()
    #: read_url(*) 가 이미 들어 있는가. ARIA 가 넣지는 않지만 있으면 알린다.
    wildcard: bool = False
    #: 읽지 못한 이유. 비어 있지 않으면 다른 칸은 신뢰할 수 없다.
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "exists": self.exists,
            "allowed_hosts": list(self.allowed_hosts),
            "recommended": list(RECOMMENDED_HOSTS),
            "applied": list(self.applied),
            "missing": list(self.missing),
            "wildcard": self.wildcard,
            "error": self.error,
        }


def _state_from(path: Path, document: dict) -> AgyPermissionState:
    permissions = document.get("permissions")
    raw_rules = permissions.get("allow") if isinstance(permissions, dict) else None
    rules = raw_rules if isinstance(raw_rules, list) else []
    hosts: list[str] = []
    for rule in rules:
        host = _host_of(rule)
        if host and host not in hosts:
            hosts.append(host)
    known = set(hosts)
    return AgyPermissionState(
        path=str(path),
        exists=True,
        allowed_hosts=tuple(hosts),
        applied=tuple(host for host in RECOMMENDED_HOSTS if host in known),
        missing=tuple(host for host in RECOMMENDED_HOSTS if host not in known),
        wildcard=WILDCARD in known,
    )


def _load(path: Path) -> dict:
    """설정 파일을 읽는다. 깨져 있으면 올린다 — 덮어쓰지 않기 위해서다."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgyPermissionsError(
            f"agy 설정 파일을 읽지 못했습니다: {path} ({exc.strerror or exc})"
        ) from exc
    if not raw.strip():
        raise AgyPermissionsError(
            f"agy 설정 파일이 비어 있습니다: {path}. ARIA 가 임의로 새로 만들지 "
            "않았습니다. 파일을 확인한 뒤 다시 적용하십시오."
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgyPermissionsError(
            f"agy 설정 파일이 올바른 JSON 이 아닙니다: {path} "
            f"({exc.lineno}행 {exc.colno}열: {exc.msg}). 손상된 파일을 덮어쓰지 "
            "않았습니다. 직접 고친 뒤 다시 적용하십시오."
        ) from exc
    if not isinstance(document, dict):
        raise AgyPermissionsError(
            f"agy 설정 파일의 최상위가 객체가 아닙니다: {path}. 덮어쓰지 않았습니다."
        )
    return document


def read_state() -> AgyPermissionState:
    """지금 상태를 읽는다. 아무것도 쓰지 않는다.

    예외를 올리지 않는다 — 이 함수는 설정 화면과 실행 경로가 부르고, 둘 다
    "허용 목록을 못 읽었다"가 실행을 멈출 이유는 아니기 때문이다. 실패는
    error 칸에 담아 그대로 보여 준다.
    """
    path = settings_path()
    if not path.exists():
        return AgyPermissionState(
            path=str(path), exists=False, missing=RECOMMENDED_HOSTS
        )
    try:
        return _state_from(path, _load(path))
    except AgyPermissionsError as exc:
        return AgyPermissionState(path=str(path), exists=True, error=str(exc))


def allowed_hosts() -> tuple[str, ...]:
    """이 실행에서 실제로 열 수 있는 호스트. 읽지 못하면 빈 튜플.

    빈 튜플은 "제한 없음"이 아니라 "하나도 열 수 없다"로 읽어야 한다. 프롬프트도
    그렇게 말한다 — 모르는 상태에서 열어 보게 하면 그 한 번의 거부로 실행 전체가
    사라진다.
    """
    return read_state().allowed_hosts


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.aria-backup-{stamp}")
    counter = 1
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}.aria-backup-{stamp}-{counter}")
    target.write_bytes(path.read_bytes())
    return target


def _atomic_write(path: Path, text: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def apply_recommended(
    *, hosts: tuple[str, ...] | list[str] | None = None, create: bool = False
) -> tuple[AgyPermissionState, list[str]]:
    """넘긴 호스트를 병합한다. (상태, 새로 추가한 호스트) 를 돌려준다.

    멱등하다. 이미 다 들어 있으면 읽기만 하고 쓰지 않는다 — 따라서 백업도 만들지
    않는다.

    hosts 는 이번에 넣을 호스트다. 생략하면 권장 목록 전체이며, 그 경로는
    사용자가 설정 화면의 버튼을 눌렀을 때뿐이다. 자동 마이그레이션은 저장된
    버전 이후의 delta 만 넘긴다(hosts_since) — 전체를 넘기면 사용자가 예전
    버전에서 지운 호스트가 새 버전을 올리는 순간 되살아난다.

    create 는 파일이 없을 때 만들 것인가다. agy 를 설치하지도 않은 기계에
    ``~/.gemini`` 를 만들지 않으려고 기본값은 거짓이고, 사용자가 명시적으로
    누른 경로에서만 참으로 부른다.
    """
    wanted = tuple(RECOMMENDED_HOSTS if hosts is None else hosts)
    path = settings_path()
    if not path.exists():
        if not create:
            return (
                AgyPermissionState(
                    path=str(path), exists=False, missing=RECOMMENDED_HOSTS
                ),
                [],
            )
        document: dict = {}
    else:
        document = _load(path)

    permissions = document.get("permissions")
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, dict):
        raise AgyPermissionsError(
            f"agy 설정의 permissions 가 객체가 아닙니다: {path}. 사용자가 넣은 "
            "값을 덮어쓰지 않았습니다."
        )
    raw_rules = permissions.get("allow")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise AgyPermissionsError(
            f"agy 설정의 permissions.allow 가 배열이 아닙니다: {path}. 사용자가 "
            "넣은 값을 덮어쓰지 않았습니다."
        )

    # 기존 항목은 순서까지 그대로 둔다. 새 규칙만 뒤에 붙인다.
    rules = list(raw_rules)
    known = {_host_of(rule) for rule in rules}
    added: list[str] = []
    for host in wanted:
        if host in known or host in added:
            continue
        added.append(host)
    if added:
        rules.extend(rule_for(host) for host in added)
        if path.exists():
            _backup(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
    permissions["allow"] = rules
    document["permissions"] = permissions
    if added:
        _atomic_write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    return _state_from(path, document), added
