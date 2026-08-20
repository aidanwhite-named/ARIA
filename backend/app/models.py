"""SQLAlchemy 모델.

SQLite 에는 메타데이터만 둔다. 최종 프롬프트 원문, raw stdout/stderr 처럼
커질 수 있는 것은 artifact 디렉터리에 파일로 쓰고 경로만 저장한다.

인증 토큰, OAuth 토큰, API Key, CLI 인증 파일 내용은 어떤 컬럼에도
저장하지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionJob(Base):
    __tablename__ = "execution_jobs"

    id = Column(String(36), primary_key=True, default=_uuid)

    # 프롬프트 스냅샷. 원본 템플릿이 수정/삭제돼도 과거 실행을 확인할 수 있어야 한다.
    prompt_id = Column(String(36), nullable=True)
    prompt_name = Column(String(200), nullable=False, default="")
    prompt_version = Column(Integer, nullable=True)
    prompt_snapshot = Column(Text, nullable=False, default="")
    output_mode = Column(String(20), nullable=False, default="markdown")
    claim_text = Column(Text, nullable=False, default="")

    # 후속 분석 계보.
    #
    # source_job_id 에 ForeignKey 를 걸지 않는다. 원본 실행을 지워도 "이어받은
    # 실행이었다"는 사실은 남아야 하고, ON DELETE SET NULL 은 그 사실 자체를
    # 지운다. 대신 원본이 사라져도 화면에 표시할 수 있도록 라벨을 스냅샷한다.
    #
    # 이전 청구항과 이전 보고서도 원본에서 매번 읽지 않고 생성 시점에 복사한다.
    # prompt_snapshot 과 같은 이유다. 원본이 지워지거나 바뀌어도 이 실행이 무엇을
    # 입력받았는지가 흔들리면 안 된다.
    source_job_id = Column(String(36), nullable=True, index=True)
    source_job_label = Column(Text, nullable=False, default="")
    relation_type = Column(String(20), nullable=True)
    followup_instruction = Column(Text, nullable=False, default="")
    prior_claim_text = Column(Text, nullable=False, default="")
    prior_report = Column(Text, nullable=False, default="")

    # 이 실행의 보고서에서 읽어 검증한 문헌 매핑. 읽지 못하면 NULL 로 남고,
    # 그 경우 이 실행을 원본 삼아 번호를 물려받는 후속 실행을 만들 수 없다.
    citation_mapping = Column(JSON, nullable=True)
    # 읽지 못한 이유. 화면에서 후속 버튼이 왜 잠겼는지 설명하는 데 쓴다.
    citation_mapping_error = Column(Text, nullable=True)
    # 원본에서 물려받아 이 실행의 첨부에 다시 묶은 고정 매핑.
    prior_citation_mapping = Column(JSON, nullable=True)
    # 실행 시점 프롬프트가 선언한 ARIA 확장. 프롬프트 파일이 나중에 바뀌어도
    # 이 실행이 어떤 계약으로 돌았는지 남는다.
    prompt_capabilities = Column(JSON, nullable=False, default=list)

    provider = Column(String(30), nullable=False)
    model = Column(String(80), nullable=True)
    cli_path = Column(Text, nullable=True)
    cli_version = Column(String(80), nullable=True)
    cli_args = Column(JSON, nullable=False, default=list)

    system_prompt_snapshot = Column(Text, nullable=False, default="")
    final_prompt_path = Column(Text, nullable=True)
    final_prompt_sha256 = Column(String(64), nullable=True)
    final_prompt_chars = Column(Integer, nullable=False, default=0)

    status = Column(String(20), nullable=False, default="QUEUED")
    error_code = Column(String(40), nullable=True)
    terminal_reason = Column(String(60), nullable=True)
    exit_code = Column(Integer, nullable=True)

    warnings = Column(JSON, nullable=False, default=list)
    errors = Column(JSON, nullable=False, default=list)
    permission_denials = Column(JSON, nullable=False, default=list)
    usage = Column(JSON, nullable=True)

    result_text = Column(Text, nullable=True)
    raw_stdout_path = Column(Text, nullable=True)
    raw_stderr_path = Column(Text, nullable=True)
    attachment_manifest = Column(JSON, nullable=False, default=list)
    preprocessing_versions = Column(JSON, nullable=False, default=dict)
    work_dir = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)

    events = relationship(
        "ExecutionEvent",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ExecutionEvent.seq",
    )
    attachments = relationship(
        "Attachment", back_populates="job", cascade="all, delete-orphan"
    )


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(
        String(36), ForeignKey("execution_jobs.id", ondelete="CASCADE"), nullable=False
    )
    seq = Column(Integer, nullable=False)
    ts = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    type = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)

    job = relationship("ExecutionJob", back_populates="events")


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(
        String(36), ForeignKey("execution_jobs.id", ondelete="CASCADE"), nullable=True
    )
    upload_batch = Column(String(36), nullable=True, index=True)

    original_filename = Column(Text, nullable=False)
    internal_filename = Column(Text, nullable=False)
    mime_type = Column(String(120), nullable=False, default="application/octet-stream")
    size_bytes = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False, default="")
    required = Column(Boolean, nullable=False, default=True)
    role = Column(String(30), nullable=False, default="SUPPLEMENTAL")

    stored_path = Column(Text, nullable=False)
    normalized_text_path = Column(Text, nullable=True)
    page_count = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=False, default=0)
    extraction_method = Column(String(30), nullable=False, default="NONE")
    ocr_used = Column(Boolean, nullable=False, default=False)
    delivery_mode = Column(String(40), nullable=False, default="UNSUPPORTED")
    read_ok = Column(Boolean, nullable=False, default=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    job = relationship("ExecutionJob", back_populates="attachments")


class ProviderSnapshot(Base):
    __tablename__ = "provider_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(30), nullable=False, index=True)
    probed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    installed = Column(Boolean, nullable=False, default=False)
    executable_path = Column(Text, nullable=True)
    executable_kind = Column(String(30), nullable=True)
    executable_ok = Column(Boolean, nullable=False, default=False)
    version = Column(String(80), nullable=True)
    auth_state = Column(String(30), nullable=False, default="UNKNOWN")
    capabilities = Column(JSON, nullable=False, default=dict)
    notes = Column(JSON, nullable=False, default=list)


class ResultArtifact(Base):
    __tablename__ = "result_artifacts"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(
        String(36), ForeignKey("execution_jobs.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(String(30), nullable=False)
    path = Column(Text, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(80), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
