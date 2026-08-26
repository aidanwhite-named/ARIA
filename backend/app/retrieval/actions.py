"""AI 가 돌려줄 수 있는 action 의 전체 목록.

AI 에게 범용 셸이나 파일 도구를 주지 않는다. 대신 **이 파일에 있는 것만**
JSON 으로 돌려주게 하고, 실제 조회는 ARIA 가 한다. 그래서 이 목록이 곧 AI 가
로컬 자료에 대해 할 수 있는 일의 전부다.

각 action 은 Pydantic 모델이라 형식 검증이 파싱 단계에서 끝난다. 존재하지 않는
문헌·범위 밖 페이지·예산 초과 같은 의미 검증은 agent 가 corpus 를 보고 한다.
잘못된 action 은 셸로 우회하지 않고 구조화된 오류로 AI 에게 돌려준다.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

# 한 번에 보낼 수 있는 검색어·페이지 수. 모델이 한 라운드에 예산을 다 쓰지
# 못하게 하는 상한이며, 라운드 전체 예산과는 다른 축이다.
MAX_QUERIES_PER_ACTION = 12
MAX_PAGES_PER_ACTION = 10
MAX_ACTIONS_PER_ROUND = 24
MAX_TEXT = 400

ACTION_SEARCH = "search_document"
ACTION_EXACT = "search_exact"
ACTION_NUMBERS = "search_numbers_and_symbols"
ACTION_READ_PAGE = "read_page"
ACTION_READ_PAGES = "read_pages"
ACTION_READ_PARAGRAPH = "read_paragraph"
ACTION_STATUS = "get_document_status"
ACTION_FINALIZE = "finalize_evidence"

ACTION_NAMES = (
    ACTION_SEARCH,
    ACTION_EXACT,
    ACTION_NUMBERS,
    ACTION_READ_PAGE,
    ACTION_READ_PAGES,
    ACTION_READ_PARAGRAPH,
    ACTION_STATUS,
    ACTION_FINALIZE,
)

# 모든 문헌을 뜻하는 별칭. 구성마다 문헌을 하나씩 적게 하면 라운드가 문헌 수
# 만큼 늘어난다.
ALL_DOCUMENTS = "*"


class ActionError(Exception):
    """모델 응답을 action 으로 읽지 못했다."""


def _strings(values: list, limit: int) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()[:MAX_TEXT]
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:limit]


class _Base(BaseModel):
    model_config = {"extra": "ignore"}


class SearchDocument(_Base):
    """일반 검색. BM25 · 부분문자 · 숫자 채널이 함께 돌아간다."""

    action: Literal["search_document"]
    component_id: str = ""
    attachment: str = ALL_DOCUMENTS
    queries: list[str] = Field(default_factory=list)
    limit: int = 8

    @field_validator("queries")
    @classmethod
    def _check_queries(cls, value: list) -> list[str]:
        cleaned = _strings(value, MAX_QUERIES_PER_ACTION)
        if not cleaned:
            raise ValueError("queries 가 비어 있습니다.")
        return cleaned

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        return max(1, min(20, int(value)))


class SearchExact(_Base):
    """정확 문구 검색. 문구를 통째로 하나의 phrase 로 넣는다."""

    action: Literal["search_exact"]
    component_id: str = ""
    attachment: str = ALL_DOCUMENTS
    phrases: list[str] = Field(default_factory=list)
    limit: int = 8

    @field_validator("phrases")
    @classmethod
    def _check_phrases(cls, value: list) -> list[str]:
        cleaned = _strings(value, MAX_QUERIES_PER_ACTION)
        if not cleaned:
            raise ValueError("phrases 가 비어 있습니다.")
        return cleaned

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        return max(1, min(20, int(value)))


class SearchNumbersAndSymbols(_Base):
    """숫자·범위·단위·도면부호 검색."""

    action: Literal["search_numbers_and_symbols"]
    component_id: str = ""
    attachment: str = ALL_DOCUMENTS
    terms: list[str] = Field(default_factory=list)
    limit: int = 8

    @field_validator("terms")
    @classmethod
    def _check_terms(cls, value: list) -> list[str]:
        cleaned = _strings(value, MAX_QUERIES_PER_ACTION)
        if not cleaned:
            raise ValueError("terms 가 비어 있습니다.")
        return cleaned

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        return max(1, min(20, int(value)))


class ReadPage(_Base):
    action: Literal["read_page"]
    component_id: str = ""
    attachment: str
    page: int

    @field_validator("page")
    @classmethod
    def _check_page(cls, value: int) -> int:
        page = int(value)
        if page < 1:
            raise ValueError("page 는 1 이상이어야 합니다.")
        return page


class ReadPages(_Base):
    action: Literal["read_pages"]
    component_id: str = ""
    attachment: str
    pages: list[int] = Field(default_factory=list)

    @field_validator("pages")
    @classmethod
    def _check_pages(cls, value: list) -> list[int]:
        pages: list[int] = []
        for item in value:
            try:
                page = int(item)
            except (TypeError, ValueError):
                continue
            if page >= 1 and page not in pages:
                pages.append(page)
        if not pages:
            raise ValueError("pages 가 비어 있습니다.")
        return pages[:MAX_PAGES_PER_ACTION]


class ReadParagraph(_Base):
    action: Literal["read_paragraph"]
    component_id: str = ""
    attachment: str
    paragraph: str

    @field_validator("paragraph")
    @classmethod
    def _check_paragraph(cls, value: str) -> str:
        text = str(value).strip()
        if not re.search(r"\d", text):
            raise ValueError("paragraph 에 문단번호가 없습니다. 예: [0032]")
        return text[:32]


class GetDocumentStatus(_Base):
    action: Literal["get_document_status"]
    attachment: str = ALL_DOCUMENTS


class EvidenceRef(_Base):
    """근거 하나. 원문은 여기 담지 않는다.

    모델은 **어느 청크인가**와 **왜 관련 있는가**만 적는다. 원문 텍스트는
    ARIA 가 자기 인덱스에서 채운다. 그래야 모델이 원문을 고치거나 지어내는
    경로가 구조적으로 없다.
    """

    attachment: str
    chunk_id: str = ""
    page: int | None = None
    paragraph: str = ""
    relevance: str = ""

    @field_validator("relevance")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]


class FinalizeComponent(_Base):
    component_id: str
    searched_terms: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    # 모델의 주장. ARIA 가 그대로 쓰지 않고 자기 관측과 대조해서 확정한다.
    status_claim: str = ""
    note: str = ""

    @field_validator("searched_terms")
    @classmethod
    def _check_terms(cls, value: list) -> list[str]:
        return _strings(value, 60)

    @field_validator("note")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:2000]


class FinalizeEvidence(_Base):
    action: Literal["finalize_evidence"]
    components: list[FinalizeComponent] = Field(default_factory=list)


AnyAction = Annotated[
    Union[
        SearchDocument,
        SearchExact,
        SearchNumbersAndSymbols,
        ReadPage,
        ReadPages,
        ReadParagraph,
        GetDocumentStatus,
        FinalizeEvidence,
    ],
    Field(discriminator="action"),
]


class ComponentDeclaration(_Base):
    """AI 가 분해한 청구항 구성 하나. id 는 ARIA 가 붙인다."""

    label: str = ""
    feature: str = ""

    @field_validator("label", "feature")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:1000]


class AgentResponse(_Base):
    """한 라운드에서 AI 가 돌려주는 것 전부."""

    components: list[ComponentDeclaration] = Field(default_factory=list)
    notes: str = ""
    actions: list[AnyAction] = Field(default_factory=list)

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value).strip()[:4000]

    @field_validator("actions")
    @classmethod
    def _cap(cls, value: list) -> list:
        return value[:MAX_ACTIONS_PER_ROUND]


_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


def _candidate_payloads(text: str) -> list[str]:
    """모델 출력에서 JSON 으로 보이는 덩어리를 순서대로 뽑는다.

    코드펜스로 감싸는 모델도 있고 그냥 쓰는 모델도 있다. 형식 실수 하나로
    라운드를 통째로 버리지 않도록 몇 가지를 시도하되, 추측으로 고쳐 쓰지는
    않는다 — 어느 것도 JSON 이 아니면 오류로 돌려준다.
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
    return candidates


def parse_response(text: str) -> AgentResponse:
    """모델 출력을 AgentResponse 로 읽는다. 실패하면 ActionError."""
    if not str(text or "").strip():
        raise ActionError("모델이 아무것도 돌려주지 않았습니다.")

    last_error = ""
    for payload in _candidate_payloads(text):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            last_error = f"JSON 이 아닙니다: {exc.msg}"
            continue
        if not isinstance(data, dict):
            last_error = "최상위가 객체가 아닙니다."
            continue
        try:
            return AgentResponse.model_validate(data)
        except ValidationError as exc:
            last_error = _format_validation_error(exc)
            continue
    raise ActionError(last_error or "응답을 action 으로 읽지 못했습니다.")


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:6]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location or '(root)'}: {error.get('msg', '')}")
    return "; ".join(parts)


def schema_summary() -> str:
    """프롬프트에 넣을 action 목록 요약.

    Pydantic 모델에서 만들어내므로 스키마를 바꾸면 프롬프트도 함께 바뀐다.
    두 곳을 따로 적어 두면 반드시 어긋난다.
    """
    lines = [
        f'- {{"action":"{ACTION_SEARCH}","component_id":"R001",'
        f'"attachment":"ATT-01|{ALL_DOCUMENTS}","queries":["검색어", "..."],'
        '"limit":8}',
        f'- {{"action":"{ACTION_EXACT}","component_id":"R001",'
        f'"attachment":"ATT-01|{ALL_DOCUMENTS}","phrases":["정확히 이 문구"]}}',
        f'- {{"action":"{ACTION_NUMBERS}","component_id":"R001",'
        f'"attachment":"ATT-01|{ALL_DOCUMENTS}","terms":["110","5V","0.5mm"]}}',
        f'- {{"action":"{ACTION_READ_PAGE}","attachment":"ATT-01","page":12}}',
        f'- {{"action":"{ACTION_READ_PAGES}","attachment":"ATT-01",'
        '"pages":[11,12,13]}',
        f'- {{"action":"{ACTION_READ_PARAGRAPH}","attachment":"ATT-01",'
        '"paragraph":"[0032]"}',
        f'- {{"action":"{ACTION_STATUS}","attachment":"ATT-01|{ALL_DOCUMENTS}"}}',
        f'- {{"action":"{ACTION_FINALIZE}","components":[{{"component_id":"R001",'
        '"status_claim":"matched|not_found","searched_terms":["실제로 쓴 검색어"],'
        '"evidence":[{"attachment":"ATT-01","chunk_id":"P0012-003",'
        '"relevance":"이 구간이 왜 이 구성에 대응하는지"}],"note":""}]}',
    ]
    return "\n".join(lines)
