"""의미 검색 채널과 임베딩 캐시.

여기서 확인하는 것은 "import 가 된다"가 아니다. 실제 ARIA 검색 경로에서

  - 키워드가 거의 겹치지 않는 한국어 문장을 후보로 올리는가
  - 그 후보가 semantic 채널로 기록되고 RRF 로 합쳐지는가
  - 꺼져 있을 때 예전 키워드 검색이 그대로인가
  - 모델을 열지 못했을 때 조용히 빠지지 않고 사유가 남는가
  - 같은 문헌을 다시 검색할 때 임베딩을 다시 계산하지 않는가

모델이 필요한 테스트는 캐시가 없으면 skip 한다. 없는 환경에서 실패하면
"의미 검색이 깨졌다"와 "모델을 안 받았다"를 구분할 수 없다.
"""

from __future__ import annotations

import pytest

from app import retrieval
from app.enums import AttachmentRole, DeliveryMode, ExtractionMethod
from app.ingestion.service import IngestedFile
from app.retrieval import embedding_cache, search
from app.retrieval import semantic as semantic_module
from app.retrieval.search import (
    CHANNEL_BM25,
    CHANNEL_EXACT,
    CHANNEL_SEMANTIC,
    CHANNEL_TRIGRAM,
)

from .pdf_fixture import build_korean_pdf

# ------------------------------------------------------------------ 고정 자료

# 사용자의 검증 예시 그대로. 두 문장은 뜻이 겹치지만 낱말이 거의 겹치지 않는다.
QUERY = "사용자의 생체정보를 확인하여 접근을 인증한다"
TARGET = "등록된 지문 특징과 대조한 후 출입 권한을 부여한다"

PAGES = [
    "[0001] 본 발명은 도어락 장치에 관한 것이다.\n- 1 -",
    "[0012] 냉각수의 유량을 조절하는 밸브가 하우징에 결합된다.\n- 2 -",
    f"[0023] {TARGET}\n- 3 -",
    "[0034] 회전축에 결합된 베어링의 마모를 주기적으로 점검한다.\n- 4 -",
]


def _attachment(tmp_path, name="doc.pdf", pages=None, sha="sha-semantic"):
    data = build_korean_pdf(pages or PAGES)
    target = tmp_path / "input" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return IngestedFile(
        attachment_id=name.replace(".pdf", ""),
        original_filename=name,
        internal_filename=name,
        mime_type="application/pdf",
        size_bytes=len(data),
        sha256=sha,
        required=True,
        stored_path=str(target),
        role=AttachmentRole.CITATION,
        page_count=len(pages or PAGES),
        char_count=sum(len(p) for p in (pages or PAGES)),
        extraction_method=ExtractionMethod.PDF_TEXT_LAYER,
        delivery_mode=DeliveryMode.INLINE_CONTEXT,
        read_ok=True,
    )


def _document(tmp_path, **kwargs):
    documents, skipped = retrieval.build_corpus([_attachment(tmp_path, **kwargs)], tmp_path)
    assert not skipped, skipped
    return documents[0], documents


def _target_hit(result):
    """TARGET 문장이 들어 있는 후보. 없으면 None."""
    for hit in result.hits:
        if "지문" in hit.row.text:
            return hit
    return None


def _model_available() -> bool:
    """모델이 **이미 캐시에 있는가.** 없으면 받지 않는다.

    allow_download=False 가 핵심이다. 이 함수는 수집 단계(skipif 판정)에서
    불리므로, 여기서 받기 시작하면 모델이 없는 깨끗한 환경의 테스트 실행이
    네트워크와 458 MB 다운로드에 의존하게 된다.
    """
    encoder, state = semantic_module.load_encoder(
        True, cache=embedding_cache.NullCache(), allow_download=False
    )
    if encoder is not None:
        encoder.close()
    return state.active


needs_model = pytest.mark.skipif(
    not _model_available(),
    reason="의미 검색 모델 캐시가 없습니다. requirements-semantic.txt 설치 후 "
    "최초 1회 다운로드가 필요합니다.",
)


# ------------------------------------------------------------ 모델 로딩


@needs_model
def test_encoder_loads_from_local_cache_without_network() -> None:
    """캐시가 있으면 저장소에 묻지 않고 연다.

    이 경로가 없으면 캐시가 다 채워진 PC 에서도 저장소에 닿지 못할 때 의미
    검색 전체가 비활성이 된다. 실측으로 그 상태를 겪었기 때문에 회귀로 남긴다.
    """
    encoder, state = semantic_module.load_encoder(
        True, cache=embedding_cache.NullCache()
    )
    try:
        assert state.enabled is True
        assert state.active is True
        assert state.cache_state == "loaded"
        assert state.reason == ""
        assert state.model_source == "local_cache"
        # 고정한 모델·revision 이 그대로여야 한다.
        assert state.model == semantic_module.MODEL_NAME
        assert state.revision == semantic_module.MODEL_REVISION
        payload = state.to_dict()
        assert payload["active"] is True
        assert payload["cache_state"] == "loaded"
        assert payload["revision"] == semantic_module.MODEL_REVISION
    finally:
        if encoder is not None:
            encoder.close()


@needs_model
def test_korean_paraphrase_is_embedded_closer_than_unrelated_text() -> None:
    """생체인증 ↔ 지문대조가 무관한 기계 문장보다 가까워야 한다."""
    encoder, _state = semantic_module.load_encoder(
        True, cache=embedding_cache.NullCache()
    )
    try:
        vectors = encoder.encode(
            [QUERY, TARGET, "회전축에 결합된 베어링의 마모를 점검한다"]
        )
        related = semantic_module.cosine(vectors[0], vectors[1])
        unrelated = semantic_module.cosine(vectors[0], vectors[2])
        assert related > unrelated
        # 여유를 두고 확인한다. 값 자체를 고정하면 모델을 못 바꾸게 된다.
        assert related > 0.4
    finally:
        encoder.close()


# ------------------------------------------- 실제 검색 경로에서의 의미 채널


@needs_model
def test_semantic_channel_surfaces_low_overlap_korean_sentence(tmp_path) -> None:
    """키워드로는 안 걸리는 문장을 의미 검색이 후보로 올린다.

    이 테스트의 핵심은 **끄면 안 나온다**는 대조다. 켠 쪽만 확인하면 다른
    채널이 우연히 찾은 것을 의미 검색의 성과로 착각하게 된다.
    """
    document, documents = _document(tmp_path)
    try:
        without = search.search_document(document, queries=[QUERY])
        assert _target_hit(without) is None, (
            "키워드 채널만으로 이미 찾는다면 이 문장은 의미 검색을 검증하지 "
            "못한다. 낱말이 더 적게 겹치는 문장으로 바꿔야 한다."
        )

        encoder, state = semantic_module.load_encoder(
            True, cache=embedding_cache.NullCache()
        )
        try:
            with_semantic = search.search_document(
                document, queries=[QUERY], semantic_encoder=encoder
            )
        finally:
            encoder.close()

        assert state.active is True
        hit = _target_hit(with_semantic)
        assert hit is not None, "의미 검색을 켰는데도 후보로 올라오지 않았다."
        # 어느 채널이 올렸는지가 기록에 남아야 한다.
        assert CHANNEL_SEMANTIC in hit.channels
        assert CHANNEL_SEMANTIC in hit.ranks
        assert hit.ranks[CHANNEL_SEMANTIC] >= 0
    finally:
        retrieval.close_documents(documents)


@needs_model
def test_semantic_channel_is_recorded_as_executed(tmp_path) -> None:
    """채널 실행 기록에 semantic 이 executed 로 남는다."""
    document, documents = _document(tmp_path)
    try:
        encoder, _state = semantic_module.load_encoder(
            True, cache=embedding_cache.NullCache()
        )
        try:
            result = search.search_document(
                document, queries=[QUERY], semantic_encoder=encoder
            )
        finally:
            encoder.close()
        entry = next(
            item for item in result.channels if item["channel"] == CHANNEL_SEMANTIC
        )
        assert entry["executed"] is True
        assert entry["requested"] is True
        assert not entry.get("error")
    finally:
        retrieval.close_documents(documents)


@needs_model
def test_semantic_does_not_replace_keyword_channels(tmp_path) -> None:
    """의미 검색을 켜도 키워드 채널이 그대로 돈다. 대체가 아니라 추가다."""
    document, documents = _document(tmp_path)
    try:
        encoder, _state = semantic_module.load_encoder(
            True, cache=embedding_cache.NullCache()
        )
        try:
            result = search.search_document(
                document,
                queries=["냉각수의 유량을 조절하는 밸브"],
                phrases=["회전축에 결합된 베어링"],
                semantic_encoder=encoder,
            )
        finally:
            encoder.close()
        executed = {
            item["channel"] for item in result.channels if item.get("executed")
        }
        # 기존 채널이 하나라도 빠지면 의미 검색이 대체해 버린 것이다.
        assert {CHANNEL_EXACT, CHANNEL_BM25, CHANNEL_TRIGRAM} <= executed
        assert CHANNEL_SEMANTIC in executed
        # 키워드로 찾히던 문장은 여전히 찾힌다.
        assert any("베어링" in hit.row.text for hit in result.hits)
    finally:
        retrieval.close_documents(documents)


def test_semantic_off_keeps_keyword_search_unchanged(tmp_path) -> None:
    """끄면 예전과 똑같다. 모델 없이도 도는 회귀 테스트."""
    document, documents = _document(tmp_path)
    try:
        result = search.search_document(
            document, queries=["냉각수의 유량을 조절하는 밸브"]
        )
        channels = {item["channel"] for item in result.channels}
        assert CHANNEL_SEMANTIC not in channels
        assert any("냉각수" in hit.row.text for hit in result.hits)

        encoder, state = semantic_module.load_encoder(False)
        assert encoder is None
        assert state.enabled is False
        assert state.active is False
        assert state.cache_state == "not_checked"
        assert state.reason == semantic_module.DISABLED_BY_SETTING
        # 꺼져 있을 때는 모델 이름도 내보내지 않는다. 켜지 않은 실행이
        # 화면에서 "이 모델을 썼다"로 읽히면 안 된다.
        assert state.to_dict()["model"] is None
    finally:
        retrieval.close_documents(documents)


# ----------------------------------------------------- 모델 로딩 실패 fallback


def test_model_failure_falls_back_to_keyword_with_recorded_reason(
    tmp_path, monkeypatch
) -> None:
    """모델을 못 열어도 검색은 돌고, 왜 안 돌았는지가 남는다."""

    def boom(*_args, **_kwargs):
        raise OSError("연결할 수 없습니다")

    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", boom, raising=False
    )

    encoder, state = semantic_module.load_encoder(True)
    assert encoder is None
    assert state.enabled is True
    assert state.active is False
    assert state.cache_state == "unavailable"
    # 사유가 비어 있으면 화면에서 "의미 검색까지 돌린 결과"로 읽힌다.
    assert state.reason
    assert "OSError" in state.reason
    assert "키워드 검색만으로" in state.reason
    payload = state.to_dict()
    assert payload["active"] is False
    assert payload["reason"] == state.reason

    document, documents = _document(tmp_path)
    try:
        result = search.search_document(
            document, queries=["냉각수의 유량을 조절하는 밸브"], semantic_encoder=None
        )
        assert any("냉각수" in hit.row.text for hit in result.hits)
    finally:
        retrieval.close_documents(documents)


def test_missing_library_is_reported_as_not_installed(monkeypatch) -> None:
    """라이브러리가 없는 환경과 모델이 없는 환경을 구분해서 기록한다."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    encoder, state = semantic_module.load_encoder(True)
    assert encoder is None
    assert state.cache_state == "not_installed"
    assert "requirements-semantic.txt" in state.reason


# ------------------------------------------------------------ 임베딩 캐시


class CountingModel:
    """호출 횟수를 세는 결정론적 대역.

    SentenceTransformer 자리에 들어가므로 그쪽 호출 형태를 그대로 받는다.
    모델 없이 캐시 동작만 확인한다.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, batch_size=32, convert_to_numpy=False):
        self.calls.append(list(texts))
        # 내용에 따라 달라지되 재현 가능한 벡터.
        return [[float(len(text)), float(sum(map(ord, text)) % 97)] for text in texts]

    @property
    def encoded_count(self) -> int:
        return sum(len(batch) for batch in self.calls)


class Row:
    def __init__(self, chunk_id: str, text: str) -> None:
        self.chunk_id = chunk_id
        self.text = text


META = {
    "pdf_sha256": "abc",
    "index_version": 1,
    "extractor_version": "pypdf-x+aria-1",
}


def _encoder(tmp_path, name="cache.sqlite3"):
    inner = CountingModel()
    cache = embedding_cache.EmbeddingCache(tmp_path / name)
    return inner, semantic_module.SemanticEncoder(inner, cache=cache), cache


def test_document_embeddings_are_reused_across_calls(tmp_path) -> None:
    """같은 문헌을 다시 검색하면 청크를 다시 임베딩하지 않는다."""
    inner, encoder, cache = _encoder(tmp_path)
    rows = [Row(f"P0001-{i:03d}", f"본문 {i}") for i in range(5)]
    try:
        first = encoder.document_vectors(META, rows)
        assert inner.encoded_count == 5
        second = encoder.document_vectors(META, rows)
        # 두 번째 호출은 한 청크도 새로 계산하지 않는다.
        assert inner.encoded_count == 5
        assert first == second
        assert encoder.stats.document_encoded == 5
        assert encoder.stats.document_cache_hits == 5
    finally:
        cache.close()


def test_cache_survives_a_new_process(tmp_path) -> None:
    """실행이 끝나고 새로 열어도 남아 있다. job 마다 다시 계산하지 않는다."""
    inner_a, encoder_a, cache_a = _encoder(tmp_path)
    rows = [Row(f"P0001-{i:03d}", f"본문 {i}") for i in range(4)]
    try:
        expected = encoder_a.document_vectors(META, rows)
        assert inner_a.encoded_count == 4
    finally:
        cache_a.close()

    inner_b, encoder_b, cache_b = _encoder(tmp_path)
    try:
        again = encoder_b.document_vectors(META, rows)
        assert inner_b.encoded_count == 0
        assert again == expected
        assert encoder_b.stats.document_cache_hits == 4
    finally:
        cache_b.close()


def test_cache_key_separates_documents_and_model_revisions(tmp_path) -> None:
    """결과를 바꿀 수 있는 값이 다르면 다른 캐시다."""
    base = embedding_cache.fingerprint(META, "model-a", "rev-1")
    assert base != embedding_cache.fingerprint(META, "model-a", "rev-2")
    assert base != embedding_cache.fingerprint(META, "model-b", "rev-1")
    assert base != embedding_cache.fingerprint({**META, "pdf_sha256": "zzz"}, "model-a", "rev-1")
    assert base != embedding_cache.fingerprint({**META, "index_version": 2}, "model-a", "rev-1")
    assert base != embedding_cache.fingerprint(
        {**META, "extractor_version": "pypdf-y+aria-1"}, "model-a", "rev-1"
    )
    # 파일명이나 attachment_id 는 청크를 바꾸지 않으므로 키가 아니다.
    assert base == embedding_cache.fingerprint(
        {**META, "filename": "다른이름.pdf", "attachment_id": "other"},
        "model-a",
        "rev-1",
    )


def test_cache_row_is_rejected_when_chunk_text_changed(tmp_path) -> None:
    """키가 같은데 본문이 다르면 옛 벡터를 쓰지 않는다."""
    inner, encoder, cache = _encoder(tmp_path)
    try:
        original = [Row("P0001-001", "원래 본문")]
        encoder.document_vectors(META, original)
        assert inner.encoded_count == 1

        changed = [Row("P0001-001", "바뀐 본문")]
        vectors = encoder.document_vectors(META, changed)
        # 다시 계산했고, 결과는 바뀐 본문의 벡터다.
        assert inner.encoded_count == 2
        assert vectors == inner.encode(["바뀐 본문"])
    finally:
        cache.close()


def test_query_vectors_are_memoised_within_a_run(tmp_path) -> None:
    """같은 검색어가 라운드마다 되풀이돼도 한 번만 계산한다."""
    inner, encoder, cache = _encoder(tmp_path)
    try:
        encoder.query_vectors([QUERY, "다른 검색어"])
        assert inner.encoded_count == 2
        encoder.query_vectors([QUERY, "다른 검색어", QUERY])
        assert inner.encoded_count == 2
        assert encoder.stats.query_encoded == 2
        assert encoder.stats.query_cache_hits == 3
    finally:
        cache.close()


def test_broken_cache_does_not_break_search(tmp_path) -> None:
    """캐시를 열지 못해도 검색은 돈다. 캐시는 정확성의 근거가 아니다."""
    # 디렉터리를 파일 자리에 두어 열리지 않게 만든다.
    blocked = tmp_path / "blocked.sqlite3"
    blocked.mkdir()
    cache = embedding_cache.EmbeddingCache(blocked)
    assert cache.enabled is False
    assert cache.error

    inner = CountingModel()
    encoder = semantic_module.SemanticEncoder(inner, cache=cache)
    rows = [Row("P0001-001", "본문")]
    assert encoder.document_vectors(META, rows) == inner.encode(["본문"])
    # 실패 사실은 통계에 남는다. 조용히 매번 다시 계산하면 이유를 알 수 없다.
    assert encoder.stats.cache_error


@needs_model
def test_cache_does_not_change_which_candidates_are_returned(tmp_path) -> None:
    """캐시가 있든 없든 같은 후보가 나온다. 속도만 다르다."""
    document, documents = _document(tmp_path)
    try:
        plain, _ = semantic_module.load_encoder(
            True, cache=embedding_cache.NullCache()
        )
        try:
            expected = [
                hit.row.chunk_id
                for hit in search.search_document(
                    document, queries=[QUERY], semantic_encoder=plain
                ).hits
            ]
        finally:
            plain.close()

        cached, _ = semantic_module.load_encoder(
            True, cache=embedding_cache.EmbeddingCache(tmp_path / "emb.sqlite3")
        )
        try:
            # 두 번 돌린다. 두 번째는 전부 캐시에서 나온다.
            search.search_document(document, queries=[QUERY], semantic_encoder=cached)
            warm = [
                hit.row.chunk_id
                for hit in search.search_document(
                    document, queries=[QUERY], semantic_encoder=cached
                ).hits
            ]
            assert cached.stats.document_cache_hits > 0
        finally:
            cached.close()

        assert warm == expected
    finally:
        retrieval.close_documents(documents)


# ------------------------------- 외부 리뷰에서 나온 회귀 (2026-08-27)


def test_probe_does_not_download_when_the_model_is_missing(monkeypatch) -> None:
    """allow_download=False 는 네트워크를 건드리지 않는다.

    이 파일의 skipif 가 수집 단계에서 이 경로를 쓴다. 여기서 받기 시작하면
    모델이 없는 깨끗한 환경의 테스트 실행이 458 MB 다운로드에 묶인다.
    """
    calls: list[dict] = []

    def fake_snapshot_download(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise OSError("캐시에 없음")
        raise AssertionError("다운로드를 시도했다. allow_download=False 인데도.")

    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download, raising=False
    )

    encoder, state = semantic_module.load_encoder(
        True, cache=embedding_cache.NullCache(), allow_download=False
    )
    assert encoder is None
    assert state.active is False
    assert state.cache_state == "unavailable"
    assert "다운로드를 하지 않도록" in state.reason
    # 캐시 조회는 했고, 다운로드는 시도하지 않았다.
    assert len(calls) == 1
    assert calls[0].get("local_files_only") is True


def test_allow_download_true_still_falls_back_to_the_network(monkeypatch) -> None:
    """기본값은 여전히 「없으면 한 번 받는다」이다. 앱 동작을 바꾸지 않았다."""
    attempts: list[bool] = []

    def fake_snapshot_download(*args, **kwargs):
        attempts.append(bool(kwargs.get("local_files_only")))
        raise OSError("연결할 수 없습니다")

    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download, raising=False
    )
    encoder, state = semantic_module.load_encoder(True)
    assert encoder is None
    assert state.cache_state == "unavailable"
    # 캐시 조회 후 다운로드까지 시도했다.
    assert attempts == [True, False]


@needs_model
def test_channel_ranks_reach_the_audit_record(tmp_path) -> None:
    """어느 채널이 몇 위로 올렸는가가 근거 기록에 남는다.

    채널 이름만으로는 "함께 걸렸다"와 "이 채널이 끌어올렸다"를 구분하지 못한다.
    다만 이 값은 **모델에게 보내는 라운드 payload 에는 넣지 않는다** — 그쪽은
    예산이 걸린 자리이고 모델이 쓰지 않는 값이다.
    """
    document, documents = _document(tmp_path)
    try:
        encoder, _state = semantic_module.load_encoder(
            True, cache=embedding_cache.NullCache()
        )
        try:
            result = search.search_document(
                document, queries=[QUERY], semantic_encoder=encoder
            )
        finally:
            encoder.close()
        hit = _target_hit(result)
        assert hit is not None
        assert CHANNEL_SEMANTIC in hit.ranks

        # 모델에게 가는 직렬화에는 순위가 없다. 예산을 먹지 않아야 한다.
        payload = hit.to_dict(include_text=False)
        assert "channels" in payload
        assert "ranks" not in payload
    finally:
        retrieval.close_documents(documents)


# ------------------------- 캐시 정리와 순위 계산 (2026-08-27)


def _fill(cache, docs=3, vectors_per_doc=50, dim=100):
    """문헌마다 시간차를 두고 채운다. 앞에 넣은 것이 더 오래된 것이다."""
    import time as _time

    keys = {}
    for n in range(docs):
        meta = {**META, "pdf_sha256": f"doc{n}"}
        key = embedding_cache.fingerprint(meta, "m", "r")
        keys[n] = key
        vectors = {f"c{i}": [float(i)] * dim for i in range(vectors_per_doc)}
        cache.put_many(key, vectors, {k: "d" for k in vectors})
        _time.sleep(0.02)
    return keys


def test_cache_prunes_least_recently_used_first(tmp_path) -> None:
    """상한을 넘으면 오래 안 쓴 것부터 지운다.

    크기순으로 지우면 큰 문헌이 매번 먼저 나가는데, 큰 문헌일수록 다시
    임베딩하는 비용이 크다.
    """
    cache = embedding_cache.EmbeddingCache(tmp_path / "lru.sqlite3")
    try:
        keys = _fill(cache)
        want = {f"c{i}": "d" for i in range(50)}
        total = cache.total_bytes()
        assert total > 0

        # doc0 을 다시 읽어 「최근 사용」으로 만든다.
        cache.get_many(keys[0], want)
        removed = cache.prune(int(total * 0.7))

        assert removed > 0
        assert cache.total_bytes() <= int(total * 0.7)
        # 최근 쓴 것은 남고, 가장 오래된 것이 줄었다.
        assert len(cache.get_many(keys[0], want)) == 50
        assert len(cache.get_many(keys[1], want)) < 50
    finally:
        cache.close()


def test_prune_removes_only_what_is_needed(tmp_path) -> None:
    """상한을 조금 넘었을 뿐인데 캐시를 통째로 비우면 다음 실행이 전부 다시 돈다."""
    cache = embedding_cache.EmbeddingCache(tmp_path / "trim.sqlite3")
    try:
        _fill(cache)
        total = cache.total_bytes()
        cache.prune(total - 1_000)
        remaining = cache.total_bytes()
        assert 0 < remaining <= total - 1_000
        # 필요한 만큼만 지웠다 — 여유를 크게 남기지 않는다.
        assert remaining > (total - 1_000) * 0.5
    finally:
        cache.close()


def test_prune_below_the_cap_does_nothing(tmp_path) -> None:
    cache = embedding_cache.EmbeddingCache(tmp_path / "under.sqlite3")
    try:
        _fill(cache, docs=1)
        total = cache.total_bytes()
        assert cache.prune(total * 10) == 0
        assert cache.prune(0) == 0  # 0 = 정리하지 않음
        assert cache.total_bytes() == total
    finally:
        cache.close()


def test_prune_failure_does_not_break_search(tmp_path, monkeypatch) -> None:
    """정리가 실패해도 검색은 돈다. 캐시는 정확성의 근거가 아니다."""
    import sqlite3

    cache = embedding_cache.EmbeddingCache(tmp_path / "broken.sqlite3")
    _fill(cache, docs=1)

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("정리 실패")

    monkeypatch.setattr(cache, "total_bytes", boom)
    # 예외가 올라오지 않는다.
    assert cache.prune(1) == 0
    assert cache.error
    cache.close()


def test_close_with_a_cap_prunes_once(tmp_path) -> None:
    """정리는 실행이 끝날 때 한 번만 한다. 검색 경로에 넣으면 캐시가 무의미해진다."""
    cache = embedding_cache.EmbeddingCache(tmp_path / "close.sqlite3")
    _fill(cache)
    total = cache.total_bytes()
    cache.close(max_bytes=int(total * 0.5))

    reopened = embedding_cache.EmbeddingCache(tmp_path / "close.sqlite3")
    try:
        assert 0 < reopened.total_bytes() <= int(total * 0.5)
    finally:
        reopened.close()


def test_old_cache_files_gain_the_column_instead_of_being_discarded(tmp_path) -> None:
    """이 기능이 없던 캐시 파일을 버리면 다음 실행이 전부 다시 임베딩한다."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE embeddings ("
        "fingerprint TEXT NOT NULL, chunk_id TEXT NOT NULL, "
        "text_sha256 TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL, "
        "PRIMARY KEY (fingerprint, chunk_id))"
    )
    key = embedding_cache.fingerprint(META, "m", "r")
    legacy.execute(
        "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?)",
        (key, "c0", embedding_cache.text_digest("본문"), 2, b"\x00" * 8),
    )
    legacy.commit()
    legacy.close()

    cache = embedding_cache.EmbeddingCache(path)
    try:
        assert cache.enabled is True
        assert not cache.error
        # 옛 행이 그대로 읽힌다.
        found = cache.get_many(key, {"c0": embedding_cache.text_digest("본문")})
        assert "c0" in found
    finally:
        cache.close()


# ------------------------------------------------------ 순위 계산


def _rank_inputs(chunks: int, queries: int, dim: int = 64):
    import random

    random.seed(20260827)
    make = lambda n: [  # noqa: E731
        [random.random() - 0.5 for _ in range(dim)] for _ in range(n)
    ]
    return make(chunks), make(queries)


def test_fast_ranking_matches_the_pure_python_order() -> None:
    """빠른 경로가 후보 **순서**를 바꾸면 안 된다. 속도만 다르다."""
    vectors, queries = _rank_inputs(400, 5)
    fast = semantic_module.best_scores(vectors, queries)
    slow = [
        max((semantic_module.cosine(v, q) for q in queries), default=0.0)
        for v in vectors
    ]
    assert len(fast) == len(vectors)
    # 값은 부동소수점 오차 범위 안에서 같다.
    assert max(abs(a - b) for a, b in zip(fast, slow)) < 1e-12
    # 그리고 그 오차가 순서를 뒤집지 않는다.
    order = lambda s: sorted(range(len(s)), key=lambda i: (-s[i], i))  # noqa: E731
    assert order(fast) == order(slow)


def test_fast_ranking_falls_back_when_numpy_is_missing(monkeypatch) -> None:
    """NumPy 가 없어도 결과는 같다. 느릴 뿐이다."""
    import builtins

    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return real_import(name, *args, **kwargs)

    vectors, queries = _rank_inputs(50, 3)
    expected = semantic_module.best_scores(vectors, queries)
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    fallback = semantic_module.best_scores(vectors, queries)
    assert max(abs(a - b) for a, b in zip(expected, fallback)) < 1e-12


@pytest.mark.parametrize(
    "vectors,queries,expected",
    [
        ([], [[1.0]], []),
        ([[1.0]], [], [0.0]),
        ([[0.0, 0.0]], [[1.0, 0.0]], [0.0]),  # 0 벡터는 0.0
        ([[1.0, 2.0]], [[1.0]], [0.0]),  # 차원 불일치도 0.0
    ],
)
def test_fast_ranking_edge_cases_follow_the_pure_python_rule(
    vectors, queries, expected
) -> None:
    assert semantic_module.best_scores(vectors, queries) == expected
