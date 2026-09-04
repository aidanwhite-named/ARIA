"""File-backed analysis and search prompt storage.

The files in ``prompt/`` are the only current prompt source. SQLite keeps job
snapshots and settings, but prompt bodies are never read from or written to the
prompt template tables.

프롬프트 종류(kind)
-------------------
두 작업의 프롬프트는 같은 저장 방식을 쓰지만 **서로 다른 계약**이다.

    analysis  구성대비 분석의 분석 기준. 첨부 자료를 읽고 보고서를 쓴다.
    search    유사문헌 검색의 **검색 전략**. 무엇을 중시해 어떻게 넓힐지만 적는다.
              검색 실행·보안·감사 계약은 프로그램이 갖고 있다(search_contract).

종류는 파일 메타데이터의 ``kind`` 가 정한다. 옛 파일에는 그 칸이 없으므로
예약 id 와 ``similarity_search_v1`` 선언으로 되돌려 읽는다 — 그래야 이미
디스크에 있는 검색 프롬프트가 분석 목록에 섞여 들어가지 않는다.

프롬프트는 종류별로 여러 개를 만들고 실행마다 고를 수 있다. 함께 배포되는
분석·검색 기본 프롬프트는 어느 한쪽의 실행 기반이 사라지지 않도록 삭제할 수 없다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .config import PROMPT_DIR

_ALLOWED_SUFFIXES = frozenset({".md", ".txt"})

KIND_ANALYSIS = "analysis"
KIND_SEARCH = "search"
PROMPT_KINDS = (KIND_ANALYSIS, KIND_SEARCH)

# 옛 파일이 종류를 선언하는 대신 쓰던 값. kind 가 없는 파일에서만 본다.
_SEARCH_CAPABILITY = "similarity_search_v1"

# 배포본으로 함께 나가는 프롬프트. 지울 수 없다는 뜻이며, 각 종류의 프롬프트가
# 이것 하나뿐이라는 뜻은 아니다. 두 작업 모두 설치 직후 실행 가능한 기본값을
# 가져야 하므로 분석과 검색 배포본을 함께 보호한다.
#
# 분석 실행의 선택 목록에서 빼는 근거는 이제 예약 여부가 아니라 kind 다. 검색
# 전략 프롬프트를 사용자가 여러 개 만들 수 있게 되면서, "예약된 하나만 검색용"
# 이라는 전제가 더 이상 성립하지 않기 때문이다. 편집은 허용하되(본문이
# 검색 전략이므로 사용자의 것이다) 삭제는 막는다 — 지우면 기본값이 사라진다.
DEFAULT_ANALYSIS_PROMPT_ID = "patent-analysis-master-prompt.md"
DEFAULT_SEARCH_PROMPT_ID = "search_prompt.md"
RESERVED_PROMPT_IDS = frozenset(
    {DEFAULT_ANALYSIS_PROMPT_ID, DEFAULT_SEARCH_PROMPT_ID}
)

_METADATA_START = "<!-- ARIA_PROMPT_METADATA\n"
_METADATA_END = "\n-->\n"
_MAX_PROMPT_BYTES = 2 * 1024 * 1024
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PromptStoreError(Exception):
    """Base class for errors safe to expose through the local API."""


class PromptNotFound(PromptStoreError):
    pass


class InvalidPromptFile(PromptStoreError):
    pass


@dataclass(frozen=True)
class PromptFile:
    id: str
    name: str
    description: str
    body: str
    accepted_file_types: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime
    # 이 프롬프트가 지원한다고 선언한 ARIA 확장. 파일 메타데이터에서만 설정한다.
    # API 로는 바꿀 수 없다. 프롬프트 본문과 출력 계약이 함께 움직여야 하는데,
    # 화면에서 선언만 켜면 본문은 그대로라 계약이 어긋난다.
    capabilities: list[str] = field(default_factory=list)
    # 이 프롬프트가 어느 작업의 것인가. 파일 메타데이터가 정하며, 옛 파일은
    # resolve_kind 가 되돌려 읽는다.
    kind: str = KIND_ANALYSIS


def resolve_kind(
    declared: Any, *, prompt_id: str = "", capabilities: list[str] | None = None
) -> str:
    """이 파일이 어느 작업의 프롬프트인가.

    선언이 있으면 그대로 쓴다. 없으면(옛 파일) 예약 id 와
    ``similarity_search_v1`` 선언으로 되돌려 읽는다. 되돌려 읽지 못하면
    분석이다 — 모르는 파일을 검색 전략으로 승격하면 검색 화면의 선택지에
    분석 프롬프트가 섞인다.
    """
    value = str(declared or "").strip().lower()
    if value in PROMPT_KINDS:
        return value
    if prompt_id == DEFAULT_SEARCH_PROMPT_ID:
        return KIND_SEARCH
    if _SEARCH_CAPABILITY in (capabilities or []):
        return KIND_SEARCH
    return KIND_ANALYSIS


def _as_utc_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _parse_datetime(value: Any, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def _safe_stem(name: str) -> str:
    stem = _INVALID_FILENAME.sub("-", name).strip(" .")
    stem = re.sub(r"\s+", " ", stem)[:120].rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED:
        stem = "prompt"
    return stem


class PromptStore:
    def __init__(self, root: Path = PROMPT_DIR) -> None:
        self.root = Path(root).resolve()
        self._lock = RLock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for_id(self, prompt_id: str, *, allow_reserved: bool = False) -> Path:
        if not prompt_id or Path(prompt_id).name != prompt_id:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        if Path(prompt_id).suffix.lower() not in _ALLOWED_SUFFIXES:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        if not allow_reserved and prompt_id in RESERVED_PROMPT_IDS:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        candidate = self.root / prompt_id
        resolved = candidate.resolve(strict=False)
        if resolved.parent != self.root:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        return candidate

    def _split_document(self, raw: str, path: Path) -> tuple[dict[str, Any], str]:
        if not raw.startswith(_METADATA_START):
            return {}, raw
        end = raw.find(_METADATA_END, len(_METADATA_START))
        if end < 0:
            raise InvalidPromptFile(f"{path.name}: ARIA 메타데이터 종료 표식이 없습니다.")
        payload = raw[len(_METADATA_START) : end]
        try:
            metadata = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InvalidPromptFile(
                f"{path.name}: ARIA 메타데이터 JSON이 올바르지 않습니다."
            ) from exc
        if not isinstance(metadata, dict):
            raise InvalidPromptFile(f"{path.name}: ARIA 메타데이터는 객체여야 합니다.")
        return metadata, raw[end + len(_METADATA_END) :]

    def _read_path(self, path: Path) -> PromptFile:
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.") from exc
        if not path.is_file() or path.is_symlink():
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        if stat.st_size > _MAX_PROMPT_BYTES:
            raise InvalidPromptFile(
                f"{path.name}: 프롬프트 파일은 {_MAX_PROMPT_BYTES // 1024 // 1024}MB 이하여야 합니다."
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidPromptFile(f"{path.name}: UTF-8 텍스트 파일이 아닙니다.") from exc
        metadata, body = self._split_document(raw, path)
        if not body.strip():
            raise InvalidPromptFile(f"{path.name}: 프롬프트 본문이 비어 있습니다.")

        created_fallback = _as_utc_timestamp(stat.st_ctime)
        updated_fallback = _as_utc_timestamp(stat.st_mtime)
        fallback_name = path.stem.replace("-", " ").strip() or path.stem
        name = str(metadata.get("name") or _title_from_body(body, fallback_name)).strip()
        if not name:
            raise InvalidPromptFile(f"{path.name}: 프롬프트 이름이 비어 있습니다.")
        declared_kind = str(metadata.get("kind") or "").strip().lower()
        if declared_kind and declared_kind not in PROMPT_KINDS:
            raise InvalidPromptFile(
                f"{path.name}: kind 는 {list(PROMPT_KINDS)} 중 하나여야 합니다."
            )
        capabilities = _string_list(metadata.get("capabilities"))
        return PromptFile(
            id=path.name,
            name=name,
            description=str(metadata.get("description") or "").strip(),
            body=body,
            accepted_file_types=_string_list(metadata.get("accepted_file_types")),
            capabilities=capabilities,
            kind=resolve_kind(
                declared_kind, prompt_id=path.name, capabilities=capabilities
            ),
            enabled=bool(metadata.get("enabled", True)),
            created_at=_parse_datetime(metadata.get("created_at"), created_fallback),
            updated_at=_parse_datetime(metadata.get("updated_at"), updated_fallback),
        )

    def _serialize(self, prompt: PromptFile) -> str:
        metadata = {
            "name": prompt.name,
            "description": prompt.description,
            "accepted_file_types": prompt.accepted_file_types,
            "capabilities": prompt.capabilities,
            "kind": prompt.kind,
            "enabled": prompt.enabled,
            "version": 1,
            "created_at": prompt.created_at.isoformat(),
            "updated_at": prompt.updated_at.isoformat(),
        }
        return (
            _METADATA_START
            + json.dumps(metadata, ensure_ascii=False, indent=2)
            + _METADATA_END
            + prompt.body
        )

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".aria-prompt-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def list(
        self,
        search: str = "",
        *,
        kind: str = "",
        include_reserved: bool = False,
    ) -> list[PromptFile]:
        """프롬프트 목록.

        ``kind`` 를 주면 그 종류만 돌려준다. 분석 실행의 선택 목록은 반드시
        ``kind=KIND_ANALYSIS`` 로 부른다 — 검색 전략 프롬프트가 섞이면 그 본문이
        구성대비 분석의 분석 기준으로 선택될 수 있고, 그 본문은 분석 계약을
        만족하지 않는다.

        ``include_reserved`` 는 배포본을 목록에 넣을지만 정한다. 종류를 거르는
        일과 다른 축이다 — 사용자가 만든 검색 전략 프롬프트는 예약된 적이 없다.
        """
        self.ensure()
        wanted = str(kind or "").strip().lower()
        if wanted and wanted not in PROMPT_KINDS:
            raise PromptNotFound("알 수 없는 프롬프트 종류입니다.")
        lowered = search.casefold().strip()
        rows: list[PromptFile] = []
        for path in self.root.iterdir():
            if (
                path.name.startswith(".")
                or (not include_reserved and path.name in RESERVED_PROMPT_IDS)
                or path.suffix.lower() not in _ALLOWED_SUFFIXES
                or not path.is_file()
                or path.is_symlink()
            ):
                continue
            prompt = self._read_path(path)
            if wanted and prompt.kind != wanted:
                continue
            if lowered and lowered not in "\n".join(
                (prompt.name, prompt.description, prompt.body)
            ).casefold():
                continue
            rows.append(prompt)
        rows.sort(key=lambda item: (item.updated_at, item.name.casefold()), reverse=True)
        return rows

    def get(self, prompt_id: str) -> PromptFile:
        self.ensure()
        return self._read_path(self._path_for_id(prompt_id))

    def get_for_kind(self, prompt_id: str, kind: str) -> PromptFile:
        """그 종류의 프롬프트 하나를 읽는다.

        검색 전략 프롬프트는 배포본(예약 id)도 고를 수 있어야 하므로 예약
        차단을 종류로 푼다. 종류가 다르면 찾지 못한 것으로 다룬다 — 분석
        프롬프트를 검색 실행에 넣거나 그 반대로 넣는 경로를 만들지 않는다.
        """
        self.ensure()
        wanted = str(kind or "").strip().lower()
        if wanted not in PROMPT_KINDS:
            raise PromptNotFound("알 수 없는 프롬프트 종류입니다.")
        # 분석·검색 배포본 모두 실행 선택지다. 예약 여부는 삭제 보호일 뿐,
        # 실행 가능 여부를 뜻하지 않으므로 종류 검증을 거쳐 읽게 한다.
        prompt = self._read_path(self._path_for_id(prompt_id, allow_reserved=True))
        if prompt.kind != wanted:
            raise PromptNotFound(
                f"{prompt_id} 는 {wanted} 작업의 프롬프트가 아닙니다."
            )
        return prompt

    def get_reserved(self, prompt_id: str) -> PromptFile:
        """예약 프롬프트를 읽는다. 목록/편집 API 는 이 경로를 쓰지 않는다.

        일반 프롬프트와 같은 파일 형식·같은 검증(UTF-8, 메타데이터, 빈 본문)을
        거친다. 다른 로더를 따로 만들면 두 경로의 검증이 갈라진다.
        """
        if prompt_id not in RESERVED_PROMPT_IDS:
            raise PromptNotFound("예약된 프롬프트가 아닙니다.")
        self.ensure()
        return self._read_path(self._path_for_id(prompt_id, allow_reserved=True))

    def create(
        self,
        *,
        name: str,
        description: str,
        body: str,
        accepted_file_types: list[str],
        kind: str = KIND_ANALYSIS,
    ) -> PromptFile:
        self.ensure()
        now = datetime.now(timezone.utc)
        suffix = ".md"
        base = _safe_stem(name)
        with self._lock:
            candidate = self.root / f"{base}{suffix}"
            counter = 2
            # 예약 이름과 겹치면 비켜난다. 사용자가 만든 프롬프트가 검색
            # 프롬프트 파일을 덮어쓰면 검색 실행 계약이 통째로 바뀐다.
            while candidate.exists() or candidate.name in RESERVED_PROMPT_IDS:
                candidate = self.root / f"{base}-{counter}{suffix}"
                counter += 1
            resolved_kind = str(kind or KIND_ANALYSIS).strip().lower()
            if resolved_kind not in PROMPT_KINDS:
                raise InvalidPromptFile(
                    f"kind 는 {list(PROMPT_KINDS)} 중 하나여야 합니다."
                )
            prompt = PromptFile(
                id=candidate.name,
                name=name.strip(),
                description=description.strip(),
                body=body,
                accepted_file_types=list(accepted_file_types),
                kind=resolved_kind,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            self._atomic_write(candidate, self._serialize(prompt))
            return prompt

    def _update(
        self, prompt_id: str, changes: dict[str, Any], *, allow_reserved: bool
    ) -> PromptFile:
        with self._lock:
            current = (
                self.get_reserved(prompt_id) if allow_reserved else self.get(prompt_id)
            )
            values = {
                "name": current.name,
                "description": current.description,
                "body": current.body,
                "accepted_file_types": current.accepted_file_types,
                "enabled": current.enabled,
            }
            # 알 수 없는 키는 버린다. kind 와 capabilities 는 API 로 바꿀 수
            # 없다 — 종류·선언과 본문 계약이 함께 움직여야 하는데, 화면에서
            # 종류만 뒤집으면 본문은 그대로라 계약이 어긋난다.
            values.update({key: changes[key] for key in values if key in changes})
            if not any(values[key] != getattr(current, key) for key in values):
                return current

            updated = replace(
                current,
                name=str(values["name"]).strip(),
                description=str(values["description"] or "").strip(),
                body=str(values["body"]),
                accepted_file_types=list(values["accepted_file_types"] or []),
                enabled=bool(values["enabled"]),
                updated_at=datetime.now(timezone.utc),
            )
            self._atomic_write(
                self._path_for_id(prompt_id, allow_reserved=allow_reserved),
                self._serialize(updated),
            )
            return updated

    def update(self, prompt_id: str, changes: dict[str, Any]) -> PromptFile:
        return self._update(prompt_id, changes, allow_reserved=False)

    def update_reserved(self, prompt_id: str, changes: dict[str, Any]) -> PromptFile:
        if prompt_id not in RESERVED_PROMPT_IDS:
            raise PromptNotFound("예약된 프롬프트가 아닙니다.")
        return self._update(prompt_id, changes, allow_reserved=True)

    def delete(self, prompt_id: str) -> None:
        with self._lock:
            path = self._path_for_id(prompt_id)
            if not path.exists():
                raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
            path.unlink()


PROMPT_STORE = PromptStore()
