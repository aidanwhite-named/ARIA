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
  useCallback,
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
import type { InclusionMap } from "./attachmentSelection";
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

/** 축마다 자기 실행 결과를 갖는다.
 *
 *  한 칸을 두 축이 나눠 쓰면, 검색을 돌린 뒤 구성대비 분석을 돌렸을 때 검색의
 *  「결과 보기」에 분석 보고서가 뜬다. claimText/searchClaimText 와
 *  upload/searchUpload 를 나눈 것과 같은 이유다 — 두 축은 다른 작업이고, 축을
 *  오갈 때 한쪽 결과가 다른 쪽을 덮으면 안 된다.
 */
export type JobSlots = Record<JobKind, Job | null>;

const EMPTY_SLOTS: JobSlots = { patent_analysis: null, similarity_search: null };

/** 보고 있던 탭도 축마다 따로 기억한다.
 *
 *  한 칸을 나눠 쓰면, 분석 보고서를 보다가 검색으로 건너갔을 때 검색이 「결과
 *  보기」로 열린다 — 아직 아무것도 돌리지 않았는데 빈 결과 화면이 뜬다. 두 축은
 *  서로의 진행 상태를 건드리지 않는다. */
type TabSlots = Record<JobKind, RunTab>;

const EMPTY_TABS: TabSlots = { patent_analysis: "input", similarity_search: "input" };

const KINDS: JobKind[] = ["patent_analysis", "similarity_search"];

type Persisted = {
  jobIds: Partial<Record<JobKind, string | null>>;
  activeTabs: Partial<Record<JobKind, RunTab>>;
  jobKind: JobKind;
  claimText: string;
  searchClaimText: string;
  searchCutoffDate: string;
  followupInstruction: string;
  lineage: Lineage | null;
};

export interface RunSession {
  /** 지금 보고 있는 축의 실행. 축을 바꾸면 그 축의 실행으로 바뀐다. */
  job: Job | null;
  setJob: Dispatch<SetStateAction<Job | null>>;
  /** 두 축의 실행 전부. 상단 전환기가 "다른 축은 지금 무엇을 하고 있는지"를
   *  이 값으로 적는다 — 두 축이 동시에 살아 있다는 것을 보여 주는 자리다. */
  jobs: JobSlots;
  /** 지금 보고 있는 축의 탭. */
  activeTab: RunTab;
  /** 탭을 바꾼다. kind 를 주면 그 축의 탭을 바꾼다 — 축을 옮기면서 탭까지
   *  지정할 때(미대응 구성 검색) 필요하다. 생략하면 보고 있는 축이다. */
  setActiveTab: (tab: RunTab, kind?: JobKind) => void;
  /** 준비 중인 실행의 종류. 입력 화면이 여기서 갈린다. */
  jobKind: JobKind;
  setJobKind: Dispatch<SetStateAction<JobKind>>;
  claimText: string;
  setClaimText: Dispatch<SetStateAction<string>>;
  /** 검색용 청구항. 분석용 청구항과 따로 둔다 — 두 작업은 입력이 다르고,
   *  모드를 오갈 때 한쪽 입력이 다른 쪽에 덮여 쓰이면 안 된다. */
  searchClaimText: string;
  setSearchClaimText: Dispatch<SetStateAction<string>>;
  /** 선택적 검색 기준일(YYYY-MM-DD). 빈 문자열이 기본값이고 그것은 **날짜
   *  조건 없음**을 뜻한다. 비었다고 오늘 날짜를 채우지 않는다 — 채우면 같은
   *  청구항의 검색 범위가 실행한 날에 따라 달라진다. */
  searchCutoffDate: string;
  setSearchCutoffDate: Dispatch<SetStateAction<string>>;
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
  /** 첨부 id → 「분석에 포함」 체크 여부.
   *
   *  `required`(자료를 못 읽으면 실행을 실패시킬 것인가)와 다른 축이다. 이 값은
   *  "애초에 이 실행의 분석 자료로 쓸 것인가"이며, 화면 추정치·preflight·실제
   *  실행이 모두 이 선택 하나를 따른다. */
  included: InclusionMap;
  setIncluded: Dispatch<SetStateAction<InclusionMap>>;
  /** 이미 작업 하나에 귀속된 업로드 batch. 업로드는 작업 하나에만 쓸 수 있으므로,
   *  같은 자료로 한 번 더 돌릴 때는 이 값과 비교해 새 batch 로 다시 올린다.
   *
   *  실행을 마쳐도 upload/included 를 지우지 않기 때문에 필요하다 — 화면에는
   *  전처리 결과와 체크 상태가 남아 있어야 사용자가 선택만 바꿔 곧바로 다시
   *  돌릴 수 있고, 그 남아 있는 batch 가 이미 쓴 것인지는 여기서만 알 수 있다. */
  spentBatchId: string | null;
  setSpentBatchId: Dispatch<SetStateAction<string | null>>;
  stream: JobStreamState;
  /** 지금 보고 있는 축의 실행이 진행 중인가. 이 화면의 spinner·비활성화용. */
  running: boolean;
  /** 어느 축이든 실행이 진행 중인가.
   *
   *  스트림은 하나뿐이므로 동시에 두 작업을 띄우면 한쪽은 끝나도 화면이
   *  갱신되지 않는다. 새 실행을 막는 판단은 보이는 축이 아니라 이 값으로 한다. */
  busy: boolean;
  /** 새로고침 직후, 저장해 둔 작업을 백엔드에서 다시 읽는 중. */
  restoring: boolean;
}

const RunSessionContext = createContext<RunSession | null>(null);

function readStored(): Persisted | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Persisted> & { jobId?: unknown };
    const kind: JobKind =
      parsed.jobKind === "similarity_search"
        ? "similarity_search"
        : "patent_analysis";
    const jobIds: Partial<Record<JobKind, string | null>> = {};
    if (parsed.jobIds && typeof parsed.jobIds === "object") {
      for (const slot of KINDS) {
        const value = parsed.jobIds[slot];
        if (typeof value === "string") jobIds[slot] = value;
      }
    } else if (typeof parsed.jobId === "string") {
      // 축을 나누기 전에 저장된 캐시. 그때 보고 있던 축의 실행으로 읽는다.
      jobIds[kind] = parsed.jobId;
    }
    const activeTabs: Partial<Record<JobKind, RunTab>> = {};
    if (parsed.activeTabs && typeof parsed.activeTabs === "object") {
      for (const slot of KINDS) {
        if (parsed.activeTabs[slot] === "result") activeTabs[slot] = "result";
      }
    } else if ((parsed as { activeTab?: unknown }).activeTab === "result") {
      // 탭을 축마다 나누기 전에 저장된 캐시. 그때 보고 있던 축에만 얹는다.
      activeTabs[kind] = "result";
    }
    return {
      jobIds,
      activeTabs,
      jobKind:
        kind === "similarity_search"
          ? "similarity_search"
          : "patent_analysis",
      claimText: typeof parsed.claimText === "string" ? parsed.claimText : "",
      searchClaimText:
        typeof parsed.searchClaimText === "string" ? parsed.searchClaimText : "",
      searchCutoffDate:
        typeof parsed.searchCutoffDate === "string"
          ? parsed.searchCutoffDate
          : "",
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

  const [jobs, setJobs] = useState<JobSlots>(EMPTY_SLOTS);
  const [tabs, setTabs] = useState<TabSlots>(() => ({
    ...EMPTY_TABS,
    ...(initial?.activeTabs ?? {}),
  }));
  const [jobKind, setJobKind] = useState<JobKind>(
    initial?.jobKind ?? "patent_analysis",
  );
  // setJob 은 상태 갱신 안에서 지금 보고 있는 축을 알아야 한다. jobKind 를 직접
  // 닫아 두면 축을 바꾼 직후의 갱신이 옛 축으로 들어간다.
  const kindRef = useRef(jobKind);
  kindRef.current = jobKind;

  const job = jobs[jobKind];
  const setJob = useCallback<Dispatch<SetStateAction<Job | null>>>((action) => {
    setJobs((prev) => {
      const kind = kindRef.current;
      const next =
        typeof action === "function"
          ? (action as (current: Job | null) => Job | null)(prev[kind])
          : action;
      // 작업 자신이 어느 축인지 알고 있다. 실행 기록에서 다른 축의 작업을 열어도
      // 그 보고서가 지금 보고 있는 축의 칸을 덮지 않는다.
      const slot = next?.job_kind ?? kind;
      return { ...prev, [slot]: next };
    });
  }, []);
  const activeTab = tabs[jobKind];
  const setActiveTab = useCallback((tab: RunTab, kind?: JobKind) => {
    // kind 를 주지 않으면 지금 보고 있는 축이다. jobKind 를 직접 닫아 두면 축을
    // 바꾼 직후의 호출이 옛 축의 탭을 건드린다.
    setTabs((prev) => ({ ...prev, [kind ?? kindRef.current]: tab }));
  }, []);
  const [claimText, setClaimText] = useState(initial?.claimText ?? "");
  const [searchClaimText, setSearchClaimText] = useState(
    initial?.searchClaimText ?? "",
  );
  const [searchCutoffDate, setSearchCutoffDate] = useState(
    initial?.searchCutoffDate ?? "",
  );
  const [lineage, setLineage] = useState<Lineage | null>(initial?.lineage ?? null);
  const [followupInstruction, setFollowupInstruction] = useState(
    initial?.followupInstruction ?? "",
  );
  const [citationFiles, setCitationFiles] = useState<File[]>([]);
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [searchSpecFile, setSearchSpecFile] = useState<File | null>(null);
  const [searchUpload, setSearchUpload] = useState<UploadResponse | null>(null);
  const [included, setIncluded] = useState<InclusionMap>({});
  const [spentBatchId, setSpentBatchId] = useState<string | null>(null);
  const storedIds = Object.values(initial?.jobIds ?? {}).filter(Boolean);
  const [restoring, setRestoring] = useState(
    storedIds.length > 0 && !hashJobParam(),
  );

  // 새로고침 복원. 본문은 저장하지 않았으므로 작업 id 로 다시 읽는다.
  useEffect(() => {
    const entries = Object.entries(initial?.jobIds ?? {}).filter(
      ([, id]) => typeof id === "string" && id,
    ) as [JobKind, string][];
    if (entries.length === 0 || hashJobParam()) {
      setRestoring(false);
      return;
    }
    let cancelled = false;
    Promise.all(
      entries.map(([slot, id]) =>
        api
          .getJob(id)
          .then((fresh) => [slot, fresh] as const)
          // 삭제됐거나 백엔드가 모르는 작업이면 그 칸만 비운다.
          .catch(() => null),
      ),
    )
      .then((results) => {
        if (cancelled) return;
        setJobs((prev) => {
          const next = { ...prev };
          for (const row of results) {
            if (row) next[row[0]] = row[1];
          }
          return next;
        });
      })
      .finally(() => {
        if (!cancelled) setRestoring(false);
      });
    return () => {
      cancelled = true;
    };
    // 최초 1회만. initial 은 ref 에서 온 고정값이다.
  }, []);

  // 스트림은 하나뿐이므로 실행 중인 작업을 따라간다. 보고 있는 축을 먼저 보되,
  // 다른 축에서 돌던 작업이 있으면 그쪽을 놓지 않는다 — 축을 바꿨다고 진행 중인
  // 실행의 연결이 끊기면 그 작업은 끝나도 화면이 갱신되지 않는다.
  const streamJob =
    [jobs[jobKind], jobs[jobKind === "patent_analysis" ? "similarity_search" : "patent_analysis"]].find(
      (candidate) => candidate && !TERMINAL.includes(candidate.status),
    ) ?? null;
  const streamJobId = streamJob?.id ?? null;
  const stream = useJobStream(streamJobId);

  // 실행이 끝나면 최종 상태를 다시 읽어 온다. 다른 메뉴에 있어도 돌아온다.
  useEffect(() => {
    if (!streamJobId || !stream.finished) return;
    let cancelled = false;
    api
      .getJob(streamJobId)
      .then((fresh) => {
        // 끝난 작업 자신의 축에 넣는다. 그 사이 사용자가 축을 바꿨어도 결과가
        // 엉뚱한 칸으로 가지 않는다.
        if (!cancelled) setJobs((prev) => ({ ...prev, [fresh.job_kind]: fresh }));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [stream.finished, streamJobId]);

  // 새로고침 대비 저장. 타이핑마다 쓰지 않도록 조금 미룬다.
  useEffect(() => {
    // 복원이 끝나기 전에 쓰면 아직 비어 있는 job 으로 작업 id 를 지워 버린다.
    if (restoring) return;
    const timer = setTimeout(() => {
      const snapshot: Persisted = {
        jobIds: {
          patent_analysis: jobs.patent_analysis?.id ?? null,
          similarity_search: jobs.similarity_search?.id ?? null,
        },
        activeTabs: tabs,
        jobKind,
        claimText,
        searchClaimText,
        searchCutoffDate,
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
    jobs.patent_analysis?.id,
    jobs.similarity_search?.id,
    tabs,
    jobKind,
    claimText,
    searchClaimText,
    searchCutoffDate,
    followupInstruction,
    lineage,
  ]);

  const busy = Boolean(
    streamJob &&
      ["QUEUED", "RUNNING"].includes(streamJob.status) &&
      !stream.finished,
  );
  // 보이는 축의 실행만 이 화면의 진행 표시다. 다른 축에서 도는 작업 때문에
  // 이 축의 「결과 보기」에 spinner 가 도는 것이 축을 나눈 취지에 어긋난다.
  const running = busy && streamJob?.id === job?.id;

  const value = useMemo<RunSession>(
    () => ({
      job,
      setJob,
      jobs,
      activeTab,
      setActiveTab,
      jobKind,
      setJobKind,
      claimText,
      setClaimText,
      searchClaimText,
      setSearchClaimText,
      searchCutoffDate,
      setSearchCutoffDate,
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
      included,
      setIncluded,
      spentBatchId,
      setSpentBatchId,
      stream,
      running,
      busy,
      restoring,
    }),
    [
      job,
      setJob,
      jobs,
      activeTab,
      setActiveTab,
      jobKind,
      claimText,
      searchClaimText,
      searchCutoffDate,
      lineage,
      followupInstruction,
      citationFiles,
      upload,
      searchSpecFile,
      searchUpload,
      included,
      spentBatchId,
      stream,
      running,
      busy,
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
