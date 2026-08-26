"""문헌 매핑 프로토콜.

후속 분석에서 인용발명 번호를 1차 보고서와 맞추려면 이전 상태가 조금 필요하다.
그런데 필요한 것은 "인용발명 2 = KR10-9876543" 이라는 매핑 하나이지 보고서
전체가 아니다. 보고서를 통째로 다시 넣으면 번호를 얻는 대가로 이전 유사도와
발췌문까지 모델 앞에 놓이고, 그러면 재검토가 1차 결론에 끌린다.

그래서 매핑만 따로 받는다. Master Prompt 가 보고서 끝에 아래 블록을 출력하고,
ARIA 는 그것만 읽어서 검증한 뒤 저장한다.

    [ARIA_CITATION_MAPPING_V1]
    {"items": [{"citation_number": 1, "attachment": "ATT-02",
                "document_number": "KR10-1234567"}]}
    [/ARIA_CITATION_MAPPING_V1]

여기에는 두 가지 원칙이 걸려 있다.

1. ARIA 는 보고서를 해석하지 않는다.
   Markdown 표를 파싱하면 사용자가 출력 형식을 조금만 바꿔도 조용히 깨진다.
   대신 버전이 붙은 전용 블록을 쓴다. 이건 분석이 아니라 프로토콜이다.

2. 모델에게 UUID 나 sha256 을 옮겨 적게 하지 않는다.
   32자 UUID 와 64자 해시는 모델이 틀리는 종류의 값이다. ARIA 가 첨부마다
   ATT-01 같은 짧은 별칭을 붙여서 보여주고, 모델은 그 별칭만 쓴다. 실제
   attachment_id 와 sha256 은 ARIA 가 자기 기록에서 채운다.

블록을 읽지 못해도 실행은 성공으로 둔다. 매핑은 후속 기능이지 분석 요건이
아니다. 대신 매핑을 쓰는 후속 실행만 막고, 보고서 전체 전달로 조용히
되돌아가지 않는다. 그렇게 하면 사용자가 모르는 사이에 앵커링이 되살아난다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# 프롬프트가 메타데이터에 선언해야 이 기능이 켜진다.
CAPABILITY = "citation_mapping_v1"

MAPPING_VERSION = 1
_OPEN = "[ARIA_CITATION_MAPPING_V1]"
_CLOSE = "[/ARIA_CITATION_MAPPING_V1]"

# 블록은 보고서 어디에 있어도 찾는다. 코드펜스로 감싸 나오는 경우가 흔해서
# 앞뒤 펜스도 같이 걷어낸다.
_BLOCK = re.compile(
    r"(?:```[\w-]*\s*\n)?"
    + re.escape(_OPEN)
    + r"\s*(?P<payload>.*?)\s*"
    + re.escape(_CLOSE)
    + r"(?:\s*\n```)?",
    re.DOTALL,
)

_ALIAS = re.compile(r"^ATT-\d{2,}$")


class MappingError(Exception):
    """블록이 없거나, 형식이 아니거나, 첨부와 맞지 않는다."""


@dataclass(frozen=True)
class AliasedAttachment:
    alias: str
    attachment_id: str
    sha256: str
    original_filename: str


def alias_for(index: int) -> str:
    """1부터 시작하는 표시 순번을 별칭으로 만든다."""
    return f"ATT-{index:02d}"


def ordered_attachments(attachments):
    """별칭을 붙이는 순서. 최종 프롬프트에 나타나는 순서와 같아야 한다.

    정렬과 별칭 부여를 **같은 모듈에 둔다.** 둘이 떨어져 있으면 새 호출부가
    정렬을 잊고 assign_aliases 만 부르게 되고, 그러면 같은 실행 안에서 ATT-01
    이 서로 다른 자료를 가리키게 된다. 실제로 로컬 검색 corpus 가 그렇게
    어긋난 적이 있다 — 근거 패키지의 ATT-01 과 프롬프트 첨부 헤더의 ATT-01 이
    다른 문헌이었다. 모델은 그 사실을 알 방법이 없다.
    """
    from .enums import AttachmentRole

    order = (
        AttachmentRole.APPLICATION,
        AttachmentRole.CITATION,
        AttachmentRole.SUPPLEMENTAL,
    )
    ranked = []
    for role in order:
        ranked += [a for a in attachments if a.role == role]
    # 알 수 없는 역할이 생겨도 빠뜨리지 않는다.
    ranked += [a for a in attachments if a.role not in order]
    return ranked


def assign_aliases(attachments) -> dict[str, AliasedAttachment]:
    """첨부에 별칭을 붙인다. 키는 별칭이다.

    순서는 최종 프롬프트에 나타나는 순서와 같아야 한다. 그래야 모델이 본 화면과
    ARIA 가 되돌리는 표가 일치한다. 호출하는 쪽이 정렬된 목록을 넘긴다.
    """
    table: dict[str, AliasedAttachment] = {}
    for index, item in enumerate(attachments, start=1):
        alias = alias_for(index)
        table[alias] = AliasedAttachment(
            alias=alias,
            attachment_id=item.attachment_id,
            sha256=item.sha256,
            original_filename=item.original_filename,
        )
    return table


def strip_block(text: str) -> str:
    """사람이 읽을 보고서에서 프로토콜 블록을 걷어낸다.

    사용자가 받아 가는 산출물에 기계용 JSON 이 섞여 나오면 안 된다. 원문은
    stdout.log 에 그대로 남으므로 감사 기록은 잃지 않는다.
    """
    return _BLOCK.sub("", text).rstrip() + ("\n" if text.endswith("\n") else "")


def parse(text: str, aliases: dict[str, AliasedAttachment]) -> dict:
    """보고서에서 매핑 블록을 읽어 검증된 스냅샷으로 만든다.

    실패하면 MappingError 를 던진다. 부분적으로 성공한 결과는 만들지 않는다.
    인용발명 번호 하나가 조용히 빠지면 다음 보고서의 번호가 어긋난다.
    """
    matches = _BLOCK.findall(text)
    if not matches:
        raise MappingError("보고서에서 문헌 매핑 블록을 찾지 못했습니다.")
    if len(matches) > 1:
        raise MappingError(
            f"문헌 매핑 블록이 {len(matches)}개 있습니다. 하나만 있어야 합니다."
        )

    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise MappingError(f"문헌 매핑 블록이 JSON 이 아닙니다: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MappingError("문헌 매핑 블록은 객체여야 합니다.")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise MappingError("문헌 매핑에 items 배열이 없습니다.")

    items: list[dict] = []
    seen_numbers: set[int] = set()
    seen_aliases: set[str] = set()

    for entry in raw_items:
        if not isinstance(entry, dict):
            raise MappingError("문헌 매핑 항목이 객체가 아닙니다.")

        number = entry.get("citation_number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise MappingError(f"인용발명 번호가 1 이상의 정수가 아닙니다: {number!r}")
        if number in seen_numbers:
            raise MappingError(f"인용발명 번호가 중복됩니다: {number}")
        seen_numbers.add(number)

        alias = str(entry.get("attachment") or "").strip().upper()
        if not _ALIAS.match(alias):
            raise MappingError(f"자료 번호 형식이 아닙니다: {entry.get('attachment')!r}")
        if alias not in aliases:
            raise MappingError(f"이 실행에 없는 자료 번호입니다: {alias}")
        if alias in seen_aliases:
            raise MappingError(f"같은 자료에 두 개의 인용발명 번호가 붙었습니다: {alias}")
        seen_aliases.add(alias)

        document_number = str(entry.get("document_number") or "").strip()
        if not document_number:
            raise MappingError(f"인용발명 {number} 의 고유 문헌번호가 비어 있습니다.")

        source = aliases[alias]
        items.append(
            {
                "citation_number": number,
                # 실제 식별자는 모델이 아니라 ARIA 가 채운다.
                "attachment_id": source.attachment_id,
                "attachment_sha256": source.sha256,
                "filename": source.original_filename,
                "document_number": document_number,
            }
        )

    items.sort(key=lambda row: row["citation_number"])
    return {"version": MAPPING_VERSION, "items": items}


def rebind(mapping: dict | None, attachments) -> dict:
    """부모의 매핑을 자식 실행의 첨부에 다시 묶는다.

    후속 실행은 첨부를 복제하므로 attachment_id 가 바뀐다. 내용은 같으니
    sha256 으로 다시 찾는다. 매핑에 sha256 을 넣어 두는 이유가 이것이다.

    한 항목이라도 짝을 찾지 못하면 실패시킨다. 번호 하나를 조용히 버리면
    이번 보고서의 번호가 1차와 어긋나는데, 그게 이 기능의 존재 이유다.
    """
    if not mapping or not mapping.get("items"):
        raise MappingError("이어받을 문헌 매핑이 없습니다.")

    by_hash: dict[str, object] = {}
    for item in attachments:
        by_hash.setdefault(item.sha256, item)

    rebound: list[dict] = []
    for row in mapping["items"]:
        target = by_hash.get(row.get("attachment_sha256"))
        if target is None:
            raise MappingError(
                f"인용발명 {row.get('citation_number')} 에 해당하는 자료를 "
                f"이 실행에서 찾지 못했습니다: {row.get('filename')}"
            )
        rebound.append({**row, "attachment_id": target.attachment_id})

    rebound.sort(key=lambda row: row["citation_number"])
    return {"version": MAPPING_VERSION, "items": rebound}


def render(mapping: dict | None, aliases: dict[str, AliasedAttachment]) -> str:
    """고정 매핑을 프롬프트에 넣을 형태로 쓴다.

    ARIA 는 여기서 업무 지시를 붙이지 않는다. 무엇이 어느 번호인지만 적는다.
    그 번호를 어떻게 쓸지는 Master Prompt 의 「후속 처리 규칙」에 있다.
    """
    if not mapping or not mapping.get("items"):
        return ""
    by_attachment = {item.attachment_id: item.alias for item in aliases.values()}
    lines: list[str] = []
    for row in mapping["items"]:
        alias = by_attachment.get(row["attachment_id"], "")
        suffix = f" ({alias}, {row['filename']})" if alias else f" ({row['filename']})"
        lines.append(f"인용발명 {row['citation_number']} = {row['document_number']}{suffix}")
    return "\n".join(lines)
