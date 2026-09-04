/** 추출 경고 상자가 「왜」를 말하는지 지킨다.
 *
 *  이 파일이 있는 이유는 실측 사고 하나다. 텍스트 PDF 한 건이 「원본 PDF 를
 *  직접 확인해야 하는 문헌」 목록에 올랐는데 그 줄의 사유가 통째로 비어 있었다.
 *  상태를 내린 사유(추출 방식 간 차이)가 목록이 나열하던 셋 중 어디에도 없었기
 *  때문이다. 화면은 원본을 보라고만 하고 왜인지는 갖고 있지 않았다.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import RetrievalManifestView from "./RetrievalManifestView";
import type { Job, RetrievalDocument } from "../lib/types";

afterEach(cleanup);

type Extraction = RetrievalDocument["extraction"];

function citation(overrides: Partial<Extraction> = {}): RetrievalDocument {
  return {
    alias: "ATT-04",
    attachment_id: "att-04",
    filename: "2310.07771v1.pdf",
    pdf_sha256: "d91fa579d1da1b96ea54a12f7e7aa9aca259600981199b88d30336f3a72d87ff",
    role: "CITATION",
    index_rebuilt: true,
    index: {
      index_version: 1,
      extractor_version: "pypdf-5.1.0+aria-1",
      chunk_count: 49,
      page_count: 13,
      source_page_count: 13,
      trigram_enabled: true,
      built_at: "",
    },
    extraction: {
      source_page_count: 13,
      processed_page_count: 13,
      page_count_mismatch: false,
      ok_pages: 13,
      empty_or_low_text_pages: [],
      extraction_failed_pages: [],
      visual_review_required_pages: [],
      extraction_divergence_pages: [],
      chunk_count: 49,
      chunk_failures: 0,
      status: "complete",
      open_error: null,
      ...overrides,
    },
  };
}

function job(documents: RetrievalDocument[]): Job {
  return {
    id: "job-1",
    delivery_plan: "local_retrieval",
    retrieval_manifest: {
      documents,
      not_indexed: [],
      rounds: [],
      components: [],
      action_errors: [],
      status: "partial",
      pages_read: 3,
      repeat_page_reads: 0,
      evidence_chars: 38_320,
      budget_exhausted: true,
      page_reductions: [],
      package_reductions: [],
      budget: {
        max_rounds: 10,
        max_page_reads: 80,
        max_evidence_chars: 40_000,
      },
      semantic: { active: true, reason: "" },
      sqlite: { trigram: true, sqlite_version: "3.43.1" },
    },
  } as unknown as Job;
}

describe("추출 경고", () => {
  it("부분 수록 페이지는 전문 확인과 구분하고 누락 글자 수를 표시한다", () => {
    const value = job([citation()]);
    value.retrieval_manifest!.page_truncations = [{
      attachment: "ATT-04", pdf_page: 3, source_chars: 5000,
      included_chars: 1250, omitted_chars: 3750,
    }];
    render(<RetrievalManifestView job={value} />);
    expect(screen.getByText("페이지 부분 수록")).toBeTruthy();
    expect(screen.getByText(/ATT-04 p.3: 첫 1,250자 수록/)).toBeTruthy();
    expect(screen.getByText(/누락 3,750자/)).toBeTruthy();
    expect(screen.getByText(/페이지 전문 확인이 아닙니다/)).toBeTruthy();
  });

  it("추출 방식 차이만 있는 문헌은 사유를 적고 OCR 상자에 넣지 않는다", () => {
    render(
      <RetrievalManifestView
        job={job([
          citation({
            status: "review_required",
            extraction_divergence_pages: [1],
          }),
        ])}
      />,
    );

    expect(screen.getByText(/추출 결과를 원본과 대조/)).toBeTruthy();
    expect(
      screen.getByText(/두 가지 추출 방식의 결과가 어긋난 페이지 1/),
    ).toBeTruthy();
    expect(screen.queryByText(/원본 PDF 를 직접 확인해야/)).toBeNull();
    // OCR 안내는 내용을 얻지 못한 문헌에만 붙는다.
    expect(screen.queryByText(/ARIA 는 OCR 을 수행하지 않습니다/)).toBeNull();
  });

  it("텍스트를 얻지 못한 문헌은 원본 확인 상자에 남는다", () => {
    render(
      <RetrievalManifestView
        job={job([
          citation({
            status: "review_required",
            empty_or_low_text_pages: [4],
            ok_pages: 12,
          }),
        ])}
      />,
    );

    expect(screen.getByText(/원본 PDF 를 직접 확인해야/)).toBeTruthy();
    expect(screen.getByText(/텍스트를 얻지 못한 페이지 4/)).toBeTruthy();
    expect(screen.queryByText(/추출 결과를 원본과 대조/)).toBeNull();
  });

  it("두 사유를 다 가지면 무거운 상자 한 곳에서 둘 다 적는다", () => {
    render(
      <RetrievalManifestView
        job={job([
          citation({
            status: "review_required",
            empty_or_low_text_pages: [4],
            extraction_divergence_pages: [1],
          }),
        ])}
      />,
    );

    expect(
      screen.getByText(
        /텍스트를 얻지 못한 페이지 4 · 두 가지 추출 방식의 결과가 어긋난 페이지 1/,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/추출 결과를 원본과 대조/)).toBeNull();
  });

  it("사유 없는 상태 강등도 빈 줄로 두지 않는다", () => {
    render(
      <RetrievalManifestView job={job([citation({ status: "review_required" })])} />,
    );

    expect(screen.getByText(/사유가 기록되지 않았습니다/)).toBeTruthy();
  });

  it("정상 문헌에는 경고 상자를 만들지 않는다", () => {
    render(<RetrievalManifestView job={job([citation()])} />);

    expect(screen.queryByText(/원본 PDF 를 직접 확인해야/)).toBeNull();
    expect(screen.queryByText(/추출 결과를 원본과 대조/)).toBeNull();
  });
});
