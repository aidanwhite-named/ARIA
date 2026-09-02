"""색인 → agent 루프 → 근거 패키지까지의 오케스트레이션.

runner 가 부르는 유일한 진입점이다. 여기서 만드는 실행별 감사 자료는 다음과
같고, 전부 기존 실행 폴더(`runs/<job-id>/retrieval/`) 안에 남는다.

    extraction_report.json   페이지 추출 완전성 보고서 (문헌별)
    retrieval_manifest.json  인덱스 재현 정보 · 라운드 · 예산 · 라이브러리 버전
    retrieval_trace.jsonl    LLM 입출력 해시, 실행된 검색, 읽은 페이지, 오류
    evidence_bundle.json     최종 분석에 실제로 들어간 근거 패키지
    rounds/round-NN.in.txt   각 LLM 호출의 입력 원문
    rounds/round-NN.out.txt  각 LLM 호출의 출력 원문

원본 PDF 와 마찬가지로 실행별 격리 폴더에 있으므로, 이력을 지우면 함께
사라진다. 별도 보존 폴더를 만들지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..citation_mapping import assign_aliases, ordered_attachments
from ..enums import DeliveryPlan, ErrorCode, is_local_search_target
from ..ingestion.service import IngestedFile, read_normalized
from . import evidence as evidence_module
from .agent import (
    DEFAULT_NEIGHBOR_PAGES,
    DEFAULT_EVIDENCE_CHARS,
    DEFAULT_HITS_PER_DOCUMENT,
    DEFAULT_MAX_PAGE_READS,
    DEFAULT_MAX_ROUNDS,
    RetrievalAgent,
    RetrievalBudget,
    TraceWriter,
)
from .extraction import extract_document, extract_text_document
from .index import IndexUnavailable, SqliteCapabilities, ensure_index, probe_sqlite
from .prompts import AGENT_SYSTEM_PROMPT
from .search import IndexedDocument
from .semantic import load_encoder
from .versions import EXTRACTOR_VERSION, INDEX_VERSION, library_versions

MANIFEST_VERSION = 1

# 실행 폴더 안의 검색 자료 하위 폴더.
RETRIEVAL_DIRNAME = "retrieval"


def budget_from_settings(values: dict) -> RetrievalBudget:
    """설정에서 예산을 읽는다. preflight 와 runner 가 **이 함수 하나**를 쓴다.

    두 곳이 각자 기본값을 적어 두면 화면이 안내한 크기와 실제로 나가는 크기가
    어긋나고, 그 어긋남은 실행이 실패한 뒤에야 드러난다. job_assembly 가 같은
    실수로 한 번 데었다.
    """

    def _int(key: str, fallback: int, low: int, high: int) -> int:
        try:
            number = int(values.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return max(low, min(high, number))

    return RetrievalBudget(
        max_rounds=_int("retrieval_max_rounds", DEFAULT_MAX_ROUNDS, 1, 30),
        max_page_reads=_int(
            "retrieval_max_page_reads", DEFAULT_MAX_PAGE_READS, 1, 500
        ),
        max_evidence_chars=_int(
            "retrieval_evidence_chars", DEFAULT_EVIDENCE_CHARS, 2_000, 400_000
        ),
        hits_per_document=_int(
            "retrieval_hits_per_document", DEFAULT_HITS_PER_DOCUMENT, 1, 20
        ),
        neighbor_pages=_int(
            "retrieval_neighbor_pages", DEFAULT_NEIGHBOR_PAGES, 0, 5
        )
    )


@dataclass
class RetrievalResult:
    """루프 전체의 결과. runner 가 이것만 보고 다음 단계를 정한다."""

    bundle: dict | None = None
    manifest: dict = field(default_factory=dict)
    documents: list[IndexedDocument] = field(default_factory=list)
    error: str = ""
    error_code: str = ""
    cancelled: bool = False
    timed_out: bool = False
    usage: dict = field(default_factory=dict)
    artifacts: list[tuple[str, Path]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.bundle is not None and not self.error


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _index_path(work_dir: Path, attachment_id: str) -> Path:
    return work_dir / RETRIEVAL_DIRNAME / "index" / f"{attachment_id}.sqlite3"


def build_corpus(
    attachments: list[IngestedFile],
    work_dir: Path,
    *,
    capabilities: SqliteCapabilities | None = None,
    layout_check_max_pages: int = 400,
) -> tuple[list[IndexedDocument], list[dict]]:
    """포함된 첨부 중 **검색 대상**만 색인한다.

    「분석에 포함」을 푼 자료는 호출부에서 이미 빠져 있고, 여기서도 다시
    거르지 않는다 — 거르는 지점을 여러 곳에 두면 어느 것이 진짜인지 알 수
    없게 된다.

    다만 역할은 여기서 거른다. 출원발명 문서(APPLICATION)는 검색 대상이 아니다.
    자기 발명을 인용발명처럼 검색해 "대응 구성을 찾았다"고 판정하는 것이 이
    작업에서 가장 위험한 오류이기 때문이다. 그 자료는 본문 전체가 프롬프트에
    그대로 들어간다(enums.is_local_search_target, prompt_assembly._attachment_block).

    별칭은 **검색 대상만이 아니라 포함된 자료 전체**에 대해, 최종 프롬프트와
    같은 정렬로 붙인다. 검색 corpus 안에서만 번호를 매기면 근거 패키지의
    ATT-01 과 프롬프트 첨부 헤더의 ATT-01 이 다른 문헌을 가리키게 된다.

    돌려주는 값: (검색 대상 문헌 목록, 색인하지 못한 자료)
    """
    caps = capabilities or probe_sqlite()
    aliases = assign_aliases(ordered_attachments(attachments))
    alias_by_id = {item.attachment_id: alias for alias, item in aliases.items()}

    documents: list[IndexedDocument] = []
    skipped: list[dict] = []

    for item in ordered_attachments(attachments):
        if not is_local_search_target(item.role):
            continue
        alias = alias_by_id.get(item.attachment_id, "")
        stored = Path(item.stored_path)

        def factory(item=item, stored=stored):
            if stored.suffix.lower() == ".pdf":
                return extract_document(
                    stored,
                    attachment_id=item.attachment_id,
                    filename=item.original_filename,
                    sha256=item.sha256,
                    layout_check_max_pages=layout_check_max_pages,
                )
            return extract_text_document(
                read_normalized(item),
                attachment_id=item.attachment_id,
                filename=item.original_filename,
                sha256=item.sha256,
            )

        try:
            index, report, rebuilt = ensure_index(
                _index_path(work_dir, item.attachment_id),
                factory,
                sha256=item.sha256,
                capabilities=caps,
            )
        except IndexUnavailable:
            raise
        except Exception as exc:
            skipped.append(
                {
                    "alias": alias,
                    "attachment_id": item.attachment_id,
                    "filename": item.original_filename,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        documents.append(
            IndexedDocument(
                alias=alias,
                attachment_id=item.attachment_id,
                filename=item.original_filename,
                sha256=item.sha256,
                index=index,
                report=report,
                rebuilt=rebuilt,
                role=item.role,
            )
        )
    return documents, skipped


def extraction_report(documents: list[IndexedDocument], skipped: list[dict]) -> dict:
    """문헌별 완전성 보고서를 한 파일로 모은다."""
    return {
        "version": 1,
        "index_version": INDEX_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "ocr_performed": False,
        "documents": [
            {
                "alias": document.alias,
                "index_rebuilt": document.rebuilt,
                **document.report,
            }
            for document in documents
        ],
        "not_indexed": skipped,
    }


async def run_retrieval(
    *,
    job_id: str,
    provider,
    model: str | None,
    timeout_seconds: int,
    work_dir: Path,
    attachments: list[IngestedFile],
    claim_text: str,
    budget: RetrievalBudget,
    semantic_enabled: bool = False,
    # 임베딩 캐시 상한(bytes). 0 = 정리하지 않음. 실행이 끝날 때 한 번만 본다.
    embedding_cache_max_bytes: int = 0,
    emit=None,
    is_cancelled=None,
    layout_check_max_pages: int = 400,
) -> RetrievalResult:
    """색인부터 근거 패키지까지 한 번에 수행한다."""
    base = work_dir / RETRIEVAL_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    result = RetrievalResult()

    capabilities = probe_sqlite()
    try:
        documents, skipped = build_corpus(
            attachments,
            work_dir,
            capabilities=capabilities,
            layout_check_max_pages=layout_check_max_pages,
        )
    except IndexUnavailable as exc:
        result.error = str(exc)
        result.error_code = ErrorCode.RETRIEVAL_UNAVAILABLE
        return result

    result.documents = documents
    report = extraction_report(documents, skipped)
    report_path = base / "extraction_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result.artifacts.append(("extraction_report", report_path))

    if skipped:
        # 일부만 색인하고 계속 가지 않는다. D1 은 색인됐고 D2 는 실패한 상태로
        # 검색하면, D2 에 있는 구성도 "검토 범위에서 미발견"으로 나온다. 사용자
        # 눈에는 두 문헌을 다 본 결과로 보이고, 빠진 문헌이 무엇인지는 manifest
        # 를 열어야만 알 수 있다. 조용히 좁아진 검색은 아무 검색도 하지 않은
        # 것보다 나쁘다.
        # 별칭만 적지 않는다. 사용자가 지워야 할 것은 ATT-02 가 아니라
        # 화면에 보이는 파일이다.
        names = ", ".join(
            f"{item['filename']}"
            + (f"[{item['alias']}]" if item["alias"] else "")
            + f" — {item['reason']}"
            for item in skipped
        )
        result.error = (
            f"인용발명 문헌 중 {len(skipped)}건을 색인하지 못해 실행을 "
            f"중단했습니다: {names}. 일부 문헌만 검색하면 그 문헌에 있는 구성도 "
            "「검토 범위에서 미발견」으로 나오므로, ARIA 는 좁아진 검색으로 "
            "계속 진행하지 않습니다. 해당 파일을 제외하거나 다시 업로드한 뒤 "
            "재실행하십시오."
        )
        result.error_code = ErrorCode.RETRIEVAL_UNAVAILABLE
        close_documents(documents)
        result.documents = []
        return result

    if not documents:
        result.error = (
            "색인할 수 있는 인용발명 문헌이 없습니다. 출원발명 문서는 검색 "
            "대상이 아니므로, 「분석에 포함」한 인용발명 문헌이 최소 1건 "
            "있어야 합니다. 업로드한 PDF 에서 텍스트를 얻지 못한 경우에도 "
            "같습니다 — ARIA 는 OCR 을 수행하지 않으므로 텍스트 레이어가 있는 "
            "PDF 가 필요합니다."
        )
        result.error_code = ErrorCode.RETRIEVAL_UNAVAILABLE
        return result

    encoder, semantic_state = load_encoder(semantic_enabled)
    trace = TraceWriter(base / "retrieval_trace.jsonl")
    trace.write(
        "start",
        {
            "documents": [document.manifest_entry() for document in documents],
            "budget": budget.to_dict(),
            "sqlite": capabilities.to_dict(),
            "semantic": semantic_state.to_dict(),
            "libraries": library_versions(),
            "agent_prompt_sha256": _sha256(AGENT_SYSTEM_PROMPT),
        },
    )

    agent = RetrievalAgent(
        job_id=job_id,
        provider=provider,
        model=model,
        timeout_seconds=timeout_seconds,
        work_dir=base,
        corpus=documents,
        claim_text=claim_text,
        budget=budget,
        trace=trace,
        emit=emit,
        is_cancelled=is_cancelled,
        semantic_encoder=encoder,
    )
    try:
        run = await agent.run()
    finally:
        # 임베딩 캐시 연결을 놓아준다. 실행이 끝나면 더 검색하지 않는다.
        # 정리도 여기서 한 번만 한다 — 검색 경로에 넣으면 캐시가 아끼려던
        # 시간을 정리가 다시 쓴다. 정리 실패는 검색 결과를 바꾸지 않는다.
        if encoder is not None:
            encoder.close(embedding_cache_max_bytes)

    result.cancelled = run.cancelled
    result.timed_out = run.timed_out
    result.usage = {
        "retrieval_rounds": [record.usage for record in run.rounds],
        "retrieval_round_count": len(run.rounds),
        "retrieval_pages_read": run.pages_read,
        "retrieval_deferred_executed": run.deferred_executed,
        "retrieval_deferred_pending": len(run.deferred_pending),
    }

    bundle = None
    if not run.error_code:
        builder = evidence_module.EvidenceBuilder(
            corpus=documents,
            run=run,
            budget=budget,
            claim_text=claim_text,
            semantic=semantic_state.to_dict(),
            capabilities=capabilities.to_dict(),
            library_versions=library_versions(),
        )
        bundle = builder.build()
        # 완성된 문자열을 직접 재서 예산 안으로 맞춘다. 항목별로 더하기 전에
        # 재는 것만으로는 부족하다 — 구성 메타데이터와 문헌 목록은 개수가
        # 예산과 무관하게 늘어난다.
        rendered = evidence_module.fit(bundle, budget)
        bundle["evidence_chars"] = len(rendered)
        bundle["evidence_bytes"] = len(rendered.encode("utf-8"))


    manifest = {
        "version": MANIFEST_VERSION,
        "delivery_mode": DeliveryPlan.LOCAL_RETRIEVAL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "claim_sha256": _sha256(claim_text),
        "agent_prompt_sha256": _sha256(AGENT_SYSTEM_PROMPT),
        "budget": budget.to_dict(),
        "sqlite": capabilities.to_dict(),
        "semantic": semantic_state.to_dict(),
        "libraries": library_versions(),
        "ocr_performed": False,
        "documents": [document.manifest_entry() for document in documents],
        "not_indexed": skipped,
        "rounds": [record.to_dict() for record in run.rounds],
        "pages_read": run.pages_read,
        "repeat_page_reads": run.repeat_page_reads,
        # 예산에 맞추려고 무엇을 줄였는가. 비어 있어야 정상이다.
        "package_reductions": list((bundle or {}).get("package_reductions") or []),
        # 예산 때문에 뺀 페이지. package_reductions 와 다른 채널이다 — 페이지를
        # 뺀 것은 근거를 뺀 것이 아니므로 구성 판정을 흔들지 않는다.
        "page_reductions": list((bundle or {}).get("page_reductions") or []),
        "evidence_chars": (bundle or {}).get("evidence_chars", 0),
        "components": [
            {
                "id": state.id,
                "label": state.label,
                "importance": state.declared_importance,
                "importance_reasons": list(state.importance_reasons),
                "depends_on": list(state.depends_on),
                "priority": state.current_priority,
                "uncertainty": state.uncertainty,
                "priority_reasons": list(state.priority_reasons),
                "search_completeness": state.search_completeness,
                "coverage_ratio": round(state.coverage_ratio, 3),
                "queries": list(state.queries),
                "channels_used": list(state.channels_used),
                "channels_failed": list(state.failed_channels),
                "candidates": len(state.hit_chunks),
                # 문헌별 검색 실행 기록. 0건이었던 검색도 들어 있다 —
                # "찾지 못했다"와 "찾아보지 않았다"를 가르는 근거다.
                "searched_documents": [
                    record.to_dict() for record in state.searched.values()
                ],
                "unsearched_documents": [
                    document.alias
                    for document in documents
                    if document.attachment_id not in state.searched
                ],
                "candidate_ledger": state.top_candidates(),
                "deferred_actions": [
                    item
                    for item in run.deferred_pending
                    if item.get("component_id") == state.id
                ],
            }
            for state in run.components
        ],
        "action_errors": list(run.action_errors),
        "deferred_actions": list(run.deferred_actions),
        "deferred_pending": list(run.deferred_pending),
        "deferred_executed": run.deferred_executed,
        "notes": list(run.notes),
        "budget_exhausted": run.budget_exhausted,
        "error": run.error or "",
        "error_code": run.error_code or "",
        "status": (
            "failed"
            if run.error_code
            else ("partial" if run.budget_exhausted else "complete")
        ),
    }
    manifest_path = base / "retrieval_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result.manifest = manifest
    result.artifacts.append(("retrieval_manifest", manifest_path))
    result.artifacts.append(("retrieval_trace", trace.path))

    if run.error_code:
        result.error = run.error
        result.error_code = run.error_code
        return result

    if bundle is not None and bundle.get("package_over_budget"):
        # 조용히 넘겨 보내지 않는다. 넘기면 preflight 가 안내한 크기가 거짓이
        # 되고, 검색 비용을 다 쓴 뒤 Provider 호출 직전에 막힌다.
        needed = int(bundle.get("package_required_chars") or 0)
        result.error = (
            f"근거 패키지가 설정한 예산({budget.max_evidence_chars:,}자)에 "
            f"들어가지 않습니다. 최소 {needed:,}자가 필요합니다. 환경설정의 "
            "「근거 패키지 최대 문자 수」를 올리거나, 청구항 구성 수 또는 "
            "인용문헌 수를 줄여 실행하십시오. ARIA 는 원문을 잘라서 맞추지 "
            "않습니다."
        )
        result.error_code = ErrorCode.RETRIEVAL_FAILED
        # 무엇이 얼마나 필요한지는 기록에 남긴다.
        manifest["error"] = result.error
        manifest["error_code"] = result.error_code
        manifest["status"] = "failed"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    bundle_path = base / "evidence_bundle.json"
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result.artifacts.append(("evidence_bundle", bundle_path))
    result.bundle = bundle
    return result


def close_documents(documents: list[IndexedDocument]) -> None:
    for document in documents:
        try:
            document.index.close()
        except Exception:
            continue
