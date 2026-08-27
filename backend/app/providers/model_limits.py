"""모델별 입력 토큰 예산.

두 가지 한도를 구분한다. 이름을 섞으면 둘 다 뜻이 흐려진다.

  Provider 전송 하드 한도 (Provider.max_input_bytes)
      그 **CLI** 가 모델에 넘기기 전에 입력을 자르는 지점. agy 만 선언한다.
      사용자가 끌 수 없고, 넘겨 보내면 뒷부분이 조용히 사라진 채 종료 코드 0
      으로 "성공"한다.

  모델 컨텍스트 한도 (이 파일)
      그 **모델** 이 받을 수 있는 토큰 수. codex, claude 처럼 CLI 가 자르지
      않는 Provider 의 실제 한계다. 넘기면 CLI 나 API 가 거절한다 — 조용히
      잘리지는 않지만, 검색 비용을 다 쓴 뒤 실패한다.

agy 에는 이 파일을 적용하지 않는다. 그쪽은 CLI 가 180,000 bytes 에서 자르고,
그 값이 어떤 현대 모델의 컨텍스트보다도 작기 때문에 항상 바이트 한도가 먼저
걸린다. 두 규칙을 겹쳐 걸면 판정 사유만 복잡해지고 결과는 같다.

**ARIA 는 모델 한도를 추측하지 않는다.**

공급사가 공개한 숫자를 코드에 박아 두면 모델이 바뀔 때마다 조용히 틀린다. 그
틀림은 "실행이 실패했다"가 아니라 "전체를 넣을 수 있었는데 좁혀 읽었다"거나 그
반대로 나타나므로 알아차리기 어렵다. 그래서 기본 표는 **비어 있고**, 아는 값은
설정(`model_context_tokens`)으로 넣는다. 모르는 모델에는 보수적 대체값을 쓰고
그 사실을 판정 사유에 남긴다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 값을 어디서 얻었는가. 판정 사유에 그대로 실린다.
SOURCE_CONFIGURED = "configured"
SOURCE_FALLBACK = "fallback"

# 토큰 추정에 쓰는 UTF-8 바이트/토큰 비율.
#
# 실제 비율은 토크나이저와 언어에 따라 다르다. 한국어 UTF-8 은 음절 하나가
# 3 bytes 이고 서브워드 하나가 보통 음절 1~3개이므로 대략 4~9 bytes/token,
# 영문은 대략 4 bytes/token 이다. 여기서 2 를 쓰는 것은 **일부러 실제보다 많이
# 세기 위해서다** — 틀렸을 때 좁아지는 쪽이, 다 넣었다가 모델에 거절당해 검색
# 비용을 날리는 쪽보다 낫다.
#
# 정확한 토큰 수를 알려면 모델의 토크나이저가 필요한데, ARIA 는 CLI 로만
# 대화하므로 그것을 얻을 수 없다. 이 값은 추정이며 그 사실이 판정 사유에 남는다.
CONSERVATIVE_BYTES_PER_TOKEN = 2

# 공급사 공개 값을 코드에 박지 않는다. 위 docstring 참조.
KNOWN_CONTEXT_TOKENS: dict[str, int] = {}


@dataclass(frozen=True)
class TokenBudget:
    """이 실행이 쓸 수 있는 입력 토큰과 그 근거."""

    context_tokens: int
    reserve_tokens: int
    source: str
    model: str = ""

    @property
    def input_tokens(self) -> int:
        """출력·추론 자리를 뺀 입력 예산. 최소 0."""
        return max(0, self.context_tokens - self.reserve_tokens)

    @property
    def is_estimated(self) -> bool:
        """모델 한도를 확인하지 못하고 대체값을 쓴 것인가."""
        return self.source == SOURCE_FALLBACK

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "context_tokens": self.context_tokens,
            "reserve_tokens": self.reserve_tokens,
            "input_tokens": self.input_tokens,
            "source": self.source,
        }


def _lookup(overrides: dict, provider_id: str, model: str) -> int | None:
    """재정의 표에서 찾는다. 구체적인 키를 먼저 본다.

    `provider:model` → `model` 순이다. 같은 이름의 모델을 여러 Provider 가
    제공할 수 있고(agy 가 claude 계열 모델을 노출한다), 그때 한도가 다를 수
    있기 때문이다.
    """
    if not isinstance(overrides, dict):
        overrides = {}
    for key in (f"{provider_id}:{model}", model):
        if not key or key == ":":
            continue
        value = overrides.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def token_budget(
    *,
    provider_id: str,
    model: str | None,
    overrides: dict | None = None,
    reserve_tokens: int,
    fallback_context_tokens: int,
) -> TokenBudget:
    """이 (Provider, 모델) 조합의 입력 토큰 예산.

    확인 순서는 하나뿐이다 — 설정에 있으면 그 값, 없으면 보수적 대체값.
    중간에 "아마 이 정도일 것"이 끼어들 자리는 없다.
    """
    name = str(model or "").strip()
    configured = _lookup(overrides or {}, provider_id, name)
    if configured is not None:
        return TokenBudget(
            context_tokens=configured,
            reserve_tokens=reserve_tokens,
            source=SOURCE_CONFIGURED,
            model=name,
        )
    return TokenBudget(
        context_tokens=fallback_context_tokens,
        reserve_tokens=reserve_tokens,
        source=SOURCE_FALLBACK,
        model=name,
    )


def estimate_tokens(*texts: str) -> int:
    """이 텍스트가 차지할 입력 토큰 수의 **보수적 상한 추정**.

    실제보다 많게 센다. 적게 세면 "들어간다"고 판정한 입력이 모델에 거절되고,
    그때는 이미 검색 비용을 다 쓴 뒤다.
    """
    total = sum(len(text.encode("utf-8")) for text in texts if text)
    return math.ceil(total / CONSERVATIVE_BYTES_PER_TOKEN)


def describe(budget: TokenBudget) -> str:
    """판정 사유에 붙일 한 문장. 추정값이면 그 사실을 반드시 적는다."""
    if budget.is_estimated:
        return (
            f"모델 {budget.model or '(미지정)'} 의 컨텍스트 한도를 확인하지 "
            f"못해 보수적 대체값 {budget.context_tokens:,} 토큰을 썼습니다. "
            f"출력·추론용 {budget.reserve_tokens:,} 토큰을 빼면 입력 예산은 "
            f"{budget.input_tokens:,} 토큰입니다. 환경설정의 「모델 컨텍스트 "
            "한도」에 실제 값을 넣으면 그 값을 씁니다."
        )
    return (
        f"모델 {budget.model} 의 컨텍스트 한도 {budget.context_tokens:,} 토큰에서 "
        f"출력·추론용 {budget.reserve_tokens:,} 토큰을 뺀 {budget.input_tokens:,} "
        "토큰을 입력 예산으로 씁니다(환경설정 값)."
    )
