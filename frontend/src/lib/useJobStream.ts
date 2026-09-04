import { useEffect, useRef, useState } from "react";

import type { JobStatus, StreamEvent } from "./types";

const STAGE_LABEL: Record<string, string> = {
  queued: "실행 대기 중",
  preprocessing: "입력 전처리 및 프롬프트 조립",
  indexing: "인용발명 문헌 로컬 색인 중",
  executing: "Provider 실행 중",
  verifying: "결과 검증 중",
};

export interface JobStreamState {
  events: StreamEvent[];
  /** 최종 결과 도착 전까지 받은 글자 수. 본문은 완료 후 job.result_text 로 온다. */
  resultChars: number;
  status: JobStatus | null;
  stage: string;
  finished: boolean;
  errors: string[];
  /** 검색 실행에서 관측한 도구 호출 수. 진행 표시에만 쓴다. */
  searchCount: number;
  fetchCount: number;
  /** 로컬 검색 실행의 진행 상황. 색인·라운드·읽은 페이지는 ARIA 가 센다. */
  retrievalRound: number;
  retrievalPagesRead: number;
}

const EMPTY: JobStreamState = {
  events: [],
  resultChars: 0,
  status: null,
  stage: "",
  finished: false,
  errors: [],
  searchCount: 0,
  fetchCount: 0,
  retrievalRound: 0,
  retrievalPagesRead: 0,
};

export function useJobStream(jobId: string | null): JobStreamState {
  const [state, setState] = useState<JobStreamState>(EMPTY);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    sourceRef.current?.close();
    sourceRef.current = null;

    if (!jobId) {
      setState(EMPTY);
      return;
    }

    setState({ ...EMPTY, status: "QUEUED", stage: STAGE_LABEL.queued });

    const source = new EventSource(`/api/jobs/${jobId}/events`);
    sourceRef.current = source;

    source.onmessage = (message) => {
      let parsed: StreamEvent | { type: string };
      try {
        parsed = JSON.parse(message.data);
      } catch {
        return;
      }
      if (!("seq" in parsed)) {
        // stream_end
        source.close();
        setState((prev) => ({ ...prev, finished: true }));
        return;
      }

      const event = parsed as StreamEvent;
      setState((prev) => {
        const next: JobStreamState = {
          ...prev,
          events: [...prev.events, event].slice(-500),
        };
        const payload = event.payload ?? {};

        switch (event.type) {
          case "result_progress":
            // 모델 출력을 화면에 실시간으로 붙이지 않는다. 완성 전의 원문에는
            // 기계 판독 블록이 섞여 있고, 보고서 자리에는 그것을 걷어낸 최종
            // 결과만 놓는다. 여기서는 진행 정도만 받는다.
            next.resultChars = Number(payload.chars ?? prev.resultChars);
            next.stage = `결과 수신 중 (${Number(
              payload.chars ?? 0,
            ).toLocaleString()}자)`;
            break;
          case "status":
            next.status = payload.status as JobStatus;
            break;
          case "stage": {
            const key = String(payload.stage ?? "");
            next.stage = STAGE_LABEL[key] ?? String(payload.message ?? key);
            break;
          }
          case "provider_start":
            next.stage = "Provider 실행 중";
            break;
          case "analyzing":
            next.stage = String(payload.message ?? "분석 중");
            break;
          case "search_progress":
            next.searchCount = Number(payload.searches ?? prev.searchCount);
            next.fetchCount = Number(payload.fetches ?? prev.fetchCount);
            next.stage = String(payload.message ?? "검색 중");
            break;
          case "retrieval_progress":
            next.retrievalRound = Number(payload.round ?? prev.retrievalRound);
            next.retrievalPagesRead = Number(
              payload.pages_read ?? prev.retrievalPagesRead,
            );
            next.stage = String(payload.message ?? "로컬 검색 중");
            break;
          case "retrieval_ready":
            next.stage = "근거 패키지 조립 완료 — 구성대비 분석 시작";
            break;
          case "tool_budget_exceeded":
            next.stage = String(payload.message ?? "검색 호출 상한 초과");
            break;
          case "content_read_budget_exceeded":
            next.stage = String(payload.message ?? "페이지 본문 읽기 상한 초과");
            break;
          case "provider_done":
            next.stage = "결과 수신 완료";
            break;
          case "error":
            next.errors = [...prev.errors, String(payload.message ?? "")];
            break;
          case "done":
            next.finished = true;
            next.stage = "완료";
            break;
        }
        return next;
      });
    };

    source.onerror = () => {
      // 서버가 스트림을 닫으면 브라우저가 재연결을 시도한다.
      // 작업이 이미 끝났다면 재연결할 필요가 없다.
      setState((prev) => {
        if (prev.finished) source.close();
        return prev;
      });
    };

    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [jobId]);

  return state;
}
