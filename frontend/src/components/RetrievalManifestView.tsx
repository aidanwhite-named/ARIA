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
import { isNarrowed } from "../lib/types";
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

/** 이 문헌이 왜 「정상」에서 내려갔는가.
 *
 *  두 갈래로 나눠서 돌려준다.
 *    unreadable  내용을 **얻지 못한** 페이지. 원본을 봐야만 확인된다.
 *    suspect     텍스트는 얻었으나 추출 결과를 그대로 믿기 어려운 페이지.
 *
 *  섞으면 뒤엣것까지 OCR 문제로 읽힌다. 실제로 추출 방식 간 차이 하나로 올라온
 *  텍스트 PDF 가 「원본 PDF 를 직접 확인해야 한다」 목록에 실렸고, 그 줄에는
 *  사유가 통째로 비어 있었다 — 여기서 나열하던 셋(도면 전용·저문자·추출 실패)
 *  중 어디에도 안 걸리는 사유였기 때문이다. 이유 없는 「원본을 보라」가 가장
 *  나쁘다. 그래서 마지막에 빈 줄이 되지 않도록 한 번 더 막는다. */
function reviewReasons(document: RetrievalDocument): {
  unreadable: string[];
  suspect: string[];
} {
  const extraction = document.extraction;
  const unreadable: string[] = [];
  const suspect: string[] = [];

  if (pageCount(extraction.visual_review_required_pages)) {
    unreadable.push(
      `도면·이미지만 있는 페이지 ${extraction.visual_review_required_pages.join(", ")}`,
    );
  }
  if (pageCount(extraction.empty_or_low_text_pages)) {
    unreadable.push(
      `텍스트를 얻지 못한 페이지 ${extraction.empty_or_low_text_pages.join(", ")}`,
    );
  }
  if (pageCount(extraction.extraction_failed_pages)) {
    unreadable.push(
      `추출 실패 페이지 ${extraction.extraction_failed_pages.join(", ")}`,
    );
  }
  if (extraction.open_error) {
    unreadable.push(`파일을 열지 못했습니다: ${extraction.open_error}`);
  }
  if (extraction.status === "unusable" && unreadable.length === 0) {
    unreadable.push("쓸 수 있는 페이지가 없습니다");
  }

  if (pageCount(extraction.extraction_divergence_pages)) {
    suspect.push(
      `두 가지 추출 방식의 결과가 어긋난 페이지 ${extraction.extraction_divergence_pages.join(", ")}`,
    );
  }
  if (extraction.page_count_mismatch) {
    suspect.push(
      `원본 ${extraction.source_page_count}쪽 중 ${extraction.processed_page_count}쪽만 처리했습니다`,
    );
  }

  if (
    extraction.status !== "complete" &&
    unreadable.length === 0 &&
    suspect.length === 0
  ) {
    suspect.push(
      `추출 상태가 「${EXTRACTION_LABEL[extraction.status] ?? extraction.status}」인데 사유가 기록되지 않았습니다`,
    );
  }

  return { unreadable, suspect };
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

  if (!isNarrowed(job.delivery_plan) && !manifest) return null;

  // 상자를 둘로 나눈다. 내용을 못 얻은 문헌과 추출을 믿기 어려운 문헌은
  // 사용자가 할 일이 다르다 — 앞은 원본을 봐야 하고, 뒤는 대조해 보면 된다.
  // 한 문헌이 양쪽 사유를 다 가지면 무거운 쪽에만 넣고 사유를 합쳐 적는다.
  const reviewed = (manifest?.documents ?? []).map((document) => ({
    document,
    ...reviewReasons(document),
  }));
  const unreadableDocuments = reviewed.filter(
    (entry) => entry.unreadable.length > 0,
  );
  const suspectDocuments = reviewed.filter(
    (entry) => entry.unreadable.length === 0 && entry.suspect.length > 0,
  );

  return (
    <section className="card" style={{ marginTop: 16 }}>
      <h2>로컬 검색 기록</h2>
      <p className="faint">
        이 실행은 인용발명 문헌의 <strong>전체 본문을 프롬프트에 넣지
        않았습니다.</strong> 근거 패키지에는 찾은 구간과 함께 그 구간이 실린
        페이지 전문·앞뒤 페이지가 예산이 허락하는 만큼 들어갑니다. 거기에 없는
        페이지는 이번 검토 범위 밖입니다.{" "}
        ARIA 가 페이지·문단 단위로 로컬 색인한 뒤, AI 가
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
                {manifest.budget.max_evidence_bytes != null && (
                  <span className="faint">
                    {" · 바이트 상한 "}{manifest.budget.max_evidence_bytes.toLocaleString()} bytes
                  </span>
                )}
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
              검색·열람 중 예산 제한이 발생했습니다. 확인하지 못한 범위와
              미처리 요청은 근거 패키지에 기록되어 있습니다.
            </div>
          )}

          {(manifest.page_truncations ?? []).length > 0 && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              <strong>페이지 부분 수록</strong>
              <ul>
                {manifest.page_truncations!.map((page) => (
                  <li key={`${page.attachment}-${page.pdf_page}`}>
                    {page.attachment} p.{page.pdf_page}: 첫 {page.included_chars.toLocaleString()}자 수록
                    {" / 전체 "}{page.source_chars.toLocaleString()}자
                    {" · 누락 "}{page.omitted_chars.toLocaleString()}자
                  </li>
                ))}
              </ul>
              <div className="faint">부분 수록은 페이지 전문 확인이 아닙니다. 누락 구간은 검토 범위 밖입니다.</div>
            </div>
          )}

          {(manifest.page_reductions ?? []).length > 0 && (
            <div className="notice" style={{ marginTop: 12 }}>
              <strong>예산 때문에 뺀 페이지</strong>
              <div className="faint" style={{ marginTop: 4 }}>
                {(manifest.page_reductions ?? []).join(", ")}
              </div>
              <div className="faint" style={{ marginTop: 4 }}>
                근거 구간과 그 발췌는 그대로입니다. 빠진 것은 페이지 전문과 앞뒤
                문맥이며, 해당 페이지는 「미확인 페이지」로 기록됩니다. 문자 예산을
                올리면 입력 바이트 한도에 여유가 있는 범위에서 더 담을 수 있습니다.
              </div>
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
                환경설정의 문자 예산과 실행 전 안내의 바이트 예산을 함께 확인해 주세요.
              </div>
            </div>
          )}

          {unreadableDocuments.length > 0 && (
            <div className="notice warn" style={{ marginTop: 12 }}>
              <strong>원본 PDF 를 직접 확인해야 하는 문헌이 있습니다</strong>
              <ul style={{ marginTop: 6 }}>
                {unreadableDocuments.map((entry) => (
                  <li key={entry.document.attachment_id}>
                    {entry.document.alias} · {entry.document.filename} —{" "}
                    {[...entry.unreadable, ...entry.suspect].join(" · ")}
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

          {suspectDocuments.length > 0 && (
            <div className="notice" style={{ marginTop: 12 }}>
              <strong>추출 결과를 원본과 대조해 두면 좋은 문헌이 있습니다</strong>
              <ul style={{ marginTop: 6 }}>
                {suspectDocuments.map((entry) => (
                  <li key={entry.document.attachment_id}>
                    {entry.document.alias} · {entry.document.filename} —{" "}
                    {entry.suspect.join(" · ")}
                  </li>
                ))}
              </ul>
              <div className="faint" style={{ marginTop: 4 }}>
                이 문헌들은 텍스트를 정상적으로 얻었습니다. ARIA 가 같은 페이지를
                두 가지 방식으로 뽑아 견주었을 때 글자 수가 크게 어긋났다는
                뜻이며, 회전된 라벨이나 다단 편집처럼 배치가 까다로운 페이지에서
                자주 나옵니다. OCR 로 해결되는 문제가 아니고 본문 판독에도 대개
                지장이 없습니다. 그 페이지가 판단의 근거가 됐다면 원본을 한 번
                보십시오.
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
