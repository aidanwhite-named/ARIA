"""File-backed Master Prompt storage.

The files in ``prompt/`` are the only current prompt source. SQLite keeps job
snapshots and settings, but prompt bodies are never read from or written to the
prompt template tables.
"""

from __future__ import annotations

import hashlib
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
    version: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    # 이 프롬프트가 지원한다고 선언한 ARIA 확장. 파일 메타데이터에서만 설정한다.
    # API 로는 바꿀 수 없다. 프롬프트 본문과 출력 계약이 함께 움직여야 하는데,
    # 화면에서 선언만 켜면 본문은 그대로라 계약이 어긋난다.
    capabilities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptFileVersion:
    id: str
    version: int
    name: str
    description: str
    body: str
    output_mode: str
    created_at: datetime


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
        self.history_root = self.root / ".history"
        self._lock = RLock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_root.mkdir(parents=True, exist_ok=True)

    def _path_for_id(self, prompt_id: str) -> Path:
        if not prompt_id or Path(prompt_id).name != prompt_id:
            raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
        if Path(prompt_id).suffix.lower() not in _ALLOWED_SUFFIXES:
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
        try:
            version = max(1, int(metadata.get("version", 1)))
        except (TypeError, ValueError) as exc:
            raise InvalidPromptFile(f"{path.name}: version은 정수여야 합니다.") from exc
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
            version=version,
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
            "version": prompt.version,
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

    def _history_dir(self, prompt_id: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9._-]", "_", prompt_id)[:60].strip("._-")
        digest = hashlib.sha256(prompt_id.encode("utf-8")).hexdigest()[:12]
        return self.history_root / f"{readable or 'prompt'}-{digest}"

    def _snapshot_payload(self, prompt: PromptFile) -> dict[str, Any]:
        return {
            "id": f"{prompt.id}:v{prompt.version}",
            "version": prompt.version,
            "name": prompt.name,
            "description": prompt.description,
            "body": prompt.body,
            "output_mode": prompt.output_mode,
            "created_at": prompt.updated_at.isoformat(),
        }

    def _write_snapshot(self, prompt: PromptFile) -> None:
        target = self._history_dir(prompt.id) / f"v{prompt.version}.json"
        if not target.exists():
            self._atomic_write(
                target,
                json.dumps(self._snapshot_payload(prompt), ensure_ascii=False, indent=2)
                + "\n",
            )

    def list(self, search: str = "", tag: str = "") -> list[PromptFile]:
        self.ensure()
        lowered = search.casefold().strip()
        rows: list[PromptFile] = []
        for path in self.root.iterdir():
            if (
                path.name.startswith(".")
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
            while candidate.exists():
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
                version=1,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            self._atomic_write(candidate, self._serialize(prompt))
            self._write_snapshot(prompt)
            return prompt

    def update(self, prompt_id: str, changes: dict[str, Any]) -> PromptFile:
        with self._lock:
            current = self.get(prompt_id)
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
            versioned_fields = {"name", "description", "body", "output_mode"}
            version_changed = any(
                key in changes and changes[key] != getattr(current, key)
                for key in versioned_fields
            )
            if not any(values[key] != getattr(current, key) for key in values):
                return current

            self._write_snapshot(current)
            updated = replace(
                current,
                name=str(values["name"]).strip(),
                description=str(values["description"] or "").strip(),
                body=str(values["body"]),
                output_mode=str(values["output_mode"]),
                tags=list(values["tags"] or []),
                accepted_file_types=list(values["accepted_file_types"] or []),
                enabled=bool(values["enabled"]),
                version=current.version + 1 if version_changed else current.version,
                updated_at=datetime.now(timezone.utc),
            )
            self._atomic_write(self._path_for_id(prompt_id), self._serialize(updated))
            self._write_snapshot(updated)
            return updated

    def delete(self, prompt_id: str) -> None:
        with self._lock:
            path = self._path_for_id(prompt_id)
            if not path.exists():
                raise PromptNotFound("프롬프트를 찾을 수 없습니다.")
            path.unlink()
            history_dir = self._history_dir(prompt_id)
            if history_dir.exists() and history_dir.parent == self.history_root:
                for child in history_dir.iterdir():
                    if child.is_file() and child.parent == history_dir:
                        child.unlink()
                history_dir.rmdir()

    def versions(self, prompt_id: str) -> list[PromptFileVersion]:
        current = self.get(prompt_id)
        history_dir = self._history_dir(prompt_id)
        rows: dict[int, PromptFileVersion] = {}
        if history_dir.exists():
            for path in history_dir.glob("v*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    version = int(payload["version"])
                    rows[version] = PromptFileVersion(
                        id=str(payload.get("id") or f"{prompt_id}:v{version}"),
                        version=version,
                        name=str(payload["name"]),
                        description=str(payload.get("description") or ""),
                        body=str(payload["body"]),
                        output_mode=str(payload.get("output_mode") or "markdown"),
                        created_at=_parse_datetime(
                            payload.get("created_at"), current.updated_at
                        ),
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        rows[current.version] = PromptFileVersion(
            id=f"{current.id}:v{current.version}",
            version=current.version,
            name=current.name,
            description=current.description,
            body=current.body,
            output_mode=current.output_mode,
            created_at=current.updated_at,
        )
        return [rows[key] for key in sorted(rows, reverse=True)]


PROMPT_STORE = PromptStore()
