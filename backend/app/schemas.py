"""API 요청/응답 스키마."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .enums import AttachmentRole, JobKind, OutputMode, PromptKind, RelationType


class PromptBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    body: str = Field(min_length=1)
    output_mode: str = OutputMode.MARKDOWN
    tags: list[str] = Field(default_factory=list)
    accepted_file_types: list[str] = Field(default_factory=list)

    @field_validator("output_mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        allowed = {OutputMode.MARKDOWN.value, OutputMode.TEXT.value}
        if value not in allowed:
            raise ValueError(f"output_mode 는 {sorted(allowed)} 중 하나여야 합니다.")
        return value


class PromptCreate(PromptBase):
    # 만들 프롬프트의 종류. 생략하면 분석 프롬프트다 — 이 필드를 모르는 기존
    # 클라이언트의 동작이 바뀌지 않아야 한다.
    kind: str = PromptKind.ANALYSIS

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        allowed = {item.value for item in PromptKind}
        if value not in allowed:
            raise ValueError(f"kind 는 {sorted(allowed)} 중 하나여야 합니다.")
        return value


class PromptUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    body: str | None = Field(default=None, min_length=1)
    output_mode: str | None = None
    tags: list[str] | None = None
    accepted_file_types: list[str] | None = None
    enabled: bool | None = None

    @field_validator("output_mode")
    @classmethod
    def _check_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {OutputMode.MARKDOWN.value, OutputMode.TEXT.value}
        if value not in allowed:
            raise ValueError(f"output_mode 는 {sorted(allowed)} 중 하나여야 합니다.")
        return value


class PromptOut(PromptBase):
    id: str
    enabled: bool
    # 어느 작업의 프롬프트인가. 파일 메타데이터가 정하며 API 로 바꿀 수 없다 —
    # 종류와 본문 계약이 함께 움직여야 한다.
    kind: str = PromptKind.ANALYSIS
    # 프롬프트 파일 메타데이터에서만 정한다. 본문과 출력 계약이 함께 움직여야
    # 해서 API 로는 바꿀 수 없다.
    capabilities: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PromptCatalogOut(PromptOut):
    """프롬프트 관리 화면에 표시하는 작업별 카탈로그 항목."""

    editable: bool = True
    deletable: bool = True


class PromptImportItem(PromptBase):
    # 내보내기가 적어 준 종류. 없으면 분석이다(옛 내보내기 파일 호환).
    kind: str = PromptKind.ANALYSIS

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        allowed = {item.value for item in PromptKind}
        if value not in allowed:
            raise ValueError(f"kind 는 {sorted(allowed)} 중 하나여야 합니다.")
        return value


class PromptImportRequest(BaseModel):
    prompts: list[PromptImportItem]
    replace_existing: bool = False


class AttachmentAnalysis(BaseModel):
    attachment_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    role: str = AttachmentRole.SUPPLEMENTAL
    page_count: int | None = None
    char_count: int
    extraction_method: str
    delivery_mode: str
    read_ok: bool
    error: str | None = None
    # 업로드 직후의 「분석에 포함」 초기값. 정상 처리된 자료만 True 이며,
    # 화면은 이 값으로 체크박스를 seed 한다(스스로 추측하지 않는다).
    included: bool = True


class UploadResponse(BaseModel):
    batch_id: str
    files: list[AttachmentAnalysis]
    rejected: list[dict[str, str]]
    total_chars: int
    # ARIA 자체 글자 수 한도. null 이면 제한 없음(기본값)이며, 실행을 실제로
    # 막는 것은 Provider 전송 한도와 모델 컨텍스트 한도다.
    max_inline_chars: int | None = None


class JobCreate(BaseModel):
    # 작업 종류. 생략하면 기존 PDF 구성대비 분석이다. 기존 API 클라이언트가
    # 이 필드를 모르고 보내도 동작이 바뀌지 않아야 한다.
    job_kind: str = JobKind.PATENT_ANALYSIS
    # 실행 화면은 이 값을 보내지 않고 Settings 의 기본값을 사용한다.
    # 선택적 override 는 기존 API 클라이언트와 테스트 호환을 위해 유지한다.
    prompt_id: str | None = None
    provider: str | None = None
    model: str | None = None
    claim_text: str = ""
    batch_id: str | None = None
    required_map: dict[str, bool] = Field(default_factory=dict)
    # 이 실행의 분석 자료로 쓸 첨부 id. 준비 화면의 「분석에 포함」 체크박스가
    # 정한다. required_map 과 다른 축이라 재사용하지 않는다 — required 는 "넣기로
    # 한 자료를 못 읽으면 실패시켜라"이고 이쪽은 "애초에 넣을 것인가"다.
    #
    #   None(생략) : 각 첨부에 저장된 값을 그대로 쓴다. 새 업로드는 전부 포함이
    #                기본이므로, 이 필드를 모르는 기존 클라이언트의 동작이 바뀌지
    #                않는다. 물려받은 자료는 원본 실행에서 정한 포함 여부를 잇는다.
    #   목록       : 목록에 있는 첨부만 포함한다. 나머지는 프롬프트·문헌 매핑·
    #                조립 manifest 어디에도 들어가지 않는다.
    #
    # 물려받은 자료를 가리킬 때는 화면에 보이는 원본 실행의 attachment_id 를
    # 쓴다. 복제되며 id 가 바뀌는 것은 작업 생성 쪽에서 맞춘다.
    selected_attachment_ids: list[str] | None = None

    # 후속 분석. source_job_id 와 relation_type 은 항상 함께 온다.
    # batch_id 와 같이 보내면 물려받은 첨부에 새 업로드가 더해진다.
    source_job_id: str | None = None
    relation_type: str | None = None
    followup_instruction: str = ""
    # 구성대비 결과에서 시작하는 미대응 구성 검색. source_job_id 와 함께 쓰며,
    # 일반 유사문헌 검색과 후속 분석에서는 비워 둔다.
    search_component_ids: list[str] = Field(default_factory=list)

    @field_validator("relation_type")
    @classmethod
    def _check_relation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {item.value for item in RelationType}
        if value not in allowed:
            raise ValueError(f"relation_type 은 {sorted(allowed)} 중 하나여야 합니다.")
        return value

    @field_validator("job_kind")
    @classmethod
    def _check_job_kind(cls, value: str) -> str:
        allowed = {item.value for item in JobKind}
        if value not in allowed:
            raise ValueError(f"job_kind 는 {sorted(allowed)} 중 하나여야 합니다.")
        return value


class JobAttachmentOut(BaseModel):
    attachment_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    required: bool
    # 이 실행의 분석 자료였는가. False 면 프롬프트에 들어가지 않았다.
    included: bool = True
    role: str = AttachmentRole.SUPPLEMENTAL
    page_count: int | None = None
    char_count: int
    extraction_method: str
    delivery_mode: str
    read_ok: bool
    error: str | None = None


class PreflightLane(BaseModel):
    """독립 실행 하나가 실제로 보낼 크기. 검색은 두 개, 분석은 한 개다."""

    id: str
    chars: int
    bytes: int


class PreflightOut(BaseModel):
    """실행 전에 잰 최종 조립 프롬프트의 크기.

    화면이 원본 첨부의 글자 수를 세는 것으로는 이 값을 맞힐 수 없다. 실제로
    나가는 본문에는 런타임 컨텍스트·경계 표시·명세서 절이 모두 붙고, Provider
    한도는 문자가 아니라 UTF-8 바이트로 걸린다.
    """

    job_kind: str
    provider: str
    lanes: list[PreflightLane]
    # 한도와 비교할 대표값. 레인이 여럿이면 가장 큰 레인이다 — 한도는 레인마다
    # 따로 걸리므로 합계가 아니라 최댓값이 실행을 막는다.
    chars: int
    bytes: int
    # 사용자가 환경설정에서 스스로 건 글자 수 한도. None 이면 제한 없음.
    char_budget: int | None = None
    # 이 Provider 가 자료 전체를 손실 없이 모델에 전달할 수 있는 바이트 한도.
    # 사용자 입력 제한이 아니라 전달 경로의 한계이며, 끌 수 없다. 한도를
    # 선언하지 않은 Provider 는 None.
    byte_budget: int | None = None
    over_chars: bool = False
    over_bytes: bool = False
    # 지금 실행하면 Provider 호출 전에 막힌다.
    blocked: bool = False
    # 이 입력이 실제로 어떤 방식으로 전달되는가
    # (full_inline / local_retrieval).
    # runner 와 같은 판정 함수(job_assembly.decide_delivery)를 쓴다.
    delivery_plan: str = "full_inline"
    # 왜 그 방식을 골랐는가. 화면이 문장을 새로 만들지 않고 이 값을 그대로 쓴다.
    selection_reason: str = ""
    # 전체 인라인으로 넣었을 때의 크기. auto 가 왜 좁혔는지 설명한다.
    full_inline_bytes: int = 0
    full_inline_chars: int = 0
    # 전달 판정 한 벌. 필드 목록은 job_assembly.AssemblyResult.delivery_manifest.
    delivery_manifest: dict[str, Any] | None = None
    # local_retrieval 일 때 위 chars/bytes 는 예산 상한으로 계산한 **최댓값**이다.
    # 실제 근거 패키지는 이 값을 넘지 못한다.
    evidence_budget_chars: int | None = None
    message: str = ""
    # 조립 자체가 불가능한 상태(명세서 본문을 읽지 못함 등). 크기는 재지 못한다.
    error: str | None = None


class JobOut(BaseModel):
    id: str
    status: str
    error_code: str | None = None
    job_kind: str = JobKind.PATENT_ANALYSIS
    prompt_id: str | None = None
    prompt_name: str
    prompt_snapshot: str
    output_mode: str
    claim_text: str = ""
    source_job_id: str | None = None
    source_job_label: str = ""
    relation_type: str | None = None
    followup_instruction: str = ""
    prior_claim_text: str = ""
    prior_report: str = ""
    citation_mapping: dict[str, Any] | None = None
    prior_citation_mapping: dict[str, Any] | None = None
    prompt_capabilities: list[str] = Field(default_factory=list)
    citation_mapping_error: str | None = None
    analysis_manifest: dict[str, Any] | None = None
    analysis_manifest_error: str | None = None
    search_manifest: dict[str, Any] | None = None
    search_manifest_error: str | None = None
    search_focus: dict[str, Any] | None = None
    # 인용발명 문헌을 어떻게 전달했는가. 값이 없는 과거 실행은 full_inline.
    delivery_plan: str = "full_inline"
    # 그 판정의 근거와 실제 전송 크기. 이 기능 이전 실행은 null 이며, 화면은
    # 없는 사유를 지어내지 않고 delivery_plan 만 표시한다.
    delivery_manifest: dict[str, Any] | None = None
    # 로컬 검색 실행의 감사 기록. 전체 인라인 실행에서는 null.
    retrieval_manifest: dict[str, Any] | None = None
    retrieval_manifest_error: str | None = None
    provider: str
    model: str | None = None
    cli_path: str | None = None
    cli_version: str | None = None
    cli_args: list[str] = Field(default_factory=list)
    system_prompt_snapshot: str = ""
    final_prompt_sha256: str | None = None
    final_prompt_chars: int = 0
    terminal_reason: str | None = None
    exit_code: int | None = None
    errors: list[str] = Field(default_factory=list)
    permission_denials: list[Any] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    result_text: str | None = None
    attachments: list[JobAttachmentOut] = Field(default_factory=list)
    preprocessing_versions: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None


class HistoryItem(BaseModel):
    id: str
    status: str
    error_code: str | None = None
    job_kind: str = JobKind.PATENT_ANALYSIS
    prompt_name: str
    provider: str
    model: str | None = None
    created_at: datetime
    duration_ms: int | None = None
    attachment_count: int = 0
    source_job_id: str | None = None
    source_job_label: str = ""
    relation_type: str | None = None
    has_citation_mapping: bool = False
    # 이 실행에서 이어진 후속 실행 수. 스레드 일괄 삭제 대상 건수와 같다.
    descendant_count: int = 0
    # 인용발명 문헌을 어떻게 전달했는가. 목록에서 바로 구분할 수 있어야 한다.
    delivery_plan: str = "full_inline"


class SettingsOut(BaseModel):
    values: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    data_dir: str
    runs_dir: str
    env_filtering: dict[str, Any] = Field(default_factory=dict)
    # 비밀 값은 values 에서 지워져 나간다. 화면이 "설정됨/미설정"을 그릴 근거는
    # 이쪽뿐이다 — values 의 빈 문자열로는 '지워졌다'와 '가려졌다'를 구별할 수
    # 없다.
    secrets_set: dict[str, bool] = Field(default_factory=dict)
    # EPO OPS 사용량. values 의 날것 상태가 아니라 한도·남은 양까지 계산된 값
    # 이다. 화면과 경고 문구가 같은 숫자를 봐야 하기 때문이다.
    epo_quota: dict[str, Any] = Field(default_factory=dict)
    # agy 의 페이지 열람 허용 목록. ARIA 설정값이 아니라 **다른 도구의 설정
    # 파일에서 읽은 사실**이므로 values 가 아니라 별도 칸이다. 화면이 "권장
    # 호스트가 실제로 적용됐는가"를 그릴 유일한 근거다.
    agy_permissions: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class CredentialCheckOut(BaseModel):
    """외부 데이터 소스 자격증명 확인 결과. 토큰 값은 담지 않는다."""

    ok: bool
    detail: str
    http_status: int | None = None
    expires_in: int | None = None
