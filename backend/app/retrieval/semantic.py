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

import math
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

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "model": self.model if self.enabled else None,
            "revision": self.revision if self.enabled else None,
            "cache_state": self.cache_state,
            "reason": self.reason,
            "notes": list(self.notes),
        }


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


class SemanticEncoder:
    """sentence-transformers 어댑터. 없으면 만들어지지 않는다."""

    def __init__(self, model) -> None:
        self._model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, batch_size=BATCH_SIZE, convert_to_numpy=False
        )
        return [[float(value) for value in vector] for vector in vectors]


def load_encoder(enabled: bool) -> tuple[SemanticEncoder | None, SemanticState]:
    """의미 검색 어댑터를 만든다. 실패는 예외가 아니라 상태로 돌려준다.

    호출부는 encoder 가 None 이면 그냥 키워드 채널만 쓰면 된다. 실패 사유는
    state.reason 에 있고 그대로 실행 기록과 보고서에 실린다.
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

    try:
        model = SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION)
    except Exception as exc:
        state.reason = (
            f"의미 검색 모델을 열지 못해 건너뛰었습니다({type(exc).__name__}). "
            "오프라인이면 모델 캐시가 필요합니다. 키워드 검색만으로 진행했습니다."
        )
        state.cache_state = "unavailable"
        return None, state

    state.active = True
    state.cache_state = "loaded"
    state.reason = ""
    return SemanticEncoder(model), state
