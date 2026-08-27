"""여러 검색 채널을 합쳐서 "청구항 구성 × 인용문헌" 단위로 후보를 고른다.

전역 top-k 를 쓰지 않는다. 그렇게 하면 용어가 잘 맞는 문헌 하나가 결과를
독점하고, 나머지 문헌은 검색해 본 적도 없는데 "없다"로 넘어간다. 구성대비는
문헌마다 대응 여부를 따로 말해야 하는 작업이므로 후보도 문헌마다 확보한다.

채널 병합은 Reciprocal Rank Fusion 이다. 채널마다 점수 척도가 다르고(BM25 는
음수, LIKE 는 점수가 없다) 정규화는 척도 가정을 하나 더 만든다. RRF 는 순위만
쓰므로 검증이 쉽고, 어느 채널이 이 결과를 올렸는지도 그대로 남는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .index import (
    TRIGRAM_MIN_CHARS,
    ChannelResult,
    DocumentIndex,
    SearchRow,
)

# RRF 상수. 원 논문(Cormack et al., 2009)의 기본값이다. 크면 순위 차이가
# 완만해지고 작으면 1등이 강해진다.
RRF_K = 60

# 문헌 하나가 한 구성에 대해 최소한 이만큼은 후보를 낸다. 다른 문헌이 아무리
# 잘 맞아도 이 자리는 뺏기지 않는다.
MIN_HITS_PER_DOCUMENT = 3

CHANNEL_EXACT = "exact_phrase"
CHANNEL_BM25 = "fts_bm25"
CHANNEL_TRIGRAM = "trigram"
CHANNEL_LITERAL = "numbers_symbols"
CHANNEL_SUBSTRING = "substring"
CHANNEL_SEMANTIC = "semantic"
CHANNELS = (
    CHANNEL_EXACT,
    CHANNEL_BM25,
    CHANNEL_TRIGRAM,
    CHANNEL_LITERAL,
    CHANNEL_SUBSTRING,
    CHANNEL_SEMANTIC,
)

# 숫자·단위·도면부호처럼 보이는 토큰. 모델이 따로 지정하지 않아도 검색어에서
# 자동으로 뽑아 literal 채널에 넘긴다.
_NUMERIC_TOKEN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|℃|°C|㎛|㎚|㎜|㎝|㎡|㎥|㎏|㎎|mm|cm|nm|um|kg|mg|"
    r"Hz|kHz|MHz|GHz|V|kV|mV|A|mA|W|kW|Pa|kPa|MPa|rpm|초|분|시간|배|개)?"
)


@dataclass
class IndexedDocument:
    """검색 대상 문헌 하나.

    「분석에 포함」을 푼 첨부는 애초에 이 목록에 들어오지 않는다. 그래서
    제외한 자료는 검색 인덱스에도, 결과에도, 근거 패키지에도 나타나지 않는다.
    """

    alias: str
    attachment_id: str
    filename: str
    sha256: str
    index: DocumentIndex
    report: dict
    rebuilt: bool = False
    role: str = ""

    @property
    def page_count(self) -> int:
        return self.index.page_count

    def manifest_entry(self) -> dict:
        return {
            "alias": self.alias,
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "pdf_sha256": self.sha256,
            "role": self.role,
            "index_rebuilt": self.rebuilt,
            "index": self.index.fingerprint(),
            "extraction": self.report,
        }


@dataclass
class Hit:
    """융합된 검색 결과 한 줄."""

    alias: str
    attachment_id: str
    filename: str
    row: SearchRow
    channels: list[str] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)
    score: float = 0.0

    def to_dict(self, *, include_text: bool = True) -> dict:
        payload = {
            "alias": self.alias,
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "channels": list(self.channels),
            "score": round(self.score, 6),
            **self.row.to_dict(),
        }
        if not include_text:
            payload.pop("text", None)
        return payload


@dataclass
class DocumentSearchResult:
    """문헌 하나에 대한 검색 결과와 채널 진단."""

    document: IndexedDocument
    hits: list[Hit] = field(default_factory=list)
    channels: list[dict] = field(default_factory=list)

    @property
    def failed_channels(self) -> list[str]:
        return [
            entry["channel"]
            for entry in self.channels
            if entry.get("error") or (not entry.get("executed") and entry.get("requested"))
        ]


def extract_literals(queries: list[str]) -> list[str]:
    """검색어에서 숫자·단위 토큰을 뽑는다. 없으면 빈 목록."""
    found: list[str] = []
    for query in queries:
        for match in _NUMERIC_TOKEN.finditer(str(query)):
            token = match.group(0).strip()
            if token and any(ch.isdigit() for ch in token) and token not in found:
                found.append(token)
    return found


def fuse(results: list[ChannelResult], limit: int) -> tuple[list[SearchRow], dict, dict]:
    """RRF 로 채널 결과를 합친다.

    돌려주는 값: (정렬된 행, chunk_id → 채널 목록, chunk_id → 채널별 순위)
    """
    scores: dict[str, float] = {}
    channels: dict[str, list[str]] = {}
    ranks: dict[str, dict[str, int]] = {}
    rows: dict[str, SearchRow] = {}

    for result in results:
        if not result.executed or result.error:
            continue
        for position, row in enumerate(result.rows, start=1):
            scores[row.chunk_id] = scores.get(row.chunk_id, 0.0) + 1.0 / (
                RRF_K + position
            )
            channels.setdefault(row.chunk_id, [])
            if result.channel not in channels[row.chunk_id]:
                channels[row.chunk_id].append(result.channel)
            ranks.setdefault(row.chunk_id, {})[result.channel] = position
            rows.setdefault(row.chunk_id, row)

    ordered = sorted(
        rows.values(),
        key=lambda row: (-scores[row.chunk_id], row.page_number, row.page_order),
    )
    return ordered[:limit], channels, ranks


def _channel_entry(result: ChannelResult, requested: bool) -> dict:
    return {
        "channel": result.channel,
        "requested": requested,
        "executed": result.executed and not result.error,
        "queries": list(result.queries),
        "hits": len(result.rows),
        "skipped_reason": result.skipped_reason,
        "error": result.error,
    }


def search_document(
    document: IndexedDocument,
    *,
    queries: list[str] | None = None,
    phrases: list[str] | None = None,
    literals: list[str] | None = None,
    limit: int = 8,
    per_channel_limit: int = 20,
    semantic_encoder=None,
) -> DocumentSearchResult:
    """문헌 하나를 여러 채널로 찾고 RRF 로 합친다."""
    queries = [q for q in (queries or []) if str(q).strip()]
    phrases = [p for p in (phrases or []) if str(p).strip()]
    literals = [t for t in (literals or []) if str(t).strip()]
    if queries and not literals:
        literals = extract_literals(queries)

    index = document.index
    results: list[tuple[ChannelResult, bool]] = []

    exact = index.search_phrase(phrases, limit=per_channel_limit)
    results.append((exact, bool(phrases)))

    bm25 = index.search_bm25(queries, limit=per_channel_limit)
    results.append((bm25, bool(queries)))

    trigram = index.search_trigram(queries or phrases, limit=per_channel_limit)
    results.append((trigram, bool(queries or phrases)))

    literal = index.search_literal(literals, limit=per_channel_limit)
    results.append((literal, bool(literals)))

    # trigram 이 다루지 못하는 짧은 검색어. 한국어 특허 문언의 2자 낱말
    # ("센서", "제어", "결합")이 여기서 살아난다. 이 채널이 없으면 그런
    # 검색어는 어느 채널에도 걸리지 않고 조용히 0건이 된다.
    short = [
        term
        for term in (*queries, *phrases)
        if 0 < len(term.replace(" ", "")) < TRIGRAM_MIN_CHARS
    ]
    substring = index.search_literal(
        short, limit=per_channel_limit, channel=CHANNEL_SUBSTRING
    )
    results.append((substring, bool(short)))

    if semantic_encoder is not None and queries:
        results.append((_semantic_channel(index, queries, semantic_encoder, per_channel_limit), True))

    rows, channel_map, rank_map = fuse([result for result, _ in results], limit)
    hits = [
        Hit(
            alias=document.alias,
            attachment_id=document.attachment_id,
            filename=document.filename,
            row=row,
            channels=channel_map.get(row.chunk_id, []),
            ranks=rank_map.get(row.chunk_id, {}),
            score=sum(
                1.0 / (RRF_K + position)
                for position in rank_map.get(row.chunk_id, {}).values()
            ),
        )
        for row in rows
    ]
    return DocumentSearchResult(
        document=document,
        hits=hits,
        channels=[_channel_entry(result, requested) for result, requested in results],
    )


def _semantic_channel(
    index: DocumentIndex, queries: list[str], encoder, limit: int
) -> ChannelResult:
    """임베딩 코사인 상위 N. 실패해도 다른 채널을 막지 않는다.

    청크 임베딩은 (pdf_sha256 · 인덱스/추출기 버전 · 모델 · revision) 로 캐시되고
    검색어 임베딩은 실행 안에서 기억된다. 그 판단은 전부 SemanticEncoder 안에
    있다 — 여기서 캐시를 분기하면 캐시 있는 경로와 없는 경로가 서로 다른 후보를
    내놓아도 알아채지 못한다.
    """
    result = ChannelResult(channel=CHANNEL_SEMANTIC, queries=list(queries))
    try:
        from .semantic import best_scores

        chunks = index.all_chunks()
        if not chunks:
            result.executed = False
            result.skipped_reason = "색인된 청크가 없습니다."
            return result
        vectors = encoder.document_vectors(index.fingerprint(), chunks)
        query_vectors = encoder.query_vectors(list(queries))
        # 순위 계산은 한 번에 한다. 청크마다 파이썬 루프를 돌면 임베딩을 캐시해
        # 아낀 시간을 여기서 다시 쓴다(semantic.best_scores 주석의 실측 참조).
        scores = best_scores(vectors, query_vectors)
        scored: list[tuple[float, SearchRow]] = []
        for chunk, best in zip(chunks, scores):
            chunk.score = best
            scored.append((best, chunk))
        # 점수가 같으면 페이지 순서를 지킨다. 정렬이 흔들리면 같은 입력에서
        # 라운드마다 다른 후보가 나와 실행이 재현되지 않는다.
        scored.sort(key=lambda pair: (-pair[0], pair[1].page_number, pair[1].page_order))
        result.rows = [row for _score, row in scored[:limit]]
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def search_corpus(
    documents: list[IndexedDocument],
    *,
    queries: list[str] | None = None,
    phrases: list[str] | None = None,
    literals: list[str] | None = None,
    per_document_limit: int = 8,
    semantic_encoder=None,
) -> list[DocumentSearchResult]:
    """모든 대상 문헌을 각각 검색한다. 문헌 간 경쟁을 시키지 않는다."""
    return [
        search_document(
            document,
            queries=queries,
            phrases=phrases,
            literals=literals,
            limit=max(MIN_HITS_PER_DOCUMENT, per_document_limit),
            semantic_encoder=semantic_encoder,
        )
        for document in documents
    ]


def interleave(results: list[DocumentSearchResult], total_limit: int) -> list[Hit]:
    """문헌별 결과를 라운드로빈으로 섞는다.

    합쳐서 점수순으로 자르면 문헌 하나가 앞자리를 다 가져간다. 자리를 돌아가며
    주면 상한이 작아도 모든 문헌이 최소한 몇 줄씩은 남는다.
    """
    queues = [list(result.hits) for result in results]
    merged: list[Hit] = []
    position = 0
    while len(merged) < total_limit and any(
        position < len(queue) for queue in queues
    ):
        for queue in queues:
            if position < len(queue):
                merged.append(queue[position])
                if len(merged) >= total_limit:
                    break
        position += 1
    return merged
