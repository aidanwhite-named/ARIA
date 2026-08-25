/** 실행 화면 세션 캐시.
 *
 *  RunPage 는 메뉴를 옮길 때마다 언마운트된다. 실행 상태를 화면 안에만 두면
 *  결과 보고서도 함께 사라지므로, 라우터 위에 올려 두고 화면은 읽어 쓰기만
 *  한다. 진행 중인 작업의 SSE 연결도 여기서 유지하므로, 실행 중에 다른 메뉴로
 *  갔다 와도 스트림이 끊기지 않는다.
 *
 *  새로고침까지 살아남아야 하는 값만 sessionStorage 에 적는다. 보고서 본문은
 *  적지 않는다 — 작업 id 만 남기고 백엔드에서 다시 읽는 편이 용량도 정확성도
 *  낫다. 선택한 File 객체는 직렬화할 수 없으므로 메모리에만 남는다(메뉴 이동은
 *  견디고, 새로고침은 견디지 못한다).
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

import { api } from "./api";
import type {
  CitationMapping,
  Job,
  JobAttachment,
  JobKind,
  JobStatus,
  RelationType,
  UploadResponse,
} from "./types";
import { useJobStream, type JobStreamState } from "./useJobStream";

export type RunTab = "input" | "result";

/** 원본 실행에서 물려받는 것. 화면 표시와 실행 요청에 함께 쓴다. */
export type Lineage = {
  sourceJobId: string;
  sourceLabel: string;
  relationType: RelationType;
  inheritedAttachments: JobAttachment[];
  priorMapping: CitationMapping | null;
  priorClaimChars: number;
  priorReportChars: number;
};

const STORAGE_KEY = "aria.run-session.v1";
const STORAGE_DEBOUNCE_MS = 250;
const TERMINAL: JobStatus[] = ["SUCCEEDED", "FAILED", "CANCELLED"];

type Persisted = {
  jobId: string | null;
  activeTab: RunTab;
  jobKind: JobKind;
  claimText: string;
  searchClaimText: string;
  followupInstruction: string;
  lineage: Lineage | null;
};

export interface RunSession {
  job: Job | null;
  setJob: Dispatch<SetStateAction<Job | null>>;
  activeTab: RunTab;
  setActiveTab: Dispatch<SetStateAction<RunTab>>;
  /** 준비 중인 실행의 종류. 입력 화면이 여기서 갈린다. */
  jobKind: JobKind;
  setJobKind: Dispatch<SetStateAction<JobKind>>;
  claimText: string;
  setClaimText: Dispatch<SetStateAction<string>>;
  /** 검색용 청구항. 분석용 청구항과 따로 둔다 — 두 작업은 입력이 다르고,
   *  모드를 오갈 때 한쪽 입력이 다른 쪽에 덮여 쓰이면 안 된다. */
  searchClaimText: string;
  setSearchClaimText: Dispatch<SetStateAction<string>>;
  lineage: Lineage | null;
  setLineage: Dispatch<SetStateAction<Lineage | null>>;
  followupInstruction: string;
  setFollowupInstruction: Dispatch<SetStateAction<string>>;
  citationFiles: File[];
  setCitationFiles: Dispatch<SetStateAction<File[]>>;
  upload: UploadResponse | null;
  setUpload: Dispatch<SetStateAction<UploadResponse | null>>;
  /** 검색 실행에 곁들이는 출원발명 문서(명세서). 격리된 확장 검색용 자료다.
   *
   *  분석용 첨부와 상태를 나누는 이유는 searchClaimText 와 같다. 두 축은 받는
   *  자료가 다르고, 축을 오갈 때 한쪽에서 고른 파일이 다른 쪽 실행에 딸려
   *  들어가면 안 된다. */
  searchSpecFile: File | null;
  setSearchSpecFile: Dispatch<SetStateAction<File | null>>;
  searchUpload: UploadResponse | null;
  setSearchUpload: Dispatch<SetStateAction<UploadResponse | null>>;
  required: Record<string, boolean>;
  setRequired: Dispatch<SetStateAction<Record<string, boolean>>>;
  stream: JobStreamState;
  running: boolean;
  /** 새로고침 직후, 저장해 둔 작업을 백엔드에서 다시 읽는 중. */
  restoring: boolean;
}

const RunSessionContext = createContext<RunSession | null>(null);

function readStored(): Persisted | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Persisted>;
    return {
      jobId: typeof parsed.jobId === "string" ? parsed.jobId : null,
      activeTab: parsed.activeTab === "result" ? "result" : "input",
      jobKind:
        parsed.jobKind === "similarity_search"
          ? "similarity_search"
          : "patent_analysis",
      claimText: typeof parsed.claimText === "string" ? parsed.claimText : "",
      searchClaimText:
        typeof parsed.searchClaimText === "string" ? parsed.searchClaimText : "",
      followupInstruction:
        typeof parsed.followupInstruction === "string"
          ? parsed.followupInstruction
          : "",
      lineage: (parsed.lineage as Lineage | null) ?? null,
    };
  } catch {
    // 손상된 캐시로 화면을 못 열면 안 된다. 없는 셈 친다.
    return null;
  }
}

/** HashRouter 주소의 ?job= 파라미터.
 *
 *  실행 기록에서 연 작업이 있으면 RunPage 가 그 작업을 불러오므로, 캐시에
 *  남아 있던 작업을 복원하면 둘이 경쟁해 엉뚱한 보고서가 뜬다. 그럴 때는
 *  복원을 건너뛴다.
 */
function hashJobParam(): string | null {
  const hash = window.location.hash;
  const mark = hash.indexOf("?");
  if (mark < 0) return null;
  try {
    return new URLSearchParams(hash.slice(mark + 1)).get("job");
  } catch {
    return null;
  }
}

export function RunSessionProvider({ children }: { children: ReactNode }) {
  const stored = useRef<Persisted | null | undefined>(undefined);
  if (stored.current === undefined) stored.current = readStored();
  const initial = stored.current;

  const [job, setJob] = useState<Job | null>(null);
  const [activeTab, setActiveTab] = useState<RunTab>(initial?.activeTab ?? "input");
  const [jobKind, setJobKind] = useState<JobKind>(
    initial?.jobKind ?? "patent_analysis",
  );
  const [claimText, setClaimText] = useState(initial?.claimText ?? "");
  const [searchClaimText, setSearchClaimText] = useState(
    initial?.searchClaimText ?? "",
  );
  const [lineage, setLineage] = useState<Lineage | null>(initial?.lineage ?? null);
  const [followupInstruction, setFollowupInstruction] = useState(
    initial?.followupInstruction ?? "",
  );
  const [citationFiles, setCitationFiles] = useState<File[]>([]);
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [searchSpecFile, setSearchSpecFile] = useState<File | null>(null);
  const [searchUpload, setSearchUpload] = useState<UploadResponse | null>(null);
  const [required, setRequired] = useState<Record<string, boolean>>({});
  const [restoring, setRestoring] = useState(
    Boolean(initial?.jobId) && !hashJobParam(),
  );

  // 새로고침 복원. 본문은 저장하지 않았으므로 작업 id 로 다시 읽는다.
  useEffect(() => {
    const jobId = initial?.jobId;
    if (!jobId || hashJobParam()) {
      setRestoring(false);
      return;
    }
    let cancelled = false;
    api
      .getJob(jobId)
      .then((fresh) => {
        if (!cancelled) setJob(fresh);
      })
      .catch(() => {
        // 삭제됐거나 백엔드가 모르는 작업이면 캐시만 버린다.
      })
      .finally(() => {
        if (!cancelled) setRestoring(false);
      });
    return () => {
      cancelled = true;
    };
    // 최초 1회만. initial 은 ref 에서 온 고정값이다.
  }, []);

  const streamJobId = job && !TERMINAL.includes(job.status) ? job.id : null;
  const stream = useJobStream(streamJobId);

  // 실행이 끝나면 최종 상태를 다시 읽어 온다. 다른 메뉴에 있어도 돌아온다.
  useEffect(() => {
    if (!job || !stream.finished) return;
    let cancelled = false;
    api
      .getJob(job.id)
      .then((fresh) => {
        if (!cancelled) setJob(fresh);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [stream.finished, job?.id]);

  // 새로고침 대비 저장. 타이핑마다 쓰지 않도록 조금 미룬다.
  useEffect(() => {
    // 복원이 끝나기 전에 쓰면 아직 비어 있는 job 으로 작업 id 를 지워 버린다.
    if (restoring) return;
    const timer = setTimeout(() => {
      const snapshot: Persisted = {
        jobId: job?.id ?? null,
        activeTab,
        jobKind,
        claimText,
        searchClaimText,
        followupInstruction,
        lineage,
      };
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
      } catch {
        // 용량 초과 등. 캐시는 편의 기능이므로 실패해도 화면은 그대로 둔다.
      }
    }, STORAGE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [
    restoring,
    job?.id,
    activeTab,
    jobKind,
    claimText,
    searchClaimText,
    followupInstruction,
    lineage,
  ]);

  const running = Boolean(
    job && ["QUEUED", "RUNNING"].includes(job.status) && !stream.finished,
  );

  const value = useMemo<RunSession>(
    () => ({
      job,
      setJob,
      activeTab,
      setActiveTab,
      jobKind,
      setJobKind,
      claimText,
      setClaimText,
      searchClaimText,
      setSearchClaimText,
      lineage,
      setLineage,
      followupInstruction,
      setFollowupInstruction,
      citationFiles,
      setCitationFiles,
      upload,
      setUpload,
      searchSpecFile,
      setSearchSpecFile,
      searchUpload,
      setSearchUpload,
      required,
      setRequired,
      stream,
      running,
      restoring,
    }),
    [
      job,
      activeTab,
      jobKind,
      claimText,
      searchClaimText,
      lineage,
      followupInstruction,
      citationFiles,
      upload,
      searchSpecFile,
      searchUpload,
      required,
      stream,
      running,
      restoring,
    ],
  );

  return (
    <RunSessionContext.Provider value={value}>
      {children}
    </RunSessionContext.Provider>
  );
}

export function useRunSession(): RunSession {
  const value = useContext(RunSessionContext);
  if (!value) {
    throw new Error("useRunSession 은 RunSessionProvider 안에서만 쓸 수 있습니다.");
  }
  return value;
}
