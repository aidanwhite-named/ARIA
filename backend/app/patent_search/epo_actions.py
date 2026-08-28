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
ACTION_FETCH = "epo_fetch_document"
ACTION_FINISH = "finish"
ACTION_NAMES = (ACTION_SEARCH, ACTION_FETCH, ACTION_FINISH)

# 한 라운드에서 받아들이는 action 수.
#
#     검색 3(라운드 상한) + 상세 12(실행 상한) + finish 1 = 16
#
# 예전에는 4였다. 그러면 상세 조회 12건은 **한 라운드 안에서 도달할 수 없고**,
# 모델이 12건과 finish 를 함께 보내면 finish 가 조용히 잘려 나갔다. 상한이
# 서로 모순되면 큰 쪽은 장식이 된다.
#
# 예산 숫자는 EpoAgentBudget 에 있고 여기서 import 하면 순환이 된다. 대신
# test_epo_agent 가 두 값이 어긋나지 않는지 대조한다.
MAX_ACTIONS_PER_ROUND = 16
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


class EpoFetchDocument(_Base):
    """후보 하나의 상세(청구항·초록·설명)를 받는다.

    검색 결과 목록만으로 판단하지 않게 하려고 둔 통로다. 검색 응답과 상세
    응답은 서로 다른 아티팩트를 가리키므로, "초록까지만 본 후보"와 "청구항까지
    본 후보"가 기록에서 구분된다.
    """

    action: Literal["epo_fetch_document"]
    doc_number: str
    constituent: Literal["biblio", "abstract", "claims", "description"] = "claims"
    reason: str = ""

    @field_validator("doc_number", "reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:MAX_TEXT]


class Finish(_Base):
    """더 찾을 것이 없다고 모델이 선언한다."""

    action: Literal["finish"]
    notes: str = ""

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]


AnyAction = Annotated[
    Union[EpoSearch, EpoFetchDocument, Finish],
    Field(discriminator="action"),
]


class AgentResponse(_Base):
    """한 라운드에서 모델이 돌려주는 것 전부."""

    strategy: str = ""
    actions: list[AnyAction] = Field(default_factory=list)

    @field_validator("strategy")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]

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
            if "action" in data and "actions" not in data:
                strategy = str(data.get("strategy", "") or "")
                data = {"strategy": strategy, "actions": [data]}
        else:
            last_error = "최상위가 객체 또는 배열이 아닙니다."
            continue
        if _depth(data) > DEPTH_LIMIT:
            last_error = too_deep
            continue
        try:
            return AgentResponse.model_validate(data)
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
            f'- {{"action":"{ACTION_FETCH}","doc_number":"EP1000000A1",'
            '"constituent":"claims","reason":"초록만으로는 대응을 볼 수 없어서"}',
            f'- {{"action":"{ACTION_FINISH}","notes":"더 넓힐 축이 없습니다."}}',
            "",
            f"query 의 field 로 쓸 수 있는 값: {fields}",
            "match 는 all(단어 전부) · any(하나라도) · exact(구 그대로) 중 하나.",
            'date_range 는 {"kind":"date_range","field":"pd",'
            '"begin":"20100101","end":"20201231"}.',
        ]
    )
