"""신뢰 파서와 소스 프로필 — 보존 아티팩트에서 필드와 그 출처를 다시 뽑는다.

검증기는 어댑터가 준 값을 쓰지 않는다. 텍스트도, **그 텍스트가 무엇인지(공식
원문인가·번역문인가)도** 여기서 나온다.

왜 메타데이터까지 여기서 나와야 하나
------------------------------------
텍스트만 아티팩트에서 재추출하고 source_kind·is_translation 은 어댑터가 준
값을 쓰면, 원문 등급을 실제로 결정하는 두 값이 여전히 '선언'이다. 실측으로
재현했다: 일반 JSON blob 을 official_xml 이라고 선언하기만 하면 원문 등급이
나왔다. 그래서 FieldValue 에서 출처 관련 필드를 전부 없애고, 출처는 등록된
소스 프로필에서만 나오게 했다. 어댑터에는 출처를 주장할 통로가 없다.

소스 프로필
-----------
프로필은 '이 응답 형식의 이 경로는 무엇인가'를 한 번 검토해서 등록해 둔 것이다.

    (parser, 응답 형식, 필드 의미) -> source_kind, is_translation, language

generic_json 프로필은 텍스트가 거기 있다는 것만 증명한다. 그것으로 '공식
원문'을 증명할 수는 없으므로 raw_capable=False 다. raw 를 받을 수 있는
프로필은 실제 Kiwee 응답을 검토한 뒤에만 등록한다 — 지금은 하나도 없다.

파서 구현 해시
--------------
(parser_id, version) 등록만으로는 프로세스 안에서만 덮어쓰기를 막는다. 배포
후 같은 json_path v1 의 코드를 고치면 같은 아티팩트에서 다른 문자열이 나올 수
있고, 그러면 과거 판정을 재현할 수 없다. 그래서 구현 소스의 해시를 함께
기록한다. 재검증 때 해시가 다르면 '다른 결과'가 아니라 '재검증 불가'로 다룬다.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass

from .base import (
    SOURCE_KINDS,
    SOURCE_UNKNOWN,
    TRANSLATION_NO,
    TRANSLATION_STATES,
    TRANSLATION_YES,
    PatentSearchError,
)

PARSER_JSON_PATH = "json_path"
PARSER_JSON_PATH_VERSION = "1"

# 검토 없이 쓸 수 있는 기본 프로필. 텍스트 존재만 증명한다.
PROFILE_GENERIC_JSON = "generic_json"


class ParserError(PatentSearchError):
    """파서를 찾을 수 없거나 경로를 추출할 수 없다."""


class ParserNotRegistered(ParserError):
    """등록되지 않은 파서 id 또는 버전이다."""


class ProfileNotRegistered(ParserError):
    """등록되지 않은 소스 프로필이다."""


class FieldPathMissing(ParserError):
    """아티팩트 안에 그 경로가 없다."""


@dataclass(frozen=True)
class ExtractedField:
    """아티팩트에서 재추출한 필드. 텍스트와 출처가 함께 온다.

    이 값들은 전부 등록된 프로필과 신뢰 파서에서 나온다. 어댑터가 개입할 수
    있는 곳이 없다.
    """

    text: str
    source_kind: str
    translation_state: str
    language: str
    profile_id: str
    parser_id: str
    parser_version: str
    parser_sha256: str
    raw_capable: bool

    @property
    def is_translation(self) -> bool:
        """번역이라고 **확인된** 경우에만 참.

        unknown 을 참으로 만들지 않는다. 원문 등급 판정은 이 값이 아니라
        translation_state == TRANSLATION_NO 를 봐야 한다 — unknown 을 거짓으로
        읽고 "번역이 아니다"로 통과시키면 안 되기 때문이다.
        """
        return self.translation_state == TRANSLATION_YES


@dataclass(frozen=True)
class SourceProfile:
    """응답 형식의 특정 필드가 무엇인지에 대한, 검토를 거친 선언.

    raw_capable 은 '이 프로필로 뽑은 값이 공식 원문 인용이 될 수 있는가'다.
    참으로 두려면 실제 응답을 보고 그 필드가 공식 XML 원문임을 확인해야 한다.

    번역 여부는 세 값을 가진다(base.TRANSLATION_*). 예전에는 불리언이었는데,
    그러면 "번역이 아니다"와 "번역인지 모른다"가 같은 값이 되어 기록이 실제로
    아는 것보다 강해진다. is_translation 인자는 그 시절 호출부를 위해 남겨
    두었고, translation_state 를 명시하면 그쪽이 이긴다.
    """

    profile_id: str
    parser_id: str
    parser_version: str
    source_kind: str = SOURCE_UNKNOWN
    is_translation: bool = False
    translation_state: str = ""
    language: str = ""
    raw_capable: bool = False
    note: str = ""

    @property
    def resolved_translation_state(self) -> str:
        """실제로 쓰는 값. 명시하지 않았으면 옛 불리언에서 끌어온다."""
        if self.translation_state:
            return self.translation_state
        return TRANSLATION_YES if self.is_translation else TRANSLATION_NO


def _json_path_extract(data: bytes, field_path: str) -> str:
    """'records/0/claims' 같은 경로로 JSON 에서 문자열 하나를 뽑는다.

    반드시 문자열 노드여야 한다. dict/list 를 문자열로 뭉개면 원문 대조의
    의미가 사라지므로 거절한다.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParserError(f"아티팩트를 JSON 으로 읽을 수 없습니다: {exc}") from exc

    node = document
    for part in [p for p in field_path.split("/") if p]:
        if isinstance(node, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise FieldPathMissing(
                    f"배열 인덱스가 아닙니다: {part} (경로 {field_path})"
                ) from exc
            if not 0 <= index < len(node):
                raise FieldPathMissing(f"배열 범위를 벗어났습니다: {field_path}")
            node = node[index]
        elif isinstance(node, dict):
            if part not in node:
                raise FieldPathMissing(f"경로를 찾을 수 없습니다: {field_path}")
            node = node[part]
        else:
            raise FieldPathMissing(f"경로를 찾을 수 없습니다: {field_path}")

    if not isinstance(node, str):
        raise FieldPathMissing(
            f"경로가 문자열이 아닙니다: {field_path} ({type(node).__name__})"
        )
    return node


def _implementation_sha256(fn: Callable[[bytes, str], str]) -> str:
    """파서 구현 소스의 해시.

    주석만 고쳐도 값이 바뀐다. 그 방향이 안전한 쪽이다 — 바뀐 것을 못 보고
    지나가는 것보다, 안 바뀐 것을 바뀌었다고 표시하는 편이 낫다.
    """
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):  # pragma: no cover - 소스 없는 환경
        return ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


# (parser_id, parser_version) -> 추출 함수
_PARSERS: dict[tuple[str, str], Callable[[bytes, str], str]] = {
    (PARSER_JSON_PATH, PARSER_JSON_PATH_VERSION): _json_path_extract,
}

# profile_id -> SourceProfile
#
# raw_capable=True 인 프로필은 하나도 없다. 실제 Kiwee 응답에 공식 XML 원문
# 필드가 있다는 것이 확인된 뒤에 등록한다.
_PROFILES: dict[str, SourceProfile] = {
    PROFILE_GENERIC_JSON: SourceProfile(
        profile_id=PROFILE_GENERIC_JSON,
        parser_id=PARSER_JSON_PATH,
        parser_version=PARSER_JSON_PATH_VERSION,
        source_kind=SOURCE_UNKNOWN,
        raw_capable=False,
        note="검토되지 않은 일반 JSON. 텍스트 존재만 증명한다.",
    ),
}


def register_parser(
    parser_id: str, version: str, fn: Callable[[bytes, str], str]
) -> None:
    """새 파서를 등록한다. 기존 (id, version) 은 덮어쓰지 않는다 —
    이미 그 버전으로 내려진 판정을 재현할 수 없게 되기 때문이다."""
    key = (parser_id, version)
    if key in _PARSERS:
        raise ParserError(f"이미 등록된 파서입니다: {parser_id} v{version}")
    _PARSERS[key] = fn


def register_profile(profile: SourceProfile) -> None:
    """소스 프로필을 등록한다.

    raw_capable 프로필은 실제 응답을 검토한 뒤에만 등록해야 한다. 그 검토가
    이 시스템에서 '공식 원문'을 주장할 수 있는 유일한 근거다.
    """
    if profile.profile_id in _PROFILES:
        raise ParserError(f"이미 등록된 프로필입니다: {profile.profile_id}")
    if profile.source_kind not in SOURCE_KINDS:
        raise ParserError(f"알 수 없는 source_kind 입니다: {profile.source_kind}")
    if profile.resolved_translation_state not in TRANSLATION_STATES:
        raise ParserError(
            f"알 수 없는 translation_state 입니다: {profile.translation_state!r}"
        )
    if (profile.parser_id, profile.parser_version) not in _PARSERS:
        raise ParserNotRegistered(
            f"프로필이 가리키는 파서가 없습니다: "
            f"{profile.parser_id} v{profile.parser_version}"
        )
    _PROFILES[profile.profile_id] = profile


def get_profile(profile_id: str) -> SourceProfile:
    profile = _PROFILES.get(profile_id)
    if profile is None:
        raise ProfileNotRegistered(f"등록되지 않은 프로필입니다: {profile_id!r}")
    return profile


def raw_capable_profiles() -> tuple[str, ...]:
    """원문 등급을 받을 수 있는 프로필 목록. 지금은 비어 있다."""
    return tuple(
        sorted(pid for pid, p in _PROFILES.items() if p.raw_capable)
    )


def extract(data: bytes, field_path: str, profile_id: str) -> ExtractedField:
    """등록된 프로필로 아티팩트에서 필드와 그 출처를 재추출한다."""
    profile = get_profile(profile_id)
    fn = _PARSERS.get((profile.parser_id, profile.parser_version))
    if fn is None:
        raise ParserNotRegistered(
            f"등록되지 않은 파서입니다: "
            f"{profile.parser_id!r} v{profile.parser_version!r}"
        )
    text = fn(data, field_path)
    return ExtractedField(
        text=text,
        source_kind=profile.source_kind,
        translation_state=profile.resolved_translation_state,
        language=profile.language,
        profile_id=profile.profile_id,
        parser_id=profile.parser_id,
        parser_version=profile.parser_version,
        parser_sha256=_implementation_sha256(fn),
        raw_capable=profile.raw_capable,
    )
