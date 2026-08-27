"""특허 검색 연동 인터페이스 (Provider·벤더 중립).

ARIA 본체는 이 파일의 타입과 예외에만 의존한다. Kiwee 든 다른 특허 DB 든
구현 세부는 별도 백엔드 파일에 격리하고, 여기 있는 계약만 공유한다.

증거는 '선언'이 아니라 '재현 가능한 대조'다
--------------------------------------------
백엔드가 돌려준 필드가 곧 '공식 원문'인 것은 아니다. 정규화·태그 제거·OCR·
기계번역·벤더 재가공된 값일 수 있다. 그래서 이 계약에는 두 가지 원칙이 있다.

1) 출처는 **필드마다** 따로 붙는다. 같은 특허 안에서도 청구항은 공식 XML,
   초록은 기계번역, 설명은 OCR 일 수 있다. 레코드 하나에 출처를 하나만 두면
   기계번역 초록이 원문으로 승격된다(실측으로 재현된 결함이다).

2) FieldValue.value 는 **어댑터의 주장이지 증거가 아니다.** 어댑터가 값과
   함께 임의의 해시·경로를 채워 넣을 수 있으므로, 값이 객체 안에 '들어 있다'는
   사실은 아무것도 보증하지 않는다. 최종 판정은 provenance 검증기가 불변
   저장소에서 원본 바이트를 다시 읽고, 해시를 재계산하고, 신뢰된 파서로 필드를
   재추출한 뒤, 그 **재추출한 값**에서 발췌를 찾아 내린다.

   이렇게 하는 진짜 이유는 어댑터를 적으로 보기 때문이 아니다. 어댑터는 우리가
   쓰는 1차 코드다. 이유는 **재현 가능성**이다. 보존된 바이트로부터 같은 판정을
   언제든 다시 만들 수 있어야, 6개월 뒤에도 "이 발췌가 정말 원문인가"에 답할 수
   있다. 이는 search_manifest 가 '모델이 보고한 것'과 'ARIA 가 관측한 것'을
   나눈 원리와 같다.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class PatentSearchError(Exception):
    """특허 검색 연동의 기반 예외."""


class PatentSearchDisabled(PatentSearchError):
    """연동이 설정에서 꺼져 있다."""


class PatentSearchNotConfigured(PatentSearchError):
    """연동은 켜졌지만 접속·인증이 없어 검색할 수 없다.

    지금 단계의 모든 백엔드는 검색 요청에 이 예외를 던진다. 외부 접속과
    인증은 API 계약이 확정되고 NK 동등성이 검증된 뒤에만 구현한다. 그 전까지
    search() 는 절대 네트워크를 건드리지 않는다.
    """


# --- 필드 출처 ------------------------------------------------------------
# 같은 문헌 안에서도 필드마다 다르다. 원문 승격 자격의 입력이 된다.
SOURCE_OFFICIAL_XML = "official_xml"              # 특허청 공식 XML 원문
SOURCE_VENDOR_XML = "vendor_xml"                  # 벤더가 재구성한 XML
SOURCE_NORMALIZED = "normalized"                  # 정규화·태그 제거 텍스트
SOURCE_OCR = "ocr"                                # OCR 결과
SOURCE_MACHINE_TRANSLATION = "machine_translation" # 기계번역문
SOURCE_UNKNOWN = "unknown"                        # 출처 미상 (기본값)

SOURCE_KINDS = (
    SOURCE_OFFICIAL_XML,
    SOURCE_VENDOR_XML,
    SOURCE_NORMALIZED,
    SOURCE_OCR,
    SOURCE_MACHINE_TRANSLATION,
    SOURCE_UNKNOWN,
)

# 원문 인용 등급을 받을 자격이 있는 출처. 벤더 XML 은 여기 없다 — 벤더가
# 재구성한 XML 은 공식 문헌과 문자가 다를 수 있다.
ORIGINAL_SOURCE_KINDS = frozenset({SOURCE_OFFICIAL_XML})


@dataclass(frozen=True)
class EvidenceRef:
    """이 필드가 어느 보존 아티팩트의 어느 위치에서 왔는가.

    검증기는 이 참조만 신뢰 입력으로 받는다. artifact_id 는 내용 주소
    (원본 바이트의 SHA-256)이므로, 저장된 바이트가 id 와 다시 해시되지 않으면
    변조·손상으로 판정된다.

    profile_id 는 '이 경로가 무엇인지'를 검토해서 등록해 둔 소스 프로필이다.
    파서·버전·출처(source_kind, is_translation, language)가 전부 프로필에서
    나온다. 어댑터가 출처를 주장할 통로를 남기지 않기 위해서다.
    """

    artifact_id: str = ""   # 저장소 키 = 원본 바이트의 SHA-256 (hex)
    field_path: str = ""    # 아티팩트 내부 경로 (예: "records/0/claims")
    profile_id: str = ""    # 등록된 소스 프로필 id

    @property
    def complete(self) -> bool:
        """재검증에 필요한 값이 모두 있는가. 하나라도 없으면 검증 불가."""
        return bool(self.artifact_id and self.field_path and self.profile_id)


@dataclass(frozen=True)
class FieldValue:
    """문헌의 텍스트 필드 하나.

    value 는 어댑터가 보고한 값이며 **검증 기준이 아니다**. 검증기는 evidence
    가 가리키는 아티팩트에서 값을 다시 뽑아 그것을 기준으로 삼는다.

    출처를 나타내는 필드가 여기 없는 것은 의도적이다. source_kind 와
    is_translation 을 어댑터가 채우게 두면, 원문 등급을 실제로 결정하는 두
    값이 여전히 '선언'이 된다(실측으로 재현된 결함). 출처는 EvidenceRef 의
    프로필에서만 나온다.
    """

    value: str
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class PatentRecord:
    """검색 결과 한 건. Provider 중립 표현."""

    doc_number: str
    title: str = ""
    fields: Mapping[str, FieldValue] = field(default_factory=dict)
    source_url: str = ""


@dataclass(frozen=True)
class PatentSearchQuery:
    """검색 요청. 사용자 검색식은 백엔드가 자기 문법으로 변환한다."""

    text: str
    max_results: int = 100


@dataclass(frozen=True)
class PatentSearchResponse:
    """검색 응답.

    raw_artifact_id 는 이 응답의 원본 바이트를 보존한 아티팩트다. 필드별
    EvidenceRef 가 이 아티팩트를 가리킨다.
    """

    records: tuple[PatentRecord, ...]
    total_found: int
    raw_artifact_id: str = ""
    fetched_at: str = ""


@dataclass(frozen=True)
class BackendStatus:
    """백엔드의 사용 가능 상태. 화면·경고 문구의 단일 출처."""

    backend_id: str
    display_name: str
    enabled: bool     # 설정 토글이 켜져 있는가
    configured: bool  # 실제로 검색을 수행할 수 있는가
    detail: str = ""


class PatentSearchBackend(abc.ABC):
    """특허 검색 백엔드 계약.

    새 백엔드(Kiwee 재구축 포함, 다른 특허 DB)는 이 클래스를 구현하고
    레지스트리에 등록하면 된다. 본체 코드는 바뀌지 않는다.
    """

    id: str = ""
    display_name: str = ""

    def configure(self, values: Mapping[str, Any]) -> None:
        """설정값을 주입한다. 기본은 아무것도 하지 않는다.

        자격증명이 설정에 저장되는 백엔드(EPO OPS)만 재정의한다. 팩토리를
        무인자로 유지하기 위한 훅이다 — 팩토리 시그니처를 바꾸면 이미 등록된
        백엔드와 register_backend 호출부가 전부 깨진다.

        여기로 들어오는 값은 settings_service.get_all 의 원본이다. API 응답용
        가림(redact) 을 거친 값을 넘기면 자격증명이 빈 문자열이 되어 조용히
        '미설정'으로 보인다.
        """

    @abc.abstractmethod
    def status(self) -> BackendStatus:
        """모델 호출·네트워크 없이 확인 가능한 사용 가능 상태."""

    @abc.abstractmethod
    def search(self, query: PatentSearchQuery) -> PatentSearchResponse:
        """검색 수행.

        구현되지 않은 백엔드는 네트워크를 열기 전에
        PatentSearchNotConfigured 를 던져야 한다.
        """
