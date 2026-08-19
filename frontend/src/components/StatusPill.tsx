import type { JobStatus, ResultQuality } from "../lib/types";

const STATUS_LABEL: Record<JobStatus, string> = {
  QUEUED: "대기 중",
  RUNNING: "실행 중",
  SUCCEEDED: "성공",
  FAILED: "실패",
  CANCELLED: "취소됨",
};

export const ERROR_LABEL: Record<string, string> = {
  AUTH_REQUIRED: "로그인 필요",
  RATE_LIMITED: "사용량 제한",
  PROVIDER_UNAVAILABLE: "Provider 사용 불가",
  INPUT_TOO_LARGE: "입력 크기 초과",
  TIMED_OUT: "시간 초과",
  INVALID_OUTPUT: "출력 형식 오류",
  EMPTY_RESULT: "결과 없음",
  PROCESS_ERROR: "실행 오류",
  ATTACHMENT_ERROR: "첨부 전달 실패",
  CANCELLED: "취소됨",
};

interface Props {
  status: JobStatus;
  quality?: ResultQuality | null;
  errorCode?: string | null;
}

export default function StatusPill({ status, quality, errorCode }: Props) {
  if (status === "SUCCEEDED" && quality === "SUCCESS_WITH_WARNINGS") {
    return <span className="pill warn">경고 동반 성공</span>;
  }
  if (status === "SUCCEEDED") return <span className="pill ok">성공</span>;
  if (status === "FAILED") {
    const detail = errorCode ? ERROR_LABEL[errorCode] ?? errorCode : null;
    return (
      <span className="pill danger">실패{detail ? ` · ${detail}` : ""}</span>
    );
  }
  if (status === "RUNNING") {
    return (
      <span className="pill accent">
        <span className="spinner" /> 실행 중
      </span>
    );
  }
  if (status === "CANCELLED") return <span className="pill neutral">취소됨</span>;
  return <span className="pill neutral">{STATUS_LABEL[status] ?? status}</span>;
}
