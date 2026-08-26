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


class ExecutionJob(Base):
    __tablename__ = "execution_jobs"

    id = Column(String(36), primary_key=True, default=_uuid)

    # 작업 종류. 도구 정책과 입력 형태를 결정한다. 과거 실행은 전부 PDF 구성대비
    # 분석이므로 기본값이 그것이다. enums.JobKind 를 보라.
    job_kind = Column(String(30), nullable=False, default="patent_analysis")

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

    # 구성별 유사도·미발견·판독 제한을 담은 기계 판독용 분석 결과. 사용자용
    # Markdown 을 다시 파싱하지 않고 미대응 구성 검색을 시작하는 근거다.
    analysis_manifest = Column(JSON, nullable=True)
    analysis_manifest_error = Column(Text, nullable=True)

    # 인용발명 문헌을 최종 분석 모델에게 어떻게 전달했는가.
    # enums.DeliveryPlan 을 보라. 값이 비어 있는 과거 실행은 full_inline 이다.
    delivery_plan = Column(String(30), nullable=False, default="full_inline")

    # 로컬 검색(retrieval) 실행의 감사 기록. 인덱스 재현 정보, 라운드별 LLM
    # 입출력 해시, 실행된 검색어, 예산, 라이브러리 버전이 들어간다. 전체 인라인
    # 실행에서는 NULL 이다.
    retrieval_manifest = Column(JSON, nullable=True)
    # 로컬 검색이 근거 패키지를 만들지 못한 사유. 화면이 "왜 실패했는지"를
    # 설명하는 데 쓴다.
    retrieval_manifest_error = Column(Text, nullable=True)

    # 유사 문헌 검색의 감사 기록. 검색어, 라운드, 후보 출처, 접근 실패,
    # 검색 프롬프트 해시가 들어간다. 분석 작업에서는 NULL 이다.
    search_manifest = Column(JSON, nullable=True)
    # 모델 보고 블록을 읽지 못한 사유. 화면에서 "후보 목록을 왜 못 만들었는지"
    # 를 설명하는 데 쓴다. 관측 기록은 이 경우에도 남는다.
    search_manifest_error = Column(Text, nullable=True)
    # 구성대비 결과에서 시작한 검색이면 원본 실행과 선택 구성 스냅샷이 들어간다.
    # 일반 유사문헌 검색은 NULL 이다.
    search_focus = Column(JSON, nullable=True)

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
    # 이 실행의 분석 자료인가. 준비 화면의 「분석에 포함」 체크박스가 정한다.
    # required 와 다른 축이다 — 아래 db.py 마이그레이션이 기존 행을 True 로
    # 채우므로, 이 개념이 없던 시절의 실행 기록은 예전과 똑같이 전부 포함이다.
    included = Column(Boolean, nullable=False, default=True)
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


class EvidenceReference(Base):
    """작업 ↔ 증거 아티팩트 참조.

    증거(특허 원문 응답)는 내용 주소 저장소에 한 벌만 있고 여러 작업이 같은
    것을 가리킬 수 있다. 그래서 작업을 지울 때 아티팩트를 바로 지우면 안
    되고, 아무도 참조하지 않을 때만 지운다.

    참조를 두는 이유는 보존이 아니라 그 반대다. 별도 폴더에 영구 보존하면
    사용자가 "모든 이력 삭제"를 눌러도 특허 원문이 디스크에 남는다. 삭제
    의도가 지켜지지 않는 것이고, 개인정보·보안·저장공간 모두 문제가 된다.
    작업이 사라지면 그 증거가 재현을 뒷받침할 대상도 없다.
    """

    __tablename__ = "evidence_references"

    id = Column(String(36), primary_key=True, default=_uuid)
    job_id = Column(
        String(36), ForeignKey("execution_jobs.id", ondelete="CASCADE"), nullable=False
    )
    # 아티팩트 id = 내용의 SHA-256 (hex 64자)
    artifact_id = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("job_id", "artifact_id", name="uq_evidence_job_artifact"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(80), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
