/** 「분석에 포함」 선택이 화면 추정치와 요청 목록에 그대로 반영되는지 고정한다.
 *
 *  이 세 값(체크 상태 · 화면 글자 수 · 요청에 실리는 첨부 목록)이 갈라지면
 *  체크를 풀어도 본문이 그대로 실행에 들어간다. 그 어긋남은 실행이 끝난 뒤에야
 *  드러나므로 여기서 못박는다.
 *
 *  PDF 개수는 사용자가 정한다. 그래서 이 파일의 검사는 특정 개수나 특정 자리에
 *  기대지 않고, 여러 N 에 대해 N 에서 계산한 부분집합으로 돌린다.
 */

import { describe, expect, it } from "vitest";

import {
  carryInclusion,
  estimateTotalChars,
  hasAnalysisMaterial,
  includedFiles,
  selectedAttachmentIds,
  type InclusionMap,
} from "./attachmentSelection";
import type { AttachmentAnalysis, JobAttachment } from "./types";

/** 업로드 응답 한 줄. batch 를 다시 올리면 attachment_id 만 새로 발급된다. */
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

/** 인용발명 PDF N 건을 올린 결과. `batch` 는 재업로드를 구분하는 표식이다. */
function upload(count: number, batch = "b1"): AttachmentAnalysis[] {
  return Array.from({ length: count }, (_, i) =>
    file(`${batch}-doc${i}`, {
      // 같은 파일을 다시 올리면 id 는 바뀌고 내용 해시는 그대로다.
      sha256: `sha-doc${i}`,
      original_filename: `doc${i}.pdf`,
      char_count: 1000 * (i + 1),
    }),
  );
}

/** 자리 목록으로 체크 상태를 만든다. 개수도 자리도 호출부가 N 에서 계산한다. */
function pick(files: AttachmentAnalysis[], positions: number[]): InclusionMap {
  const chosen = new Set(positions);
  const map: InclusionMap = {};
  files.forEach((f, i) => {
    map[f.attachment_id] = chosen.has(i);
  });
  return map;
}

/** 앞 절반 / 뒤 절반. N 이 홀수면 겹치는 부분집합이 되어 그 경우도 함께 본다. */
const firstHalf = (n: number) =>
  Array.from({ length: Math.ceil(n / 2) }, (_, i) => i);
const lastHalf = (n: number) =>
  Array.from({ length: Math.ceil(n / 2) }, (_, i) => n - 1 - i).reverse();

const SIZES = [1, 2, 3, 6, 9];

const BASE = {
  claimText: "청구항 1. 테스트 청구항",
  followupInstruction: "",
  priorClaimChars: 0,
  priorReportChars: 0,
  promptBodyChars: 500,
};

describe("carryInclusion", () => {
  it("첫 업로드에서는 정상 처리된 PDF 만 체크한다", () => {
    const files = [
      file("a"),
      file("b", { read_ok: false, included: false, error: "스캔본" }),
    ];
    expect(carryInclusion([], {}, files)).toEqual({ a: true, b: false });
  });

  it.each(SIZES)(
    "PDF %i건: 같은 자료를 새 batch 로 다시 올려도 고른 부분집합이 그대로 따라온다",
    (n) => {
      const first = upload(n, "b1");
      const chosen = pick(first, lastHalf(n));

      // 두 번째 실행을 위해 같은 파일을 다시 올린다 — id 는 전부 바뀐다.
      const second = upload(n, "b2");
      const carried = carryInclusion(first, chosen, second);

      expect(Object.keys(carried)).toEqual(second.map((f) => f.attachment_id));
      // 새 id 로 옮겨졌지만 고른 자리는 같다.
      expect(second.filter((f) => carried[f.attachment_id]).map((f) => f.sha256)).toEqual(
        first.filter((f) => chosen[f.attachment_id]).map((f) => f.sha256),
      );
    },
  );

  it.each(SIZES)("PDF %i건: 선택을 다른 부분집합으로 바꿔도 그대로 따라온다", (n) => {
    const first = upload(n, "b1");
    const second = upload(n, "b2");

    const a = carryInclusion(first, pick(first, firstHalf(n)), second);
    const b = carryInclusion(first, pick(first, lastHalf(n)), second);

    const namesOf = (map: InclusionMap) =>
      second.filter((f) => map[f.attachment_id]).map((f) => f.original_filename);
    expect(namesOf(a)).toEqual(firstHalf(n).map((i) => `doc${i}.pdf`));
    expect(namesOf(b)).toEqual(lastHalf(n).map((i) => `doc${i}.pdf`));
  });

  it("이전 업로드에 없던 자료는 서버가 준 초기값을 따른다", () => {
    const first = upload(1, "b1");
    const second = [
      ...upload(1, "b2"),
      file("new-ok"),
      file("new-bad", { read_ok: false, included: false }),
    ];
    const carried = carryInclusion(first, pick(first, []), second);

    expect(carried["b2-doc0"]).toBe(false);
    expect(carried["new-ok"]).toBe(true);
    expect(carried["new-bad"]).toBe(false);
  });

  it("같은 문서를 두 번 올렸어도 각 자리의 선택을 따로 유지한다", () => {
    const first = [
      file("a1", { sha256: "same" }),
      file("a2", { sha256: "same" }),
    ];
    const second = [
      file("b1", { sha256: "same" }),
      file("b2", { sha256: "same" }),
    ];
    const carried = carryInclusion(first, { a1: false, a2: true }, second);
    expect(carried).toEqual({ b1: false, b2: true });
  });
});

describe("estimateTotalChars", () => {
  it.each(SIZES)("PDF %i건: 해제한 자료의 본문만큼 글자 수가 줄어든다", (n) => {
    const files = upload(n);
    const all = pick(
      files,
      files.map((_, i) => i),
    );
    const subset = pick(files, firstHalf(n));

    const withAll = estimateTotalChars({
      ...BASE,
      uploadedFiles: files,
      inclusion: all,
      inheritedAttachments: [],
    });
    const withSubset = estimateTotalChars({
      ...BASE,
      uploadedFiles: files,
      inclusion: subset,
      inheritedAttachments: [],
    });

    const droppedChars = files
      .filter((f) => !subset[f.attachment_id])
      .reduce((sum, f) => sum + f.char_count + 200, 0);
    expect(withAll - withSubset).toBe(droppedChars);
    // 전부 체크한 기본 상태는 모든 본문을 센다.
    expect(withAll).toBe(
      files.reduce((sum, f) => sum + f.char_count + 200, 0) +
        BASE.claimText.length +
        BASE.promptBodyChars,
    );
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
    expect(total).toBe(2000 + 200 + BASE.claimText.length + BASE.promptBodyChars);
  });
});

describe("selectedAttachmentIds", () => {
  it.each(SIZES)("PDF %i건: 체크한 자료만 목록에 담는다", (n) => {
    const files = upload(n);
    const positions = lastHalf(n);
    expect(selectedAttachmentIds(files, pick(files, positions))).toEqual(
      positions.map((i) => files[i].attachment_id),
    );
  });

  it("물려받은 자료의 id 도 함께 보낸다", () => {
    const files = upload(2);
    const inherited: JobAttachment[] = [{ ...file("parent"), required: true }];
    expect(
      selectedAttachmentIds(files, pick(files, [1]), inherited),
    ).toEqual(["b1-doc1", "parent"]);
  });

  it("전처리 전에는 null 이다 — 빈 목록은 '전부 제외'라는 뜻이라 보내면 안 된다", () => {
    expect(selectedAttachmentIds(null, {})).toBeNull();
  });

  it("모두 해제하면 빈 목록을 보낸다", () => {
    const files = upload(3);
    expect(selectedAttachmentIds(files, pick(files, []))).toEqual([]);
  });
});

describe("includedFiles", () => {
  it("체크 상태가 없는 첨부는 포함하지 않는다", () => {
    const files = upload(2);
    expect(includedFiles(files, { "b1-doc0": true }).map((f) => f.attachment_id)).toEqual([
      "b1-doc0",
    ]);
  });
});

describe("hasAnalysisMaterial", () => {
  it.each(SIZES)("PDF %i건: 모두 해제하면 실행할 수 없다", (n) => {
    const files = upload(n);
    expect(hasAnalysisMaterial(files, pick(files, []))).toBe(false);
    expect(hasAnalysisMaterial(files, pick(files, [n - 1]))).toBe(true);
  });

  it("물려받은 자료가 있으면 새 업로드를 모두 해제해도 실행할 수 있다", () => {
    const files = upload(2);
    const inherited: JobAttachment[] = [{ ...file("parent"), required: true }];
    expect(hasAnalysisMaterial(files, pick(files, []), inherited)).toBe(true);
  });
});
