/** 「분석에 포함」 선택 상태를 다루는 순수 함수들.
 *
 *  화면이 보여주는 예상 글자 수와, preflight·실행 요청에 실어 보내는 첨부
 *  목록은 반드시 같은 선택에서 나와야 한다. 세 곳이 각자 계산하면 체크를
 *  풀었는데 글자 수만 줄고 실제 실행에는 그대로 들어가는 어긋남이 생긴다.
 *  그래서 그 계산을 이 파일 하나에 모으고, RunPage 는 여기를 부르기만 한다.
 *
 *  백엔드의 같은 계약은 prompt_assembly.included_attachments 다.
 */

import type { AttachmentAnalysis, JobAttachment } from "./types";

/** 첨부 id → 「분석에 포함」 체크 여부. */
export type InclusionMap = Record<string, boolean>;

/** 첨부 하나가 붙는 조립 오버헤드(헤더·경계 표시) 추정치.
 *
 *  백엔드 prompt_assembly.estimate_total_chars 와 같은 값을 쓴다. 정확한 크기는
 *  preflight 가 돌려주므로 이 값은 preflight 응답이 오기 전의 임시 표시용이다. */
const ATTACHMENT_OVERHEAD_CHARS = 200;

/** 업로드 직후의 초기 체크 상태.
 *
 *  서버가 응답에 실어 준 included 를 그대로 쓴다(= 정상 처리된 자료만 체크).
 *  화면이 read_ok 를 다시 해석하지 않는다 — 판단 지점이 둘이면 갈라진다. */
export function seedInclusion(files: AttachmentAnalysis[]): InclusionMap {
  const map: InclusionMap = {};
  for (const file of files) map[file.attachment_id] = file.included;
  return map;
}

/** 체크된 첨부만. 체크 상태가 없는 첨부는 포함하지 않는다. */
export function includedFiles<T extends { attachment_id: string }>(
  files: T[],
  inclusion: InclusionMap,
): T[] {
  return files.filter((file) => inclusion[file.attachment_id] === true);
}

/** 요청에 실어 보낼 첨부 id 목록.
 *
 *  아직 업로드를 마치지 않았으면 null 을 돌려준다 — 서버는 null 을 "저장된
 *  포함 여부를 그대로 쓰라"로 읽으므로, 고를 것이 없는 상태에서 빈 목록을
 *  보내 "전부 제외"로 오해받지 않는다.
 *
 *  물려받은 자료의 id 도 함께 보낸다. 목록을 보내는 순간 그 목록에 없는 첨부는
 *  전부 제외되므로, 빠뜨리면 이전 실행에서 물려받은 자료가 조용히 사라진다.
 *
 *  inheritedAttachments 는 원본 실행에서 이미 포함이었던 자료만 담겨 있어야
 *  한다(RunPage 의 startFollowUp 이 걸러 넣는다). 원본에서 뺀 자료를 여기 넣으면
 *  후속 실행에서 되살아난다. */
export function selectedAttachmentIds(
  uploadedFiles: AttachmentAnalysis[] | null,
  inclusion: InclusionMap,
  inheritedAttachments: JobAttachment[] = [],
): string[] | null {
  if (!uploadedFiles) return null;
  return [
    ...includedFiles(uploadedFiles, inclusion).map((file) => file.attachment_id),
    ...inheritedAttachments.map((file) => file.attachment_id),
  ];
}

/** 화면에 표시할 예상 입력 크기.
 *
 *  preflight 응답이 오기 전까지만 쓰는 추정치다. 체크를 푼 자료는 여기서도
 *  빠져야, 체크를 바꾼 직후(응답을 기다리는 동안)에도 숫자가 거꾸로 움직이지
 *  않는다. */
export function estimateTotalChars(input: {
  uploadedFiles: AttachmentAnalysis[] | null;
  inclusion: InclusionMap;
  inheritedAttachments: JobAttachment[];
  claimText: string;
  followupInstruction: string;
  priorClaimChars: number;
  priorReportChars: number;
  promptBodyChars: number;
}): number {
  const attachmentChars = (files: { read_ok: boolean; char_count: number }[]) =>
    files.reduce(
      (sum, file) =>
        sum + (file.read_ok ? file.char_count + ATTACHMENT_OVERHEAD_CHARS : 0),
      0,
    );

  return (
    attachmentChars(includedFiles(input.uploadedFiles ?? [], input.inclusion)) +
    // 물려받는 첨부도 자식 실행의 프롬프트에 그대로 다시 들어간다.
    attachmentChars(input.inheritedAttachments) +
    input.claimText.length +
    input.followupInstruction.length +
    input.priorClaimChars +
    input.priorReportChars +
    input.promptBodyChars
  );
}

/** 구성대비 분석을 시작할 수 있는가.
 *
 *  업로드를 마친 뒤에는 체크된 자료가 최소 1건 있어야 한다. 백엔드도 같은
 *  이유로 거절하지만(job_assembly.NO_INCLUDED_MATERIAL), 화면이 먼저 말해 주면
 *  왕복 한 번을 아낀다. */
export function hasAnalysisMaterial(
  uploadedFiles: AttachmentAnalysis[] | null,
  inclusion: InclusionMap,
  inheritedAttachments: JobAttachment[] = [],
): boolean {
  if (inheritedAttachments.length > 0) return true;
  if (!uploadedFiles) return false;
  return includedFiles(uploadedFiles, inclusion).length > 0;
}
