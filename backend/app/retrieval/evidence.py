"""검증된 근거 패키지(evidence bundle).

최종 분석 모델에게 PDF 전체를 넣지 않고 이 패키지만 넣는다. 패키지에는 두
종류의 값이 있고 **구조적으로 분리**되어 있다.

  ARIA 가 채우는 것 : 원문 텍스트, 앞뒤 문맥, PDF 페이지, 문단번호, 검색 채널,
                      추출 상태, 검토 범위, 확인하지 못한 페이지
  AI 가 채우는 것   : 관련성 설명(relevance), 구성 분해, 사용한 검색어

AI 는 chunk_id 만 가리킨다. 원문은 ARIA 가 자기 인덱스에서 꺼내 넣으므로,
AI 가 반환된 원문을 고치거나 없는 문장을 만들어 넣을 경로가 없다.

「없음」 판정도 AI 가 정하지 못한다. AI 의 주장은 status_claim 으로 받되,
ARIA 가 자기 관측(페이지 수 일치, 추출 상태, 실행된 검색 채널, 실제 검색어 수)
과 대조해서 확정한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import pages as pages_module
from .agent import MIN_EXPANSION_TERMS, ComponentState, RetrievalBudget, RetrievalRun
from .extraction import (
    DOC_UNUSABLE,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_VISUAL,
)
from .prompts import NOT_FOUND_PHRASE
from .search import IndexedDocument
from .versions import EXTRACTOR_VERSION, INDEX_VERSION

BUNDLE_VERSION = 1

STATUS_MATCHED = "matched"
STATUS_NOT_FOUND_SCOPE = "not_found_in_reviewed_scope"
STATUS_COVERAGE = "coverage_insufficient"
STATUS_UNREADABLE = "extraction_unreadable"
STATUS_VISUAL_REVIEW = "visual_review_required"
BUNDLE_STATUSES = (
    STATUS_MATCHED,
    STATUS_NOT_FOUND_SCOPE,
    STATUS_COVERAGE,
    STATUS_UNREADABLE,
    STATUS_VISUAL_REVIEW,
)

STATUS_LABEL = {
    STATUS_MATCHED: "검토 범위에서 대응 구간을 확인함",
    STATUS_NOT_FOUND_SCOPE: NOT_FOUND_PHRASE,
    STATUS_COVERAGE: "검토 범위가 부족해 대응 여부를 확정하지 못함",
    STATUS_UNREADABLE: "텍스트를 얻지 못한 문헌이 있어 확인하지 못함",
    STATUS_VISUAL_REVIEW: "사람이 원본 PDF 를 직접 확인해야 함",
}

# 근거 구간 하나에 붙이는 앞뒤 문맥 청크 수.
CONTEXT_CHUNKS = 1

# 문헌마다 서지 확인용으로 붙이는 첫 페이지 발췌 길이.
#
# 검색 결과만 넣으면 최종 분석 모델이 그 문헌의 **공개번호**를 볼 수 없다.
# 문헌 매핑 프로토콜(citation_mapping_v1)은 모델이 자료 번호와 문헌번호를 짝지어
# 출력하도록 요구하므로, 번호를 볼 수 없으면 로컬 검색 실행에서만 그 계약이
# 조용히 깨진다. 서지사항은 거의 언제나 첫 페이지에 있으므로 그 앞부분을
# 그대로(요약하지 않고) 싣는다. 이것도 ARIA 가 인덱스에서 꺼낸 원문이다.
IDENTITY_EXCERPT_CHARS = 1_200

# 근거 하나·구성 하나가 렌더링될 때 붙는 구조 문자(위치 줄, 경계 표시, 상태
# 설명)의 몫. 원문 길이만 예산에 세면, 짧은 근거를 아주 많이 담았을 때 실제
# 프롬프트가 preflight 가 안내한 최댓값을 넘어선다. 예산은 "모델에게 나가는
# 크기"의 상한이어야 하므로 구조 문자도 함께 센다.
FINDING_OVERHEAD_CHARS = 320
COMPONENT_OVERHEAD_CHARS = 400


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ranges(pages: list[int], limit: int = 24) -> list[str]:
    """페이지 목록을 "3-7, 12" 형태로 줄인다. 200쪽짜리 목록을 그대로 싣지 않는다."""
    if not pages:
        return []
    ordered = sorted(set(pages))
    spans: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        spans.append((start, previous))
        start = previous = page
    spans.append((start, previous))
    rendered = [
        str(low) if low == high else f"{low}-{high}" for low, high in spans[:limit]
    ]
    if len(spans) > limit:
        rendered.append(f"… 외 {len(spans) - limit}개 구간")
    return rendered


def document_gate(document: IndexedDocument) -> list[str]:
    """이 문헌 때문에 not_found 를 확정할 수 없는 사유들."""
    report = document.report or {}
    reasons: list[str] = []

    # 인덱스 신원 검사. ensure_index 가 불일치를 만나면 다시 만들므로 정상
    # 경로에서는 걸리지 않는다. 그래도 확인하는 이유는, 이 검사를 통과하지
    # 못한 인덱스로 만든 "없음" 판정이 조용히 나가는 것이 이 기능에서 가장
    # 나쁜 실패이기 때문이다.
    fingerprint = document.index.fingerprint()
    if fingerprint.get("pdf_sha256") != document.sha256:
        reasons.append(
            f"{document.alias}: 인덱스의 PDF 해시가 이번 자료와 다릅니다 "
            f"(인덱스 {str(fingerprint.get('pdf_sha256'))[:12]}…, 자료 "
            f"{document.sha256[:12]}…)."
        )
    if fingerprint.get("index_version") != INDEX_VERSION:
        reasons.append(
            f"{document.alias}: 인덱스 버전이 다릅니다 "
            f"(인덱스 v{fingerprint.get('index_version')}, 현재 v{INDEX_VERSION})."
        )
    if fingerprint.get("extractor_version") != EXTRACTOR_VERSION:
        reasons.append(
            f"{document.alias}: 추출기 버전이 다릅니다 "
            f"(인덱스 {fingerprint.get('extractor_version')}, 현재 "
            f"{EXTRACTOR_VERSION})."
        )

    if report.get("page_count_mismatch"):
        reasons.append(
            f"{document.alias}: 원본 {report.get('source_page_count')}쪽 중 "
            f"{report.get('processed_page_count')}쪽만 처리됨"
        )
    failed = report.get("extraction_failed_pages") or []
    if failed:
        reasons.append(
            f"{document.alias}: 추출 실패 페이지 {len(failed)}쪽 "
            f"({', '.join(_ranges(failed, limit=6))})"
        )
    empty = report.get("empty_or_low_text_pages") or []
    if empty:
        reasons.append(
            f"{document.alias}: 빈 페이지·저문자 페이지 {len(empty)}쪽 "
            f"({', '.join(_ranges(empty, limit=6))})"
        )
    visual = report.get("visual_review_required_pages") or []
    if visual:
        reasons.append(
            f"{document.alias}: 도면·이미지만 있어 원본 확인이 필요한 페이지 "
            f"{len(visual)}쪽 ({', '.join(_ranges(visual, limit=6))})"
        )
    return reasons


# 자료 구분 표시. 최종 프롬프트의 첨부 헤더와 같은 낱말을 쓴다.
_ROLE_LABEL = {
    "APPLICATION": "출원발명 문서",
    "CITATION": "인용발명 문헌",
    "SUPPLEMENTAL": "기타 첨부 자료",
}


def identity_excerpt(document: IndexedDocument) -> str:
    """서지사항 확인용 첫 페이지 발췌. 요약하지 않고 원문 그대로 자른다."""
    for page in range(1, max(1, document.page_count) + 1):
        rows = document.index.page_rows(page)
        text = "\n".join(row.text for row in rows).strip()
        if text:
            return text[:IDENTITY_EXCERPT_CHARS]
    return ""


def _document_entry(document: IndexedDocument, excerpt: str = "") -> dict:
    report = document.report or {}
    return {
        "identity_excerpt": excerpt,
        "attachment": document.alias,
        "attachment_id": document.attachment_id,
        "role": document.role,
        "filename": document.filename,
        "pdf_sha256": document.sha256,
        "pdf_pages": document.page_count,
        "source_page_count": report.get("source_page_count"),
        "processed_page_count": report.get("processed_page_count"),
        "extraction_status": report.get("status"),
        "empty_or_low_text_pages": report.get("empty_or_low_text_pages") or [],
        "extraction_failed_pages": report.get("extraction_failed_pages") or [],
        "visual_review_required_pages": (
            report.get("visual_review_required_pages") or []
        ),
        "extraction_divergence_pages": (
            report.get("extraction_divergence_pages") or []
        ),
        "chunk_count": report.get("chunk_count"),
        "index_version": report.get("index_version"),
        "extractor_version": report.get("extractor_version"),
        "ocr_performed": False,
    }


class EvidenceBuilder:
    """AI 의 finalize 요청을 ARIA 의 관측과 대조해 근거 패키지로 만든다."""

    def __init__(
        self,
        *,
        corpus: list[IndexedDocument],
        run: RetrievalRun,
        budget: RetrievalBudget,
        claim_text: str,
        semantic: dict,
        capabilities: dict,
        library_versions: dict,
    ) -> None:
        self.corpus = corpus
        self.run = run
        self.budget = budget
        self.claim_text = claim_text
        self.semantic = semantic
        self.capabilities = capabilities
        self.library_versions = library_versions
        self._by_alias = {document.alias: document for document in corpus}
        self._used_chars = 0
        self.truncated = False

    # ------------------------------------------------------------ 근거 수집

    def _resolve(self, ref, component: ComponentState | None) -> tuple[dict | None, str]:
        document = self._by_alias.get(str(ref.attachment or "").strip())
        if document is None:
            return None, f"알 수 없는 자료 번호: {ref.attachment}"

        row = None
        if ref.chunk_id:
            row = document.index.chunk(str(ref.chunk_id).strip())
        if row is None and ref.paragraph:
            rows = document.index.paragraph_rows(ref.paragraph)
            row = rows[0] if rows else None
        if row is None and ref.page:
            rows = document.index.page_rows(int(ref.page))
            row = rows[0] if rows else None
        if row is None:
            return None, (
                f"{document.alias} 에서 지목한 구간을 찾지 못했습니다 "
                f"(chunk_id={ref.chunk_id or '-'}, page={ref.page or '-'}, "
                f"paragraph={ref.paragraph or '-'})."
            )

        # 이번 실행에서 AI 에게 실제로 돌려준 구간인가.
        #
        # 원문이 실재한다는 것만으로는 근거가 되지 않는다. chunk_id 형식은
        # action 스키마에 그대로 노출돼 있어서, AI 가 P0001-001 같은 값을
        # 추측해 적으면 보지도 않은 구간이 근거가 되고 곧바로 matched 가 된다.
        # 검색 결과 또는 read_page/read_paragraph 로 돌려준 것만 통과시킨다.
        if (document.attachment_id, row.chunk_id) not in self.run.exposed_chunks:
            return None, (
                f"{document.alias} {row.chunk_id} 는 이번 실행에서 검색 결과나 "
                "페이지 열람으로 반환된 적이 없습니다. 실제로 확인한 구간만 "
                "근거가 됩니다."
            )

        before, after = document.index.neighbours(
            row.chunk_id, before=CONTEXT_CHUNKS, after=CONTEXT_CHUNKS
        )
        observed = (
            component.hit_chunks.get(f"{document.attachment_id}:{row.chunk_id}")
            if component is not None
            else None
        )
        finding = {
            "attachment": document.alias,
            "attachment_id": document.attachment_id,
            "filename": document.filename,
            "chunk_id": row.chunk_id,
            "pdf_page": row.page_number,
            "printed_page": row.printed_page or None,
            "paragraph": row.paragraph or None,
            "section": row.section or None,
            "extraction_status": row.extraction_status,
            "extraction_method": row.extraction_method,
            # ARIA 가 인덱스에서 그대로 꺼낸 원문. AI 는 이 값을 만들지도
            # 고치지도 못한다.
            "source_text": row.text,
            "context_before": before,
            "context_after": after,
            "channels": list((observed or {}).get("channels") or []),
            # 어느 채널이 몇 위로 올렸는가. 의미 검색이 실제로 이 구간을
            # 올렸는지 나중에 되짚을 수 있어야 한다 — 채널 이름만으로는
            # "함께 걸렸다"와 "이 채널이 끌어올렸다"를 구분하지 못한다.
            "channel_ranks": dict((observed or {}).get("ranks") or {}),
            "found_by_search": observed is not None,
            # AI 가 쓴 판단. 원문과 같은 칸에 두지 않는다.
            "ai_relevance": str(ref.relevance or "").strip(),
        }

        # 예산은 **더한 뒤가 아니라 더하기 전에** 확인한다. 남은 자리가 있는지만
        # 보고 청크를 통째로 얹으면, 마지막 하나가 상한을 훌쩍 넘겨 preflight 가
        # 안내한 최댓값이 상한이 아니게 된다. 여기서 넘기면 Provider 호출 직전
        # 바이트 검사에 걸려, 검색 비용을 다 쓰고 나서 실행이 실패한다.
        addition = (
            len(row.text)
            + len(before)
            + len(after)
            + len(finding["ai_relevance"])
            + FINDING_OVERHEAD_CHARS
        )
        remaining = self.budget.max_evidence_chars - self._used_chars
        if addition > remaining:
            self.truncated = True
            return None, (
                f"근거 패키지 문자 예산이 부족해 이 구간을 넣지 못했습니다 "
                f"(필요 {addition:,}자, 남은 {max(0, remaining):,}자)."
            )
        self._used_chars += addition
        return finding, ""

    # ------------------------------------------------------------ 상태 확정

    def _component_status(
        self,
        component: ComponentState | None,
        findings: list[dict],
        claim: str,
        blockers: list[str],
    ) -> tuple[str, list[str]]:
        reasons = list(blockers)

        if component is None:
            reasons.append("ARIA 가 이 구성에 대한 검색 실행 기록을 찾지 못했습니다.")
        else:
            # 검사는 **문헌마다** 한다. 전체 검색어 수만 세면, AI 가 D1 만
            # 검색어 3개로 뒤지고 D2 는 건드리지도 않은 채 "검토 범위에서
            # 미발견"을 받을 수 있다. 검색하지 않은 문헌은 검토한 문헌이 아니다.
            for document in self.corpus:
                record = component.search_attempts.get(document.attachment_id)
                if record is None:
                    reasons.append(
                        f"{document.alias}({document.filename}): 이 구성에 대해 "
                        "검색을 실행한 기록이 없습니다."
                    )
                    continue
                if len(record.queries) < MIN_EXPANSION_TERMS:
                    reasons.append(
                        f"{document.alias}: 이 구성에 대해 실제로 실행된 검색어가 "
                        f"{len(record.queries)}개뿐입니다(최소 "
                        f"{MIN_EXPANSION_TERMS}개의 확장 검색 필요)."
                    )
                if record.failed_channels:
                    reasons.append(
                        f"{document.alias}: 정상적으로 실행되지 않은 검색 채널 — "
                        + ", ".join(record.failed_channels)
                    )

        if findings:
            # 근거가 있으면 대응을 확인한 것이다. 다만 검토 범위 제한은 그대로
            # 남겨서 최종 분석이 그것을 보고 판단하게 한다.
            return STATUS_MATCHED, reasons

        # 근거가 없는 경우에만 "없음"에 가까운 상태가 후보가 된다.
        if any(
            (document.report or {}).get("status") == DOC_UNUSABLE
            for document in self.corpus
        ):
            return STATUS_UNREADABLE, reasons
        if any(
            (document.report or {}).get("visual_review_required_pages")
            for document in self.corpus
        ):
            return STATUS_VISUAL_REVIEW, reasons
        if reasons or self.run.budget_exhausted or self.truncated:
            return STATUS_COVERAGE, reasons
        if str(claim or "").strip().lower() in {"matched", "found"}:
            # 모델이 대응을 주장했는데 지목한 구간이 하나도 확인되지 않았다.
            reasons.append(
                "모델이 대응을 주장했으나 지목한 구간이 인덱스에서 확인되지 "
                "않았습니다."
            )
            return STATUS_COVERAGE, reasons
        return STATUS_NOT_FOUND_SCOPE, reasons

    # ------------------------------------------------------------ 패키지 생성

    def build(self) -> dict:
        # 서지 발췌를 먼저 뽑아 예산에서 뺀다. 근거를 다 담은 뒤에 더하면 실제
        # 프롬프트가 preflight 가 안내한 최댓값을 넘어선다.
        excerpts: dict[str, str] = {}
        for document in self.corpus:
            excerpt = identity_excerpt(document)
            excerpts[document.attachment_id] = excerpt
            self._used_chars += len(excerpt)

        blockers: list[str] = []
        for document in self.corpus:
            blockers.extend(document_gate(document))
        if not self.capabilities.get("trigram"):
            blockers.append(
                "이 실행 환경의 SQLite 에 trigram 토크나이저가 없어 부분문자 "
                "검색을 수행하지 못했습니다."
            )
        if self.run.budget_exhausted:
            blockers.append(
                "예산 제한으로 검색·열람 요청 또는 검색 마무리를 완료하지 "
                "못했습니다."
            )

        finalize_by_id = {
            str(item.component_id): item
            for item in (self.run.finalize.components if self.run.finalize else [])
        }
        components: list[dict] = []
        rejected: list[dict] = []

        for state in self.run.components:
            request = finalize_by_id.get(state.id)
            self._used_chars += COMPONENT_OVERHEAD_CHARS + len(state.label) + len(
                state.feature
            )
            findings: list[dict] = []
            if request is not None:
                for ref in request.evidence:
                    finding, error = self._resolve(ref, state)
                    if finding is None:
                        rejected.append(
                            {
                                "component_id": state.id,
                                "attachment": ref.attachment,
                                "chunk_id": ref.chunk_id,
                                "reason": error,
                            }
                        )
                        continue
                    findings.append(finding)

            status, reasons = self._component_status(
                state,
                findings,
                request.status_claim if request is not None else "",
                blockers,
            )
            reviewed = {
                alias: sorted(pages)
                for alias, pages in (
                    (
                        self._alias_of(attachment_id),
                        pages,
                    )
                    for attachment_id, pages in state.reviewed_pages.items()
                )
                if alias
            }
            unreviewed = {}
            for document in self.corpus:
                seen = state.reviewed_pages.get(document.attachment_id, set())
                missing = [
                    page
                    for page in range(1, document.page_count + 1)
                    if page not in seen
                ]
                unreviewed[document.alias] = {
                    "count": len(missing),
                    "ranges": _ranges(missing),
                }
            components.append(
                {
                    "component_id": state.id,
                    "claim_component": state.label,
                    "feature": state.feature,
                    "declared_importance": state.declared_importance,
                    "importance_reasons": list(state.importance_reasons),
                    "depends_on": list(state.depends_on),
                    "priority": state.current_priority,
                    "uncertainty": state.uncertainty,
                    "priority_reasons": list(state.priority_reasons),
                    "search_completeness": state.search_completeness,
                    "coverage_ratio": round(state.coverage_ratio, 3),
                    "stable_rounds": state.stable_rounds,
                    "queries_used": list(state.queries),
                    "search_channels_used": list(state.channels_used),
                    "search_channels_failed": list(state.failed_channels),
                    # 문헌별 검색 실행 기록. 결과가 0건인 검색도 들어 있다.
                    "searched_documents": [
                        record.to_dict() for record in state.search_attempts.values()
                    ],
                    "unsearched_documents": [
                        document.alias
                        for document in self.corpus
                        if document.attachment_id not in state.search_attempts
                    ],
                    "candidate_documents": sorted(
                        {entry["alias"] for entry in state.hit_chunks.values()}
                    ),
                    "candidate_ledger": [
                        {
                            "attachment": entry.get("alias"),
                            "chunk_id": entry.get("chunk_id"),
                            "page": entry.get("page_number"),
                            "paragraph": entry.get("paragraph") or "",
                            "score": round(float(entry.get("score") or 0), 6),
                            "channels": list(entry.get("channels") or []),
                            "snippet": str(entry.get("snippet") or ""),
                            "seen_count": int(entry.get("seen_count") or 1),
                            "first_seen_round": entry.get("first_seen_round"),
                            "last_seen_round": entry.get("last_seen_round"),
                        }
                        for entry in state.top_candidates()
                    ],
                    "deferred_actions": [
                        item
                        for item in self.run.deferred_pending
                        if item.get("component_id") == state.id
                    ],
                    "findings": findings,
                    "reviewed_pages": {
                        alias: {"count": len(pages), "ranges": _ranges(pages)}
                        for alias, pages in reviewed.items()
                    },
                    "unreviewed_pages": unreviewed,
                    "status": status,
                    "status_label": STATUS_LABEL[status],
                    "status_reasons": reasons,
                    "model_status_claim": (
                        request.status_claim if request is not None else ""
                    ),
                    "ai_note": request.note if request is not None else "",
                    "needs_original_review": status != STATUS_MATCHED or bool(reasons),
                    "evidence_truncated": self.truncated,
                }
            )

        finding_pages: dict[str, set[int]] = {}
        for component in components:
            for finding in component.get("findings", []):
                attachment_id = str(finding.get("attachment_id") or "")
                page = finding.get("pdf_page")
                if attachment_id and page:
                    finding_pages.setdefault(attachment_id, set()).add(int(page))

        # 예산 때문에 뺀 페이지. package_reductions 와 다른 채널이다 — 위 pages
        # 모듈 주석 참조. 목록을 build() 에 넘겨서 **만들어 보지도 못한** 페이지
        # 까지 같은 곳에 담는다. fit() 만 채우게 두면 근거 발췌로 예산이 찬
        # 실행에서 페이지가 0쪽 들어간 사실이 아무 데도 남지 않는다.
        page_reductions: list[str] = []
        evidence_pages = pages_module.build(
            corpus=self.corpus,
            finding_pages=finding_pages,
            neighbours=self.budget.neighbor_pages,
            char_budget=max(0, self.budget.max_evidence_chars - self._used_chars),
            skipped=page_reductions,
        )

        return {
            "version": BUNDLE_VERSION,
            "generated_at": _utcnow(),
            "delivery_mode": "local_retrieval",
            "page_reductions": page_reductions,
            "evidence_pages": evidence_pages,
            "ocr_performed": False,
            "claim_chars": len(self.claim_text or ""),
            "documents": [
                _document_entry(document, excerpts.get(document.attachment_id, ""))
                for document in self.corpus
            ],
            "components": components,
            "rejected_evidence": rejected,
            "coverage_blockers": blockers,
            # 예산에 맞추려고 줄인 내역. fit() 이 채운다. 키를 항상 두는 이유는
            # 소비하는 쪽(화면, manifest, 테스트)이 존재 여부를 확인하지 않아도
            # 되게 하기 위해서다.
            "package_reductions": [],
            "budget": self.budget.to_dict(),
            "budget_exhausted": self.run.budget_exhausted or self.truncated,
            "budget_limited": self.run.budget_limited or self.truncated,
            "rounds": len(self.run.rounds),
            "pages_read": self.run.pages_read,
            "evidence_chars": self._used_chars,
            "semantic": self.semantic,
            "sqlite": self.capabilities,
            "libraries": self.library_versions,
            "action_errors": list(self.run.action_errors),
            "deferred_actions": list(self.run.deferred_actions),
            "deferred_pending": list(self.run.deferred_pending),
            "deferred_executed": self.run.deferred_executed,
            "notes": list(self.run.notes),
        }

    def _alias_of(self, attachment_id: str) -> str:
        for document in self.corpus:
            if document.attachment_id == attachment_id:
                return document.alias
        return ""


# ------------------------------------------------------------------- 렌더링


def _finding_lines(finding: dict) -> list[str]:
    location = [f"{finding['attachment']} · PDF {finding['pdf_page']}쪽"]
    if finding.get("printed_page"):
        location.append(f"인쇄 {finding['printed_page']}쪽")
    if finding.get("paragraph"):
        location.append(f"문단 {finding['paragraph']}")
    if finding.get("section"):
        location.append(finding["section"])
    lines = [
        f"  - 위치: {' · '.join(location)}",
        f"    chunk_id: {finding['chunk_id']} · 검색 채널: "
        f"{', '.join(finding['channels']) or '직접 지정'} · 추출 상태: "
        f"{finding['extraction_status']}",
    ]
    if finding.get("context_before"):
        lines += [
            "    --- 앞 문맥 (ARIA 가 PDF 에서 그대로 꺼낸 원문) ---",
            *[f"    {line}" for line in finding["context_before"].split("\n")],
        ]
    lines += [
        "    --- 원문 시작 (ARIA 가 PDF 에서 그대로 꺼낸 원문) ---",
        *[f"    {line}" for line in finding["source_text"].split("\n")],
        "    --- 원문 끝 ---",
    ]
    if finding.get("context_after"):
        lines += [
            "    --- 뒤 문맥 (ARIA 가 PDF 에서 그대로 꺼낸 원문) ---",
            *[f"    {line}" for line in finding["context_after"].split("\n")],
        ]
    if finding.get("ai_relevance"):
        lines.append(f"    [검색 단계 AI 의 관련성 메모 — 원문 아님] {finding['ai_relevance']}")
    return lines


# preflight 자리표를 담는 키. 실제 패키지에는 절대 나타나지 않는다.
# 크기만 재는 조립본과 진짜 조립본이 **같은 함수**를 지나가게 하려고 둔다 —
# 두 경로를 나누면 화면이 안내한 크기와 실제로 나가는 크기가 어긋난다.
PLACEHOLDER_KEY = "__preflight_placeholder__"


def render(bundle: dict) -> str:
    """최종 분석 프롬프트에 넣을 근거 패키지 본문."""
    placeholder = (bundle or {}).get(PLACEHOLDER_KEY)
    if placeholder is not None:
        return str(placeholder)

    lines = [
        "[ARIA 로컬 검색 근거 패키지]",
        "",
        "이 실행에서는 인용발명 문헌의 **전체 본문을 넣지 않았습니다.** ARIA 가",
        "각 문헌을 페이지·문단 단위로 로컬 색인한 뒤, 검색 단계의 AI 가 청구항",
        "구성별로 검색·열람한 구간만 아래에 담았습니다.",
        "",
        "규칙:",
        "- 「원문」으로 표시된 구간은 ARIA 가 PDF 에서 그대로 꺼낸 텍스트입니다.",
        "  발췌로 인용할 수 있는 것은 이 구간뿐입니다.",
        "- 「관련성 메모」는 검색 단계 AI 의 판단이며 원문이 아닙니다. 인용하지",
        "  마십시오.",
        "- 아래에 없는 페이지는 이번 검토 범위 밖입니다. 검토하지 않은 것과",
        "  문헌에 없는 것은 다릅니다.",
        f"- OCR 은 수행하지 않았습니다. 추출 상태가 "
        f"{STATUS_EMPTY}/{STATUS_FAILED}/{STATUS_VISUAL} 인 페이지의 내용은",
        "  확인되지 않았습니다.",
        "",
        "[대상 문헌]",
    ]
    for document in bundle.get("documents", []):
        # 자료 구분을 함께 적는다. 「기타 첨부 자료」에서 찾은 구간을 인용발명의
        # 개시로 읽으면 안 되고, 그 판단은 최종 분석 모델이 해야 한다.
        lines.append(
            f"- {document['attachment']} · {document['filename']} · "
            f"{_ROLE_LABEL.get(document.get('role'), '기타 첨부 자료')} · "
            f"{document['pdf_pages']}쪽 · 추출 상태 {document['extraction_status']}"
        )
        detail = []
        if document.get("empty_or_low_text_pages"):
            detail.append(
                f"빈/저문자 {len(document['empty_or_low_text_pages'])}쪽"
            )
        if document.get("extraction_failed_pages"):
            detail.append(f"추출 실패 {len(document['extraction_failed_pages'])}쪽")
        if document.get("visual_review_required_pages"):
            detail.append(
                f"원본 확인 필요 {len(document['visual_review_required_pages'])}쪽"
            )
        if document.get("extraction_divergence_pages"):
            detail.append(
                f"추출 방식 간 차이 의심 {len(document['extraction_divergence_pages'])}쪽"
            )
        if detail:
            lines.append(f"  · {' · '.join(detail)}")
        if document.get("identity_excerpt"):
            lines += [
                "  --- 첫 페이지 원문 발췌 (서지사항 확인용, ARIA 가 그대로 꺼낸 원문) ---",
                *[
                    f"  {line}"
                    for line in str(document["identity_excerpt"]).split("\n")
                ],
                "  --- 발췌 끝 ---",
            ]

    semantic = bundle.get("semantic") or {}
    lines += [
        "",
        "[검색 구성]",
        # 근거 패키지 문자 수는 여기 적지 않는다. 그 값은 이 문자열의 길이라서,
        # 적는 순간 "길이를 재서 맞춘다"가 자기 참조가 된다(재면 값이 바뀌고,
        # 값이 바뀌면 길이가 바뀐다). 실제 크기는 manifest 와 화면에 있다.
        f"- 검색 라운드 {bundle.get('rounds', 0)}회 · 읽은 페이지 "
        f"{bundle.get('pages_read', 0)}쪽",
        "- 의미 검색: "
        + (
            "사용함"
            if semantic.get("active")
            else f"사용하지 않음 — {semantic.get('reason') or '사유 미기록'}"
        ),
    ]
    if bundle.get("coverage_blockers"):
        lines.append("- 검토 범위 제한:")
        lines += [f"  · {reason}" for reason in bundle["coverage_blockers"]]
    if bundle.get("deferred_pending"):
        lines.append(
            f"- 반환 예산으로 이월되어 이번 실행에서 확인하지 못한 action: "
            f"{len(bundle['deferred_pending'])}건"
        )

    lines += ["", "[청구항 구성별 근거]"]
    for component in bundle.get("components", []):
        lines += [
            "",
            f"### {component['component_id']} · {component['claim_component']}",
            f"- 구성 내용: {component.get('feature') or '(미기재)'}",
            f"- 중요도/현재 우선순위: {component.get('declared_importance', 'medium')} / "
            f"{component.get('priority', 'medium')} · 불확실성: "
            f"{component.get('uncertainty', 'high')} · 검색 완전성: "
            f"{component.get('search_completeness', 'unsearched')} "
            f"({component.get('coverage_ratio', 0):.0%})",
            f"- 사용한 검색어: {', '.join(component.get('queries_used') or []) or '(없음)'}",
            f"- 검색 채널: {', '.join(component.get('search_channels_used') or []) or '(없음)'}",
            f"- ARIA 확정 상태: {component['status']} — {component['status_label']}",
        ]
        if component.get("status_reasons"):
            lines.append("- 확정하지 못한 사유:")
            lines += [f"  · {reason}" for reason in component["status_reasons"]]
        if component.get("priority_reasons"):
            lines.append("- 우선순위 재평가 사유:")
            lines += [f"  · {reason}" for reason in component["priority_reasons"]]
        reviewed = component.get("reviewed_pages") or {}
        if reviewed:
            lines.append(
                "- 검토한 페이지: "
                + " / ".join(
                    f"{alias} {', '.join(entry['ranges'])}"
                    for alias, entry in reviewed.items()
                    if entry.get("ranges")
                )
            )
        unreviewed = component.get("unreviewed_pages") or {}
        if unreviewed:
            lines.append(
                "- 확인하지 못한 페이지: "
                + " / ".join(
                    f"{alias} {entry['count']}쪽"
                    for alias, entry in unreviewed.items()
                    if entry.get("count")
                )
            )
        if component.get("findings"):
            lines.append("- 근거 구간:")
            for finding in component["findings"]:
                lines += _finding_lines(finding)
        else:
            lines.append("- 근거 구간: 없음")
        if component.get("ai_note"):
            lines.append(
                f"- [검색 단계 AI 메모 — 원문 아님] {component['ai_note']}"
            )
        if component.get("needs_original_review"):
            lines.append("- 사람이 원본 PDF 를 확인해야 하는 구성입니다.")

    if bundle.get("rejected_evidence"):
        lines += ["", "[확인되지 않아 제외한 근거 주장]"]
        for item in bundle["rejected_evidence"]:
            lines.append(
                f"- {item.get('component_id')} · {item.get('attachment')} "
                f"{item.get('chunk_id') or ''}: {item.get('reason')}"
            )

    # 근거 구간이 실린 페이지의 전문. 발췌만으로는 앞뒤 문맥이 끊기므로,
    # 예산이 허락하는 만큼 페이지를 통째로 덧붙인다. 예산이 모자라면 fit() 이
    # 여기서부터 줄인다 — 덧붙임이므로 압박이 오면 가장 먼저 사라진다.
    lines += pages_module.render(
        bundle.get("evidence_pages") or [], bundle.get("page_reductions") or []
    )

    lines += [
        "",
        "[판정 제한]",
        "검색 결과가 없다는 것만으로 「인용발명에 해당 구성이 없다」고 쓰지",
        "마십시오. 위 상태가 matched 가 아닌 구성에 대해서는 다음 표현을",
        "사용하십시오:",
        f"  \"{NOT_FOUND_PHRASE}.\"",
    ]
    return "\n".join(lines)


# 렌더링에서 보여주는 구성 메타데이터의 상한. 원문이 아니라 AI 가 쓴 문구와
# 검색어 목록이므로, 길이를 제한해도 근거가 사라지지 않는다.
MAX_RENDERED_QUERIES = 40
MAX_RENDERED_LABEL = 200
MAX_RENDERED_FEATURE = 400


# 축약 사유 문구. **길이가 변하지 않는 고정 문자열**이어야 한다.
# 사유를 렌더링에 반영하면 패키지가 커지므로, 반영 → 재측정을 반복해서 크기를
# 수렴시킨다. 사유 문구에 개수 같은 가변 값을 넣으면 그 반복이 수렴하지 않는다.
REDUCTION_NO_EXCERPT = (
    "근거 패키지 예산에 맞추려고 문헌 서지 확인용 첫 페이지 발췌를 뺐습니다. "
    "최종 분석 모델이 공개번호를 읽지 못하므로 문헌 매핑이 실패할 수 있습니다."
)
REDUCTION_TRIMMED_META = (
    "근거 패키지 예산에 맞추려고 구성 이름·내용과 검색어 목록을 줄였습니다. "
    "원문 근거는 그대로입니다."
)
REDUCTION_DROPPED_FINDINGS = (
    "근거 패키지 예산에 맞추려고 일부 근거 구간을 뺐습니다. 구성마다 최소 1건은 "
    "남겼습니다. 뺀 구간은 검토 범위에서 빠진 것이므로 「없음」 판정의 근거가 "
    "되지 않습니다."
)
REDUCTION_OVER_BUDGET = (
    "근거 패키지가 이번 실행의 문자 또는 바이트 예산에 들어가지 않습니다. "
    "근거 패키지 예산과 Provider 입력 한도를 확인해야 합니다."
)


def _add_reduction(bundle: dict, reason: str) -> None:
    reductions = bundle.setdefault("package_reductions", [])
    if reason not in reductions:
        reductions.append(reason)


def _apply_reductions(bundle: dict) -> None:
    """축약 사유를 bundle 전체에 반영한다. 여러 번 불러도 결과가 같다.

    두 가지를 함께 한다.

      1. 전역 검토 범위 제한(coverage_blockers)에 올린다.
      2. **구성별 상태를 다시 내린다.** 구성 상태는 축약 이전에 확정되므로,
         여기서 내리지 않으면 "예산 때문에 근거를 뺐는데 그 구성은 여전히
         「검토 범위에서 미발견」" 인 상태가 남는다. 뺀 범위를 근거로 없음을
         말하는 것이라 가장 위험한 조합이다.

    idempotent 여야 한다. fit() 이 반영 → 재렌더링 → 재측정을 반복하는데,
    부를 때마다 내용이 늘면 크기가 수렴하지 않는다.
    """
    reductions = bundle.get("package_reductions") or []
    if not reductions:
        return

    blockers = bundle.setdefault("coverage_blockers", [])
    for reason in reductions:
        if reason not in blockers:
            blockers.append(reason)

    for component in bundle.get("components", []):
        reasons = component.setdefault("status_reasons", [])
        for reason in reductions:
            if reason not in reasons:
                reasons.append(reason)
        if component.get("status") == STATUS_NOT_FOUND_SCOPE:
            component["status"] = STATUS_COVERAGE
            component["status_label"] = STATUS_LABEL[STATUS_COVERAGE]
        component["needs_original_review"] = True


def _drop_identity_excerpts(bundle: dict) -> bool:
    changed = False
    for document in bundle.get("documents", []):
        if document.get("identity_excerpt"):
            document["identity_excerpt"] = ""
            changed = True
    if changed:
        _add_reduction(bundle, REDUCTION_NO_EXCERPT)
    return changed


def _trim_component_metadata(bundle: dict) -> bool:
    changed = False
    for component in bundle.get("components", []):
        if len(component.get("queries_used") or []) > MAX_RENDERED_QUERIES:
            component["queries_used"] = component["queries_used"][:MAX_RENDERED_QUERIES]
            changed = True
        if len(component.get("claim_component") or "") > MAX_RENDERED_LABEL:
            component["claim_component"] = (
                component["claim_component"][:MAX_RENDERED_LABEL] + "…"
            )
            changed = True
        if len(component.get("feature") or "") > MAX_RENDERED_FEATURE:
            component["feature"] = component["feature"][:MAX_RENDERED_FEATURE] + "…"
            changed = True
    if changed:
        _add_reduction(bundle, REDUCTION_TRIMMED_META)
    return changed


def _drop_one_finding(bundle: dict) -> bool:
    """근거가 가장 많은 구성에서 하나를 뺀다. 마지막 하나는 남긴다.

    마지막 근거까지 빼면 matched 인데 근거가 없는 상태가 되고, 그건 보고서에서
    구분되지 않는다. 원문 자체를 자르는 것도 하지 않는다 — 잘라 넣으면 모델은
    그것을 문헌의 전부로 읽는다.
    """
    target = max(
        bundle.get("components", []),
        key=lambda component: len(component.get("findings") or []),
        default=None,
    )
    if target is None or len(target.get("findings") or []) <= 1:
        return False
    target["findings"].pop()
    target["evidence_truncated"] = True
    bundle["budget_exhausted"] = True
    _add_reduction(bundle, REDUCTION_DROPPED_FINDINGS)
    return True


def fit(bundle: dict, budget: RetrievalBudget) -> str:
    """근거 패키지를 예산 안으로 **실제로** 맞춘다.

    항목별로 더하기 전에 재는 것만으로는 부족하다. 서지 발췌, 구성 이름과 내용,
    검색어 목록, 문헌별 검색 기록, 상태 사유는 모두 렌더링에 들어가지만 근거
    구간과 달리 개수가 예산과 무관하게 늘어난다. 구성 20개에 긴 문구를 붙이면
    근거를 하나도 넣지 않아도 예산의 여섯 배가 나온다(실측).

    그래서 **완성된 문자열을 직접 재고**, 넘으면 아래 순서로 줄인다.

      1. 페이지 확장 — 근거 구간 앞뒤 문맥 페이지부터, 그다음 근거 페이지 전문.
         덧붙임이므로 가장 먼저 사라진다. 다 빠지면 예전의 청크 단위 패키지와
         같아지고, 뺀 페이지는 미확인으로 기록된다.
      2. 서지 확인용 첫 페이지 발췌 — 문헌 번호를 붙이는 편의이지 근거가 아니다
      3. 구성 메타데이터(검색어 목록, 이름, 내용) 축약 — AI 가 쓴 문구다
      4. 구성마다 근거 구간을 하나만 남긴다

    줄일 때마다 그 사실을 bundle 에 **반영한 뒤 다시 렌더링해서 잰다.** 반영이
    렌더링을 키우기 때문이다 — 반영 전 문자열을 재고 그 값을 믿으면, 최종
    프롬프트가 안내한 크기보다 커진다. 그래서 이 함수가 돌려주는 문자열은 항상
    `render(bundle)` 과 같다.

    세 단계로도 못 맞추면 조용히 넘기지 않고 표시한다. 넘겨 보내면 preflight
    가 안내한 크기가 거짓이 되고, 검색 비용을 다 쓴 뒤 Provider 호출 직전에
    막힌다.
    """
    max_chars = budget.max_evidence_chars
    max_bytes = budget.evidence_byte_limit

    def current() -> tuple[str, bool]:
        partial = pages_module.truncations(bundle.get("evidence_pages") or [])
        bundle["page_truncations"] = partial
        if partial or bundle.get("page_reductions") or bundle.get("package_reductions"):
            bundle["budget_exhausted"] = True
            bundle["budget_limited"] = True
        if partial:
            bundle["budget_exhausted"] = True
            for component in bundle.get("components", []):
                component["needs_original_review"] = True
                if component.get("status") == STATUS_NOT_FOUND_SCOPE:
                    component["status"] = STATUS_COVERAGE
                    component["status_label"] = STATUS_LABEL[STATUS_COVERAGE]
                    reason = "페이지 일부가 예산으로 누락돼 전문 검토가 완료되지 않았습니다."
                    if reason not in component.setdefault("status_reasons", []):
                        component["status_reasons"].append(reason)
        _apply_reductions(bundle)
        text = render(bundle)
        return text, (
            len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes
        )

    text, ok = current()
    if ok:
        return text

    # 1단계: 페이지 확장부터 줄인다. 덧붙임이므로 가장 먼저 사라져야 한다.
    #        문맥 페이지(후보에서 먼 것) → 근거 페이지 순이다.
    #
    # 이 축약은 package_reductions 에 넣지 않는다. 그쪽에 넣으면
    # _apply_reductions 가 모든 구성의 상태 사유에 같은 문장을 붙이고 not_found
    # 를 coverage 로 내리는데, **페이지를 뺀 것은 근거를 뺀 것이 아니다.**
    # 근거 구간과 발췌는 그대로이고 빠진 것은 앞뒤 문맥뿐이다. 빠진 페이지는
    # 「미확인 페이지」에 자동으로 나타난다.
    dropped = bundle.setdefault("page_reductions", [])
    for only_context in (True, False):
        while True:
            removed = pages_module.drop_one(
                bundle.get("evidence_pages") or [], only_context=only_context
            )
            if removed is None:
                break
            dropped.append(removed["label"])
            text, ok = current()
            if ok:
                return text

    for step in (_drop_identity_excerpts, _trim_component_metadata):
        if step(bundle):
            text, ok = current()
            if ok:
                return text

    while _drop_one_finding(bundle):
        text, ok = current()
        if ok:
            return text

    bundle["package_over_budget"] = True
    _add_reduction(bundle, REDUCTION_OVER_BUDGET)
    text, _ok = current()
    # 사용자가 올려야 할 값. 렌더링에는 들어가지 않으므로 크기에 영향이 없다.
    bundle["package_required_chars"] = len(text)
    bundle["package_required_bytes"] = len(text.encode("utf-8"))
    return text


def render_placeholder(budget: RetrievalBudget, documents: list[dict]) -> str:
    """preflight 전용. 실제 패키지가 만들어지기 전에 크기만 재기 위한 자리표.

    예산의 뜻이 "근거 구간 원문의 합"이 아니라 **"렌더링된 근거 패키지 전체"**
    이므로, 자리표는 그냥 그 길이만큼의 문자다. fit() 이 완성된 문자열을 직접
    재서 이 값을 넘지 못하게 하므로, 여기서 잰 크기가 실제 크기의 상한이 된다.

    fit() 이 문자 수와 실행별 UTF-8 바이트 수를 함께 강제한다. 자리표도 두
    상한을 동시에 나타내므로 영문 문헌에서도 바이트 여유를 사용할 수 있다.

    documents 는 더 이상 크기에 영향을 주지 않는다. 문헌 목록도 예산 안에
    들어가는 렌더링의 일부이기 때문이다.
    """
    # 문자 상한과 바이트 상한을 동시에 나타낸다. 영문이 많은 패키지는 같은
    # 바이트 안에 더 많은 문자를 담을 수 있으므로 바이트를 무조건 3으로 나눠
    # 문자 상한을 낮추지 않는다. fit() 이 두 상한을 각각 검사한다.
    chars = min(max(0, budget.max_evidence_chars), budget.evidence_byte_limit)
    byte_count = min(chars * 3, budget.evidence_byte_limit)
    extra = byte_count - chars
    triples, doubles = divmod(extra, 2)
    return "가" * triples + "é" * doubles + "a" * (chars - triples - doubles)
