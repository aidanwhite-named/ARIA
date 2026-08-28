"""상태값 정의.

생명주기(status)와 실패 원인(error_code)을 분리한다. 두 축을 하나의 enum 에
섞으면 Provider 를 추가할 때마다 계속 커지고, UI 와 재시도 정책이 같은 값을
다르게 해석하게 된다.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    TIMED_OUT = "TIMED_OUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    EMPTY_RESULT = "EMPTY_RESULT"
    PROCESS_ERROR = "PROCESS_ERROR"
    ATTACHMENT_ERROR = "ATTACHMENT_ERROR"
    TOOL_POLICY_VIOLATION = "TOOL_POLICY_VIOLATION"
    CANCELLED = "CANCELLED"
    # 검색 작업 전용.
    # 검색 도구를 한 번도 부르지 않고 기억만으로 답한 실행. 결과가 그럴듯해도
    # 그것은 검색 결과가 아니므로 성공으로 두지 않는다.
    SEARCH_NOT_PERFORMED = "SEARCH_NOT_PERFORMED"
    # 허용된 도구 호출 횟수를 넘겨서 ARIA 가 프로세스를 끊었다.
    SEARCH_BUDGET_EXCEEDED = "SEARCH_BUDGET_EXCEEDED"
    # 검색 프롬프트 파일을 읽지 못했거나 placeholder 가 없다.
    SEARCH_PROMPT_ERROR = "SEARCH_PROMPT_ERROR"
    # 비대화형 Provider 가 검색/페이지 열람 권한을 요청했지만 승인할 사람이 없어
    # 거부됐다. agy 는 이 경우에도 종료 코드 0 과 빈 응답을 돌려줄 수 있으므로
    # EMPTY_RESULT 보다 먼저 구분해야 한다.
    SEARCH_PERMISSION_DENIED = "SEARCH_PERMISSION_DENIED"
    # 로컬 검색(retrieval) 전용.
    # 이 실행 환경에서 로컬 검색 인덱스를 만들 수 없거나(FTS5 없음), 색인할 수
    # 있는 문헌이 하나도 없다. 검색 없이 근거를 지어내지 않으므로 실패시킨다.
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    # 로컬 검색 루프가 근거 패키지를 만들지 못했다.
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"


class JobKind(StrEnum):
    """이 실행이 무슨 종류의 작업인가.

    작업 종류는 도구 정책을 결정한다. 두 종류는 입력도 출력도 실행 계약도
    다르므로 하나의 경로에 플래그로 섞지 않는다.

      PATENT_ANALYSIS   : 첨부한 PDF 를 인라인으로 넣고 도구를 전부 끈 채
                          청구항과 인용발명을 구성별로 대비한다.
      SIMILARITY_SEARCH : 청구항을 기준으로 WebSearch/WebFetch 만 허용해서
                          유사 문헌 검토 후보를 탐색한다. 선택 명세서는 격리된
                          보조 검색에만 사용한다.

    값이 비어 있는 과거 실행은 PATENT_ANALYSIS 로 읽는다.
    """

    PATENT_ANALYSIS = "patent_analysis"
    SIMILARITY_SEARCH = "similarity_search"

    @property
    def accepts_attachments(self) -> bool:
        return self is JobKind.PATENT_ANALYSIS


class DeliveryMode(StrEnum):
    """첨부 자료가 실제로 모델에게 전달된 방식."""

    INLINE_CONTEXT = "DELIVERED_AS_INLINE_CONTEXT"
    NATIVE_FILE = "NATIVE_FILE"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"


class DeliveryPlan(StrEnum):
    """인용발명 문헌을 최종 분석 모델에게 어떻게 전달했는가.

    첨부 하나의 상태가 아니라 **실행 전체의 전달 방식**이다. DeliveryMode 와
    축이 다르므로 섞지 않는다 — 로컬 검색으로 전달한 실행에서도 각 첨부의
    delivery_mode 는 "본문을 읽었는가"를 그대로 뜻한다.

      FULL_INLINE      정규화 텍스트 전체를 프롬프트에 넣는다.
      LOCAL_RETRIEVAL  ARIA 가 로컬 색인하고, AI 가 구조화된 검색 action 으로
                       찾은 구간을 근거 패키지로 넣는다. 근거 패키지에는 찾은
                       구간뿐 아니라 **그 구간이 있는 페이지 전문과 앞뒤
                       페이지**가 예산이 허락하는 만큼 함께 들어간다
                       (retrieval.pages).

    폭이 둘뿐인 것은 의도다. 한때 그 사이에 「페이지 단위」 전달을 따로 두었는데,
    같은 검색을 돌리고 담는 단위만 다른 것이라 전달 방식이 아니라 **근거 패키지의
    확장 방식**이 맞았다. 모드로 두면 사용자가 고를 축이 하나 늘어날 뿐이고,
    "검색은 했는데 어느 폭으로 담겼나"를 두 군데서 설명하게 된다.

    값이 비어 있는 과거 실행은 FULL_INLINE 이다.
    """

    FULL_INLINE = "full_inline"
    LOCAL_RETRIEVAL = "local_retrieval"

    @classmethod
    def coerce(cls, value: str | None) -> "DeliveryPlan":
        """저장된 값을 읽는다. 모르는 값은 좁은 쪽으로 해석한다.

        폐기된 focused_pages 같은 옛 값이 남아 있을 수 있다. 그 실행도 검색을
        돌린 실행이므로 LOCAL_RETRIEVAL 로 읽는 편이 사실에 가깝다 — 전체
        인라인으로 읽으면 "문헌 전체를 모델이 봤다"가 되어 거짓이 된다.
        """
        text = str(value or "").strip()
        if not text:
            return cls.FULL_INLINE
        try:
            return cls(text)
        except ValueError:
            return cls.LOCAL_RETRIEVAL


class RetrievalMode(StrEnum):
    """사용자가 고른 전달 방식 정책.

    AUTO 는 크기를 보고 고른다. 문서를 조용히 자르거나 요약하지 않으므로,
    고르는 기준은 "이 Provider 와 모델이 자료 전체를 손실 없이 받을 수 있는가"
    하나다. 넣지 못한 범위는 미확인으로 기록된다.
    """

    AUTO = "auto"
    FULL = "full"
    RETRIEVAL = "retrieval"

    @classmethod
    def coerce(cls, value: str | None) -> "RetrievalMode":
        """설정에서 읽는다. 폐기된 값은 뜻이 가장 가까운 쪽으로 옮긴다.

        focused 는 「페이지 단위로 담아라」였고 그 동작은 지금 RETRIEVAL 안에
        들어가 있다. AUTO 로 되돌리면 사용자가 명시적으로 좁혀 두었던 설정이
        조용히 넓어지므로 RETRIEVAL 로 옮긴다.
        """
        text = str(value or "").strip().lower()
        if text == "focused":
            return cls.RETRIEVAL
        try:
            return cls(text or cls.AUTO)
        except ValueError:
            return cls.AUTO


class AttachmentRole(StrEnum):
    """분석 안에서 첨부 자료가 맡는 역할."""

    APPLICATION = "APPLICATION"
    CITATION = "CITATION"
    SUPPLEMENTAL = "SUPPLEMENTAL"


def is_local_search_target(role: str) -> bool:
    """이 첨부를 로컬 검색(retrieval)의 **검색 대상**으로 삼아도 되는가.

    출원발명 문서는 절대 검색 대상이 아니다. 두 가지가 동시에 깨지기 때문이다.

      1. 자기 발명을 인용발명처럼 검색해서 "대응 구성을 찾았다"고 판정할 수 있다.
         구성대비에서 이보다 나쁜 오류는 없다.
      2. 출원발명 명세서는 청구항 문언을 해석하는 기준 자료다. 검색으로 일부만
         전달하면 해석의 근거가 임의로 잘린다.

    그래서 출원발명 문서는 로컬 검색 실행에서도 **본문 전체가 인라인으로**
    들어간다. 그 때문에 전송 한도를 넘으면 자르지 않고 INPUT_TOO_LARGE 로
    중단한다.

    이 함수를 prompt_assembly 와 retrieval 양쪽이 함께 쓴다. 두 곳이 각자
    판단하면 "검색 대상에서는 뺐는데 본문도 빠진" 상태가 만들어진다.
    """
    return str(role) != AttachmentRole.APPLICATION


class RelationType(StrEnum):
    """후속 실행이 원본 실행에서 무엇을 물려받았는지.

    자료 재사용과 맥락 이어받기는 서로 다른 선택이다. 하나의 컬럼에 두 의미를
    섞으면 "보고서는 이어받았는데 자료는 안 받았다" 같은 표현 불가능한 조합이
    스키마상 가능해진다.

      MAPPED     : 첨부 + 이전 청구항 + 검증된 문헌 매핑. 이전 보고서는 넣지
                   않는다. 종속항 추가 분석의 기본 경로다. 번호는 유지되고
                   유사도·발췌문은 앵커링 없이 다시 판단된다.
      CONTINUED  : 여기에 이전 보고서 전체를 더한다. 보고서 자체를 고치거나
                   보완할 때만 쓴다.
      REANALYZED : 첨부만 물려받는다. 번호도 이전 판단도 물려받지 않는다.

    값이 없으면 독립 실행이다.
    """

    MAPPED = "MAPPED"
    CONTINUED = "CONTINUED"
    REANALYZED = "REANALYZED"

    @property
    def inherits_mapping(self) -> bool:
        return self in (RelationType.MAPPED, RelationType.CONTINUED)

    @property
    def inherits_report(self) -> bool:
        return self is RelationType.CONTINUED


class ExtractionMethod(StrEnum):
    RAW_TEXT = "RAW_TEXT"
    PDF_TEXT_LAYER = "PDF_TEXT_LAYER"
    NONE = "NONE"


class OutputMode(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"


class AuthState(StrEnum):
    OK = "OK"
    NOT_LOGGED_IN = "NOT_LOGGED_IN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
