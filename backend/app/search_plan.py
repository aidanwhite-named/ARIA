"""검색 시작 전에 만드는 **정규화된 내부 검색 계획**.

무엇인가
--------
검색을 시작하기 전에 ARIA 가 자기 스키마로 만드는 계획이다. 입력은 사용자
전략 프롬프트와 청구항(그리고 있으면 미대응 구성)이고, 출력은 검색어·동의어·
영문어·분류코드 후보·검색 대상 구성의 구조화 목록이다.

왜 내부 계약인가
----------------
이 스키마는 사용자가 편집하는 프롬프트에 노출되지 않고 거기에 의존하지도
않는다. 사용자 프롬프트가 "이런 JSON 을 내놔"라고 적어야 계획이 생기는 구조면,
프롬프트를 바꾸는 것만으로 계획이 사라지고 그와 함께 감사 기록의 절반이
사라진다. 계획은 프로그램이 만들고 프로그램이 읽는다.

무엇을 하지 않는가
------------------
**모델을 부르지 않는다.** 이 단계는 관측 가능한 사실만 다룬다 — 청구항 문언에
실제로 있는 낱말, 전략 프롬프트에 사용자가 실제로 적은 용어, 두 곳에서 실제로
발견된 분류코드 표기. 모델에게 계획을 물으면 그 답을 검증할 방법이 없고, 검증할
수 없는 것을 계획이라고 저장하면 "ARIA 가 관측한 사실"과 "모델이 해석한 내용"의
경계가 다시 무너진다.

그래서 이 계획은 검색을 대신 설계하지 않는다. 실제 검색 전략은 모델이 사용자
프롬프트를 읽고 세우며, 이 계획은 두 가지 일만 한다.

  1. 감사 — 무엇을 재료로 검색에 들어갔는지 기록한다. 실제로 나간 검색어는
     별도로(observed) 기록되므로, 둘을 대조하면 "계획한 것"과 "실행한 것"을
     구분할 수 있다.
  2. 대체 질의 — 모델의 검색어를 관측하지 못한 실행에서 서지 채널이 쓸 질의를
     제공한다. 그것이 없으면 그 채널은 통째로 건너뛰어졌다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLAN_VERSION = 1

# 계획 항목의 출처. 관측한 자리를 잃지 않는다.
SOURCE_CLAIM = "claim"
SOURCE_STRATEGY = "strategy_prompt"
SOURCE_FOCUS = "search_focus"

# 질의의 목적. 실제로 나간 검색어와 대조할 때 축이 된다.
PURPOSE_COMBINED = "combined"
PURPOSE_COMPONENT = "component"
PURPOSE_TERM = "term"

_MAX_TERMS = 40
_MAX_QUERIES = 12
_MAX_COMPONENTS = 20
_TEXT_LIMIT = 400

# IPC·CPC 표기. "G06F 3/041", "G06F3/041", "H04N 19/00" 을 모두 잡는다.
_CLASSIFICATION = re.compile(r"\b([A-H][0-9]{2}[A-Z])\s?([0-9]{1,4}/[0-9]{2,6})\b")

# 영문 기술어. 두 글자 이상이고 순수 로마자인 토큰.
_ENGLISH = re.compile(r"\b[A-Za-z][A-Za-z0-9\-]{2,}\b")

# 한국어 기술어. 조사를 떼기 위해 명사 뭉치만 남긴다.
_KOREAN = re.compile(r"[가-힣]{2,}")

# 청구항 문언에서 기술적 식별력이 없는 낱말. 검색어로 뽑으면 잡음만 늘어난다.
_KOREAN_STOPWORDS = frozenset(
    {
        "청구항", "포함", "구비", "특징", "발명", "장치", "방법", "시스템",
        "상기", "하는", "되는", "위한", "이상", "이하", "또는", "그리고",
        "구성", "수단", "부재", "제어", "연결", "기재", "따른", "대한",
        "있는", "없는", "가능", "동작", "수행", "제공", "이때", "여기서",
    }
)

_ENGLISH_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was",
        "which", "wherein", "said", "claim", "comprising", "configured",
        "least", "one", "such", "into", "based", "system", "method",
        "device", "apparatus", "including", "having", "each", "when",
    }
)


@dataclass(frozen=True)
class PlannedTerm:
    """계획에 오른 낱말 하나. 어디서 왔는지를 함께 든다."""

    text: str
    kind: str
    source: str

    def as_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "source": self.source}


@dataclass(frozen=True)
class PlannedQuery:
    """이 계획이 제안하는 질의. 실제로 나간 검색어와 다른 축이다."""

    id: str
    purpose: str
    text: str
    terms: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "purpose": self.purpose,
            "text": self.text,
            "terms": list(self.terms),
        }


@dataclass(frozen=True)
class SearchPlan:
    """정규화된 검색 계획. 이 모양 그대로 감사 기록에 들어간다."""

    version: int = PLAN_VERSION
    strategy_prompt_id: str = ""
    strategy_prompt_sha256: str = ""
    terms: tuple[PlannedTerm, ...] = ()
    classifications: tuple[str, ...] = ()
    components: tuple[dict, ...] = ()
    queries: tuple[PlannedQuery, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def query_texts(self) -> list[str]:
        return [query.text for query in self.queries]

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            # 이 계획을 어떤 전략 본문에서 만들었는가. 전략을 바꾸면 계획도
            # 바뀌므로, 계획만 보고 "같은 조건이었다"고 말할 수 없게 남긴다.
            "strategy_prompt_id": self.strategy_prompt_id,
            "strategy_prompt_sha256": self.strategy_prompt_sha256,
            "generator": "aria_deterministic_v1",
            "terms": [term.as_dict() for term in self.terms],
            "classifications": list(self.classifications),
            "components": [dict(item) for item in self.components],
            "queries": [query.as_dict() for query in self.queries],
            "notes": list(self.notes),
        }


def _text(value, limit: int = _TEXT_LIMIT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _classifications(*sources: str) -> list[str]:
    found: list[str] = []
    for source in sources:
        for prefix, number in _CLASSIFICATION.findall(source or ""):
            code = f"{prefix} {number}"
            if code not in found:
                found.append(code)
    return found[:_MAX_TERMS]


# 낱말 끝에 붙는 조사·어미. 형태소 분석기 없이 뒤에서 잘라 낸다. 정확한 분해가
# 아니라 **검색어로 쓸 수 있는 모양**을 만드는 것이 목적이고, 잘라 낸 뒤 남는
# 글자가 두 자 미만이면 자르지 않는다 — 낱말 자체가 사라지는 편이 더 나쁘다.
_KOREAN_SUFFIXES = (
    "하는", "되는", "하며", "하고", "한다", "된다", "이며", "으로", "에서",
    "에게", "까지", "부터", "이고", "라도", "와의", "과의", "들의",
    "을", "를", "이", "가", "은", "는", "의", "에", "와", "과", "로", "도",
)


def _strip_particles(token: str) -> str:
    for suffix in _KOREAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _korean_terms(text: str, source: str) -> list[PlannedTerm]:
    rows: list[PlannedTerm] = []
    seen: set[str] = set()
    for raw in _KOREAN.findall(text or ""):
        token = _strip_particles(raw)
        if token in _KOREAN_STOPWORDS or len(token) < 2 or token in seen:
            continue
        seen.add(token)
        rows.append(PlannedTerm(text=token, kind="korean", source=source))
    return rows


def _english_terms(text: str, source: str) -> list[PlannedTerm]:
    rows: list[PlannedTerm] = []
    seen: set[str] = set()
    for token in _ENGLISH.findall(text or ""):
        lowered = token.lower()
        if lowered in _ENGLISH_STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        rows.append(PlannedTerm(text=token, kind="english", source=source))
    return rows


def claim_components(claim_text: str) -> list[dict]:
    """청구항을 구성 단위로 나눈 **어림값**.

    정확한 분해는 모델이 한다. 여기서는 구분자로만 나눈다 — 이 값은 검색을
    좁히는 데 쓰이지 않고, "무엇을 대상으로 검색에 들어갔는가"의 기록이다.
    """
    text = str(claim_text or "").strip()
    if not text:
        return []
    rows: list[dict] = []
    for line in text.replace(";", "\n").splitlines():
        cleaned = line.strip()
        if len(cleaned) < 8:
            continue
        rows.append(
            {
                "id": f"C{len(rows) + 1}",
                "text": _text(cleaned),
                "source": SOURCE_CLAIM,
            }
        )
        if len(rows) >= _MAX_COMPONENTS:
            break
    return rows


def focus_components(search_focus: dict | None) -> list[dict]:
    """미대응 구성 검색의 대상 구성. 이미 검증된 스냅샷이므로 그대로 쓴다."""
    rows: list[dict] = []
    for item in ((search_focus or {}).get("components") or []):
        if not isinstance(item, dict):
            continue
        label = " ".join(
            part
            for part in (str(item.get("symbol") or ""), str(item.get("claim") or ""))
            if part
        )
        rows.append(
            {
                "id": str(item.get("id") or f"F{len(rows) + 1}"),
                "text": _text(item.get("feature") or item.get("difference") or label),
                "label": _text(label, 80),
                "source": SOURCE_FOCUS,
            }
        )
        if len(rows) >= _MAX_COMPONENTS:
            break
    return rows


def build(
    *,
    claim_text: str,
    strategy_body: str = "",
    strategy_prompt_id: str = "",
    strategy_prompt_sha256: str = "",
    search_focus: dict | None = None,
    spec_provided: bool = False,
) -> SearchPlan:
    """이번 실행의 검색 계획을 만든다.

    전략 프롬프트는 **재료로만** 읽는다. 거기 적힌 문장을 지시로 해석하지
    않으며, 뽑는 것은 사용자가 적어 둔 용어와 분류코드뿐이다.
    """
    claim = str(claim_text or "")
    strategy = str(strategy_body or "")

    terms: list[PlannedTerm] = []
    seen: set[tuple[str, str]] = set()
    for row in (
        _korean_terms(claim, SOURCE_CLAIM)
        + _english_terms(claim, SOURCE_CLAIM)
        + _english_terms(strategy, SOURCE_STRATEGY)
    ):
        key = (row.text.casefold(), row.kind)
        if key in seen:
            continue
        seen.add(key)
        terms.append(row)
        if len(terms) >= _MAX_TERMS:
            break

    classifications = _classifications(claim, strategy)

    focus_rows = focus_components(search_focus)
    components = focus_rows or claim_components(claim)

    queries: list[PlannedQuery] = []
    claim_terms = [row.text for row in terms if row.source == SOURCE_CLAIM][:6]
    if claim_terms:
        queries.append(
            PlannedQuery(
                id="Q1",
                purpose=PURPOSE_COMBINED,
                text=" ".join(claim_terms),
                terms=tuple(claim_terms),
            )
        )
    for component in components[:_MAX_QUERIES - len(queries)]:
        component_terms = [
            row.text
            for row in _korean_terms(component.get("text") or "", SOURCE_CLAIM)
            + _english_terms(component.get("text") or "", SOURCE_CLAIM)
        ][:5]
        if not component_terms:
            continue
        queries.append(
            PlannedQuery(
                id=f"Q{len(queries) + 1}",
                purpose=PURPOSE_COMPONENT,
                text=" ".join(component_terms),
                terms=tuple(component_terms),
            )
        )
    for code in classifications[: max(0, _MAX_QUERIES - len(queries))]:
        queries.append(
            PlannedQuery(
                id=f"Q{len(queries) + 1}",
                purpose=PURPOSE_TERM,
                text=code,
                terms=(code,),
            )
        )

    notes: list[str] = [
        "이 계획은 ARIA 가 청구항과 검색 전략 본문에서 기계적으로 뽑은 것입니다. "
        "모델에게 묻지 않았고, 실제로 실행된 검색어는 observed 에 따로 기록됩니다."
    ]
    if search_focus:
        notes.append("미대응 구성 보완 검색이라 대상 구성을 선택 구성으로 한정했습니다.")
    if spec_provided:
        notes.append(
            "명세서 보조 확장 레인이 함께 돕니다. 명세서에서 넓힌 용어는 이 계획이 "
            "아니라 모델의 term_expansions 에 기록됩니다."
        )
    if not terms:
        notes.append("청구항에서 뽑을 수 있는 검색어가 없어 계획이 비어 있습니다.")

    return SearchPlan(
        strategy_prompt_id=str(strategy_prompt_id or ""),
        strategy_prompt_sha256=str(strategy_prompt_sha256 or ""),
        terms=tuple(terms),
        classifications=tuple(classifications),
        components=tuple(components),
        queries=tuple(queries[:_MAX_QUERIES]),
        notes=tuple(notes),
    )
