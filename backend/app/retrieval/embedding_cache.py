"""문헌 청크 임베딩 캐시.

없앨 비용이 무엇인지부터 적는다. 의미 검색 채널은 search action 한 번마다
`index.all_chunks()` 전체를 다시 임베딩했다. 기본 예산이 6라운드이므로 같은
문헌을 최대 6번, 문헌이 여러 건이면 그 배수만큼 다시 계산한다. 검색 결과는
매번 같다 — 문헌도 모델도 그 사이에 바뀌지 않기 때문이다.

캐시 키는 **결과를 바꿀 수 있는 값 전부**다.

    pdf_sha256 · INDEX_VERSION · EXTRACTOR_VERSION · 모델명 · revision

앞의 셋은 청크의 내용과 경계를 정하고, 뒤의 둘은 같은 청크에서 나오는 벡터를
정한다. 하나라도 다르면 다른 캐시다. 인덱스 재사용 조건(index._meta_matches)과
같은 세 값을 쓰는 것은 우연이 아니다 — 청크가 재사용될 수 있는 조건이 곧
그 청크의 임베딩이 재사용될 수 있는 조건이다.

거기에 청크 본문 해시를 한 겹 더 검증한다. 키가 같은데 본문이 다르면 그
행은 버리고 다시 계산한다. 키 설계가 틀렸을 때 조용히 엉뚱한 벡터로 검색하는
것보다, 한 번 더 계산하는 쪽이 언제나 옳다.

저장 위치는 실행 폴더가 아니라 데이터 디렉터리다. 실행 폴더에 두면 job 마다
새로 만들어져 "같은 문헌 재실행"에서 아무것도 아끼지 못한다.

캐시가 열리지 않아도 검색은 그대로 돈다. 캐시는 속도를 위한 것이지 정확성의
근거가 아니므로, 실패는 예외가 아니라 통계의 cache_error 로 남는다.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from pathlib import Path

from .versions import EXTRACTOR_VERSION, INDEX_VERSION

CACHE_DIRNAME = "semantic"
CACHE_FILENAME = "embeddings.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    fingerprint  TEXT NOT NULL,
    chunk_id     TEXT NOT NULL,
    text_sha256  TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    last_used_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (fingerprint, chunk_id)
);
"""

# 이 기능이 없던 시절의 캐시 파일에 붙이는 열. 이미 있으면 조용히 넘어간다.
# 옛 캐시를 버리지 않는다 — 버리면 다음 실행이 전부 다시 임베딩한다.
#
# **순서가 중요하다.** 인덱스를 먼저 만들면 그 열이 없는 옛 파일에서 실패하고,
# 실패한 연결은 캐시 전체를 비활성으로 만든다. 열을 붙인 뒤에 인덱스를 만든다.
_MIGRATIONS = (
    "ALTER TABLE embeddings ADD COLUMN last_used_at REAL NOT NULL DEFAULT 0",
)

_INDEX = "CREATE INDEX IF NOT EXISTS embeddings_last_used ON embeddings (last_used_at)"


def default_path() -> Path:
    """캐시 파일의 기본 위치. 실행 폴더가 아니라 데이터 디렉터리다."""
    from ..config import PATHS

    return PATHS.data_dir / CACHE_DIRNAME / CACHE_FILENAME


def fingerprint(index_meta: dict, model: str, revision: str) -> str:
    """이 (문헌 × 모델) 조합의 캐시 신원.

    index_meta 는 DocumentIndex.fingerprint() 의 결과다. 거기서 청크 내용을
    좌우하는 세 값만 쓴다 — 파일명이나 attachment_id 는 같은 PDF 를 다시
    업로드했을 때 달라지지만 청크는 같으므로 키에 넣지 않는다.
    """
    parts = [
        str(index_meta.get("pdf_sha256") or ""),
        str(index_meta.get("index_version") or INDEX_VERSION),
        str(index_meta.get("extractor_version") or EXTRACTOR_VERSION),
        model,
        revision,
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack(vector: list[float]) -> bytes:
    # 엔디안을 명시한다. 캐시 파일이 다른 기계로 옮겨 가도 같은 값으로 읽힌다.
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


class EmbeddingCache:
    """청크 임베딩 저장소. 열리지 않으면 아무것도 하지 않는 대역이 된다."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.error = ""
        self._connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), check_same_thread=False)
            connection.executescript(_SCHEMA)
            for statement in _MIGRATIONS:
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError:
                    # 이미 있는 열이다.
                    pass
            connection.execute(_INDEX)
            connection.commit()
            self._connection = connection
        except (sqlite3.Error, OSError) as exc:
            # 캐시는 정확성의 근거가 아니다. 못 열면 매번 계산할 뿐이다.
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def enabled(self) -> bool:
        return self._connection is not None

    def close(self, max_bytes: int = 0) -> None:
        """연결을 놓아준다. max_bytes 가 있으면 그 전에 정리한다.

        정리를 검색 경로가 아니라 여기서 하는 이유는, 라운드마다 용량을 세면
        캐시가 아끼려던 시간을 정리가 다시 쓰기 때문이다. 실행이 끝날 때 한 번만
        본다.
        """
        if self._connection is None:
            return
        if max_bytes:
            self.prune(max_bytes)
        connection = self._connection
        self._connection = None
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def total_bytes(self) -> int:
        """저장된 벡터의 총 바이트. 열지 못했으면 0."""
        if self._connection is None:
            return 0
        try:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(LENGTH(vector)), 0) FROM embeddings"
            ).fetchone()
        except sqlite3.Error as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return 0
        return int(row[0] or 0)

    def prune(self, max_bytes: int) -> int:
        """상한을 넘으면 **최근 사용 시각이 오래된 것부터** 지운다.

        지운 바이트 수를 돌려준다. **정리 실패는 예외로 올리지 않는다** — 캐시는
        속도를 위한 것이지 정확성의 근거가 아니므로, 정리가 실패했다고 검색
        실행이 막히면 안 된다. 실패 사실은 error 에 남는다.

        LRU 로 고른다. 크기순으로 지우면 큰 문헌이 매번 먼저 나가는데, 큰 문헌일
        수록 다시 임베딩하는 비용이 크다. 오래 안 쓴 것을 지우는 편이 낫다.
        """
        if self._connection is None or max_bytes <= 0:
            return 0
        try:
            total = self.total_bytes()
            if total <= max_bytes:
                return 0
            target = total - max_bytes
            removed = 0
            while removed < target:
                rows = self._connection.execute(
                    "SELECT fingerprint, chunk_id, LENGTH(vector) FROM embeddings "
                    "ORDER BY last_used_at ASC, rowid ASC LIMIT 500"
                ).fetchall()
                if not rows:
                    break
                # **필요한 만큼만** 지운다. 가져온 뭉치를 통째로 지우면 상한을
                # 조금 넘었을 뿐인데 캐시가 통째로 비고, 다음 실행이 전부 다시
                # 임베딩한다.
                doomed = []
                for row in rows:
                    if removed >= target:
                        break
                    doomed.append((row[0], row[1]))
                    removed += int(row[2] or 0)
                if not doomed:
                    break
                self._connection.executemany(
                    "DELETE FROM embeddings WHERE fingerprint = ? AND chunk_id = ?",
                    doomed,
                )
            self._connection.commit()
            try:
                self._connection.execute("VACUUM")
            except sqlite3.Error:
                # 공간 회수는 못 해도 행은 지워졌다. 다음 기회에 줄어든다.
                pass
            return removed
        except sqlite3.Error as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return 0

    def __enter__(self) -> EmbeddingCache:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get_many(
        self, fingerprint_key: str, wanted: dict[str, str]
    ) -> dict[str, list[float]]:
        """캐시에 있는 것만 돌려준다.

        wanted 는 {chunk_id: text_sha256}. 저장된 본문 해시가 다르면 그 행은
        없는 것으로 친다 — 키가 같은데 내용이 다른 상황에서 옛 벡터를 쓰면
        검색 결과가 조용히 원문과 어긋난다.
        """
        if self._connection is None or not wanted:
            return {}
        found: dict[str, list[float]] = {}
        ids = list(wanted)
        # SQLite 의 변수 상한(기본 999)에 걸리지 않게 나눠 묻는다.
        for start in range(0, len(ids), 400):
            batch = ids[start : start + 400]
            placeholders = ",".join("?" * len(batch))
            try:
                records = self._connection.execute(
                    "SELECT chunk_id, text_sha256, dim, vector FROM embeddings "
                    f"WHERE fingerprint = ? AND chunk_id IN ({placeholders})",
                    (fingerprint_key, *batch),
                ).fetchall()
            except sqlite3.Error as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return found
            for chunk_id, text_sha256, dim, blob in records:
                if wanted.get(chunk_id) != text_sha256:
                    continue
                try:
                    found[chunk_id] = _unpack(blob, int(dim))
                except (struct.error, TypeError, ValueError):
                    continue
        self._touch(fingerprint_key, list(found))
        return found

    def _touch(self, fingerprint_key: str, chunk_ids: list) -> None:
        """쓴 행의 최근 사용 시각을 올린다. LRU 정리가 이 값을 본다."""
        if self._connection is None or not chunk_ids:
            return
        now = time.time()
        try:
            for start in range(0, len(chunk_ids), 400):
                batch = chunk_ids[start : start + 400]
                placeholders = ",".join("?" * len(batch))
                self._connection.execute(
                    "UPDATE embeddings SET last_used_at = ? "
                    f"WHERE fingerprint = ? AND chunk_id IN ({placeholders})",
                    (now, fingerprint_key, *batch),
                )
            self._connection.commit()
        except sqlite3.Error as exc:
            # 시각을 못 올려도 검색은 그대로다. 정리 순서만 덜 정확해진다.
            self.error = f"{type(exc).__name__}: {exc}"

    def put_many(
        self,
        fingerprint_key: str,
        vectors: dict[str, list[float]],
        digests: dict[str, str],
    ) -> None:
        if self._connection is None or not vectors:
            return
        now = time.time()
        rows = [
            (
                fingerprint_key,
                chunk_id,
                digests.get(chunk_id, ""),
                len(vector),
                _pack(vector),
                now,
            )
            for chunk_id, vector in vectors.items()
            if vector
        ]
        if not rows:
            return
        try:
            self._connection.executemany(
                "INSERT OR REPLACE INTO embeddings "
                "(fingerprint, chunk_id, text_sha256, dim, vector, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._connection.commit()
        except sqlite3.Error as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class NullCache:
    """캐시를 쓰지 않는 경로용. 호출부에 분기를 만들지 않으려고 둔다."""

    enabled = False
    error = ""
    path = None

    def get_many(self, fingerprint_key: str, wanted: dict[str, str]) -> dict:
        return {}

    def put_many(self, fingerprint_key: str, vectors: dict, digests: dict) -> None:
        return None

    def close(self, max_bytes: int = 0) -> None:
        return None

    def total_bytes(self) -> int:
        return 0

    def prune(self, max_bytes: int) -> int:
        return 0

    def __enter__(self) -> NullCache:
        return self

    def __exit__(self, *_exc) -> None:
        return None
