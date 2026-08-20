"""API 요청/응답 스키마."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .enums import AttachmentRole, OutputMode, RelationType


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
    max_inline_chars: int


class JobCreate(BaseModel):
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

    @field_validator("relation_type")
    @classmethod
    def _check_relation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {item.value for item in RelationType}
        if value not in allowed:
            raise ValueError(f"relation_type 은 {sorted(allowed)} 중 하나여야 합니다.")
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


class JobOut(BaseModel):
    id: str
    status: str
    result_quality: str | None = None
    error_code: str | None = None
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
    warnings: list[str] = Field(default_factory=list)
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
    result_quality: str | None = None
    error_code: str | None = None
    prompt_name: str
    prompt_version: int | None = None
    provider: str
    model: str | None = None
    created_at: datetime
    duration_ms: int | None = None
    attachment_count: int = 0
    warning_count: int = 0
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
