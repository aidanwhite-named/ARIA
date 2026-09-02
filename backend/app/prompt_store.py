"""File-backed analysis and search prompt storage.

The files in ``prompt/`` are the only current prompt source. SQLite keeps job
snapshots and settings, but prompt bodies are never read from or written to the
prompt template tables.
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

# 작업 종류가 고정된 예약 프롬프트. 같은 파일 형식·같은 로더를 쓰지만 분석
# 실행의 프롬프트 선택 목록에는 넣지 않는다.
#
# 목록에 두면 PDF 구성대비 분석의 분석 기준으로 고를 수 있게 되는데, 그 본문은
# 웹 검색 실행 계약이라 첨부 분석에 쓰면 계약이 어긋난다. create_job 의
# "첫 번째 활성 프롬프트" 폴백에 걸릴 위험도 있다. API 로 편집·삭제되는 것도
# 막는다 — 실행 계약과 본문이 함께 움직여야 한다.
RESERVED_PROMPT_IDS = frozenset({"search_prompt.md"})
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
    output_mode: str
    tags: list[str]
    accepted_file_types: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime
    # 이 프롬프트가 지원한다고 선언한 ARIA 확장. 파일 메타데이터에서만 설정한다.
    # API 로는 바꿀 수 없다. 프롬프트 본문과 출력 계약이 함께 움직여야 하는데,
    # 화면에서 선언만 켜면 본문은 그대로라 계약이 어긋난다.
    capabilities: list[str] = field(default_factory=list)


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
        output_mode = str(metadata.get("output_mode") or "markdown").strip().lower()
        if output_mode not in {"markdown", "text"}:
            raise InvalidPromptFile(
                f"{path.name}: output_mode는 markdown 또는 text여야 합니다."
            )
        fallback_name = path.stem.replace("-", " ").strip() or path.stem
        name = str(metadata.get("name") or _title_from_body(body, fallback_name)).strip()
        if not name:
            raise InvalidPromptFile(f"{path.name}: 프롬프트 이름이 비어 있습니다.")
        return PromptFile(
            id=path.name,
            name=name,
            description=str(metadata.get("description") or "").strip(),
            body=body,
            output_mode=output_mode,
            tags=_string_list(metadata.get("tags")),
            accepted_file_types=_string_list(metadata.get("accepted_file_types")),
            capabilities=_string_list(metadata.get("capabilities")),
            enabled=bool(metadata.get("enabled", True)),
            created_at=_parse_datetime(metadata.get("created_at"), created_fallback),
            updated_at=_parse_datetime(metadata.get("updated_at"), updated_fallback),
        )

    def _serialize(self, prompt: PromptFile) -> str:
        metadata = {
            "name": prompt.name,
            "description": prompt.description,
            "output_mode": prompt.output_mode,
            "tags": prompt.tags,
            "accepted_file_types": prompt.accepted_file_types,
            "capabilities": prompt.capabilities,
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
        self, search: str = "", tag: str = "", *, include_reserved: bool = False
    ) -> list[PromptFile]:
        self.ensure()
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
            if lowered and lowered not in "\n".join(
                (prompt.name, prompt.description, prompt.body)
            ).casefold():
                continue
            if tag and tag not in prompt.tags:
                continue
            rows.append(prompt)
        rows.sort(key=lambda item: (item.updated_at, item.name.casefold()), reverse=True)
        return rows

    def get(self, prompt_id: str) -> PromptFile:
        self.ensure()
        return self._read_path(self._path_for_id(prompt_id))

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
        output_mode: str,
        tags: list[str],
        accepted_file_types: list[str],
    ) -> PromptFile:
        self.ensure()
        now = datetime.now(timezone.utc)
        suffix = ".md" if output_mode == "markdown" else ".txt"
        base = _safe_stem(name)
        with self._lock:
            candidate = self.root / f"{base}{suffix}"
            counter = 2
            # 예약 이름과 겹치면 비켜난다. 사용자가 만든 프롬프트가 검색
            # 프롬프트 파일을 덮어쓰면 검색 실행 계약이 통째로 바뀐다.
            while candidate.exists() or candidate.name in RESERVED_PROMPT_IDS:
                candidate = self.root / f"{base}-{counter}{suffix}"
                counter += 1
            prompt = PromptFile(
                id=candidate.name,
                name=name.strip(),
                description=description.strip(),
                body=body,
                output_mode=output_mode,
                tags=list(tags),
                accepted_file_types=list(accepted_file_types),
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
                "output_mode": current.output_mode,
                "tags": current.tags,
                "accepted_file_types": current.accepted_file_types,
                "enabled": current.enabled,
            }
            values.update(changes)
            if not any(values[key] != getattr(current, key) for key in values):
                return current

            updated = replace(
                current,
                name=str(values["name"]).strip(),
                description=str(values["description"] or "").strip(),
                body=str(values["body"]),
                output_mode=str(values["output_mode"]),
                tags=list(values["tags"] or []),
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
