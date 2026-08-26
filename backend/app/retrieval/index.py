"""문헌 하나당 SQLite FTS5 인덱스.

표준 라이브러리 sqlite3 만 쓴다. 별도 검색 서버도, 벡터 DB 도 요구하지 않는다.
왜 이 구성인지는 docs/adr-0001-local-retrieval.md 에 있다.

인덱스는 **문헌마다 파일 하나**다. 합치지 않는 이유가 두 가지다.

  1. 사용자가 분석에서 제외한 첨부는 검색 인덱스에도 없어야 한다. 문헌별
     파일이면 "열지 않는다"가 곧 "인덱스에 없다"이다.
  2. 한 문헌이 전역 top-k 를 독점하지 못하게 하려면 어차피 문헌마다 따로
     질의해야 한다.

재사용 조건은 (PDF sha256, INDEX_VERSION, EXTRACTOR_VERSION) 세 값이 전부
일치할 때뿐이다. 하나라도 다르면 파일을 지우고 다시 만든다. PDF 가 바뀌었는데
옛 인덱스를 쓰면 보고서의 페이지 출처가 조용히 틀린다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import Chunk, chunk_document
from .extraction import DocumentExtraction
from .versions import EXTRACTOR_VERSION, INDEX_VERSION

# trigram 토크나이저가 다룰 수 있는 최소 길이. SQLite 문서가 정한 값이다.
TRIGRAM_MIN_CHARS = 3

_UNICODE61 = "unicode61 remove_diacritics 2"


class IndexUnavailable(Exception):
    """이 실행 환경의 SQLite 로는 로컬 검색 인덱스를 만들 수 없다."""


@dataclass(frozen=True)
class SqliteCapabilities:
    """지금 이 프로세스의 SQLite 가 실제로 할 수 있는 것.

    빌드마다 다르므로 import 시점이 아니라 런타임에 확인한다. 확인하지 않고
    가정하면, trigram 이 없는 환경에서 부분문자 검색이 조용히 0건을 돌려주고
    그것이 "문헌에 없음"으로 읽힌다.
    """

    fts5: bool
    trigram: bool
    sqlite_version: str
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "fts5": self.fts5,
            "trigram": self.trigram,
            "sqlite_version": self.sqlite_version,
            "error": self.error,
        }


def probe_sqlite() -> SqliteCapabilities:
    """FTS5 와 trigram 토크나이저를 실제로 만들어 본다."""
    fts5 = False
    trigram = False
    error = ""
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.execute(
                f"CREATE VIRTUAL TABLE probe_fts USING fts5(t, tokenize='{_UNICODE61}')"
            )
            fts5 = True
        except sqlite3.Error as exc:
            error = f"FTS5 를 사용할 수 없습니다: {exc}"
        if fts5:
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE probe_tri USING fts5(t, tokenize='trigram')"
                )
                trigram = True
            except sqlite3.Error as exc:
                error = f"trigram 토크나이저를 사용할 수 없습니다: {exc}"
    finally:
        connection.close()
    return SqliteCapabilities(
        fts5=fts5,
        trigram=trigram,
        sqlite_version=sqlite3.sqlite_version,
        error=error,
    )


def escape_match(value: str) -> str:
    """FTS5 MATCH 식에 넣을 수 있는 인용 문자열로 바꾼다.

    사용자·모델이 준 문자열을 그대로 넣으면 AND/OR/NEAR 같은 연산자와 괄호가
    질의 문법으로 해석된다. 전부 하나의 구(phrase)로 묶어서 데이터로만 쓴다.
    """
    return '"' + str(value).replace('"', '""') + '"'


def _match_any(values: list[str]) -> str:
    return " OR ".join(escape_match(value) for value in values)


@dataclass
class SearchRow:
    """인덱스가 돌려주는 한 줄. 출처 메타데이터를 전부 갖고 나온다."""

    chunk_id: str
    page_number: int
    page_order: int
    paragraph: str
    section: str
    printed_page: str
    text: str
    extraction_status: str
    extraction_method: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "pdf_page": self.page_number,
            "printed_page": self.printed_page or None,
            "paragraph": self.paragraph or None,
            "section": self.section or None,
            "page_order": self.page_order,
            "text": self.text,
            "extraction_status": self.extraction_status,
            "extraction_method": self.extraction_method,
        }


_ROW_COLUMNS = (
    "chunk_id, page_number, page_order, paragraph, section, printed_page, "
    "text, extraction_status, extraction_method"
)


def _row(record, score: float = 0.0) -> SearchRow:
    return SearchRow(
        chunk_id=record[0],
        page_number=int(record[1]),
        page_order=int(record[2]),
        paragraph=record[3] or "",
        section=record[4] or "",
        printed_page=record[5] or "",
        text=record[6] or "",
        extraction_status=record[7] or "",
        extraction_method=record[8] or "",
        score=score,
    )


@dataclass
class ChannelResult:
    """검색 채널 하나의 결과. 실행되지 않았으면 그 사실이 남는다."""

    channel: str
    rows: list[SearchRow] = field(default_factory=list)
    executed: bool = True
    skipped_reason: str = ""
    error: str = ""
    queries: list[str] = field(default_factory=list)


class DocumentIndex:
    """문헌 하나의 검색 인덱스. 열려 있는 SQLite 연결을 감싼다."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection
        self.meta = {
            key: value
            for key, value in connection.execute("SELECT key, value FROM meta")
        }
        self.attachment_id = self.meta.get("attachment_id", "")
        self.filename = self.meta.get("filename", "")
        self.sha256 = self.meta.get("pdf_sha256", "")
        self.page_count = int(self.meta.get("processed_page_count") or 0)
        self.source_page_count = int(self.meta.get("source_page_count") or 0)
        self.chunk_count = int(self.meta.get("chunk_count") or 0)
        self.trigram_enabled = self.meta.get("trigram_enabled") == "1"

    # ------------------------------------------------------------------ 조회

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DocumentIndex:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def extraction_report(self) -> dict:
        try:
            return json.loads(self.meta.get("extraction_report") or "{}")
        except json.JSONDecodeError:
            return {}

    def fingerprint(self) -> dict:
        """인덱스를 다시 만들 수 있게 하는 재현 정보."""
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "pdf_sha256": self.sha256,
            "index_version": int(self.meta.get("index_version") or 0),
            "extractor_version": self.meta.get("extractor_version", ""),
            "chunk_count": self.chunk_count,
            "page_count": self.page_count,
            "source_page_count": self.source_page_count,
            "trigram_enabled": self.trigram_enabled,
            "built_at": self.meta.get("built_at", ""),
        }

    def chunk(self, chunk_id: str) -> SearchRow | None:
        record = self._connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return _row(record) if record else None

    def page_rows(self, page_number: int) -> list[SearchRow]:
        records = self._connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM chunks WHERE page_number = ? "
            "ORDER BY page_order",
            (page_number,),
        ).fetchall()
        return [_row(record) for record in records]

    def page_status(self, page_number: int) -> dict | None:
        record = self._connection.execute(
            "SELECT page_number, printed_page, status, extraction_method, "
            "extraction_error, char_count, warnings FROM pages WHERE page_number = ?",
            (page_number,),
        ).fetchone()
        if record is None:
            return None
        return {
            "pdf_page": int(record[0]),
            "printed_page": record[1] or None,
            "status": record[2],
            "extraction_method": record[3],
            "extraction_error": record[4],
            "char_count": int(record[5] or 0),
            "warnings": json.loads(record[6] or "[]"),
        }

    def paragraph_rows(self, paragraph: str) -> list[SearchRow]:
        """[0032] 같은 문단번호로 찾는다. 표기 차이를 흡수한다."""
        digits = re.sub(r"\D", "", str(paragraph))
        if not digits:
            return []
        normalized = f"[{int(digits):04d}]"
        records = self._connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM chunks WHERE paragraph = ? "
            "ORDER BY page_number, page_order",
            (normalized,),
        ).fetchall()
        return [_row(record) for record in records]

    def neighbours(self, chunk_id: str, before: int = 1, after: int = 1) -> tuple[str, str]:
        """청크 앞뒤 문맥. 같은 페이지 안에서만 가져온다."""
        target = self.chunk(chunk_id)
        if target is None:
            return "", ""
        rows = self.page_rows(target.page_number)
        position = next(
            (i for i, row in enumerate(rows) if row.chunk_id == chunk_id), None
        )
        if position is None:
            return "", ""
        head = "\n".join(row.text for row in rows[max(0, position - before) : position])
        tail = "\n".join(row.text for row in rows[position + 1 : position + 1 + after])
        return head, tail

    # ------------------------------------------------------------------ 검색

    def _match(
        self, table: str, expression: str, limit: int, ordered: bool
    ) -> list[SearchRow]:
        order = f"ORDER BY bm25({table})" if ordered else "ORDER BY rank"
        sql = (
            f"SELECT {', '.join('c.' + c.strip() for c in _ROW_COLUMNS.split(','))}, "
            f"bm25({table}) AS score "
            f"FROM {table} JOIN chunks c ON c.id = CAST({table}.chunk_ref AS INTEGER) "
            f"WHERE {table} MATCH ? {order} LIMIT ?"
        )
        records = self._connection.execute(sql, (expression, limit)).fetchall()
        return [_row(record, score=float(record[9])) for record in records]

    def search_bm25(self, terms: list[str], limit: int = 20) -> ChannelResult:
        """unicode61 토큰 기반 BM25. 여러 검색어는 OR 로 묶는다.

        각 검색어를 **접두 질의**(`"센서"*`)로 넣는다. 한국어는 조사가 어간에
        붙어 하나의 토큰이 되므로("센서를", "센서의"), 완전일치로는 같은 낱말도
        걸리지 않는다. 접두 질의가 그 차이를 흡수한다. 어간이 뒤에 오는 합성어
        ("압력센서")는 접두로도 걸리지 않으므로 trigram·부분문자 채널이 맡는다.
        """
        cleaned = [term.strip() for term in terms if str(term).strip()]
        result = ChannelResult(channel="fts_bm25", queries=cleaned)
        if not cleaned:
            result.executed = False
            result.skipped_reason = "검색어가 비어 있습니다."
            return result
        expression = " OR ".join(f"{escape_match(term)}*" for term in cleaned)
        try:
            result.rows = self._match("chunk_fts", expression, limit, ordered=True)
        except sqlite3.Error:
            # 접두 질의를 만들 수 없는 검색어(토크나이저가 전부 버리는 기호 등)
            # 는 완전일치로 한 번 더 시도한다. 조용히 0건으로 두지 않는다.
            try:
                result.rows = self._match(
                    "chunk_fts", _match_any(cleaned), limit, ordered=True
                )
            except sqlite3.Error as exc:
                result.error = str(exc)
        return result

    def search_phrase(self, phrases: list[str], limit: int = 20) -> ChannelResult:
        """정확 문구 검색. 각 문구를 통째로 하나의 phrase 로 넣는다."""
        cleaned = [phrase.strip() for phrase in phrases if str(phrase).strip()]
        result = ChannelResult(channel="exact_phrase", queries=cleaned)
        if not cleaned:
            result.executed = False
            result.skipped_reason = "문구가 비어 있습니다."
            return result
        rows: list[SearchRow] = []
        seen: set[str] = set()
        for phrase in cleaned:
            try:
                found = self._match(
                    "chunk_fts", escape_match(phrase), limit, ordered=True
                )
            except sqlite3.Error as exc:
                result.error = str(exc)
                continue
            for row in found:
                if row.chunk_id not in seen:
                    seen.add(row.chunk_id)
                    rows.append(row)
        result.rows = rows[:limit]
        return result

    def search_trigram(self, fragments: list[str], limit: int = 20) -> ChannelResult:
        """부분문자 검색. 조사·합성어 차이로 토큰이 갈려도 걸린다."""
        cleaned = [
            fragment.strip() for fragment in fragments if str(fragment).strip()
        ]
        result = ChannelResult(channel="trigram", queries=cleaned)
        if not self.trigram_enabled:
            result.executed = False
            result.skipped_reason = (
                "이 실행 환경의 SQLite 에 trigram 토크나이저가 없어 부분문자 "
                "검색을 수행하지 못했습니다."
            )
            return result
        usable = [f for f in cleaned if len(f.replace(" ", "")) >= TRIGRAM_MIN_CHARS]
        if not usable:
            result.executed = False
            result.skipped_reason = (
                f"부분문자 검색은 {TRIGRAM_MIN_CHARS}자 이상만 가능합니다."
            )
            return result
        rows: list[SearchRow] = []
        seen: set[str] = set()
        for fragment in usable:
            try:
                found = self._match(
                    "chunk_tri", escape_match(fragment), limit, ordered=True
                )
            except sqlite3.Error as exc:
                result.error = str(exc)
                continue
            for row in found:
                if row.chunk_id not in seen:
                    seen.add(row.chunk_id)
                    rows.append(row)
        result.rows = rows[:limit]
        return result

    def search_literal(
        self, terms: list[str], limit: int = 20, channel: str = "numbers_symbols"
    ) -> ChannelResult:
        """토큰 경계와 무관한 부분문자 검색(LIKE).

        두 곳에서 쓴다.

          numbers_symbols  숫자·범위·단위·도면부호 ("110", "5V", "0.5mm")
          substring        trigram 이 다루지 못하는 2자 이하 검색어. 한국어
                           특허 문언에는 "센서", "제어", "결합" 같은 2자 낱말이
                           매우 흔하고, trigram 은 3자 미만을 색인하지 못한다.

        인덱스를 타지 않지만 문헌 하나의 청크 수는 수천 규모라 로컬에서 문제가
        되지 않는다.
        """
        cleaned = [term.strip() for term in terms if str(term).strip()]
        result = ChannelResult(channel=channel, queries=cleaned)
        if not cleaned:
            result.executed = False
            result.skipped_reason = "검색어가 비어 있습니다."
            return result
        rows: list[SearchRow] = []
        seen: set[str] = set()
        for term in cleaned:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            try:
                records = self._connection.execute(
                    f"SELECT {_ROW_COLUMNS} FROM chunks "
                    "WHERE text LIKE ? ESCAPE '\\' "
                    "ORDER BY page_number, page_order LIMIT ?",
                    (f"%{escaped}%", limit),
                ).fetchall()
            except sqlite3.Error as exc:
                result.error = str(exc)
                continue
            for record in records:
                row = _row(record)
                if row.chunk_id not in seen:
                    seen.add(row.chunk_id)
                    rows.append(row)
        result.rows = rows[:limit]
        return result

    def all_chunks(self) -> list[SearchRow]:
        records = self._connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM chunks ORDER BY page_number, page_order"
        ).fetchall()
        return [_row(record) for record in records]


# --------------------------------------------------------------------- 생성


_SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE pages (
    page_number INTEGER PRIMARY KEY,
    printed_page TEXT,
    status TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    extraction_error TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    warnings TEXT NOT NULL DEFAULT '[]',
    text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL UNIQUE,
    page_number INTEGER NOT NULL,
    page_order INTEGER NOT NULL,
    paragraph TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    printed_page TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'ok',
    extraction_method TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_chunks_page ON chunks (page_number, page_order);
CREATE INDEX ix_chunks_paragraph ON chunks (paragraph);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    text, chunk_ref UNINDEXED, tokenize='{_UNICODE61}'
);
"""

_TRIGRAM_SCHEMA = """
CREATE VIRTUAL TABLE chunk_tri USING fts5(
    text, chunk_ref UNINDEXED, tokenize='trigram'
);
"""


def build_index(
    path: Path,
    extraction: DocumentExtraction,
    *,
    capabilities: SqliteCapabilities | None = None,
) -> dict:
    """인덱스 파일을 새로 만든다. 이미 있으면 지우고 다시 만든다.

    돌려주는 값은 완전성 보고서다(extraction_report.json 에 그대로 들어간다).
    """
    caps = capabilities or probe_sqlite()
    if not caps.fts5:
        raise IndexUnavailable(
            caps.error
            or "이 실행 환경의 SQLite 에 FTS5 가 없어 로컬 검색 인덱스를 만들 수 "
            "없습니다. ARIA 는 검색 없이 근거를 지어내지 않으므로 실행을 "
            "중단합니다."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    chunks: list[Chunk] = chunk_document(extraction)
    chunk_failures = 0

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(_SCHEMA)
        if caps.trigram:
            connection.executescript(_TRIGRAM_SCHEMA)

        for page in extraction.pages:
            connection.execute(
                "INSERT INTO pages (page_number, printed_page, status, "
                "extraction_method, extraction_error, char_count, warnings, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    page.page_number,
                    page.printed_page,
                    page.status,
                    page.extraction_method,
                    page.extraction_error,
                    page.char_count,
                    json.dumps(page.warnings, ensure_ascii=False),
                    page.text,
                ),
            )

        for chunk in chunks:
            try:
                cursor = connection.execute(
                    "INSERT INTO chunks (chunk_id, page_number, page_order, "
                    "paragraph, section, printed_page, text, char_count, "
                    "extraction_status, extraction_method) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.chunk_id,
                        chunk.page_number,
                        chunk.page_order,
                        chunk.paragraph,
                        chunk.section,
                        chunk.printed_page,
                        chunk.text,
                        chunk.char_count,
                        chunk.extraction_status,
                        chunk.extraction_method,
                    ),
                )
            except sqlite3.Error:
                chunk_failures += 1
                continue
            rowid = cursor.lastrowid
            connection.execute(
                "INSERT INTO chunk_fts (text, chunk_ref) VALUES (?, ?)",
                (chunk.text, str(rowid)),
            )
            if caps.trigram:
                connection.execute(
                    "INSERT INTO chunk_tri (text, chunk_ref) VALUES (?, ?)",
                    (chunk.text, str(rowid)),
                )

        report = extraction.report(
            chunk_count=len(chunks) - chunk_failures, chunk_failures=chunk_failures
        )
        from datetime import datetime, timezone

        meta = {
            "attachment_id": extraction.attachment_id,
            "filename": extraction.filename,
            "pdf_sha256": extraction.sha256,
            "index_version": str(INDEX_VERSION),
            "extractor_version": EXTRACTOR_VERSION,
            "source_page_count": str(extraction.source_page_count),
            "processed_page_count": str(extraction.processed_page_count),
            "chunk_count": str(len(chunks) - chunk_failures),
            "chunk_failures": str(chunk_failures),
            "trigram_enabled": "1" if caps.trigram else "0",
            "sqlite_version": caps.sqlite_version,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "extraction_report": json.dumps(report, ensure_ascii=False),
        }
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)", list(meta.items())
        )
        connection.commit()
    finally:
        connection.close()
    return report


def _meta_matches(path: Path, sha256: str) -> bool:
    """저장된 인덱스를 그대로 써도 되는가.

    PDF 해시와 두 버전이 전부 같아야 한다. 하나라도 다르면 False 이고 호출부가
    다시 만든다. 열지 못하는 파일도 False 다 — 손상된 인덱스를 쓰느니 다시
    만드는 쪽이 항상 옳다.
    """
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error:
        return False
    try:
        rows = dict(connection.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return (
        rows.get("pdf_sha256") == sha256
        and rows.get("index_version") == str(INDEX_VERSION)
        and rows.get("extractor_version") == EXTRACTOR_VERSION
    )


def open_index(path: Path) -> DocumentIndex:
    connection = sqlite3.connect(str(path), check_same_thread=False)
    return DocumentIndex(path, connection)


def ensure_index(
    path: Path,
    extraction_factory,
    *,
    sha256: str,
    capabilities: SqliteCapabilities | None = None,
) -> tuple[DocumentIndex, dict, bool]:
    """인덱스를 재사용하거나 다시 만든다.

    extraction_factory 는 인덱스를 새로 만들어야 할 때만 호출된다. 재사용할 수
    있는 인덱스가 있으면 PDF 를 다시 파싱하지 않는다.

    돌려주는 값: (인덱스, 완전성 보고서, 새로 만들었는가)
    """
    if path.exists() and _meta_matches(path, sha256):
        index = open_index(path)
        return index, index.extraction_report, False
    report = build_index(path, extraction_factory(), capabilities=capabilities)
    return open_index(path), report, True
