"""EPO OPS 응답 XML 의 신뢰 파서와 소스 프로필.

이 모듈은 두 가지 일을 한다. 둘을 섞지 않는 것이 중요하다.

    read_documents()   보존된 바이트에서 후보 목록을 읽는다. 어댑터가 쓴다.
    _extract()         보존된 바이트에서 **필드 하나**를 다시 뽑는다.
                       provenance.verify_excerpt 가 쓴다.

둘 다 같은 바이트에서 같은 방식으로 읽는다. 그래서 "어댑터가 보고한 값"과
"검증기가 다시 뽑은 값"이 같아야 정상이고, 다르면 그 자체가 결함 신호다.
검증기는 어댑터의 값을 절대 참조하지 않는다 — 같은 함수를 쓰는 것과 같은
값을 믿는 것은 다르다.

왜 raw_capable=False 인가
-------------------------
OPS 가 돌려주는 XML 은 EPO 의 exchange 형식이다. EP 문헌에서는 EPO 자신의
공식 텍스트지만, 다른 관청 문헌에서는 EPO 가 재수집·재직렬화한 것이고,
초록과 청구항은 **문헌마다** 원문일 수도 EPO 가 제공한 번역일 수도 있다
(``lang`` 속성이 그것을 말해 준다).

소스 프로필은 (파서, 응답 형식, 필드 의미) 단위의 **정적** 선언이다. 문헌마다
달라지는 사실을 정적 선언에 담을 수는 없다. 여기서 raw_capable=True 로 두면
JP 출원의 EPO 영문 번역 초록이 "공식 원문 인용"으로 승격된다 — 이 패키지가
없애려고 만들어진 바로 그 결함이다.

그래서 두 관문을 모두 닫아 둔다.

    source_kind = vendor_xml   ORIGINAL_SOURCE_KINDS 에 없다
    raw_capable = False        프로필이 원문을 증명하지 못한다

원문 등급을 여는 조건은 명확하다. ``country == "EP"`` 이고 해당 필드의
``lang`` 이 그 문헌의 절차언어와 같다는 것을 **파서가 문헌마다 확인**하는
별도 프로필(예: ``epo_ops_ep_procedural_xml``)을 등록하고, 그 프로필로만
그 필드를 가리키게 하는 것이다. 지금은 등록하지 않는다.

그래도 web 채널보다 강하다. web 은 아티팩트 자체가 없어 MATCH_EXACT 에
도달할 수 없고, 여기서는 보존된 바이트에서 문자 그대로 확인된다.

XML 안전
--------
외부에서 온 XML 이다. DOCTYPE·ENTITY 선언이 있으면 파싱하지 않고 거절한다
(엔티티 확장 폭탄). 크기 상한도 둔다. 표준 ElementTree 는 외부 엔티티를
가져오지 않지만 확장 폭탄은 막아 주지 않으므로, 파서에 넣기 전에 끊는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from xml.etree import ElementTree

from . import parsers
from .base import SOURCE_VENDOR_XML, TRANSLATION_UNKNOWN, PatentSearchError

PARSER_ID = "epo_ops_xml"
PARSER_VERSION = "1"
PROFILE_EPO_OPS_XML = "epo_ops_exchange_xml_v1"

# 파싱 전에 끊는 상한. epo_client 의 응답 상한과 같은 값이다.
MAX_XML_BYTES = 8 * 1024 * 1024

# 엔티티 확장 폭탄 차단. 선언 자체를 거절한다.
_DANGEROUS_DECL = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# field_path 문법:  documents/{doc_key}/{field}[:{lang}]
_FIELD_PATH = re.compile(
    r"^documents/(?P<doc>[A-Z0-9.]{3,40})/(?P<field>[a-z_]+)(?::(?P<lang>[a-z]{2}))?$"
)

# 여러 언어로 올 수 있는 텍스트 필드.
MULTILINGUAL_FIELDS = ("title", "abstract", "claims", "description")
# 하나로 조립되는 필드. 원본 문자열이 아니라 ARIA 가 이어 붙인 값이라는 뜻이며,
# 그래서 더더욱 원문 등급을 받을 수 없다.
COMPOSED_FIELDS = (
    "publication_number",
    "application_number",
    "applicants",
    "inventors",
    "ipc",
)
SCALAR_FIELDS = ("publication_date", "family_id")
SUPPORTED_FIELDS = MULTILINGUAL_FIELDS + COMPOSED_FIELDS + SCALAR_FIELDS


class EpoXmlError(PatentSearchError):
    """OPS XML 을 읽을 수 없다."""


def _local(tag: str) -> str:
    """``{http://www.epo.org/exchange}abstract`` → ``abstract``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter(node, name: str):
    """네임스페이스를 무시하고 자손을 훑는다.

    OPS 응답은 한 문서 안에서 ops / exchange / fulltext 세 네임스페이스를
    섞어 쓰고, 구성요소마다 어느 것을 쓰는지 다르다. 접두사를 고정하면
    청구항은 읽히는데 초록은 안 읽히는 식이 된다.
    """
    for child in node.iter():
        if _local(child.tag) == name:
            yield child


def _text_of(node) -> str:
    """요소 아래 텍스트를 문서 순서대로 모은다."""
    return "".join(node.itertext())


def _clean(value: str) -> str:
    """줄 끝 공백과 빈 줄만 정리한다. 문자를 바꾸지 않는다.

    NFKC 같은 정규화는 절대 하지 않는다. ㎜→mm 로 바꾸면 원문에 없는
    문자열이 원문 대조를 통과한다(provenance 모듈이 같은 이유로 금지한다).
    """
    lines = [line.strip() for line in str(value or "").replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _parse_xml(data: bytes):
    if not data:
        raise EpoXmlError("응답이 비어 있습니다.")
    if len(data) > MAX_XML_BYTES:
        raise EpoXmlError(
            f"XML 이 상한({MAX_XML_BYTES:,} bytes)을 넘습니다."
        )
    # **전체 바이트**를 본다. 앞부분만 검사하면 그 길이만큼 주석을 채운 뒤
    # DOCTYPE 을 놓는 것으로 우회된다 — 실측으로 재현했다(5KB 주석 뒤의
    # 내부 엔티티가 그대로 확장됐다). 입력이 이미 8MB 로 제한되어 있으므로
    # 전체를 훑는 비용은 문제가 되지 않는다.
    if _DANGEROUS_DECL.search(data):
        raise EpoXmlError(
            "DOCTYPE 또는 ENTITY 선언이 있는 XML 은 파싱하지 않습니다."
        )
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise EpoXmlError(f"XML 을 파싱하지 못했습니다: {exc}") from exc


def _docdb_id(node):
    """``document-id[document-id-type=docdb]`` 하나에서 (country, number, kind, date)."""
    for candidate in _iter(node, "document-id"):
        if candidate.get("document-id-type") not in (None, "docdb"):
            continue
        parts = {}
        for child in candidate:
            parts[_local(child.tag)] = (child.text or "").strip()
        if parts.get("country") or parts.get("doc-number"):
            return (
                parts.get("country", ""),
                parts.get("doc-number", ""),
                parts.get("kind", ""),
                parts.get("date", ""),
            )
    return ("", "", "", "")


def _doc_key(country: str, number: str, kind: str) -> str:
    return ".".join(part for part in (country, number, kind) if part)


@dataclass(frozen=True)
class EpoDocument:
    """OPS 응답 안의 문헌 하나. 텍스트는 전부 보존된 바이트에서 나온다."""

    doc_key: str
    country: str = ""
    doc_number: str = ""
    kind: str = ""
    family_id: str = ""
    publication_date: str = ""
    application_number: str = ""
    titles: dict = dataclass_field(default_factory=dict)      # lang -> text
    abstracts: dict = dataclass_field(default_factory=dict)
    claims: dict = dataclass_field(default_factory=dict)
    descriptions: dict = dataclass_field(default_factory=dict)
    applicants: tuple = ()
    inventors: tuple = ()
    ipc: tuple = ()

    @property
    def publication_number(self) -> str:
        return f"{self.country}{self.doc_number}{self.kind}"

    @property
    def espacenet_url(self) -> str:
        """Espacenet 의 공개 상세 주소. 응답 본문에서 만들지 않고 조립한다."""
        if not (self.country and self.doc_number):
            return ""
        return (
            "https://worldwide.espacenet.com/patent/search?q="
            f"pn%3D{self.country}{self.doc_number}"
        )

    def text_fields(self) -> dict:
        """(field_path 접미사 -> 텍스트). 어댑터가 EvidenceRef 를 만들 때 쓴다."""
        found: dict = {}
        for name, table in (
            ("title", self.titles),
            ("abstract", self.abstracts),
            ("claims", self.claims),
            ("description", self.descriptions),
        ):
            for lang, text in table.items():
                if not text:
                    continue
                found[f"{name}:{lang}" if lang else name] = text
        if self.applicants:
            found["applicants"] = "\n".join(self.applicants)
        if self.inventors:
            found["inventors"] = "\n".join(self.inventors)
        if self.ipc:
            found["ipc"] = "\n".join(self.ipc)
        if self.publication_date:
            found["publication_date"] = self.publication_date
        if self.family_id:
            found["family_id"] = self.family_id
        if self.doc_number:
            found["publication_number"] = self.publication_number
        if self.application_number:
            found["application_number"] = self.application_number
        return found


def _collect_langs(root, name: str, inner: str | None) -> dict:
    """``name`` 요소들을 lang 별로 모은다. 같은 lang 이 여럿이면 문서 순서로 잇는다."""
    table: dict = {}
    for node in _iter(root, name):
        lang = (node.get("lang") or "").strip().lower()
        if inner:
            pieces = [_text_of(child) for child in _iter(node, inner)]
            text = _clean("\n".join(piece for piece in pieces if piece.strip()))
            if not text:
                text = _clean(_text_of(node))
        else:
            text = _clean(_text_of(node))
        if not text:
            continue
        table[lang] = f"{table[lang]}\n{text}" if lang in table else text
    return table


def _dedupe(values) -> tuple:
    """순서를 지키며 중복을 없앤다.

    OPS 는 같은 출원인을 docdb 형식과 epodoc 형식으로 두 번 준다. 그대로
    이어 붙이면 모든 이름이 두 번 나오고, 그 문자열이 아티팩트의 사실인 것처럼
    보인다.
    """
    seen = set()
    ordered = []
    for value in values:
        text = _clean(value)
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return tuple(ordered)


def _read_one(node) -> EpoDocument | None:
    """exchange-document 또는 fulltext-document 하나를 읽는다."""
    country = (node.get("country") or "").strip()
    number = (node.get("doc-number") or "").strip()
    kind = (node.get("kind") or "").strip()
    date = ""

    publication = next(_iter(node, "publication-reference"), None)
    if publication is not None:
        found_country, found_number, found_kind, found_date = _docdb_id(publication)
        country = country or found_country
        number = number or found_number
        kind = kind or found_kind
        date = found_date
    if not (country and number):
        return None

    application = ""
    application_node = next(_iter(node, "application-reference"), None)
    if application_node is not None:
        app_country, app_number, _, _ = _docdb_id(application_node)
        application = f"{app_country}{app_number}" if app_number else ""

    applicants = _dedupe(
        _text_of(name)
        for holder in _iter(node, "applicant-name")
        for name in _iter(holder, "name")
    )
    inventors = _dedupe(
        _text_of(name)
        for holder in _iter(node, "inventor-name")
        for name in _iter(holder, "name")
    )
    ipc = _dedupe(
        _text_of(entry) for entry in _iter(node, "classification-ipcr")
    )

    return EpoDocument(
        doc_key=_doc_key(country, number, kind),
        country=country,
        doc_number=number,
        kind=kind,
        family_id=(node.get("family-id") or "").strip(),
        publication_date=date,
        application_number=application,
        titles=_collect_langs(node, "invention-title", None),
        abstracts=_collect_langs(node, "abstract", "p"),
        claims=_collect_langs(node, "claims", "claim-text"),
        descriptions=_collect_langs(node, "description", "p"),
        applicants=applicants,
        inventors=inventors,
        ipc=ipc,
    )


def read_documents(data: bytes) -> tuple:
    """보존된 응답 바이트에서 문헌 목록을 읽는다. 검색 응답도 상세 응답도 같다."""
    root = _parse_xml(data)
    documents = []
    seen = set()
    for name in ("exchange-document", "fulltext-document"):
        for node in _iter(root, name):
            document = _read_one(node)
            if document is None or document.doc_key in seen:
                continue
            seen.add(document.doc_key)
            documents.append(document)
    return tuple(documents)


def total_result_count(data: bytes) -> int:
    """검색 응답이 보고한 전체 건수. 없으면 0."""
    root = _parse_xml(data)
    for node in _iter(root, "biblio-search"):
        try:
            return int(node.get("total-result-count") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def field_path(doc_key: str, field_name: str) -> str:
    """EvidenceRef 에 넣을 경로. 만드는 곳을 한 군데로 모은다."""
    return f"documents/{doc_key}/{field_name}"


def _extract(data: bytes, path: str) -> str:
    """신뢰 파서. provenance 가 보존 바이트에서 필드를 다시 뽑을 때 부른다.

    시그니처는 parsers 의 계약(``(bytes, str) -> str``)을 따른다.
    """
    matched = _FIELD_PATH.match(str(path or "").strip())
    if not matched:
        raise parsers.FieldPathMissing(f"경로 형식이 올바르지 않습니다: {path!r}")
    doc_key = matched.group("doc")
    name = matched.group("field")
    lang = matched.group("lang") or ""
    if name not in SUPPORTED_FIELDS:
        raise parsers.FieldPathMissing(f"지원하지 않는 필드입니다: {name!r}")

    try:
        documents = read_documents(data)
    except EpoXmlError as exc:
        raise parsers.ParserError(str(exc)) from exc

    target = next((doc for doc in documents if doc.doc_key == doc_key), None)
    if target is None:
        raise parsers.FieldPathMissing(
            f"응답에 그 문헌이 없습니다: {doc_key}"
        )

    key = f"{name}:{lang}" if lang else name
    values = target.text_fields()
    if key in values:
        return values[key]
    # 언어를 지정하지 않았는데 언어별로만 있는 경우, 어느 것을 줄지 고르지
    # 않는다. 고르면 그 선택이 판정에 들어가고, 다음 버전에서 선택이 바뀌면
    # 과거 판정을 재현할 수 없다.
    if not lang and name in MULTILINGUAL_FIELDS:
        available = sorted(
            existing.split(":", 1)[1]
            for existing in values
            if existing.startswith(f"{name}:")
        )
        if available:
            raise parsers.FieldPathMissing(
                f"{name} 은 언어를 지정해야 합니다. 있는 언어: "
                + ", ".join(available)
            )
    raise parsers.FieldPathMissing(f"응답에 그 필드가 없습니다: {path}")


_REGISTERED = False


def register() -> None:
    """파서와 프로필을 등록한다. 프로세스당 한 번."""
    global _REGISTERED
    if _REGISTERED:
        return
    parsers.register_parser(PARSER_ID, PARSER_VERSION, _extract)
    parsers.register_profile(
        parsers.SourceProfile(
            profile_id=PROFILE_EPO_OPS_XML,
            parser_id=PARSER_ID,
            parser_version=PARSER_VERSION,
            # EPO 의 exchange 재직렬화다. 관청 공식 원문과 문자가 같다고
            # 보증할 수 없으므로 official_xml 이 아니다.
            source_kind=SOURCE_VENDOR_XML,
            # 문헌마다 다르므로 **모른다**고 적는다. False 로 적으면 "번역이
            # 아니다"라는, 우리가 확인하지 않은 진술이 감사 기록에 남는다.
            # 언어는 field_path 의 `:en` 같은 접미사가 들고 있고, 프로필
            # 수준에서는 정할 수 없으므로 빈 값이다.
            translation_state=TRANSLATION_UNKNOWN,
            language="",
            raw_capable=False,
            note=(
                "EPO OPS exchange XML. 문헌마다 원문일 수도 EPO 제공 번역일 "
                "수도 있어 정적 프로필로는 원문을 증명할 수 없다(번역 여부 "
                "unknown). 언어는 field_path 접미사가 들고 있다. 원문 등급을 "
                "열려면 country=EP 와 절차언어를 문헌마다 확인하는 별도 "
                "프로필을 등록해야 한다."
            ),
        )
    )
    _REGISTERED = True


register()
