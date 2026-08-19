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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    body = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    archived = Column(Boolean, nullable=False, default=False)
    output_mode = Column(String(20), nullable=False, default="markdown")
    default_provider = Column(String(30), nullable=True)
    default_model = Column(String(80), nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    accepted_file_types = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    versions = relationship(
        "PromptVersion",
        back_populates="prompt",
        cascade="all, delete-orphan",
        order_by="PromptVersion.version",
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version", name="uq_prompt_version"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    prompt_id = Column(
        String(36), ForeignKey("prompt_templates.id", ondelete="CASCADE"), nullable=False
    )
    version = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    body = Column(Text, nullable=False)
    output_mode = Column(String(20), nullable=False, default="markdown")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    prompt = relationship("PromptTemplate", back_populates="versions")


class ExecutionJob(Base):
    __tablename__ = "execution_jobs"

    id = Column(String(36), primary_key=True, default=_uuid)

    # 프롬프트 스냅샷. 원본 템플릿이 수정/삭제돼도 과거 실행을 확인할 수 있어야 한다.
    prompt_id = Column(String(36), nullable=True)
    prompt_name = Column(String(200), nullable=False, default="")
    prompt_version = Column(Integer, nullable=True)
    prompt_snapshot = Column(Text, nullable=False, default="")
    output_mode = Column(String(20), nullable=False, default="markdown")
    user_input = Column(Text, nullable=False, default="")

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
