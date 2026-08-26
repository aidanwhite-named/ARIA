/** 로컬 검색 실행의 감사 기록 표시.
 *
 *  보고서 본문과 따로 둔다. 여기 있는 것은 "무엇을 색인했고, 어떤 검색어로
 *  어디를 읽었으며, 무엇을 확인하지 못했는가"이고, 그건 보고서의 결론과 다른
 *  종류의 정보다.
 *
 *  두 층을 화면에서도 섞지 않는다.
 *    ARIA 가 관측한 것 : 페이지 수, 추출 상태, 실제 실행된 검색어, 읽은 페이지
 *    AI 가 정한 것     : 구성 분해, 검색어 선택, 관련성 판단
 *  전자만 ARIA 가 보증한다.
 *
 *  OCR 버튼도, OCR 이 수행된 것처럼 보이는 표시도 만들지 않는다. 텍스트를 얻지
 *  못한 페이지는 그렇다고만 적는다.
 */

import { useState } from "react";

import { api } from "../lib/api";
import type { Job, RetrievalDocument } from "../lib/types";

const EXTRACTION_LABEL: Record<string, string> = {
  complete: "정상",
  review_required: "검토 필요",
  unusable: "사용 불가",
};

const EXTRACTION_CLASS: Record<string, string> = {
  complete: "ok",
  review_required: "warn",
  unusable: "danger",
};

const STATUS_LABEL: Record<string, string> = {
  complete: "완료",
  partial: "예산 소진으로 중단",
  failed: "실패",
};

function pageCount(pages: number[] | undefined): number {
  return pages?.length ?? 0;
}

function DocumentRow({ document }: { document: RetrievalDocument }) {
  const extraction = document.extraction;
  const warnings = [
    pageCount(extraction.empty_or_low_text_pages) &&
      `빈·저문자 ${pageCount(extraction.empty_or_low_text_pages)}쪽`,
    pageCount(extraction.extraction_failed_pages) &&
      `추출 실패 ${pageCount(extraction.extraction_failed_pages)}쪽`,
    pageCount(extraction.visual_review_required_pages) &&
      `원본 확인 필요 ${pageCount(extraction.visual_review_required_pages)}쪽`,
    pageCount(extraction.extraction_divergence_pages) &&
      `추출 방식 간 차이 의심 ${pageCount(extraction.extraction_divergence_pages)}쪽`,
  ].filter(Boolean) as string[];

  return (
    <tr>
      <td className="mono-text">{document.alias}</td>
      <td className="break">{document.filename}</td>
      <td>
        {extraction.processed_page_count} / {extraction.source_page_count}쪽
        {extraction.page_count_mismatch && (
          <span className="pill danger" style={{ marginLeft: 6 }}>
            페이지 수 불일치
          </span>
        )}
      </td>
      <td>{extraction.ok_pages}쪽</td>
      <td>
        <span className={`pill ${EXTRACTION_CLASS[extraction.status] ?? "neutral"}`}>
          {EXTRACTION_LABEL[extraction.status] ?? extraction.status}
        </span>
        {warnings.length > 0 && (
          <div className="faint" style={{ marginTop: 4 }}>
            {warnings.join(" · ")}
          </div>
        )}
      </td>
      <td className="mono-text">
        {extraction.chunk_count.toLocaleString()}
        {extraction.chunk_failures > 0 && ` (실패 ${extraction.chunk_failures})`}
      </td>
      <td className="break mono-text">
        {document.pdf_sha256.slice(0, 12)}…
        <div className="faint">
          idx v{document.index.index_version} · {document.index.extractor_version}
          {document.index_rebuilt ? " · 재생성" : " · 재사용"}
        </div>
      </td>
    </tr>
  );
}

export default function RetrievalManifestView({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const manifest = job.retrieval_manifest;

  if (job.delivery_plan !== "local_retrieval" && !manifest) return null;

  const reviewNeeded =
    manifest?.documents.filter(
      (document) =>
        document.extraction.status !== "complete" ||
        pageCount(document.extraction.visual_review_required_pages) > 0,
    ) ?? [];

  return (
    <section className="card" style={{ marginTop: 16 }}>
      <h2>로컬 검색 기록</h2>
      <p className="faint">
        이 실행은 인용발명 문헌의 <strong>전체 본문을 프롬프트에 넣지
        않았습니다.</strong> ARIA 가 페이지·문단 단위로 로컬 색인한 뒤, AI 가
        청구항 구성별로 검색·열람한 구간만 근거 패키지로 전달했습니다. 아래에
        없는 페이지는 이번 검토 범위 밖이며, 검토하지 않은 것과 문헌에 없는
        것은 다릅니다.
      </p>

      {job.retrieval_manifest_error && (
        <div className="notice danger">
          <strong>근거 패키지를 만들지 못했습니다</strong>
          <div style={{ marginTop: 4 }}>{job.retrieval_manifest_error}</div>
        </div>
      )}

      {!manifest && (
        <div className="notice info">
          이 실행의 검색 기록이 남아 있지 않습니다.
        </div>
      )}

      {manifest && (
        <>
          <div className="run-ready" style={{ marginTop: 12 }}>
            <div className="run-ready-row">
              <span>전달 방식</span>
              <strong>로컬 검색 (근거 패키지)</strong>
            </div>
            <div className="run-ready-row">
              <span>색인 상태</span>
              <strong>
                {manifest.documents.length}건 색인 ·{" "}
                {STATUS_LABEL[manifest.status] ?? manifest.status}
              </strong>
            </div>
            <div className="run-ready-row">
              <span>AI 검색 라운드</span>
              <strong>
                {manifest.rounds.length} / {manifest.budget.max_rounds}회
              </strong>
            </div>
            <div className="run-ready-row">
              <span>읽은 페이지</span>
              <strong>
                {manifest.pages_read} / {manifest.budget.max_page_reads}쪽
              </strong>
            </div>
            <div className="run-ready-row">
              <span>근거 패키지</span>
              <strong>
                {manifest.evidence_chars.toLocaleString()} /{" "}
                {manifest.budget.max_evidence_chars.toLocaleString()}자
              </strong>
            </div>
            <div className="run-ready-row">
              <span>의미 검색</span>
              <strong>
                {manifest.semantic.active ? "사용함" : "사용하지 않음"}
              </strong>
            </div>
          </div>

          {!manifest.semantic.active && manifest.semantic.reason && (
            <div className="notice info" style={{ marginTop: 12 }}>
              {manifest.semantic.reason}
            </div>
          )}

          {!manifest.sqlite.trigram && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              이 환경의 SQLite(v{manifest.sqlite.sqlite_version})에 trigram
              토크나이저가 없어 부분문자 검색을 수행하지 못했습니다. 합성어와
              조사 차이로 놓친 구간이 있을 수 있습니다.
            </div>
          )}

          {manifest.budget_exhausted && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              검색 라운드 또는 페이지 읽기 예산을 모두 사용해 검토를
              중단했습니다. 확인하지 못한 범위가 근거 패키지에 그대로
              기록되어 있습니다.
            </div>
          )}

          {(manifest.package_reductions ?? []).length > 0 && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              <strong>근거 패키지를 예산에 맞추려고 줄였습니다</strong>
              <ul style={{ marginTop: 6 }}>
                {manifest.package_reductions.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
              <div className="faint" style={{ marginTop: 4 }}>
                원문은 자르지 않았습니다. 줄인 범위는 검토 범위 제한으로
                기록되며, 그 때문에 해당 구성은 「없음」으로 판정되지 않습니다.
                환경설정의 「근거 패키지 최대 문자 수」를 올리면 줄이지 않습니다.
              </div>
            </div>
          )}

          {reviewNeeded.length > 0 && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              <strong>원본 PDF 를 직접 확인해야 하는 문헌이 있습니다</strong>
              <ul style={{ marginTop: 6 }}>
                {reviewNeeded.map((document) => (
                  <li key={document.attachment_id}>
                    {document.alias} · {document.filename} —{" "}
                    {[
                      pageCount(document.extraction.visual_review_required_pages) &&
                        `도면·이미지만 있는 페이지 ${document.extraction.visual_review_required_pages.join(", ")}`,
                      pageCount(document.extraction.empty_or_low_text_pages) &&
                        `텍스트를 얻지 못한 페이지 ${document.extraction.empty_or_low_text_pages.join(", ")}`,
                      pageCount(document.extraction.extraction_failed_pages) &&
                        `추출 실패 페이지 ${document.extraction.extraction_failed_pages.join(", ")}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </li>
                ))}
              </ul>
              <div className="faint" style={{ marginTop: 4 }}>
                ARIA 는 OCR 을 수행하지 않습니다. 이 페이지들의 내용은 확인되지
                않았으며, 그 사실 때문에 해당 구성은 「문헌에 없음」으로
                판정되지 않습니다.
              </div>
            </div>
          )}

          <div className="table-scroll" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>자료</th>
                  <th>파일</th>
                  <th>처리/원본</th>
                  <th>정상</th>
                  <th>추출 상태</th>
                  <th>청크</th>
                  <th>PDF sha256 · 인덱스</th>
                </tr>
              </thead>
              <tbody>
                {manifest.documents.map((document) => (
                  <DocumentRow key={document.attachment_id} document={document} />
                ))}
              </tbody>
            </table>
          </div>

          {manifest.not_indexed.length > 0 && (
            <div className="notice danger" style={{ marginTop: 12 }}>
              <strong>색인하지 못한 자료</strong>
              <ul>
                {manifest.not_indexed.map((item) => (
                  <li key={item.filename}>
                    {item.filename} — {item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="btn-row no-print" style={{ marginTop: 12 }}>
            <a
              className="btn"
              href={api.retrievalArtifactUrl(job.id, "evidence")}
              target="_blank"
              rel="noreferrer"
            >
              근거 패키지 열기
            </a>
            <a
              className="btn"
              href={api.retrievalArtifactUrl(job.id, "trace")}
              target="_blank"
              rel="noreferrer"
            >
              검색 trace 열기
            </a>
            <a
              className="btn"
              href={api.retrievalArtifactUrl(job.id, "extraction")}
              target="_blank"
              rel="noreferrer"
            >
              추출 완전성 보고서
            </a>
            <button className="btn" onClick={() => setOpen((value) => !value)}>
              {open ? "상세 접기" : "구성별 검색어 보기"}
            </button>
          </div>

          {open && (
            <div className="table-scroll" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>구성</th>
                    <th>실제 실행된 검색어</th>
                    <th>문헌별 검색</th>
                    <th>검색 채널</th>
                    <th>후보</th>
                  </tr>
                </thead>
                <tbody>
                  {manifest.components.map((component) => (
                    <tr key={component.id}>
                      <td className="mono-text">
                        {component.id}
                        <div className="faint">{component.label}</div>
                      </td>
                      <td className="break">
                        {component.queries.join(", ") || "(없음)"}
                      </td>
                      <td className="mono-text">
                        {(component.searched_documents ?? []).map((record) => (
                          <div key={record.attachment_id}>
                            {record.attachment} · 검색어 {record.queries.length}개
                            · 후보 {record.hits}건
                          </div>
                        ))}
                        {(component.unsearched_documents ?? []).length > 0 && (
                          <div style={{ color: "var(--danger)" }}>
                            검색하지 않음:{" "}
                            {component.unsearched_documents.join(", ")}
                          </div>
                        )}
                      </td>
                      <td className="mono-text">
                        {component.channels_used.join(", ") || "(없음)"}
                        {component.channels_failed.length > 0 && (
                          <div style={{ color: "var(--danger)" }}>
                            실행 실패: {component.channels_failed.join(", ")}
                          </div>
                        )}
                      </td>
                      <td>{component.candidates}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <table style={{ marginTop: 12 }}>
                <thead>
                  <tr>
                    <th>라운드</th>
                    <th>상태</th>
                    <th>action</th>
                    <th>입력 sha256</th>
                    <th>출력 sha256</th>
                  </tr>
                </thead>
                <tbody>
                  {manifest.rounds.map((round) => (
                    <tr key={round.round}>
                      <td>{round.round}</td>
                      <td>
                        {round.status}
                        {round.error && (
                          <div className="faint break">{round.error}</div>
                        )}
                      </td>
                      <td>{round.actions}</td>
                      <td className="mono-text">
                        {round.input_sha256.slice(0, 16)}…
                      </td>
                      <td className="mono-text">
                        {round.output_sha256.slice(0, 16)}…
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {manifest.action_errors.length > 0 && (
                <div className="notice warn" style={{ marginTop: 12 }}>
                  <strong>거절한 AI 요청</strong>
                  <ul>
                    {manifest.action_errors.map((item, index) => (
                      <li key={index}>
                        {item.action ? `${item.action}: ` : ""}
                        {item.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
