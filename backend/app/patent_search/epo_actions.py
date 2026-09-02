"""EPO 검색 루프에서 모델이 돌려줄 수 있는 것 — 구조화된 질의뿐이다.

모델은 **CQL 문자열을 만들지 않는다.** 여기 있는 스키마로만 질의를 표현하고,
CQL 은 epo_cql.build 가 만든다. 문자열을 받으면 검색식이 곧 입력이면서 동시에
실행 명령이 되고, 막아야 할 것이 "무엇을 검색하는가"에서 "무엇을 실행하는가"로
바뀐다.

스키마는 두 겹으로 검사된다.

  1. 여기(pydantic): 모양·타입·크기. 모르는 필드는 무시하고, 재귀 깊이와
     항 개수를 먼저 끊는다.
  2. epo_cql: 필드 허용 목록, 값의 문자, 길이, 분류코드·문헌번호 형식.

두 번째가 진짜 관문이다. 여기서 통과했다고 안전한 것이 아니라, epo_cql 이
거절하지 않은 것만 나간다. 그래서 이 파일은 epo_cql 의 규칙을 복사하지 않는다 —
복사하면 두 벌이 어긋나는 날이 온다.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import epo_client, epo_cql

ACTION_SEARCH = "epo_search"
ACTION_FINISH = "finish"
ACTION_NAMES = (ACTION_SEARCH, ACTION_FINISH)

#: 더 이상 실행하지 않는 action 이름.
#:
#: 문헌 상세조회는 검색 단계에서 사라졌다. 검색과 공식 검증을 나누기 위해서다 —
#: 검색 에이전트가 상세를 받아 버리면 같은 예산(epo_max_detail_fetches)을 먼저
#: 써 버리고, 정작 공식 검증이 조회할 몫이 남지 않는다.
#:
#: 이름을 지우지 않고 남기는 이유: 모델이 옛 습관으로 이 action 을 보내도
#: 응답 **전체**를 거절하지 않기 위해서다. 검색 계획 턴은 이제 한 번뿐이라,
#: 그 한 번을 형식 오류로 날리면 레인이 통째로 빈손이 된다. 걷어내고 사유를
#: 남긴 뒤 나머지 action 은 실행한다.
RETIRED_ACTIONS = frozenset({"epo_fetch_document"})

# 한 응답에서 받아들이는 action 수.
#
#     검색 3(턴 상한) + finish 1 = 4. 여유를 두어 6.
#
# 상세조회가 빠지면서 12가 필요 없어졌다. 상한이 실행 상한보다 크게 남아
# 있으면 "왜 이 숫자인가"를 아무도 설명할 수 없게 된다.
#
# 예산 숫자는 EpoAgentBudget 에 있고 여기서 import 하면 순환이 된다. 대신
# test_epo_agent 가 두 값이 어긋나지 않는지 대조한다.
MAX_ACTIONS_PER_ROUND = 6
# 모델 출력에서 읽을 최대 길이. 넘으면 파싱 전에 끊는다 — 거대한 입력을
# json.loads 에 그대로 넣지 않는다.
MAX_PAYLOAD_CHARS = 200_000
# 질의 트리의 최대 중첩. epo_cql 이 다시 검사하지만, 재귀 파싱 전에 먼저 끊어야
# 깊은 중첩이 파이썬 재귀 한계를 건드리지 않는다.
MAX_QUERY_DEPTH = 6
MAX_TEXT = 300


class ActionError(Exception):
    """모델 응답을 action 으로 읽지 못했다."""


class _Base(BaseModel):
    model_config = {"extra": "ignore"}


# --- 질의 트리 ------------------------------------------------------------


class QueryTerm(_Base):
    """검색항 하나. field 는 epo_cql 의 허용 목록에서만 통과한다."""

    kind: Literal["term"]
    field: str
    value: str
    match: Literal["all", "any", "exact"] = "all"

    @field_validator("field", "value", "match")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:MAX_TEXT]


class QueryDateRange(_Base):
    """발행일 범위. YYYYMMDD 두 개."""

    kind: Literal["date_range"]
    field: str = epo_cql.FIELD_PUBLICATION_DATE
    begin: str
    end: str

    @field_validator("field", "begin", "end")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:MAX_TEXT]


class QueryGroup(_Base):
    """항들을 논리 연산자로 묶은 것."""

    kind: Literal["group"]
    op: Literal["and", "or", "not"]
    items: list["QueryNode"] = Field(default_factory=list)


QueryNode = Annotated[
    Union[QueryTerm, QueryDateRange, QueryGroup],
    Field(discriminator="kind"),
]

QueryGroup.model_rebuild()


def to_cql_node(node):
    """모델이 준 질의 트리를 epo_cql 의 노드로 옮긴다.

    옮기기만 한다. 검증은 epo_cql.build 가 한다 — 여기서 미리 걸러 내면 규칙이
    두 곳에 생기고, 그 둘은 반드시 어긋난다.
    """
    if isinstance(node, QueryTerm):
        return epo_cql.Term(field=node.field, value=node.value, match=node.match)
    if isinstance(node, QueryDateRange):
        return epo_cql.DateRange(field=node.field, begin=node.begin, end=node.end)
    if isinstance(node, QueryGroup):
        return epo_cql.Group(
            op=node.op, items=tuple(to_cql_node(item) for item in node.items)
        )
    raise epo_cql.CqlError(f"질의에 넣을 수 없는 값입니다: {type(node).__name__}")


# --- action ---------------------------------------------------------------


class EpoSearch(_Base):
    """OPS 검색 한 번."""

    action: Literal["epo_search"]
    query: QueryNode
    # OPS 가 한 질의로 돌려주는 최대 건수. 기본을 그보다 작게 두면 모델이
    # 지정하지 않았을 때 조용히 좁게 검색된다.
    max_results: int = epo_client.MAX_RESULTS_PER_QUERY
    reason: str = ""

    @field_validator("max_results")
    @classmethod
    def _cap(cls, value: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(number, epo_client.MAX_RESULTS_PER_QUERY))

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]


class Finish(_Base):
    """더 찾을 것이 없다고 모델이 선언한다."""

    action: Literal["finish"]
    notes: str = ""

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]


# --- 청구항 분석 ----------------------------------------------------------
#
# 이것은 action 이 아니다. 모델이 **첫 응답에 함께 적는 기록**이며 ARIA 가
# 실행하는 것이 없다. 별도 모델 턴을 만들지 않는 이유는 하나다 — 검색어를 만든
# 그 판단이 곧 검색 전략이므로, 나중에 다시 물으면 그때의 근거가 아니라 새로
# 지어낸 근거를 받게 된다.
#
# 조용히 자르지 않는다. 상한을 넘으면 pydantic 이 거절하고, 그 사유가 모델에게
# 되돌아간다.
MAX_CLAIM_ELEMENTS = 40
MAX_RELATIONS = 60
MAX_COMBINATIONS = 30
MAX_SYNONYMS = 20
MAX_CONDITIONS = 30


class ClaimElement(_Base):
    """청구항 구성요소 하나."""

    id: str
    text: str
    # "필수 구성인가"를 불리언으로 두면 '아니다'와 '모르겠다'가 같은 값이 된다.
    # 모델이 적지 않으면 None 으로 남고, 기록에도 '판단 없음'으로 남는다.
    essential: bool | None = None
    # 이 구성요소를 다른 문헌이 부르는 말. 검색어 확장의 근거가 된다.
    synonyms: list[str] = Field(default_factory=list)

    @field_validator("id", "text")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]

    @field_validator("synonyms")
    @classmethod
    def _cap_synonyms(cls, value: list) -> list:
        return [str(item).strip()[:200] for item in value[:MAX_SYNONYMS] if str(item).strip()]


class ClaimRelation(_Base):
    """구성요소 사이의 관계. 구성요소를 다 찾아도 관계가 다르면 다른 발명이다."""

    source: str = ""
    target: str = ""
    kind: str = ""
    description: str = ""

    @field_validator("source", "target", "kind")
    @classmethod
    def _trim_short(cls, value: str) -> str:
        return str(value).strip()[:200]

    @field_validator("description")
    @classmethod
    def _trim_long(cls, value: str) -> str:
        return str(value).strip()[:1000]


class ConceptCombination(_Base):
    """EPO 검색에 실제로 쓴 개념 조합."""

    elements: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("elements", "terms")
    @classmethod
    def _cap(cls, value: list) -> list:
        return [str(item).strip()[:200] for item in value[:MAX_SYNONYMS] if str(item).strip()]

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]


class SearchCondition(_Base):
    """IPC/CPC 또는 그 밖의 검색 한정."""

    kind: str = ""
    value: str = ""
    reason: str = ""

    @field_validator("kind", "value")
    @classmethod
    def _trim_short(cls, value: str) -> str:
        return str(value).strip()[:200]

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]


class ClaimAnalysis(_Base):
    """검색 전략의 근거. 첫 응답에 한 번 적는다."""

    elements: list[ClaimElement] = Field(default_factory=list)
    relations: list[ClaimRelation] = Field(default_factory=list)
    concept_combinations: list[ConceptCombination] = Field(default_factory=list)
    search_conditions: list[SearchCondition] = Field(default_factory=list)
    notes: str = ""

    @field_validator("elements")
    @classmethod
    def _cap_elements(cls, value: list) -> list:
        if len(value) > MAX_CLAIM_ELEMENTS:
            raise ValueError(
                f"청구항 구성요소가 상한({MAX_CLAIM_ELEMENTS}개)을 넘습니다"
                f"({len(value)}개)."
            )
        return value

    @field_validator("relations")
    @classmethod
    def _cap_relations(cls, value: list) -> list:
        if len(value) > MAX_RELATIONS:
            raise ValueError(
                f"구성요소 관계가 상한({MAX_RELATIONS}개)을 넘습니다({len(value)}개)."
            )
        return value

    @field_validator("concept_combinations")
    @classmethod
    def _cap_combinations(cls, value: list) -> list:
        if len(value) > MAX_COMBINATIONS:
            raise ValueError(
                f"개념 조합이 상한({MAX_COMBINATIONS}개)을 넘습니다({len(value)}개)."
            )
        return value

    @field_validator("search_conditions")
    @classmethod
    def _cap_conditions(cls, value: list) -> list:
        if len(value) > MAX_CONDITIONS:
            raise ValueError(
                f"검색 조건이 상한({MAX_CONDITIONS}개)을 넘습니다({len(value)}개)."
            )
        return value

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]

    @property
    def empty(self) -> bool:
        return not (
            self.elements
            or self.relations
            or self.concept_combinations
            or self.search_conditions
            or self.notes
        )

    def to_dict(self) -> dict:
        return {
            "elements": [
                {
                    "id": item.id,
                    "text": item.text,
                    "essential": item.essential,
                    "synonyms": list(item.synonyms),
                }
                for item in self.elements
            ],
            "relations": [
                {
                    "source": item.source,
                    "target": item.target,
                    "kind": item.kind,
                    "description": item.description,
                }
                for item in self.relations
            ],
            "concept_combinations": [
                {
                    "elements": list(item.elements),
                    "terms": list(item.terms),
                    "reason": item.reason,
                }
                for item in self.concept_combinations
            ],
            "search_conditions": [
                {"kind": item.kind, "value": item.value, "reason": item.reason}
                for item in self.search_conditions
            ],
            "notes": self.notes,
        }


class ShortlistItem(_Base):
    """모델이 고른 유망 EPO 후보 하나.

    ARIA 는 이 목록을 **후보 선정**으로만 쓴다. 여기 적힌 이유는 A/B/C 근거가
    아니다 — 그 판정은 공식 응답 대조를 통과한 뒤에야 나온다.
    """

    doc_number: str
    reason: str = ""
    matched_elements: list[str] = Field(default_factory=list)

    @field_validator("doc_number")
    @classmethod
    def _trim_number(cls, value: str) -> str:
        return str(value).strip()[:MAX_TEXT]

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:2000]

    @field_validator("matched_elements")
    @classmethod
    def _cap(cls, value: list) -> list:
        return [
            str(item).strip()[:200]
            for item in value[:MAX_CLAIM_ELEMENTS]
            if str(item).strip()
        ]

    def to_dict(self) -> dict:
        return {
            "doc_number": self.doc_number,
            "reason": self.reason,
            "matched_elements": list(self.matched_elements),
        }


AnyAction = Annotated[
    Union[EpoSearch, Finish],
    Field(discriminator="action"),
]


#: 한 응답에 실을 수 있는 shortlist 항목 수의 **구조적** 상한. 사용자 설정
#: (epo_shortlist_limit)은 이것과 다른 축이다 — 이쪽은 응답을 파싱할 때의
#: 안전 한도이고, 그쪽은 최종 대응표에 몇 건까지 올릴 것인가다. 설정이 이보다
#: 크면 파서가 먼저 거절하므로 설정 상한을 여기에 맞춰 둔다.
MAX_SHORTLIST_ITEMS = 50


class AgentResponse(_Base):
    """한 라운드에서 모델이 돌려주는 것 전부."""

    strategy: str = ""
    actions: list[AnyAction] = Field(default_factory=list)
    # 첫 응답에만 있으면 된다. 뒤 라운드에서 다시 오면 첫 것을 유지한다 —
    # 검색을 하고 난 뒤 고쳐 쓴 분석은 '검색 전략'이 아니라 '결과 해설'이다.
    claim_analysis: ClaimAnalysis | None = None
    # 유망 후보. 어느 라운드에서든 올 수 있고 누적된다.
    shortlist: list[ShortlistItem] = Field(default_factory=list)
    # 파싱 단계에서 걷어낸 은퇴 action 의 이름. 조용히 사라지지 않는다.
    retired_actions: list[str] = Field(default_factory=list)

    @field_validator("strategy")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]

    @field_validator("shortlist")
    @classmethod
    def _cap_shortlist(cls, value: list) -> list:
        if len(value) > MAX_SHORTLIST_ITEMS:
            raise ValueError(
                f"shortlist 가 상한({MAX_SHORTLIST_ITEMS}건)을 넘습니다"
                f"({len(value)}건)."
            )
        return value

    @field_validator("actions")
    @classmethod
    def _cap(cls, value: list) -> list:
        # 조용히 자르지 않는다. 자르면 모델은 자기가 보낸 action 이 전부
        # 실행됐다고 믿고, 잘려 나간 finish 나 상세 조회를 다시 요청하지 않는다.
        if len(value) > MAX_ACTIONS_PER_ROUND:
            raise ValueError(
                f"action 이 한 라운드 상한({MAX_ACTIONS_PER_ROUND}개)을 "
                f"넘습니다({len(value)}개)."
            )
        return value


# --- 파싱 -----------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


# JSON 컨테이너(dict/list) 중첩 깊이 상한.
# AgentResponse(dict) -> actions(list) -> EpoSearch(dict) -> query(dict) -> items(list) -> ...
# 질의 트리 단계마다 dict + list 2단계가 생기므로, 유효한 MAX_QUERY_DEPTH(6단계) 질의를
# 수용하려면 약 18~24 수준이 필요하다. 재귀 공격(수천 단계)은 안전하게 차단한다.
DEPTH_LIMIT = MAX_QUERY_DEPTH * 3 + 6


def _depth(value) -> int:
    """중첩 깊이. **재귀하지 않는다.**

    재귀로 세면 깊은 입력에서 이 함수 자체가 RecursionError 를 내고, 그 예외는
    ActionError 가 아니라 루프 밖으로 빠져나간다. 깊이를 재려다 깊이 때문에
    죽는 셈이다.
    """
    stack = [(value, 1)]
    deepest = 1
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if level > DEPTH_LIMIT:
            return level
        if isinstance(node, dict):
            stack.extend((item, level + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, level + 1) for item in node)
    return deepest


# 문자열 밖에서 여는 괄호를 세어 중첩 깊이를 미리 잰다. json.loads 는 깊은
# 입력에서 RecursionError 를 내는데, 그것을 잡는 것보다 애초에 넣지 않는 편이
# 낫다 — RecursionError 를 잡아도 스택은 이미 바닥까지 갔다 온 상태다.
def _text_nesting_depth(text: str) -> int:
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            deepest = max(deepest, depth)
            if deepest > DEPTH_LIMIT:
                return deepest
        elif char in "}]":
            depth = max(0, depth - 1)
    return deepest


def _candidate_payloads(text: str) -> list[str]:
    """모델 출력에서 JSON 으로 보이는 덩어리를 순서대로 뽑는다.

    코드펜스로 감싸는 모델도 있고 그냥 쓰는 모델도 있다. 형식 실수 하나로
    라운드를 통째로 버리지 않도록 몇 가지를 시도하되, 추측으로 고쳐 쓰지는
    않는다.
    """
    candidates: list[str] = []
    for match in _FENCE.finditer(text):
        body = match.group("body").strip()
        if body:
            candidates.append(body)
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    start_arr = text.find("[")
    end_arr = text.rfind("]")
    if start_arr != -1 and end_arr > start_arr:
        candidates.append(text[start_arr : end_arr + 1])
    return candidates


def parse_response(text: str) -> AgentResponse:
    """모델 출력을 AgentResponse 로 읽는다. 실패하면 ActionError."""
    raw = str(text or "")
    if not raw.strip():
        raise ActionError("모델이 아무것도 돌려주지 않았습니다.")
    if len(raw) > MAX_PAYLOAD_CHARS:
        raise ActionError(
            f"응답이 {MAX_PAYLOAD_CHARS:,}자를 넘습니다. JSON 객체 하나만 "
            "돌려주십시오."
        )

    too_deep = f"질의 중첩이 {MAX_QUERY_DEPTH}단계를 넘습니다."
    last_error = ""
    for payload in _candidate_payloads(raw):
        # json.loads 에 넣기 **전에** 깊이를 잰다. 깊은 입력은 파서 안에서
        # RecursionError 를 내고, 그 예외는 ActionError 가 아니라 루프 밖으로
        # 빠져나가 실행 전체를 끊는다.
        if _text_nesting_depth(payload) > DEPTH_LIMIT:
            last_error = too_deep
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            last_error = f"JSON 이 아닙니다: {exc.msg}"
            continue
        except RecursionError:
            # 문자 기반 검사를 통과했는데도 파서가 깊다고 판단한 경우.
            # 어떤 경로로도 이 함수 밖으로 RecursionError 를 내보내지 않는다.
            last_error = too_deep
            continue
        if isinstance(data, list):
            data = {"actions": data}
        elif isinstance(data, dict):
            # 모델이 최상위에 단일 action 객체를 직접 준 경우(예: {"action": "epo_search", ...})
            # AgentResponse 스키마에 맞게 actions 리스트로 감싼다.
            #
            # 형제 필드를 **버리지 않는다.** 감싸기는 모양을 너그럽게 받아 주려고
            # 있는 것이지 내용을 줄이려고 있는 것이 아니다. claim_analysis 를
            # 조용히 떨어뜨리면, 짧은 형식을 쓴 모델은 계약을 지킬 방법이 아예
            # 없어지고 "분석이 없다"는 이유로 검색이 영원히 거절된다.
            if "action" in data and "actions" not in data:
                wrapped = {"actions": [data]}
                for carried in ("strategy", "claim_analysis", "shortlist"):
                    if carried in data:
                        wrapped[carried] = data[carried]
                data = wrapped
        else:
            last_error = "최상위가 객체 또는 배열이 아닙니다."
            continue
        if _depth(data) > DEPTH_LIMIT:
            last_error = too_deep
            continue
        # 상한은 **모델이 보낸 수**로 잰다. 은퇴 action 을 걷어낸 뒤에 세면
        # 실행되지 않는 action 을 수백 개 보내도 상한을 통과한다.
        raw_actions = data.get("actions")
        if (
            isinstance(raw_actions, list)
            and len(raw_actions) > MAX_ACTIONS_PER_ROUND
        ):
            last_error = (
                f"action 이 한 응답 상한({MAX_ACTIONS_PER_ROUND}개)을 "
                f"넘습니다({len(raw_actions)}개)."
            )
            continue
        retired: list[str] = []
        if isinstance(raw_actions, list):
            kept = []
            for item in raw_actions:
                name = item.get("action") if isinstance(item, dict) else None
                if name in RETIRED_ACTIONS:
                    retired.append(str(name))
                else:
                    kept.append(item)
            if retired:
                data = {**data, "actions": kept}
        try:
            parsed = AgentResponse.model_validate(data)
            parsed.retired_actions = retired
            return parsed
        except ValidationError as exc:
            last_error = _format_validation_error(exc)
            continue
        except RecursionError:
            last_error = too_deep
            continue
    raise ActionError(last_error or "응답을 action 으로 읽지 못했습니다.")


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:6]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location or '(root)'}: {error.get('msg', '')}")
    return "; ".join(parts)


def schema_summary() -> str:
    """프롬프트에 넣을 action 요약.

    허용 필드 목록은 epo_cql 에서 그대로 가져온다. 프롬프트에 손으로 적어 두면
    코드가 바뀔 때 문구만 낡는다.
    """
    fields = ", ".join(epo_cql.ALLOWED_FIELDS)
    return "\n".join(
        [
            f'- {{"action":"{ACTION_SEARCH}","max_results":20,'
            '"reason":"왜 이렇게 좁혔는지",'
            '"query":{"kind":"group","op":"and","items":['
            '{"kind":"term","field":"ta","value":"robot arm","match":"all"},'
            '{"kind":"term","field":"ipc","value":"B25J 9/16","match":"exact"}]}}',
            f'- {{"action":"{ACTION_FINISH}","notes":"더 넓힐 축이 없습니다."}}',
            "",
            f"query 의 field 로 쓸 수 있는 값: {fields}",
            "match 는 all(단어 전부) · any(하나라도) · exact(구 그대로) 중 하나.",
            'date_range 는 {"kind":"date_range","field":"pd",'
            '"begin":"20100101","end":"20201231"}.',
        ]
    )


def analysis_schema_summary() -> str:
    """프롬프트에 넣을 claim_analysis · shortlist 요약."""
    return "\n".join(
        [
            '"claim_analysis": {',
            '  "elements": [{"id":"E1","text":"청구항 문언 그대로","essential":true,',
            '                "synonyms":["다른 문헌이 쓰는 표현"]}],',
            '  "relations": [{"source":"E1","target":"E2","kind":"결합",',
            '                 "description":"E2 가 E1 의 끝단에 배치된다"}],',
            '  "concept_combinations": [{"elements":["E1","E2"],',
            '                            "terms":["robot arm","force sensor"],',
            '                            "reason":"이 조합으로 검색한 이유"}],',
            '  "search_conditions": [{"kind":"ipc","value":"B25J 9/16",',
            '                         "reason":"이 분류로 좁힌 이유"}],',
            '  "notes": ""',
            "},",
            '"shortlist": [{"doc_number":"EP1000000A1",',
            '               "reason":"이 문헌을 유망하다고 본 이유",',
            '               "matched_elements":["E1","E2"]}]',
        ]
    )
