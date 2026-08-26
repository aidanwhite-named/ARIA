/** 「분석에 포함」 선택이 화면 추정치와 요청 목록에 그대로 반영되는지 고정한다.
 *
 *  이 세 값(체크 상태 · 화면 글자 수 · 요청에 실리는 첨부 목록)이 갈라지면
 *  체크를 풀어도 본문이 그대로 실행에 들어간다. 그 어긋남은 실행이 끝난 뒤에야
 *  드러나므로 여기서 못박는다.
 */

import { describe, expect, it } from "vitest";

import {
  estimateTotalChars,
  hasAnalysisMaterial,
  includedFiles,
  seedInclusion,
  selectedAttachmentIds,
} from "./attachmentSelection";
import type { AttachmentAnalysis, JobAttachment } from "./types";

function file(
  id: string,
  overrides: Partial<AttachmentAnalysis> = {},
): AttachmentAnalysis {
  return {
    attachment_id: id,
    original_filename: `${id}.pdf`,
    mime_type: "application/pdf",
    size_bytes: 1000,
    sha256: `sha-${id}`,
    role: "CITATION",
    page_count: 3,
    char_count: 1000,
    extraction_method: "PDF_TEXT_LAYER",
    delivery_mode: "INLINE_CONTEXT",
    read_ok: true,
    error: null,
    included: true,
    ...overrides,
  };
}

const BASE = {
  claimText: "청구항 1. 테스트 청구항",
  followupInstruction: "",
  priorClaimChars: 0,
  priorReportChars: 0,
  promptBodyChars: 500,
};

describe("seedInclusion", () => {
  it("정상 처리된 PDF 만 체크한다", () => {
    const files = [
      file("a"),
      file("b", { read_ok: false, included: false, error: "스캔본" }),
    ];
    expect(seedInclusion(files)).toEqual({ a: true, b: false });
  });
});

describe("estimateTotalChars", () => {
  it("PDF 2건을 처리한 뒤 하나를 해제하면 글자 수가 줄어든다", () => {
    const files = [file("a", { char_count: 1000 }), file("b", { char_count: 4000 })];
    const both = estimateTotalChars({
      ...BASE,
      uploadedFiles: files,
      inclusion: { a: true, b: true },
      inheritedAttachments: [],
    });
    const onlyA = estimateTotalChars({
      ...BASE,
      uploadedFiles: files,
      inclusion: { a: true, b: false },
      inheritedAttachments: [],
    });

    expect(onlyA).toBeLessThan(both);
    // 줄어든 폭은 해제한 자료의 본문과 조립 오버헤드만큼이다.
    expect(both - onlyA).toBe(4000 + 200);
  });

  it("모두 체크된 기본 상태는 전체를 센다", () => {
    const files = [file("a", { char_count: 1000 }), file("b", { char_count: 4000 })];
    const total = estimateTotalChars({
      ...BASE,
      uploadedFiles: files,
      inclusion: seedInclusion(files),
      inheritedAttachments: [],
    });
    expect(total).toBe(1000 + 200 + 4000 + 200 + BASE.claimText.length + 500);
  });

  it("물려받은 자료는 체크 상태와 무관하게 그대로 센다", () => {
    const inherited: JobAttachment[] = [
      { ...file("parent", { char_count: 2000 }), required: true },
    ];
    const total = estimateTotalChars({
      ...BASE,
      uploadedFiles: [],
      inclusion: {},
      inheritedAttachments: inherited,
    });
    expect(total).toBe(2000 + 200 + BASE.claimText.length + 500);
  });
});

describe("selectedAttachmentIds", () => {
  it("체크한 첨부만 목록에 담는다", () => {
    const files = [file("a"), file("b")];
    expect(selectedAttachmentIds(files, { a: true, b: false })).toEqual(["a"]);
  });

  it("물려받은 자료의 id 도 함께 보낸다", () => {
    const files = [file("a"), file("b")];
    const inherited: JobAttachment[] = [{ ...file("parent"), required: true }];
    expect(selectedAttachmentIds(files, { a: false, b: true }, inherited)).toEqual([
      "b",
      "parent",
    ]);
  });

  it("전처리 전에는 null 이다 — 빈 목록은 '전부 제외'라는 뜻이라 보내면 안 된다", () => {
    expect(selectedAttachmentIds(null, {})).toBeNull();
  });

  it("모두 해제하면 빈 목록을 보낸다", () => {
    const files = [file("a"), file("b")];
    expect(selectedAttachmentIds(files, { a: false, b: false })).toEqual([]);
  });
});

describe("includedFiles", () => {
  it("체크 상태가 없는 첨부는 포함하지 않는다", () => {
    const files = [file("a"), file("b")];
    expect(includedFiles(files, { a: true }).map((f) => f.attachment_id)).toEqual([
      "a",
    ]);
  });
});

describe("hasAnalysisMaterial", () => {
  it("모두 해제하면 실행할 수 없다", () => {
    const files = [file("a"), file("b")];
    expect(hasAnalysisMaterial(files, { a: false, b: false })).toBe(false);
  });

  it("하나라도 체크되어 있으면 실행할 수 있다", () => {
    const files = [file("a"), file("b")];
    expect(hasAnalysisMaterial(files, { a: false, b: true })).toBe(true);
  });

  it("물려받은 자료가 있으면 새 업로드를 모두 해제해도 실행할 수 있다", () => {
    const files = [file("a")];
    const inherited: JobAttachment[] = [{ ...file("parent"), required: true }];
    expect(hasAnalysisMaterial(files, { a: false }, inherited)).toBe(true);
  });
});
