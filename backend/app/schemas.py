"""API 요청/응답 스키마."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .enums import AttachmentRole, JobKind, OutputMode, RelationType


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
    pass


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
    version: int
    enabled: bool
    # 프롬프트 파일 메타데이터에서만 정한다. 본문과 출력 계약이 함께 움직여야
    # 해서 API 로는 바꿀 수 없다.
    capabilities: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PromptCatalogOut(PromptOut):
    """프롬프트 관리 화면에 표시하는 작업별 카탈로그 항목."""

    kind: str
    editable: bool = True
    deletable: bool = True


class PromptVersionOut(BaseModel):
    id: str
    version: int
    name: str
    description: str = ""
    body: str
    output_mode: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptImportItem(PromptBase):
    pass


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
    prompt_version: int | None = None
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
    prompt_version: int | None = None
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


class SettingsOut(BaseModel):
    values: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    data_dir: str
    runs_dir: str
    env_filtering: dict[str, Any] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    values: dict[str, Any]
