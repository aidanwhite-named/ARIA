"""선택적 의미 검색 채널.

기본은 **꺼짐**이고, `requirements.txt` 에도 없다. 이유는 docs/adr-0001 에
적었다 — sentence-transformers 는 torch 를 끌고 오고(수 GB), 모델은 최초 실행에
네트워크 다운로드가 필요하다. 두 조건 모두 "오프라인에서도 키워드 검색은
동작해야 한다"는 요구와 충돌한다.

그래서 여기서는 어댑터 경계만 둔다.

- 설정에서 켜지 않으면 아예 시도하지 않는다.
- 켜져 있어도 import 나 모델 로딩이 실패하면 키워드 채널만으로 계속 간다.
- 어느 경우든 **왜 비활성인지**가 manifest 와 보고서에 남는다. 조용히 빠지면
  사용자는 의미 검색까지 돌린 결과라고 믿게 된다.

벡터 DB 를 쓰지 않는다. 문헌 하나가 수천 청크 규모라 순수 파이썬 코사인
정렬로 충분하고, 필요성이 증명되지 않은 인프라를 넣지 않는다.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import time
from dataclasses import dataclass, field

# 모델 이름과 revision 을 고정한다. 태그를 고정하지 않으면 어느 날 조용히 다른
# 가중치가 내려와 같은 문헌에서 다른 후보가 나온다.
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "8d6b950845285729817bf8e1af1861502c2fed0c"

# 한 번에 임베딩할 청크 수. 메모리 사용량을 예측 가능하게 둔다.
BATCH_SIZE = 32


@dataclass
class SemanticState:
    """의미 검색이 이번 실행에서 실제로 돌았는가.

    enabled 는 사용자가 켰는가, active 는 실제로 동작했는가다. 둘을 하나로
    합치면 "켰는데 모델이 없어서 안 돌았다"가 화면에서 사라진다.
    """

    enabled: bool = False
    active: bool = False
    model: str = MODEL_NAME
    revision: str = MODEL_REVISION
    cache_state: str = "not_checked"
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    # 모델을 로컬 캐시에서 열었는가, 네트워크에서 받았는가. cache_state 는
    # "loaded" 하나로 두고(비활성 사유와 섞이면 판정이 흐려진다) 출처는 여기에
    # 따로 남긴다.
    model_source: str = ""
    # 이번 실행의 임베딩 비용. 쿼리와 문헌을 나눠 잰다 — 합쳐서 재면 "문헌을
    # 매번 다시 임베딩하고 있다"는 사실이 쿼리 비용에 묻힌다.
    stats: EmbeddingStats | None = None

    def to_dict(self) -> dict:
        payload = {
            "enabled": self.enabled,
            "active": self.active,
            "model": self.model if self.enabled else None,
            "revision": self.revision if self.enabled else None,
            "cache_state": self.cache_state,
            "reason": self.reason,
            "notes": list(self.notes),
        }
        if self.model_source:
            payload["model_source"] = self.model_source
        if self.stats is not None:
            payload["embedding"] = self.stats.to_dict()
        return payload


@dataclass
class EmbeddingStats:
    """임베딩 호출 계량. 캐시가 실제로 듣고 있는지를 숫자로 남긴다."""

    # 문헌 청크
    document_encoded: int = 0
    document_cache_hits: int = 0
    document_seconds: float = 0.0
    # 검색어
    query_encoded: int = 0
    query_cache_hits: int = 0
    query_seconds: float = 0.0
    cache_path: str = ""
    cache_error: str = ""

    def to_dict(self) -> dict:
        payload = {
            "document_encoded": self.document_encoded,
            "document_cache_hits": self.document_cache_hits,
            "document_seconds": round(self.document_seconds, 3),
            "query_encoded": self.query_encoded,
            "query_cache_hits": self.query_cache_hits,
            "query_seconds": round(self.query_seconds, 3),
        }
        if self.cache_path:
            payload["cache_path"] = self.cache_path
        if self.cache_error:
            payload["cache_error"] = self.cache_error
        return payload


DISABLED_BY_SETTING = (
    "의미 검색이 설정에서 꺼져 있습니다. 이번 실행은 키워드 검색 채널"
    "(정확 문구 · BM25 · 부분문자 · 숫자/도면부호)만 사용했습니다."
)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def best_scores(vectors: list, query_vectors: list) -> list[float]:
    """청크마다 「가장 가까운 검색어와의 코사인」을 돌려준다.

    순서는 vectors 와 같다. 호출부가 zip 으로 묶으므로 어긋나면 다른 청크의
    점수로 순위를 매기게 된다.

    **NumPy 가 있으면 행렬 연산으로 한 번에 계산한다.** 실측(384차원):

        청크    검색어   순수 파이썬 1회   라운드 6회
        1,000      5          168 ms         1.01 s
        5,000      5          857 ms         5.14 s

    임베딩을 캐시한 뒤 남는 시간이 이것이었다. 문헌 하나가 수천 청크인 특허에서
    라운드마다 다시 도는 비용이라 그냥 두기 어렵다.

    NumPy 가 없으면 순수 파이썬으로 같은 계산을 한다. sentence-transformers 를
    설치하면 NumPy 도 함께 오지만, 이 함수가 그것을 전제하지는 않는다 — 없으면
    느릴 뿐 결과는 같다.

    **두 경로의 결과가 같아야 한다.** float64 로 계산하고, 0 벡터는 양쪽 모두
    0.0 으로 둔다. 회귀는 tests/test_semantic_search.py 가 고정한다.
    """
    if not vectors or not query_vectors:
        return [0.0] * len(vectors)
    try:
        import numpy as np
    except Exception:
        return [
            max((cosine(vector, q) for q in query_vectors), default=0.0)
            for vector in vectors
        ]

    try:
        matrix = np.asarray(vectors, dtype=np.float64)
        queries = np.asarray(query_vectors, dtype=np.float64)
        if matrix.ndim != 2 or queries.ndim != 2 or matrix.shape[1] != queries.shape[1]:
            # 차원이 어긋나면 순수 파이썬 쪽 규칙(0.0)을 그대로 따른다.
            return [
                max((cosine(vector, q) for q in query_vectors), default=0.0)
                for vector in vectors
            ]
        matrix_norm = np.sqrt((matrix * matrix).sum(axis=1))
        query_norm = np.sqrt((queries * queries).sum(axis=1))
        denominator = np.outer(matrix_norm, query_norm)
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(denominator > 0, matrix @ queries.T / denominator, 0.0)
        return [float(value) for value in scores.max(axis=1)]
    except Exception:
        # 어떤 이유로든 실패하면 느린 쪽으로 간다. 검색이 멈추지는 않는다.
        return [
            max((cosine(vector, q) for q in query_vectors), default=0.0)
            for vector in vectors
        ]


class SemanticEncoder:
    """sentence-transformers 어댑터. 없으면 만들어지지 않는다.

    캐시를 이 안에 둔다. 호출부(search._semantic_channel)가 "캐시가 있으면
    이렇게, 없으면 저렇게"를 분기하면 두 경로 중 하나만 검증되고, 검증되지 않은
    쪽이 조용히 다른 후보를 내놓는다. 캐시 유무는 여기서만 갈린다.
    """

    def __init__(
        self, model, stats: EmbeddingStats | None = None, cache=None
    ) -> None:
        self._model = model
        self.stats = stats if stats is not None else EmbeddingStats()
        if cache is None:
            from .embedding_cache import NullCache

            cache = NullCache()
        self._cache = cache
        self.stats.cache_path = str(getattr(cache, "path", "") or "")
        if getattr(cache, "error", ""):
            self.stats.cache_error = cache.error
        # 같은 검색어가 라운드마다 되풀이된다. 실행 안에서만 기억한다 —
        # 디스크에 두면 모델·revision 이 바뀌었을 때 무효화 규칙을 하나 더
        # 관리해야 하는데, 검색어는 몇 개뿐이라 아낄 것이 없다.
        self._query_memo: dict[str, list[float]] = {}

    def close(self, cache_max_bytes: int = 0) -> None:
        """캐시 연결을 놓아준다. 상한이 있으면 그때 한 번만 정리한다."""
        self._cache.close(cache_max_bytes)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, batch_size=BATCH_SIZE, convert_to_numpy=False
        )
        return [[float(value) for value in vector] for vector in vectors]

    # ------------------------------------------------------------ 검색어

    def query_vectors(self, queries: list[str]) -> list[list[float]]:
        """검색어 임베딩. 이번 실행에서 본 것은 다시 계산하지 않는다."""
        missing = [q for q in queries if q not in self._query_memo]
        # dict 로 중복을 없앤 뒤 센다. 같은 검색어가 한 요청에 두 번 오면
        # 계산은 한 번이므로 통계도 한 번이어야 한다.
        unique = list(dict.fromkeys(missing))
        if unique:
            started = time.perf_counter()
            vectors = self.encode(unique)
            self.stats.query_seconds += time.perf_counter() - started
            self.stats.query_encoded += len(unique)
            self._query_memo.update(zip(unique, vectors))
        self.stats.query_cache_hits += len(queries) - len(unique)
        return [self._query_memo[q] for q in queries]

    # ------------------------------------------------------------ 문헌 청크

    def document_vectors(self, index_meta: dict, chunks: list) -> list[list[float]]:
        """청크 임베딩. 캐시에 있으면 꺼내 쓰고 없는 것만 계산한다.

        돌려주는 순서는 chunks 와 같다. 호출부가 zip 으로 묶으므로 순서가
        어긋나면 다른 청크의 벡터로 점수를 매기게 된다.
        """
        from . import embedding_cache

        key = embedding_cache.fingerprint(index_meta, MODEL_NAME, MODEL_REVISION)
        digests = {
            chunk.chunk_id: embedding_cache.text_digest(chunk.text) for chunk in chunks
        }
        cached = self._cache.get_many(key, digests)
        if getattr(self._cache, "error", ""):
            self.stats.cache_error = self._cache.error

        missing = [chunk for chunk in chunks if chunk.chunk_id not in cached]
        if missing:
            started = time.perf_counter()
            fresh = self.encode([chunk.text for chunk in missing])
            self.stats.document_seconds += time.perf_counter() - started
            self.stats.document_encoded += len(missing)
            produced = {
                chunk.chunk_id: vector for chunk, vector in zip(missing, fresh)
            }
            cached.update(produced)
            self._cache.put_many(key, produced, digests)
            if getattr(self._cache, "error", ""):
                self.stats.cache_error = self._cache.error

        self.stats.document_cache_hits += len(chunks) - len(missing)
        return [cached[chunk.chunk_id] for chunk in chunks]



# huggingface_hub 을 이번 호출 동안만 오프라인으로 묶는 이름. 라이브러리가
# `from .constants import HF_HUB_OFFLINE` 로 값을 복사해 간 모듈이 여럿이라
# constants 하나만 바꾸면 그 복사본들이 그대로 네트워크를 탄다.
_HUB_OFFLINE_FLAG = "HF_HUB_OFFLINE"


@contextlib.contextmanager
def _hub_offline():
    """이 블록 안에서는 huggingface_hub 이 네트워크를 쓰지 않는다.

    왜 필요한가. sentence-transformers 3.3.1 은 `local_files_only=True` 를 줘도
    모델 카드를 채우려고 `HfApi.model_info` 를 부른다. 그 호출은 캐시를 보지
    않으므로, 캐시가 완전히 채워진 PC 에서도 저장소에 닿지 못하면 로딩 전체가
    실패한다. 실측: 458 MB 캐시가 있는데도 5회 재시도 끝에 SSLError 로 77초를
    쓰고 끝났다. 오프라인 플래그를 세우면 같은 로딩이 0.9초다.

    끝나면 원래 값으로 되돌린다. ARIA 는 이 프로세스에서 다른 일도 하므로
    전역 상태를 영구히 바꾸지 않는다.
    """
    previous_env = os.environ.get(_HUB_OFFLINE_FLAG)
    os.environ[_HUB_OFFLINE_FLAG] = "1"
    patched: list[tuple[object, object]] = []
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith(
            "huggingface_hub"
        ):
            continue
        if hasattr(module, _HUB_OFFLINE_FLAG):
            patched.append((module, getattr(module, _HUB_OFFLINE_FLAG)))
            try:
                setattr(module, _HUB_OFFLINE_FLAG, True)
            except Exception:
                patched.pop()
    try:
        yield
    finally:
        for module, value in patched:
            with contextlib.suppress(Exception):
                setattr(module, _HUB_OFFLINE_FLAG, value)
        if previous_env is None:
            os.environ.pop(_HUB_OFFLINE_FLAG, None)
        else:
            os.environ[_HUB_OFFLINE_FLAG] = previous_env


def load_encoder(
    enabled: bool, cache=None, allow_download: bool = True
) -> tuple[SemanticEncoder | None, SemanticState]:
    """의미 검색 어댑터를 만든다. 실패는 예외가 아니라 상태로 돌려준다.

    호출부는 encoder 가 None 이면 그냥 키워드 채널만 쓰면 된다. 실패 사유는
    state.reason 에 있고 그대로 실행 기록과 보고서에 실린다.

    **로컬 캐시를 먼저 본다.** sentence-transformers 는 캐시가 다 채워져 있어도
    기본 경로에서 저장소에 HEAD 를 날리고, 그 요청이 실패하면(오프라인, TLS 검사
    장비, 저장소 장애) 캐시를 두고도 로딩 전체를 실패시킨다. 실측으로 확인했다 —
    458 MB 캐시가 있는 PC 에서 adapter_config.json HEAD 가 막히자 5회 재시도 뒤
    SSLError 로 끝났다. 그래서 캐시가 있으면 네트워크를 아예 쓰지 않는다.
    """
    state = SemanticState(enabled=enabled)
    if not enabled:
        state.reason = DISABLED_BY_SETTING
        state.cache_state = "not_checked"
        return None, state

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # ImportError 외에 로딩 단계 오류도 있다
        state.reason = (
            "sentence-transformers 를 불러오지 못해 의미 검색을 건너뛰었습니다: "
            f"{type(exc).__name__}. 키워드 검색만으로 진행했습니다. 설치하려면 "
            "backend/requirements-semantic.txt 를 사용하십시오."
        )
        state.cache_state = "not_installed"
        return None, state

    # 모델은 **항상 로컬 디렉터리에서** 연다.
    #
    # 허브 ID 로 바로 열면 transformers 4.57 이 토크나이저를 만들면서
    # `is_base_mistral()` → `HfApi.model_info()` 로 저장소에 묻는다. 캐시가 다
    # 채워져 있어도 그 호출은 캐시를 보지 않으므로, 저장소에 닿지 못하는 PC 에서는
    # 로딩 전체가 실패한다(실측: SSL 검사 장비가 있는 망에서 5회 재시도 뒤 77초
    # 만에 SSLError). 로컬 경로로 열면 그 분기 자체가 `_is_local` 로 걸러진다.
    #
    # snapshot_download 는 revision 을 그대로 받는다. 고정한 revision 이 로컬
    # 경로로 바뀌는 과정에서 풀리지 않는다.
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        state.reason = (
            "huggingface_hub 을 불러오지 못해 의미 검색을 건너뛰었습니다: "
            f"{type(exc).__name__}. 키워드 검색만으로 진행했습니다."
        )
        state.cache_state = "not_installed"
        return None, state

    snapshot = ""
    offline_error: Exception | None = None
    try:
        with _hub_offline():
            snapshot = snapshot_download(
                MODEL_NAME, revision=MODEL_REVISION, local_files_only=True
            )
        state.model_source = "local_cache"
    except Exception as exc:
        # 캐시에 없다.
        offline_error = exc
        if not allow_download:
            # 받지 말라고 했다. 여기서 멈춘다.
            #
            # 테스트 수집처럼 "쓸 수 있는가"만 묻는 자리가 있다. 그 자리에서
            # 458 MB 를 받기 시작하면, 모델이 없는 깨끗한 환경의 테스트가
            # 네트워크와 다운로드에 의존하게 된다. 확인과 준비를 갈라 둔다.
            state.reason = (
                "의미 검색 모델이 로컬 캐시에 없습니다"
                f"({type(exc).__name__}). 이 호출은 다운로드를 하지 않도록 "
                "설정되어 있어 키워드 검색만으로 진행했습니다."
            )
            state.cache_state = "unavailable"
            return None, state
        try:
            snapshot = snapshot_download(MODEL_NAME, revision=MODEL_REVISION)
            state.model_source = "downloaded"
        except Exception as download_error:
            state.reason = (
                "의미 검색 모델을 내려받지 못해 건너뛰었습니다"
                f"({type(download_error).__name__}). 오프라인이면 모델 캐시가 "
                "필요합니다. 키워드 검색만으로 진행했습니다."
            )
            state.cache_state = "unavailable"
            state.notes.append(
                f"로컬 캐시 조회 실패: {type(offline_error).__name__}"
            )
            return None, state

    try:
        with _hub_offline():
            model = SentenceTransformer(snapshot, local_files_only=True)
    except Exception as exc:
        state.reason = (
            f"의미 검색 모델을 열지 못해 건너뛰었습니다({type(exc).__name__}). "
            "모델 캐시가 손상되었을 수 있습니다. 키워드 검색만으로 진행했습니다."
        )
        state.cache_state = "unavailable"
        return None, state

    state.notes.append(f"모델 경로: {snapshot}")
    state.active = True
    state.cache_state = "loaded"
    state.reason = ""
    if state.model_source == "downloaded":
        state.notes.append(
            "모델을 로컬 캐시에서 찾지 못해 이번 실행에서 내려받았습니다. "
            "다음 실행부터는 캐시에서 엽니다."
        )
    if cache is None:
        from .embedding_cache import EmbeddingCache

        cache = EmbeddingCache()
    encoder = SemanticEncoder(model, EmbeddingStats(), cache)
    state.stats = encoder.stats
    return encoder, state
