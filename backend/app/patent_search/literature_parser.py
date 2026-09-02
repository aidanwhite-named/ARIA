"""Crossref·Europe PMC 응답의 신뢰 파서와 소스 프로필.

epo_parser 와 같은 자리를 차지한다. 어댑터가 보고한 값은 검증에 쓰지 않고,
보존된 아티팩트에서 여기 등록된 파서로 **다시 뽑은** 값만 근거가 된다.

왜 파서를 새로 등록하는가
-------------------------
Crossref 의 초록은 JATS XML 조각으로 등록되어 있다.

    "<jats:p>We propose a complementary metal-oxide-semiconductor ...</jats:p>"

일반 json_path 파서로 뽑으면 이 태그가 그대로 값이 된다. 태그를 호출부에서
떼면 **보고한 값과 재추출한 값이 달라져** 대조가 깨진다. 그래서 태그 제거까지
파서 안에서 하고, 그 구현의 해시를 프로필과 함께 남긴다. 재검증은 같은
바이트에 같은 파서를 다시 돌려 같은 문자열을 얻는다.

왜 raw_capable=False 인가
-------------------------
여기서 오는 초록은 **발행사가 등록한 메타데이터**이지 조판된 논문 원문이 아니다.
같은 문장이 논문 PDF 에 그대로 있는지는 이 응답으로 증명할 수 없다. 그래서 두
프로필 모두 raw_capable=False 이고, 발췌 칸은 여전히 미확인으로 남는다.

번역 여부는 unknown 이다. Crossref 에 등록되는 초록은 저자가 쓴 원문일 수도
발행사가 넣은 번역일 수도 있고, 레코드의 language 필드는 논문의 언어이지 이
초록의 언어가 아니다. 확인하지 않은 것을 "번역이 아니다"로 적지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import parsers
from .base import SOURCE_NORMALIZED, TRANSLATION_UNKNOWN

PARSER_ID = "literature_json"
PARSER_VERSION = "1"

PROFILE_CROSSREF_JSON = "crossref_work_json"
PROFILE_EUROPEPMC_JSON = "europepmc_result_json"

# 프로필이 붙는 응답 형식. 감사 기록에서 "어느 API 의 응답인가"를 가른다.
PROFILE_BY_SOURCE = {
    "crossref": PROFILE_CROSSREF_JSON,
    "europepmc": PROFILE_EUROPEPMC_JSON,
}

# 값 하나의 상한. 넘으면 자르지 않고 통째로 뺀다 — 잘린 문장을 근거로 주면
# 모델이 잘린 자리에서 문장을 이어 쓰고, 그 문장은 아티팩트 대조에서 탈락한다.
MAX_FIELD_CHARS = 20000

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class LiteratureParseError(parsers.ParserError):
    """응답을 읽을 수 없다."""


def _strip_markup(value: str) -> str:
    """JATS/HTML 태그를 떼고 공백을 정규화한다.

    엔티티는 표준 라이브러리로 풀되 태그 제거 **뒤에** 푼다. 순서를 뒤집으면
    ``&lt;jats:p&gt;`` 가 태그로 되살아나 제거되고, 원문에 있던 부등호가
    사라진다.
    """
    from html import unescape

    text = _TAG.sub(" ", str(value or ""))
    text = unescape(text)
    return _WS.sub(" ", text).strip()


def _walk(document, field_path: str):
    """'message/items/3/title/0' 경로로 노드 하나를 찾는다."""
    node = document
    for part in [p for p in str(field_path or "").split("/") if p]:
        if isinstance(node, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise parsers.FieldPathMissing(
                    f"배열 인덱스가 아닙니다: {part} (경로 {field_path})"
                ) from exc
            if not 0 <= index < len(node):
                raise parsers.FieldPathMissing(
                    f"배열 범위를 벗어났습니다: {field_path}"
                )
            node = node[index]
        elif isinstance(node, dict):
            if part not in node:
                raise parsers.FieldPathMissing(
                    f"경로를 찾을 수 없습니다: {field_path}"
                )
            node = node[part]
        else:
            raise parsers.FieldPathMissing(f"경로를 찾을 수 없습니다: {field_path}")
    return node


def _extract(data: bytes, field_path: str) -> str:
    """등록되는 파서 구현. 바이트에서 경로 하나를 문자열로 뽑는다.

    문자열 노드와 **문자열의 배열**을 받는다. 배열을 받는 이유는 Crossref 의
    저자 목록처럼 값이 여러 조각으로 등록된 필드가 있기 때문이다. 그 밖의
    노드(dict, 숫자)는 거절한다 — 구조를 문자열로 뭉개면 대조의 의미가 없다.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiteratureParseError(
            f"아티팩트를 JSON 으로 읽을 수 없습니다: {exc}"
        ) from exc

    node = _walk(document, field_path)
    if isinstance(node, str):
        return _strip_markup(node)
    if isinstance(node, list) and all(isinstance(item, str) for item in node):
        return _strip_markup("; ".join(node))
    raise parsers.FieldPathMissing(
        f"경로가 문자열이 아닙니다: {field_path} ({type(node).__name__})"
    )


@dataclass(frozen=True)
class LiteratureWork:
    """서지 레코드 하나. 각 필드가 아티팩트 안의 경로를 함께 든다."""

    doi: str
    source: str
    title: str = ""
    abstract: str = ""
    authors: str = ""
    container: str = ""
    publication_date: str = ""
    url: str = ""
    # 필드 이름 -> 아티팩트 내부 경로
    paths: dict = field(default_factory=dict)

    def text_fields(self) -> dict:
        """근거로 쓸 수 있는 필드만. 빈 값은 넣지 않는다."""
        found = {
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "container": self.container,
            "publication_date": self.publication_date,
        }
        return {
            name: value
            for name, value in found.items()
            if value and name in self.paths
        }


def _clip(value: str) -> str:
    text = _strip_markup(value)
    return "" if len(text) > MAX_FIELD_CHARS else text


def _crossref_work(message: dict, prefix: str) -> LiteratureWork | None:
    """Crossref 레코드 하나를 읽는다. ``prefix`` 는 아티팩트 안의 경로 앞부분."""
    if not isinstance(message, dict):
        return None
    doi = str(message.get("DOI") or "").strip().lower()
    if not doi:
        return None

    paths: dict = {}
    values: dict = {}

    titles = message.get("title")
    if isinstance(titles, list) and titles and isinstance(titles[0], str):
        values["title"] = _clip(titles[0])
        paths["title"] = f"{prefix}/title/0"

    abstract = message.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        text = _clip(abstract)
        if text:
            values["abstract"] = text
            paths["abstract"] = f"{prefix}/abstract"

    containers = message.get("container-title")
    if isinstance(containers, list) and containers and isinstance(containers[0], str):
        values["container"] = _clip(containers[0])
        paths["container"] = f"{prefix}/container-title/0"

    # 저자는 given/family 로 쪼개져 있어 한 경로로 뽑을 수 없다. 경로를 만들 수
    # 없는 값은 **근거가 아니다** — paths 에 넣지 않으므로 text_fields 에서
    # 빠지고, 표시용으로만 쓴다.
    authors = message.get("author")
    display_authors = ""
    if isinstance(authors, list):
        names = []
        for item in authors:
            if not isinstance(item, dict):
                continue
            name = " ".join(
                part
                for part in (
                    str(item.get("given") or "").strip(),
                    str(item.get("family") or "").strip(),
                )
                if part
            ) or str(item.get("name") or "").strip()
            if name:
                names.append(name)
        display_authors = ", ".join(names[:12])

    issued = message.get("issued")
    published = ""
    if isinstance(issued, dict):
        parts = (issued.get("date-parts") or [[]])[0]
        if isinstance(parts, list) and parts:
            published = "-".join(str(int(p)).zfill(2 if i else 4)
                                 for i, p in enumerate(parts[:3])
                                 if isinstance(p, int))

    url = str(message.get("URL") or "").strip()
    return LiteratureWork(
        doi=doi,
        source="crossref",
        title=values.get("title", ""),
        abstract=values.get("abstract", ""),
        authors=display_authors,
        container=values.get("container", ""),
        publication_date=published,
        url=url or f"https://doi.org/{doi}",
        paths=paths,
    )


def read_crossref_work(body: bytes) -> LiteratureWork | None:
    """상세 조회 응답(``/works/<doi>``) 하나를 읽는다."""
    document = _load(body)
    message = document.get("message")
    if not isinstance(message, dict):
        return None
    return _crossref_work(message, "message")


def read_crossref_items(body: bytes) -> list:
    """검색 응답(``/works?query...``)의 결과 목록을 읽는다."""
    document = _load(body)
    message = document.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []
    works = []
    for position, item in enumerate(items):
        work = _crossref_work(item, f"message/items/{position}")
        if work is not None:
            works.append(work)
    return works


def read_europepmc_results(body: bytes) -> list:
    """Europe PMC 검색 응답을 읽는다. 상세 조회도 같은 형식이다."""
    document = _load(body)
    result_list = document.get("resultList")
    if not isinstance(result_list, dict):
        return []
    results = result_list.get("result")
    if not isinstance(results, list):
        return []

    works = []
    for position, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        doi = str(item.get("doi") or "").strip().lower()
        if not doi:
            # DOI 없는 레코드는 후보로 쓰지 않는다. 웹 후보·Crossref 후보와
            # 맞출 키가 없어 같은 문헌이 둘로 남는다.
            continue
        prefix = f"resultList/result/{position}"
        paths: dict = {}
        values: dict = {}

        title = item.get("title")
        if isinstance(title, str) and title.strip():
            values["title"] = _clip(title)
            paths["title"] = f"{prefix}/title"

        abstract = item.get("abstractText")
        if isinstance(abstract, str) and abstract.strip():
            text = _clip(abstract)
            if text:
                values["abstract"] = text
                paths["abstract"] = f"{prefix}/abstractText"

        authors = item.get("authorString")
        if isinstance(authors, str) and authors.strip():
            values["authors"] = _clip(authors)
            paths["authors"] = f"{prefix}/authorString"

        journal = ""
        info = item.get("journalInfo")
        if isinstance(info, dict):
            journal_node = info.get("journal")
            if isinstance(journal_node, dict):
                name = journal_node.get("title")
                if isinstance(name, str) and name.strip():
                    journal = _clip(name)
                    paths["container"] = f"{prefix}/journalInfo/journal/title"
        if journal:
            values["container"] = journal

        published = item.get("firstPublicationDate")
        if isinstance(published, str) and published.strip():
            values["publication_date"] = published.strip()
            paths["publication_date"] = f"{prefix}/firstPublicationDate"

        works.append(
            LiteratureWork(
                doi=doi,
                source="europepmc",
                title=values.get("title", ""),
                abstract=values.get("abstract", ""),
                authors=values.get("authors", ""),
                container=values.get("container", ""),
                publication_date=values.get("publication_date", ""),
                url=f"https://doi.org/{doi}",
                paths=paths,
            )
        )
    return works


def _load(body: bytes) -> dict:
    try:
        document = json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiteratureParseError(f"응답을 JSON 으로 읽지 못했습니다: {exc}") from exc
    if not isinstance(document, dict):
        raise LiteratureParseError("응답이 객체가 아닙니다.")
    return document


_REGISTERED = False


def register() -> None:
    """파서와 프로필을 등록한다. 프로세스당 한 번."""
    global _REGISTERED
    if _REGISTERED:
        return
    parsers.register_parser(PARSER_ID, PARSER_VERSION, _extract)
    for profile_id, note in (
        (
            PROFILE_CROSSREF_JSON,
            "Crossref 등록 서지. 발행사가 등록한 메타데이터이지 논문 원문이 "
            "아니다. 초록은 JATS 조각으로 등록되어 있어 파서가 태그를 뗀다.",
        ),
        (
            PROFILE_EUROPEPMC_JSON,
            "Europe PMC 색인 레코드. 초록이 평문으로 오지만 논문 원문(PDF) 의 "
            "발췌라는 보증은 없다.",
        ),
    ):
        parsers.register_profile(
            parsers.SourceProfile(
                profile_id=profile_id,
                parser_id=PARSER_ID,
                parser_version=PARSER_VERSION,
                # 태그를 떼고 공백을 정규화한 텍스트다. 관청·발행사 원문 바이트가
                # 아니므로 official_xml 이 아니다.
                source_kind=SOURCE_NORMALIZED,
                # 확인하지 않은 것을 "번역이 아니다"로 적지 않는다.
                translation_state=TRANSLATION_UNKNOWN,
                language="",
                raw_capable=False,
                note=note,
            )
        )
    _REGISTERED = True
