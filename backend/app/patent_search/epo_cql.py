"""EPO OPS 검색식(CQL) 생성기 — 구조화된 질의만 받는다.

왜 문자열을 받지 않는가
-----------------------
2단계에서 이 검색을 모는 것은 LLM 이다. LLM 이 만든 CQL 문자열을 그대로
OPS 에 보내면, 검색식이 곧 사용자 입력이면서 동시에 실행 명령이 된다. 그러면
막아야 할 것이 "무엇을 검색하는가"가 아니라 "무엇을 실행하는가"로 바뀐다 —
필드 하나만 몰라도 검색 범위가 통째로 달라지고, 인용부호 하나로 구문이 깨져
의도하지 않은 필드가 조회된다.

그래서 이 모듈은 **문자열을 받지 않고 구조를 받는다.** 필드는 허용 목록에서만
고르고, 값은 항상 인용부호로 감싼 구(phrase)로 나가며, 연산자는 세 개뿐이다.
CQL 문자열은 여기서만 만들어진다. 바깥 어디에도 CQL 을 조립하는 코드를 두지
않는다.

이스케이프가 아니라 거절
------------------------
값에서 위험한 문자를 골라 지우는 방식은 쓰지 않는다. 지우면 사용자가 의도한
검색어와 실제로 나간 검색어가 달라지는데 아무도 그 사실을 모른다. 인용부호나
제어문자가 들어오면 조용히 고치지 말고 거절한다 — 검색어가 틀렸다는 것은
사람이 알아야 하는 사실이다.

와일드카드(``*`` ``?`` ``#``)도 지금은 거절한다. 절단 검색은 유용하지만
결과 수를 몇 자릿수씩 바꾸므로, 예산이 걸린 채널에서 LLM 이 자유롭게 쓰게 할
물건이 아니다. 필요해지면 별도 항으로 명시적으로 연다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field

# --- 필드 허용 목록 -------------------------------------------------------
#
# OPS Published Data Search 의 CQL 필드 중 검토한 것만 넣는다. 모르는 필드를
# 넣어 두면 "왜 결과가 0건이지"를 검색어가 아니라 필드에서 찾게 된다.
FIELD_TITLE = "ti"
FIELD_ABSTRACT = "ab"
FIELD_TITLE_ABSTRACT = "ta"
FIELD_FULLTEXT = "txt"          # 제목·초록·청구항·설명 (EP/WO 위주로만 채워져 있다)
FIELD_APPLICANT = "pa"
FIELD_INVENTOR = "in"
FIELD_PUBLICATION_NUMBER = "pn"
FIELD_APPLICATION_NUMBER = "ap"
FIELD_PRIORITY_NUMBER = "pr"
FIELD_IPC = "ipc"
FIELD_CPC = "cpc"
FIELD_CLASSIFICATION = "cl"     # ipc 또는 cpc
FIELD_PUBLICATION_DATE = "pd"

TEXT_FIELDS = (
    FIELD_TITLE,
    FIELD_ABSTRACT,
    FIELD_TITLE_ABSTRACT,
    FIELD_FULLTEXT,
    FIELD_APPLICANT,
    FIELD_INVENTOR,
)
IDENTIFIER_FIELDS = (
    FIELD_PUBLICATION_NUMBER,
    FIELD_APPLICATION_NUMBER,
    FIELD_PRIORITY_NUMBER,
)
CLASSIFICATION_FIELDS = (FIELD_IPC, FIELD_CPC, FIELD_CLASSIFICATION)
DATE_FIELDS = (FIELD_PUBLICATION_DATE,)

ALLOWED_FIELDS = TEXT_FIELDS + IDENTIFIER_FIELDS + CLASSIFICATION_FIELDS + DATE_FIELDS

# --- 관계 연산자 ----------------------------------------------------------
MATCH_ALL = "all"      # 단어가 전부 들어 있다
MATCH_ANY = "any"      # 단어 중 하나라도 들어 있다
MATCH_EXACT = "exact"  # 구(phrase) 그대로
MATCH_KINDS = (MATCH_ALL, MATCH_ANY, MATCH_EXACT)

_RELATION = {MATCH_ALL: "all", MATCH_ANY: "any", MATCH_EXACT: "="}

# --- 논리 연산자 ----------------------------------------------------------
OP_AND = "and"
OP_OR = "or"
OP_NOT = "not"
OPERATORS = (OP_AND, OP_OR, OP_NOT)

# --- 한도 ----------------------------------------------------------------
#
# OPS 자체 한도보다 좁게 잡는다. 여기서 막히는 것이 OPS 에서 400 을 받는 것보다
# 낫다 — 예산을 쓰지 않고, 무엇이 잘못됐는지 우리 말로 설명할 수 있다.
MAX_VALUE_CHARS = 100
MAX_VALUE_WORDS = 10
MAX_TERMS = 20
MAX_DEPTH = 3
MAX_CQL_CHARS = 500

# 값에 들어올 수 없는 문자.
#   "      구를 닫아 구문을 깨뜨린다
#   * ? #  와일드카드 (지금은 닫아 둔다)
#   \      OPS CQL 의 이스케이프 문자
_FORBIDDEN_IN_VALUE = re.compile(r'["*?#\\]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DATE = re.compile(r"^\d{8}$")
# 분류코드에 올 수 있는 문자만. 자유 텍스트를 분류 필드로 보내면 결과가
# 조용히 0건이 되고, 그것이 "그런 특허가 없다"로 읽힌다.
_CLASSIFICATION = re.compile(r"^[A-Za-z0-9/\- ]{1,30}$")
# 문헌번호. 국가코드 + 숫자 + 선택적 kind code.
_DOC_NUMBER = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9/\-]{1,25}$")


class CqlError(ValueError):
    """검색식을 만들 수 없다. 값이 허용 범위를 벗어났거나 필드를 모른다."""


@dataclass(frozen=True)
class Term:
    """검색항 하나. ``field`` 는 허용 목록에서만 온다."""

    field: str
    value: str
    match: str = MATCH_ALL


@dataclass(frozen=True)
class DateRange:
    """발행일 범위. YYYYMMDD 두 개."""

    field: str
    begin: str
    end: str


@dataclass(frozen=True)
class Group:
    """항들을 논리 연산자로 묶은 것. 안에 Group 을 넣어 중첩할 수 있다."""

    op: str
    items: tuple = dataclass_field(default_factory=tuple)


def _clean_value(value: str) -> str:
    """값을 검사한다. 고치지 않고, 어긋나면 거절한다."""
    text = str(value or "")
    if _CONTROL_CHARS.search(text):
        raise CqlError("검색어에 제어문자가 들어 있습니다.")
    # 앞뒤 공백과 연속 공백만 정리한다. 이건 의미를 바꾸지 않는다.
    text = " ".join(text.split())
    if not text:
        raise CqlError("검색어가 비어 있습니다.")
    forbidden = _FORBIDDEN_IN_VALUE.search(text)
    if forbidden:
        raise CqlError(
            f"검색어에 쓸 수 없는 문자가 있습니다: {forbidden.group(0)!r} "
            '(인용부호와 와일드카드 * ? # 는 허용하지 않습니다)'
        )
    if len(text) > MAX_VALUE_CHARS:
        raise CqlError(
            f"검색어가 {MAX_VALUE_CHARS}자를 넘습니다({len(text)}자)."
        )
    if len(text.split(" ")) > MAX_VALUE_WORDS:
        raise CqlError(f"검색어의 단어가 {MAX_VALUE_WORDS}개를 넘습니다.")
    return text


def _render_term(term: Term) -> str:
    if term.field not in ALLOWED_FIELDS:
        raise CqlError(f"허용되지 않은 검색 필드입니다: {term.field!r}")
    if term.field in DATE_FIELDS:
        raise CqlError(
            f"{term.field} 는 DateRange 로만 검색할 수 있습니다."
        )
    if term.match not in MATCH_KINDS:
        raise CqlError(f"알 수 없는 대조 방식입니다: {term.match!r}")
    value = _clean_value(term.value)

    if term.field in CLASSIFICATION_FIELDS and not _CLASSIFICATION.match(value):
        raise CqlError(
            f"분류코드 형식이 아닙니다: {value!r} (예: G06F 3/01)"
        )
    if term.field in IDENTIFIER_FIELDS and not _DOC_NUMBER.match(value):
        raise CqlError(
            f"문헌번호 형식이 아닙니다: {value!r} (예: EP1000000)"
        )
    # 분류코드와 문헌번호는 단어를 쪼갤 대상이 아니다. 늘 구로 나간다.
    match = (
        MATCH_EXACT
        if term.field in CLASSIFICATION_FIELDS + IDENTIFIER_FIELDS
        else term.match
    )
    return f'{term.field} {_RELATION[match]} "{value}"'


def _render_date(node: DateRange) -> str:
    if node.field not in DATE_FIELDS:
        raise CqlError(f"날짜 검색 필드가 아닙니다: {node.field!r}")
    for bound in (node.begin, node.end):
        if not _DATE.match(str(bound or "")):
            raise CqlError(f"날짜는 YYYYMMDD 여야 합니다: {bound!r}")
    if node.begin > node.end:
        raise CqlError("시작일이 종료일보다 늦습니다.")
    return f'{node.field} within "{node.begin} {node.end}"'


def _render(node, depth: int, counter: list[int]) -> str:
    if depth > MAX_DEPTH:
        raise CqlError(f"검색식 중첩이 {MAX_DEPTH}단계를 넘습니다.")

    if isinstance(node, Term):
        counter[0] += 1
        if counter[0] > MAX_TERMS:
            raise CqlError(f"검색항이 {MAX_TERMS}개를 넘습니다.")
        return _render_term(node)
    if isinstance(node, DateRange):
        counter[0] += 1
        if counter[0] > MAX_TERMS:
            raise CqlError(f"검색항이 {MAX_TERMS}개를 넘습니다.")
        return _render_date(node)
    if isinstance(node, Group):
        if node.op not in OPERATORS:
            raise CqlError(f"알 수 없는 연산자입니다: {node.op!r}")
        items = tuple(node.items or ())
        if not items:
            raise CqlError("빈 그룹은 검색식이 될 수 없습니다.")
        if node.op == OP_NOT and len(items) != 2:
            # CQL 의 not 은 이항이다. "A not B" = A 이면서 B 가 아닌 것.
            raise CqlError("not 은 항이 정확히 둘이어야 합니다.")
        rendered = [_render(item, depth + 1, counter) for item in items]
        if len(rendered) == 1:
            return rendered[0]
        joined = f" {node.op} ".join(rendered)
        return f"({joined})"
    raise CqlError(f"검색식에 넣을 수 없는 값입니다: {type(node).__name__}")


def build(node) -> str:
    """구조화된 질의를 CQL 문자열로 만든다. ARIA 에서 CQL 을 만드는 유일한 곳."""
    counter = [0]
    cql = _render(node, 1, counter)
    if counter[0] == 0:
        raise CqlError("검색항이 하나도 없습니다.")
    if len(cql) > MAX_CQL_CHARS:
        raise CqlError(
            f"검색식이 {MAX_CQL_CHARS}자를 넘습니다({len(cql)}자). "
            "검색어를 줄이십시오."
        )
    return cql


def from_free_text(
    text: str, *, field: str = FIELD_TITLE_ABSTRACT
) -> tuple[Term, tuple[str, ...]]:
    """자유 문장 하나를 안전한 검색항으로 바꾼다.

    2단계에서 LLM 이 구조화된 질의를 만들기 전까지, 그리고 base 계약의
    ``PatentSearchQuery.text`` 를 받을 때 쓴다. 문장을 그대로 구로 넣지 않고
    단어 전부 포함(all)으로 두는 이유는, 긴 문장을 구로 검색하면 거의 항상
    0건이 나오는데 그것이 "그런 특허가 없다"로 읽히기 때문이다.

    **버린 단어를 함께 돌려준다.** 상한을 넘긴 뒤쪽 단어는 검색식에 들어가지
    않는데, 그 사실을 돌려주지 않으면 사용자가 넣은 검색어와 실제로 나간
    검색어가 다른 채로 실행되고 아무도 모른다. 호출부는 이것을 기록해야 한다.
    """
    words = [word for word in str(text or "").split() if word]
    if not words:
        raise CqlError("검색어가 비어 있습니다.")
    kept, dropped = words[:MAX_VALUE_WORDS], tuple(words[MAX_VALUE_WORDS:])
    return Term(field=field, value=" ".join(kept), match=MATCH_ALL), dropped
