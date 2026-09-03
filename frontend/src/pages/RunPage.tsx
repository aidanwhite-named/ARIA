import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import GapSearchPanel from "../components/GapSearchPanel";
import AnalysisDegreeOverview from "../components/AnalysisDegreeOverview";
import ResultView from "../components/ResultView";
import DeliverySummary from "../components/DeliverySummary";
import RetrievalManifestView from "../components/RetrievalManifestView";
import SearchManifestView from "../components/SearchManifestView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import {
  carryInclusion,
  estimateTotalChars,
  hasAnalysisMaterial,
  selectedAttachmentIds,
} from "../lib/attachmentSelection";
import { useRunSession } from "../lib/runSession";
import { DELIVERY_LABEL, isNarrowed } from "../lib/types";
import { WORKSPACE_BY_ID, workspacePath } from "../lib/workspaces";
import type {
  AttachmentAnalysis,
  AttachmentRole,
  Job,
  JobKind,
  Preflight,
  Prompt,
  ProviderInfo,
  RelationType,
} from "../lib/types";

const PROVIDER_IDS = new Set(["agy", "claude", "codex"]);

/** 백엔드 job_assembly.NO_INCLUDED_MATERIAL 과 같은 문구. 화면이 먼저 막을
 *  때와 서버가 거절할 때가 같은 말을 해야 한다. */
const NO_INCLUDED_MATERIAL =
  "분석에 포함할 인용발명 문헌이 하나도 없습니다. 「분석에 포함」을 체크한 PDF 가 최소 1건 있어야 구성대비 분석을 실행할 수 있습니다.";

const JOB_KIND_LABEL: Record<JobKind, string> = {
  patent_analysis: "특허 구성대비 분석",
  similarity_search: "유사 특허 · 논문 검색",
};

/** 검색 실행 지원 여부. 통제 방식은 search_tool_control 로 따로 표시한다. */
function supportsSearch(provider: ProviderInfo | null): boolean {
  return provider?.capabilities?.web_search === true;
}

const RELATION_LABEL: Record<RelationType, string> = {
  MAPPED: "종속항 추가 분석",
  CONTINUED: "보고서 수정·보완",
  REANALYZED: "같은 자료로 재분석",
};

const RELATION_TITLE: Record<RelationType, string> = {
  MAPPED: "종속항 추가 분석 — 인용발명 번호 유지",
  CONTINUED: "보고서 수정·보완 — 이전 보고서까지 전달",
  REANALYZED: "같은 자료로 재분석 — 번호도 이전 판단도 물려받지 않음",
};

function jobLabel(job: Job): string {
  const stamp = new Date(job.created_at).toLocaleString();
  return `${stamp} · ${job.prompt_name}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function deliveryLabel(file: AttachmentAnalysis): { text: string; cls: string } {
  if (file.delivery_mode === "DELIVERED_AS_INLINE_CONTEXT") {
    return { text: "본문 인라인 전달", cls: "ok" };
  }
  return { text: "전달 불가", cls: "danger" };
}

function roleLabel(role: AttachmentRole): string {
  if (role === "APPLICATION") return "출원발명";
  if (role === "CITATION") return "인용발명";
  return "기타 자료";
}

/** 실행 전 크기 안내.
 *
 *  숫자는 백엔드가 runner 와 같은 조립 함수로 잰 값이다. 화면이 따로 추정하면
 *  안내한 숫자와 실행이 막히는 지점이 어긋나고, 그 어긋남은 실행이 실패한
 *  뒤에야 드러난다. 아직 못 받았을 때만 화면 추정치를 임시로 보여 준다.
 */
function SizeNotice({
  preflight,
  totalChars,
  budget,
  overBudget,
}: {
  preflight: Preflight | null;
  totalChars: number;
  /** 환경설정의 글자 수 한도. null = 제한 없음, undefined = 아직 못 읽음. */
  budget: number | null | undefined;
  overBudget: boolean;
}) {
  if (!preflight) {
    return (
      <div
        className={`notice ${overBudget ? "danger" : "info"}`}
        style={{ marginTop: 12 }}
      >
        화면 추정 입력 크기 {totalChars.toLocaleString()}자
        {budget === undefined
          ? " / 설정 확인 중"
          : budget === null
            ? ""
            : ` / 설정한 한도 ${budget.toLocaleString()}자`}
        {" — 최종 크기를 확인하는 중입니다."}
      </div>
    );
  }
  if (preflight.error) {
    return (
      <div className="notice danger" style={{ marginTop: 12 }}>
        {preflight.error}
      </div>
    );
  }
  const lanes = preflight.lanes.length > 1 ? preflight.lanes : [];
  const narrowed = isNarrowed(preflight.delivery_plan);
  const retrieval = preflight.delivery_plan === "local_retrieval";
  return (
    <div
      className={`notice ${preflight.blocked ? "danger" : "info"}`}
      style={{ marginTop: 12 }}
    >
      <div>
        <span className={`pill ${narrowed ? "accent" : "neutral"}`}>
          {DELIVERY_LABEL[preflight.delivery_plan]}
        </span>{" "}
        최종 프롬프트 {narrowed ? "최대 " : ""}
        {preflight.chars.toLocaleString()}자 ·{" "}
        {preflight.bytes.toLocaleString()} bytes
      </div>
      {/* 왜 이 폭인지는 판정부가 만든 문장을 그대로 쓴다. 화면이 문장을 새로
          지으면 실행 기록과 다른 설명이 생긴다. */}
      {narrowed && preflight.selection_reason && (
        <div className="faint">{preflight.selection_reason}</div>
      )}
      {narrowed && (
        <div className="faint">
          인용발명 전체를 넣으면 {preflight.full_inline_bytes.toLocaleString()}{" "}
          bytes
          {retrieval && (
            <>
              {" · 근거 패키지 예산 "}
              {(preflight.evidence_budget_chars ?? 0).toLocaleString()}자
            </>
          )}
          {" — 위 숫자는 예산을 모두 썼을 때의 최댓값이고 실제 실행은 이보다 작습니다."}
        </div>
      )}
      <div className="faint">
        {preflight.byte_budget !== null
          ? `${preflight.provider} 가 자료 전체를 손실 없이 전달할 수 있는 한도 ` +
            `${preflight.byte_budget.toLocaleString()} bytes`
          : `${preflight.provider || "이 Provider"} 는 전송 한도를 선언하지 않았습니다`}
        {preflight.char_budget !== null && (
          <>
            {" · "}
            환경설정에서 걸어 둔 글자 수 한도{" "}
            {preflight.char_budget.toLocaleString()}자
          </>
        )}
      </div>
      {lanes.length > 0 && (
        <div className="faint">
          독립 실행마다 따로 걸립니다 —{" "}
          {lanes
            .map(
              (lane) =>
                `${LANE_LABEL[lane.id] ?? lane.id} ${lane.bytes.toLocaleString()} bytes`,
            )
            .join(" · ")}
        </div>
      )}
      {preflight.message && <div style={{ marginTop: 6 }}>{preflight.message}</div>}
      {!preflight.blocked && !narrowed && (
        <div className="faint">
          ARIA 는 내용을 임의로 자르거나 요약하지 않습니다. 한도를 넘으면 Provider
          를 호출하기 전에 막으므로 토큰이 소모되지 않고, 그때는 문헌을 나눠 여러
          번 실행하거나 전송 한도가 더 큰 Provider 를 선택하면 됩니다.
        </div>
      )}
      {!preflight.blocked && retrieval && (
        <div className="faint">
          ARIA 는 문서를 자르거나 요약하지 않습니다. 대신 문헌을 페이지·문단
          단위로 로컬 색인하고, AI 가 청구항 구성별로 검색한 구간만 전달합니다.
          검색되지 않은 구간은 「확인하지 못한 범위」로 보고서에 남습니다.
        </div>
      )}
    </div>
  );
}

const LANE_LABEL: Record<string, string> = {
  claim_only: "청구항 단독",
  spec_assisted: "명세서 확장",
};


/** 한 작업의 화면.
 *
 *  구성대비 분석과 유사문헌 검색은 이 컴포넌트를 함께 쓰지만 같은 화면이 아니다.
 *  어느 쪽인지는 주소가 정한다(kind) — 화면 안의 상태가 정하면 두 작업이 한
 *  화면의 두 모드가 되고, 주소 하나를 나눠 쓰는 동안은 뒤로 가기도 즐겨찾기도
 *  둘을 구분하지 못한다.
 */
export default function RunPage({ kind }: { kind: JobKind }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const historyJobId = searchParams.get("job");
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  // 검색 전략 프롬프트는 분석 프롬프트와 다른 목록이다. 한 목록에 담으면
  // 어느 쪽 화면에서든 상대 작업의 프롬프트를 고를 수 있게 된다.
  const [searchPrompts, setSearchPrompts] = useState<Prompt[]>([]);
  const [searchPromptId, setSearchPromptId] = useState("");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [promptId, setPromptId] = useState("");
  // 빈 문자열 = 지정 안 함. 제한된 안전성 Provider 가 자동으로 선택되면
  // 사용자가 위험을 확인하지 않은 채 실행하게 된다.
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  // 환경설정에서 사용자가 스스로 걸어 둔 글자 수 한도. 화면에 상수를 박아 두면
  // 설정을 바꿔도 옛 숫자가 남아, 사용자가 틀린 한도를 믿고 입력을 줄이게 된다.
  // null = 제한 없음(기본값), undefined = 아직 못 읽음.
  const [inlineCharBudget, setInlineCharBudget] = useState<number | null | undefined>(
    undefined,
  );
  // 실행 전 크기는 백엔드가 잰다. 화면이 원본 첨부 글자 수를 세는 것으로는
  // 최종 조립 프롬프트의 크기를 맞힐 수 없고, Provider 한도는 문자가 아니라
  // UTF-8 바이트로 걸린다. null 이면 아직 못 받았다는 뜻이다.
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [gapSearchOpen, setGapSearchOpen] = useState(false);
  const [selectedGapIds, setSelectedGapIds] = useState<string[]>([]);
  const [error, setError] = useState("");
  const citationFileInput = useRef<HTMLInputElement>(null);
  const searchSpecInput = useRef<HTMLInputElement>(null);

  // 실행 상태와 결과는 이 화면 밖(RunSessionProvider)에 있다. 메뉴를 옮기면
  // 이 컴포넌트는 언마운트되지만 보고서와 진행 중인 스트림은 그대로 남는다.
  const {
    jobs,
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
    included,
    setIncluded,
    spentBatchId,
    setSpentBatchId,
    stream,
    running,
    busy,
    restoring,
  } = useRunSession();

  // 세션이 들고 있는 축을 주소에 맞춘다. 전환기 링크는 누를 때 이미 맞춰 두지만,
  // 즐겨찾기·뒤로 가기로 들어오면 여기가 유일한 기회다. layout effect 로 두는
  // 이유는 그리기 전에 맞추기 위해서다 — 한 프레임이라도 어긋나면 다른 작업의
  // 탭과 보고서가 스쳐 지나간다.
  useLayoutEffect(() => {
    if (jobKind !== kind) setJobKind(kind);
  }, [kind, jobKind, setJobKind]);

  // 이 화면이 다루는 실행은 주소가 가리키는 축의 것 하나뿐이다.
  const job = jobs[kind];
  const workspace = WORKSPACE_BY_ID[kind];
  const otherWorkspace =
    WORKSPACE_BY_ID[
      kind === "patent_analysis" ? "similarity_search" : "patent_analysis"
    ];

  useEffect(() => {
    Promise.all([
      api.listPrompts(),
      api.listPrompts({ kind: "search" }),
      api.listProviders(),
      api.settings(),
    ])
      .then(([promptList, searchPromptList, providerList, appSettings]) => {
        setPrompts(promptList);
        setSearchPrompts(searchPromptList);
        // 설정에 고른 전략이 있으면 그것을, 없으면 첫 활성 전략을 쓴다.
        // 백엔드도 같은 순서로 고르므로 화면과 실행이 어긋나지 않는다.
        const configuredSearchPrompt = searchPromptList.find(
          (p) => p.id === appSettings.values.default_search_prompt_id && p.enabled,
        );
        setSearchPromptId(
          configuredSearchPrompt?.id ||
            searchPromptList.find((p) => p.enabled)?.id ||
            searchPromptList[0]?.id ||
            "",
        );
        setProviders(providerList);
        // 0 = 제한 없음. 화면에서는 null 로 다룬다.
        setInlineCharBudget(appSettings.values.max_inline_chars || null);
        const configuredPromptId = appSettings.values.default_prompt_id;
        const fallbackPrompt = promptList.find((p) => p.enabled) ?? promptList[0];
        const configuredPrompt = promptList.find(
          (p) => p.id === configuredPromptId && p.enabled,
        );
        setPromptId(configuredPrompt?.id || fallbackPrompt?.id || "");

        // 설정에 없으면 비워 둔다. 백엔드도 자동 선택하지 않으므로
        // 화면만 고른 척하면 실행 시 400 이 난다.
        const configuredProvider = appSettings.values.default_provider;
        const nextProvider = PROVIDER_IDS.has(configuredProvider)
          ? configuredProvider
          : "";
        setProviderId(nextProvider);
        setModel(
          nextProvider ? appSettings.values.default_models?.[nextProvider] ?? "" : "",
        );
      })
      .catch((e) => setError(String(e.message)));
  }, []);

  // 실행 기록에서 고른 작업은 별도 팝업 대신 이 화면의 결과 탭으로 복원한다.
  useEffect(() => {
    if (!historyJobId) return;
    let cancelled = false;
    setError("");
    api
      .historyItem(historyJobId)
      .then((storedJob) => {
        if (cancelled) return;
        if (storedJob.job_kind !== kind) {
          // 다른 작업의 실행이다. 그 작업의 주소로 넘긴다 — 검색 결과가 분석
          // 화면에 열리면 두 작업이 한 화면인 것처럼 보인다.
          navigate(
            workspacePath(storedJob.job_kind) +
              "?job=" +
              encodeURIComponent(historyJobId),
            { replace: true },
          );
          return;
        }
        setJob(storedJob);
        setLineage(null);
        setFollowupInstruction("");
        // 청구항 칸은 두 작업이 따로 쓴다. 이 화면의 칸에 채운다.
        if (storedJob.job_kind === "similarity_search") {
          setSearchClaimText(storedJob.claim_text);
        } else {
          setClaimText(storedJob.claim_text);
        }
        setUpload(null);
        setIncluded({});
        setSpentBatchId(null);
        setCitationFiles([]);
        setSearchSpecFile(null);
        setSearchUpload(null);
        setGapSearchOpen(false);
        setSelectedGapIds([]);
        setActiveTab("result");
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [historyJobId, kind]);

  const selectedPrompt = useMemo(
    () => prompts.find((p) => p.id === promptId) ?? null,
    [prompts, promptId],
  );
  const selectedSearchPrompt = useMemo(
    () => searchPrompts.find((p) => p.id === searchPromptId) ?? null,
    [searchPrompts, searchPromptId],
  );
  const selectedProvider = useMemo(
    () => providers.find((p) => p.provider === providerId) ?? null,
    [providers, providerId],
  );

  const searching = kind === "similarity_search";
  const jobKindLabel = JOB_KIND_LABEL[kind];
  const searchAvailable = supportsSearch(selectedProvider);
  const eligibleGapComponents = useMemo(
    () =>
      job?.job_kind === "patent_analysis"
        ? (job.analysis_manifest?.items ?? []).filter((item) => item.search_eligible)
        : [],
    [job],
  );

  // 체크된 자료만 센다. preflight 응답이 오기 전까지 쓰는 추정치이며, 계산은
  // attachmentSelection 이 한다 — preflight 에 보내는 목록과 같은 선택에서
  // 나와야 체크를 바꾼 직후에도 두 숫자가 같은 방향으로 움직인다.
  const totalChars = useMemo(
    () =>
      estimateTotalChars({
        uploadedFiles: upload?.files ?? null,
        inclusion: included,
        inheritedAttachments: lineage?.inheritedAttachments ?? [],
        claimText,
        followupInstruction,
        priorClaimChars: lineage?.priorClaimChars ?? 0,
        priorReportChars: lineage?.priorReportChars ?? 0,
        promptBodyChars: selectedPrompt?.body.length ?? 0,
      }),
    [upload, included, lineage, claimText, followupInstruction, selectedPrompt],
  );

  // 업로드를 마쳤으면 서버가 그 응답에 실어 준 값이 가장 최신이다. 아직
  // 안 올렸으면 화면을 열 때 읽어 둔 설정값을 쓴다. 둘 다 null 이 "제한 없음"
  // 이므로 ?? 로 합치면 뜻이 뒤집힌다.
  const budget = upload ? upload.max_inline_chars : inlineCharBudget;
  const overBudget = typeof budget === "number" && totalChars > budget;

  // 실행 전 크기는 백엔드가 잰다. 입력이 바뀔 때마다 부르되 타이핑마다 보내지
  // 않도록 조금 미룬다. 작업을 만들지 않고 Provider 도 부르지 않는 호출이다.
  const activeBatchId = searching
    ? (searchUpload?.batch_id ?? null)
    : (upload?.batch_id ?? null);
  const activeClaim = searching ? searchClaimText : claimText;
  // 검색은 명세서 한 건만 받고 「분석에 포함」 개념이 없다. null 을 보내면
  // 서버가 저장된 포함 여부를 그대로 쓴다.
  const activeSelection = searching
    ? null
    : selectedAttachmentIds(
        upload?.files ?? null,
        included,
        lineage?.inheritedAttachments ?? [],
      );
  // 목록 자체를 의존성으로 두면 렌더마다 새 배열이라 매번 다시 부른다.
  // null("저장된 값을 쓰라")과 빈 배열("전부 제외")은 뜻이 다르므로 키도 달라야
  // 한다. 둘 다 빈 문자열이면 그 사이 전환에서 다시 재지 않는다.
  const selectionKey = activeSelection === null ? "*" : activeSelection.join(",");
  useEffect(() => {
    if (!providerId) {
      setPreflight(null);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      api
        .preflight({
          job_kind: kind,
          provider: providerId,
          prompt_id: (searching ? searchPromptId : promptId) || null,
          claim_text: activeClaim,
          batch_id: activeBatchId,
          selected_attachment_ids: activeSelection,
          source_job_id: lineage?.sourceJobId ?? null,
          relation_type: lineage?.relationType ?? null,
          followup_instruction: followupInstruction,
        })
        .then((result) => {
          if (!cancelled) setPreflight(result);
        })
        .catch(() => {
          // 크기 안내는 편의 기능이다. 못 받으면 화면 추정치로 되돌아간다.
          if (!cancelled) setPreflight(null);
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    kind,
    providerId,
    promptId,
    searchPromptId,
    searching,
    activeClaim,
    activeBatchId,
    // 체크를 바꾸면 곧바로 다시 잰다. 화면 추정치와 preflight 가 같은 선택을
    // 보고 있어야 하므로 이 목록이 바뀌면 반드시 다시 물어봐야 한다.
    selectionKey,
    lineage?.sourceJobId,
    lineage?.relationType,
    followupInstruction,
  ]);

  const selectedUploadItems = useMemo(
    () => citationFiles.map((file) => ({ file, role: "CITATION" as const })),
    [citationFiles],
  );
  // 전처리 전에는 사용자가 고른 파일 수, 전처리 후에는 실제로 받아들인 파일
  // 수를 쓴다. 전부 거부된 업로드를 "첨부 있음"으로 잘못 세지 않는다.
  const newAnalysisAttachmentCount = upload
    ? upload.files.length
    : citationFiles.length;
  const inheritedAnalysisAttachmentCount =
    lineage?.inheritedAttachments.length ?? 0;
  const analysisAttachmentCount =
    newAnalysisAttachmentCount + inheritedAnalysisAttachmentCount;
  const hasAnalysisAttachments = analysisAttachmentCount > 0;
  // 전처리를 마쳤으면 「분석에 포함」까지 본다. 파일을 붙였어도 전부 체크를
  // 풀었으면 대비할 자료가 없는 실행이다.
  const analysisMaterialReady = upload
    ? hasAnalysisMaterial(upload.files, included, lineage?.inheritedAttachments ?? [])
    : hasAnalysisAttachments;

  /** 검색에 곁들인 명세서에서 뽑아낸 본문. 실행 전에 확인해 둔 경우에만 있다. */
  const searchSpec = searchUpload?.files?.[0] ?? null;
  const searchSpecChars = searchSpec?.read_ok ? searchSpec.char_count : 0;
  // 명세서가 청구항보다 압도적으로 길면 보조 실행의 주의가 실시예로 쏠릴 수
  // 있다. 청구항 단독 실행은 격리되어 영향을 받지 않지만 확장 품질은 보여 준다.
  const specOutweighsClaim =
    searchSpecChars > 0 &&
    searchSpecChars > Math.max(searchClaimText.trim().length, 1) * 20;

  /** 검색에 곁들일 명세서를 올린다. 실행 전 미리 확인과 실행 직전 업로드가
   *  같은 경로를 쓴다. */
  const uploadSearchSpec = useCallback(async () => {
    if (!searchSpecFile) return null;
    setUploading(true);
    setError("");
    try {
      const response = await api.upload([
        { file: searchSpecFile, role: "APPLICATION" as const },
      ]);
      setSearchUpload(response);
      return response;
    } finally {
      setUploading(false);
    }
  }, [searchSpecFile]);

  const prepareSearchSpec = async () => {
    try {
      await uploadSearchSpec();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const clearSearchSpec = () => {
    setSearchSpecFile(null);
    setSearchUpload(null);
    if (searchSpecInput.current) searchSpecInput.current.value = "";
  };

  const uploadSelectedFiles = useCallback(async () => {
    if (selectedUploadItems.length === 0) return null;
    setUploading(true);
    setError("");
    try {
      const response = await api.upload(selectedUploadItems);
      const inclusion = carryInclusion(
        upload?.files ?? [],
        included,
        response.files,
      );
      setUpload(response);
      // 처음 올렸으면 정상 처리된 PDF 만 체크한다(본문을 읽지 못한 파일은 목록에
      // 사유와 함께 남지만 분석 자료로 들어가지 않는다). 같은 자료를 다시 올린
      // 것이면 직전에 고른 부분집합을 그대로 이어받는다 — 두 번째 분석을 돌릴
      // 때마다 처음부터 다시 고르게 하지 않는다.
      setIncluded(inclusion);
      return { response, inclusion };
    } finally {
      setUploading(false);
    }
  }, [selectedUploadItems, upload, included]);

  const prepareFiles = async () => {
    try {
      await uploadSelectedFiles();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const runSearch = async () => {
    setSubmitting(true);
    setError("");
    try {
      // 명세서를 골랐으면 실행 직전에 올린다. 미리 확인해 둔 batch 가 있으면
      // 그대로 쓴다. 고르지 않았으면 첨부 없이 청구항만으로 검색한다.
      const prepared = searchSpecFile
        ? (searchUpload ?? (await uploadSearchSpec()))
        : null;
      const created = await api.createJob({
        job_kind: "similarity_search",
        provider: providerId || null,
        model: model || null,
        // 고른 검색 전략. 보내지 않으면 백엔드가 설정 기본값을 쓰지만, 화면이
        // 보여 준 전략과 실행한 전략이 달라질 수 있으므로 명시해서 보낸다.
        prompt_id: searchPromptId || null,
        claim_text: searchClaimText,
        batch_id: prepared?.batch_id ?? null,
      });
      setJob(created);
      navigate(workspacePath("similarity_search"), { replace: true });
      // 이 batch 는 방금 만든 작업에 귀속됐다. 그대로 다시 보내면 백엔드가
      // 거절한다. 고른 File 은 남겨 두므로 다시 실행하면 새 batch 로 올라간다.
      setSearchUpload(null);
      setActiveTab("result");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const openGapSearch = () => {
    setSelectedGapIds(eligibleGapComponents.map((item) => item.id));
    setGapSearchOpen(true);
  };

  const runGapSearch = async () => {
    if (!job || job.job_kind !== "patent_analysis") return;
    setSubmitting(true);
    setError("");
    try {
      const created = await api.createJob({
        job_kind: "similarity_search",
        provider: providerId || null,
        model: model || null,
        prompt_id: searchPromptId || null,
        source_job_id: job.id,
        search_component_ids: selectedGapIds,
      });
      setSearchClaimText(created.claim_text);
      setJob(created);
      setGapSearchOpen(false);
      setSelectedGapIds([]);
      // 이 실행은 검색 작업이다. 분석 화면에서 시작했더라도 결과는 검색 화면에
      // 열린다 — 두 작업은 결과물이 다르고, 각자의 자리에 남아야 한다.
      setActiveTab("result", "similarity_search");
      setJobKind("similarity_search");
      navigate(workspacePath("similarity_search"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const run = async () => {
    if (searching) return runSearch();
    if (!promptId) return;
    if (!claimText.trim()) {
      setError(
        "구성대비 분석에는 출원발명 청구항이 필요합니다. 분석할 청구항을 입력하십시오.",
      );
      return;
    }
    if (!hasAnalysisAttachments) {
      setError(
        "구성대비 분석에는 인용발명 문헌이 최소 1건 필요합니다. PDF를 첨부하거나 이전 실행의 자료를 물려받으십시오.",
      );
      return;
    }
    if (!analysisMaterialReady) {
      setError(NO_INCLUDED_MATERIAL);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      // 업로드 batch 는 작업 하나에만 귀속된다. 아직 쓰지 않은 batch 가 화면에
      // 있으면 그대로 쓰고, 이미 한 번 실행에 쓴 batch 라면 같은 파일을 새 batch
      // 로 다시 올린다. 체크 상태는 내용 기준으로 따라오므로 이때도 사용자가 고른
      // 부분집합이 그대로 유지된다.
      const reusable = upload && upload.batch_id !== spentBatchId ? upload : null;
      const prepared = reusable
        ? { response: reusable, inclusion: included }
        : await uploadSelectedFiles();
      const preparedUpload = prepared?.response ?? null;
      const preparedAttachmentCount =
        (preparedUpload?.files.length ?? 0) + inheritedAnalysisAttachmentCount;
      if (preparedAttachmentCount === 0) {
        setError(
          "구성대비 분석에 사용할 수 있는 인용발명 문헌이 없습니다. 선택한 PDF의 처리 결과를 확인하십시오.",
        );
        return;
      }
      // 방금 올렸다면 setIncluded 의 결과가 이 클로저에는 아직 없다. 업로드가
      // 계산해 둔 값을 그대로 쓴다 — 화면에 곧 표시될 체크 상태와 같다.
      const inclusion = prepared?.inclusion ?? {};
      const inheritedAttachments = lineage?.inheritedAttachments ?? [];
      if (
        !hasAnalysisMaterial(
          preparedUpload?.files ?? null,
          inclusion,
          inheritedAttachments,
        )
      ) {
        setError(NO_INCLUDED_MATERIAL);
        return;
      }
      const created = await api.createJob({
        job_kind: "patent_analysis",
        // 화면에서 고른 값을 그대로 보낸다. 생략하면 백엔드가 설정
        // 기본값으로 되돌아가서, 화면 표시와 실제 실행이 어긋난다.
        prompt_id: promptId || null,
        provider: providerId || null,
        model: model || null,
        claim_text: claimText,
        batch_id: preparedUpload?.batch_id ?? null,
        // preflight 에 보낸 것과 같은 목록. 안내한 크기와 실제로 나가는 크기가
        // 어긋나지 않으려면 두 요청이 같은 선택을 실어야 한다.
        selected_attachment_ids: selectedAttachmentIds(
          preparedUpload?.files ?? null,
          inclusion,
          inheritedAttachments,
        ),
        source_job_id: lineage?.sourceJobId ?? null,
        relation_type: lineage?.relationType ?? null,
        followup_instruction: lineage ? followupInstruction : "",
      });
      setJob(created);
      navigate(workspacePath("patent_analysis"), { replace: true });
      // 전처리 결과와 체크 상태는 지우지 않는다. 결과를 보고 「분석 준비」로
      // 돌아왔을 때 그대로 있어야, 선택만 다른 부분집합으로 바꿔 곧바로 다시
      // 돌릴 수 있다. 다만 이 batch 는 방금 만든 작업에 귀속됐으므로 다시 보내면
      // 백엔드가 거절한다 — 표시해 두고, 다음 실행은 새 batch 로 올린다.
      setSpentBatchId(preparedUpload?.batch_id ?? null);
      setActiveTab("result");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      await api.cancelJob(job.id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const clearCitationFiles = () => {
    setCitationFiles([]);
    setUpload(null);
    setIncluded({});
    setSpentBatchId(null);
    if (citationFileInput.current) citationFileInput.current.value = "";
  };

  const clearSelectedFiles = () => {
    setUpload(null);
    setIncluded({});
    setSpentBatchId(null);
    setCitationFiles([]);
    if (citationFileInput.current) citationFileInput.current.value = "";
  };

  const reset = () => {
    setJob(null);
    setLineage(null);
    setFollowupInstruction("");
    if (searching) setSearchClaimText("");
    else setClaimText("");
    clearSelectedFiles();
    clearSearchSpec();
    setGapSearchOpen(false);
    setSelectedGapIds([]);
    setActiveTab("input");
    navigate(workspacePath(kind), { replace: true });
  };

  /** 방금 본 실행을 원본으로 삼아 다음 실행을 준비한다.
   *
   *  CONTINUED  : 첨부 + 이전 청구항 + 이전 보고서를 물려받는다.
   *  REANALYZED : 첨부만 물려받는다. 이전 보고서는 전달하지 않는다.
   *
   *  이미 소비한 업로드 batch 를 그대로 다시 보내면 백엔드가 400 으로 거절한다.
   *  첨부는 원본에서 복제해 오므로 선택 상태를 비우고, 새로 고른 PDF 만 batch 로
   *  나가게 한다.
   */
  const startFollowUp = (relationType: RelationType) => {
    if (!job) return;
    const carriesClaims = relationType !== "REANALYZED";
    setLineage({
      sourceJobId: job.id,
      sourceLabel: jobLabel(job),
      relationType,
      // 원본에서 「분석에 포함」을 풀었던 자료는 물려받지 않는다. 그 실행의
      // 분석 자료가 아니었으므로, 후속 실행에서 조용히 되살아나면 안 된다.
      inheritedAttachments: job.attachments.filter((a) => a.included),
      priorMapping: carriesClaims ? job.citation_mapping : null,
      priorClaimChars: carriesClaims ? job.claim_text.length : 0,
      priorReportChars:
        relationType === "CONTINUED" ? (job.result_text ?? "").length : 0,
    });
    setClaimText(job.claim_text);
    setFollowupInstruction("");
    setGapSearchOpen(false);
    setSelectedGapIds([]);
    clearSelectedFiles();
    setActiveTab("input");
  };

  const clearLineage = () => {
    setLineage(null);
    setFollowupInstruction("");
  };

  const displayText = job?.result_text ?? stream.streamText;
  const errors = job?.errors ?? stream.errors;

  const searchSpecPanel = (
    <section className="search-spec-panel">
      <div className="input-panel-head">
        <span className="input-step">2</span>
        <div>
          <strong>출원발명 문서 (선택)</strong>
          <div className="hint">PDF를 더하면 검색어를 넓혀 결과를 합칩니다.</div>
        </div>
      </div>
      <input
        ref={searchSpecInput}
        type="file"
        accept=".pdf,application/pdf"
        aria-label="출원발명 문서"
        onChange={(e) => {
          setSearchSpecFile(e.target.files?.[0] ?? null);
          setSearchUpload(null);
        }}
        disabled={running || uploading}
      />
      {searchSpecFile && (
        <>
          <div className="selected-file">
            <span className="pill accent">출원발명 문서</span>
            <span>{searchSpecFile.name}</span>
            <span className="faint">{formatBytes(searchSpecFile.size)}</span>
          </div>
          <div className="btn-row file-prepare-row">
            <button
              type="button"
              className="btn"
              onClick={prepareSearchSpec}
              disabled={running || uploading || Boolean(searchUpload)}
            >
              {searchUpload
                ? "본문 확인 완료"
                : uploading
                  ? "본문 확인 중…"
                  : "본문 미리 확인"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={clearSearchSpec}
              disabled={running || uploading}
            >
              빼기
            </button>
          </div>
        </>
      )}
      {searchSpec && (
        <div
          className={`notice ${searchSpec.read_ok ? "info" : "danger"}`}
          style={{ marginTop: 10 }}
        >
          {searchSpec.read_ok ? (
            <>
              본문 {searchSpec.char_count.toLocaleString()}자
              {searchSpec.page_count ? ` · ${searchSpec.page_count}페이지` : ""} · 청구항{" "}
              {searchClaimText.trim().length.toLocaleString()}자
              {specOutweighsClaim && (
                <div style={{ marginTop: 4 }}>
                  명세서가 길어 용어 확장이 실시예에 쏠릴 수 있습니다. 결과의 보조
                  검색 절을 확인하세요.
                </div>
              )}
            </>
          ) : (
            <>
              <strong>본문을 읽지 못했습니다</strong>
              <div style={{ marginTop: 4 }}>
                {searchSpec.error ?? "알 수 없는 오류"} — 다른 PDF를 선택하거나 문서를
                빼주세요.
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );

  return (
    <div className="page page-run" data-mode={kind}>
      {/* 작업 이름과 소개는 상단 작업 전환기에 이미 보인다. 본문은 바로
          준비/결과 전환부터 시작해 같은 내용을 반복하지 않는다. */}
      <div className="workspace-head no-print">
        <div
          className="run-tabs"
          role="tablist"
          aria-label={`${workspace.label} 화면`}
        >
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "input"}
            className={`run-tab ${activeTab === "input" ? "active" : ""}`}
            onClick={() => setActiveTab("input")}
          >
            {searching ? "검색 준비" : "분석 준비"}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "result"}
            className={`run-tab ${activeTab === "result" ? "active" : ""}`}
            onClick={() => setActiveTab("result")}
          >
            결과 보기
            {running && <span className="spinner" aria-label="실행 중" />}
          </button>
        </div>
      </div>

      {error && <div className="notice danger">{error}</div>}

      {!providerId && (
        <div className="notice danger">
          <strong>실행할 AI 도구가 지정되지 않았습니다</strong>
          <div style={{ marginTop: 4 }}>
            <a href="#/settings">환경 설정</a>에서 사용할 도구를 지정하십시오.
          </div>
        </div>
      )}

      {activeTab === "input" && (
        <>
      <div className="card no-print run-input-card">
        <h2>{searching ? "검색 준비" : "분석 자료 준비"}</h2>

        <div className="run-config-summary">
          <span>
            <strong>프롬프트</strong>{" "}
            {searching
              ? (selectedSearchPrompt?.name ?? "설정 필요")
              : selectedPrompt
                ? selectedPrompt.name
                : "설정 필요"}
          </span>
          <span>
            <strong>실행 도구</strong>{" "}
            {selectedProvider?.display_name ?? (providerId || "설정 필요")}
          </span>
          <span>
            <strong>모델</strong> {model || "CLI 기본값"}
          </span>
          <a href="#/settings">기본값 변경</a>
        </div>

        {searching ? (
          <>
            {!searchAvailable && providerId && (
              <div className="notice danger">
                <strong>이 실행 도구로는 검색할 수 없습니다</strong>
                <div style={{ marginTop: 4 }}>
                  이 Provider는 ARIA가 확인한 웹 검색 도구를 제공하지 않습니다.{" "}
                  <a href="#/settings">환경 설정</a>에서 Claude 또는 agy를
                  선택하십시오.
                </div>
              </div>
            )}

            <section className="input-panel search-panel-input">
              <div className="input-panel-head">
                <span className="input-step">1</span>
                <div>
                  <strong>검색 전략</strong>
                  <div className="hint">
                    무엇을 중시하고 어디까지 넓힐지를 정하는 프롬프트입니다.
                    검색 실행·보안·감사 규칙은 ARIA가 갖고 있으므로 전략을
                    바꿔도 감사 기록과 보고서 형식은 그대로입니다.
                  </div>
                </div>
              </div>
              <select
                id="searchPromptId"
                aria-label="검색 전략 프롬프트"
                value={searchPromptId}
                onChange={(e) => setSearchPromptId(e.target.value)}
                disabled={running}
              >
                {searchPrompts.length === 0 && (
                  <option value="">검색 전략 프롬프트가 없습니다</option>
                )}
                {searchPrompts.map((p) => (
                  <option key={p.id} value={p.id} disabled={!p.enabled}>
                    {p.name}
                    {p.enabled ? "" : " (비활성)"}
                  </option>
                ))}
              </select>
              {selectedSearchPrompt?.description && (
                <div className="hint" style={{ marginTop: 6 }}>
                  {selectedSearchPrompt.description}
                </div>
              )}
              <div className="hint" style={{ marginTop: 6 }}>
                <a href="#/prompts">프롬프트 관리</a>에서 검색 전략을 새로
                만들거나 고칠 수 있습니다.
              </div>
            </section>

            <section className="input-panel claim-panel search-panel-input">
              <div className="input-panel-head">
                <span className="input-step">2</span>
                <div>
                  <strong>검색할 청구항</strong>
                  <div className="hint">
                    청구항 전문을 붙여넣으세요. 입력 내용이 검색 범위를 정합니다.
                  </div>
                </div>
              </div>
              <textarea
                id="searchClaimText"
                className="claim-input"
                aria-label="검색할 청구항"
                value={searchClaimText}
                onChange={(e) => setSearchClaimText(e.target.value)}
                placeholder={
                  "예: 청구항 1. ...\n\n독립항 하나만 넣어도 되고, 종속항까지 함께 넣어도 됩니다."
                }
                disabled={running}
              />
            </section>

            <SizeNotice
              preflight={preflight}
              totalChars={totalChars}
              budget={budget}
              overBudget={overBudget}
            />
          </>
        ) : (
          <>
        {lineage && (
          <div className="notice info lineage-banner">
            <div className="split">
              <strong>{RELATION_TITLE[lineage.relationType]}</strong>
              <button type="button" className="btn small" onClick={clearLineage}>
                연결 해제
              </button>
            </div>
            <div className="faint">원본 실행: {lineage.sourceLabel}</div>
            <ul className="lineage-inherits">
              <li>
                첨부 {lineage.inheritedAttachments.length}건을 이 실행 폴더로 복제합니다
                {lineage.inheritedAttachments.length > 0 && (
                  <span className="faint">
                    {" — "}
                    {lineage.inheritedAttachments
                      .map((a) => a.original_filename)
                      .join(", ")}
                  </span>
                )}
              </li>
              {lineage.priorClaimChars > 0 && (
                <li>이전 청구항 {lineage.priorClaimChars.toLocaleString()}자</li>
              )}
              {lineage.relationType === "CONTINUED" && (
                <li>
                  이전 보고서 {lineage.priorReportChars.toLocaleString()}자 —{" "}
                  <strong>이전 유사도와 발췌문이 모델 앞에 함께 놓입니다.</strong> 보고서
                  자체를 고칠 때만 쓰십시오.
                </li>
              )}
              {lineage.relationType === "REANALYZED" && (
                <li>
                  번호도 이전 판단도 물려받지 않습니다.
                  <strong> 인용발명 번호가 원본 보고서와 달라질 수 있습니다.</strong>
                </li>
              )}
              {lineage.priorMapping && lineage.priorMapping.items.length > 0 && (
                <li>
                  고정 문헌 매핑 {lineage.priorMapping.items.length}건 — 이 번호를 그대로
                  씁니다
                  <ul className="lineage-mapping">
                    {lineage.priorMapping.items.map((item) => (
                      <li key={item.citation_number}>
                        <strong>인용발명 {item.citation_number}</strong> ={" "}
                        {item.document_number}
                        <span className="faint"> · {item.filename}</span>
                      </li>
                    ))}
                  </ul>
                </li>
              )}
              {lineage.relationType !== "REANALYZED" && (
                <li>
                  유사도, 발췌문, 대응 이유는 물려받지 않습니다. 첨부 자료에서 다시
                  판단합니다.
                </li>
              )}
            </ul>
            <div className="faint">
              아래 청구항 칸에는 원본 실행의 청구항이 채워져 있습니다. 종속항을 덧붙이거나
              수정한 뒤 실행하십시오. 인용발명 PDF 를 더 추가할 수도 있습니다.
            </div>
          </div>
        )}

        <div className="patent-input-grid">
          <section className="input-panel application claim-panel">
            <div className="input-panel-head">
              <span className="input-step">1</span>
              <div>
                <strong>출원발명의 청구항</strong>
                <div className="hint">분석할 청구항을 그대로 붙여넣으십시오.</div>
              </div>
            </div>
            <textarea
              id="claimText"
              className="claim-input"
              aria-label="출원발명의 청구항"
              value={claimText}
              onChange={(e) => setClaimText(e.target.value)}
              placeholder={
                "예: 청구항 1. ...\n\n여러 청구항을 한 번에 입력할 수 있습니다."
              }
              disabled={running}
            />
          </section>

          <div className="supporting-inputs">
          <section className="input-panel citation">
            <div className="input-panel-head">
              <span className="input-step">2</span>
              <div>
                <strong>인용발명 문헌</strong>
                <div className="hint">
                  대비할 PDF를 모두 선택하십시오. 업로드 순서로 인용번호를 정하지 않습니다.
                </div>
              </div>
            </div>
            <input
              ref={citationFileInput}
              type="file"
              multiple
              accept=".pdf,application/pdf"
              aria-label="인용발명 PDF"
              onChange={(e) => {
                setCitationFiles(Array.from(e.target.files ?? []));
                setUpload(null);
                setIncluded({});
                setSpentBatchId(null);
              }}
              disabled={running || uploading}
            />
            {citationFiles.length > 0 && (
              <div className="selected-files">
                <div className="selected-files-head">
                  <span>선택한 PDF {citationFiles.length}건</span>
                  <button
                    type="button"
                    className="btn small"
                    onClick={clearCitationFiles}
                    disabled={running || uploading}
                  >
                    모두 지우기
                  </button>
                </div>
                <div className="selected-file-list">
                  {citationFiles.map((file, index) => (
                    <div className="selected-file" key={`${file.name}-${index}`}>
                      <span className="pill warn">인용 후보 {index + 1}</span>
                      <span>{file.name}</span>
                      <span className="faint">{formatBytes(file.size)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="btn-row file-prepare-row citation-prepare-row">
              <button
                type="button"
                className="btn"
                onClick={prepareFiles}
                disabled={
                  running ||
                  uploading ||
                  selectedUploadItems.length === 0 ||
                  Boolean(upload)
                }
              >
                {upload
                  ? "PDF 처리 완료"
                  : uploading
                    ? "자료 처리 중…"
                    : "선택한 PDF 미리 확인"}
              </button>
              <span className="hint">미처리 PDF는 실행할 때 자동 업로드됩니다.</span>
            </div>
          </section>

          </div>
        </div>

        {lineage && (
          <section className="input-panel followup-panel">
            <div className="input-panel-head">
              <span className="input-step">3</span>
              <div>
                <strong>후속 지시 (선택)</strong>
                <div className="hint">
                  이번 실행에서 무엇을 해야 하는지 직접 쓰십시오. ARIA 는 이 문장을
                  만들거나 보태지 않고 그대로 전달합니다. 비워 두면 분석 프롬프트의
                  「후속 처리 규칙」만 적용됩니다.
                </div>
              </div>
            </div>
            <textarea
              className="claim-input followup-input"
              aria-label="후속 지시"
              value={followupInstruction}
              onChange={(e) => setFollowupInstruction(e.target.value)}
              placeholder={
                "예: 추가한 종속항 3~7 을 중심으로 분석하고, 인용발명 번호는 이전 보고서와 동일하게 유지하십시오."
              }
              disabled={running}
            />
          </section>
        )}

        {uploading && (
          <p className="faint">
            <span className="spinner" /> 업로드 및 전처리 중…
          </p>
        )}

        {upload && upload.rejected.length > 0 && (
          <div className="notice danger">
            <strong>차단된 파일</strong>
            <ul>
              {upload.rejected.map((r) => (
                <li key={r.filename}>
                  <code>{r.filename}</code> — {r.reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        {upload && upload.files.length > 0 && (
          <div style={{ marginTop: 8 }}>
            {upload.files.map((file) => {
              const label = deliveryLabel(file);
              return (
                <div className="file-item" key={file.attachment_id}>
                  <div className="file-main">
                    <div className="file-name">
                      <span
                        className={`pill ${file.role === "APPLICATION" ? "accent" : "warn"}`}
                      >
                        {roleLabel(file.role)}
                      </span>{" "}
                      {file.original_filename}
                    </div>
                    <div className="file-meta">
                      {formatBytes(file.size_bytes)} · {file.mime_type}
                      {file.page_count ? ` · ${file.page_count}페이지` : ""}
                      {file.read_ok ? ` · ${file.char_count.toLocaleString()}자` : ""}
                      {" · sha256 "}
                      {file.sha256.slice(0, 12)}…
                    </div>
                    {file.error && (
                      <div className="file-meta" style={{ color: "var(--danger)" }}>
                        {file.error}
                      </div>
                    )}
                  </div>
                  <span className={`pill ${label.cls}`}>{label.text}</span>
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={included[file.attachment_id] ?? false}
                      onChange={(e) =>
                        setIncluded((prev) => ({
                          ...prev,
                          [file.attachment_id]: e.target.checked,
                        }))
                      }
                      disabled={running}
                    />
                    분석에 포함
                  </label>
                </div>
              );
            })}

            <SizeNotice
              preflight={preflight}
              totalChars={totalChars}
              budget={budget}
              overBudget={overBudget}
            />
          </div>
        )}

        {!upload && (
          <SizeNotice
            preflight={preflight}
            totalChars={totalChars}
            budget={budget}
            overBudget={overBudget}
          />
        )}
          </>
        )}
      </div>

      <div className="run-side-rail no-print">
      {searching && (
        <div className="card search-spec-card">{searchSpecPanel}</div>
      )}
      <div className="card run-action-card">
        <h2>
          {searching
            ? "3. 검색 시작"
            : lineage
              ? "4. 후속 분석 시작"
              : "3. 분석 시작"}
        </h2>
        <div className="run-ready">
          <div className="run-ready-row">
            <span>작업</span>
            <strong>{jobKindLabel}</strong>
          </div>
          <div className="run-ready-row">
            <span>청구항</span>
            <strong>
              {(searching ? searchClaimText : claimText).trim()
                ? `${(searching ? searchClaimText : claimText).length.toLocaleString()}자`
                : "아직 없음"}
            </strong>
          </div>
          {!searching && (
            <div className="run-ready-row">
              <span>인용발명</span>
              <strong>
                {newAnalysisAttachmentCount}건
                {inheritedAnalysisAttachmentCount > 0
                  ? ` + 물려받은 ${inheritedAnalysisAttachmentCount}건`
                  : ""}
              </strong>
            </div>
          )}
          {searching && (
            <div className="run-ready-row">
              <span>검색 전략</span>
              <strong>{selectedSearchPrompt?.name ?? "설정 필요"}</strong>
            </div>
          )}
          {searching && (
            <div className="run-ready-row">
              <span>출원발명 문서</span>
              <strong>
                {searchSpecFile
                  ? searchSpecChars
                    ? `1건 · ${searchSpecChars.toLocaleString()}자`
                    : "1건"
                  : "없음 (청구항만으로 검색)"}
              </strong>
            </div>
          )}
        </div>

        {searching && searchPrompts.length === 0 && (
          <div className="notice danger" style={{ marginBottom: 12 }}>
            <strong>검색 전략 프롬프트가 없습니다</strong>
            <div style={{ marginTop: 4 }}>
              <a href="#/prompts">프롬프트 관리</a>에서 검색 전략을 하나
              만드십시오.
            </div>
          </div>
        )}

        {!searching && !claimText.trim() && (
          <div className="notice danger" style={{ marginBottom: 12 }}>
            <strong>출원발명 청구항이 필요합니다</strong>
            <div style={{ marginTop: 4 }}>
              분석할 청구항을 위쪽 입력 칸에 붙여넣으십시오.
            </div>
          </div>
        )}

        {!searching && !hasAnalysisAttachments && (
          <div className="notice danger" style={{ marginBottom: 12 }}>
            <strong>인용발명 문헌이 필요합니다</strong>
            <div style={{ marginTop: 4 }}>
              구성대비 분석을 시작하려면 PDF를 최소 1건 첨부하거나 이전 실행의
              자료를 물려받으십시오.
            </div>
          </div>
        )}

        {!searching && hasAnalysisAttachments && !analysisMaterialReady && (
          <div className="notice danger" style={{ marginBottom: 12 }}>
            <strong>분석에 포함한 문헌이 없습니다</strong>
            <div style={{ marginTop: 4 }}>{NO_INCLUDED_MATERIAL}</div>
          </div>
        )}

        {busy && !running && (
          <div className="notice info" style={{ marginBottom: 12 }}>
            <strong>{otherWorkspace.label}이 실행 중입니다</strong>
            <div style={{ marginTop: 4 }}>
              두 작업은 서로를 기다리지 않지만 실행 도구는 하나뿐이라 한 번에
              하나씩 끝냅니다.{" "}
              <a href={`#${otherWorkspace.path}`}>진행 상황 보기</a>
            </div>
          </div>
        )}

        <div className="btn-row">
          <button
            className="btn primary"
            onClick={run}
            disabled={
              // 다른 축에서 실행 중이어도 막는다. 스트림이 하나뿐이라 두 작업을
              // 동시에 띄우면 한쪽은 끝나도 화면이 갱신되지 않는다.
              busy ||
              uploading ||
              submitting ||
              !providerId ||
              !selectedProvider?.usable ||
              (searching
                ? !searchClaimText.trim() || !searchAvailable || !searchPromptId
                : !promptId || !claimText.trim() || !analysisMaterialReady)
            }
          >
            {submitting
              ? "준비 중…"
              : searching
                ? "검색 시작"
                : "분석 시작"}
          </button>
          <button className="btn danger" onClick={cancel} disabled={!running}>
            중단
          </button>
          {(job || lineage) && !running && (
            <button className="btn" onClick={reset}>
              {searching ? "모두 비우고 새 검색" : "모두 비우고 새 분석"}
            </button>
          )}
          {running && (
            <span className="faint">
              <span className="spinner" /> {stream.stage || "진행 중"}
              {searching && (stream.searchCount > 0 || stream.fetchCount > 0) && (
                <div style={{ marginTop: 4 }}>
                  검색 {stream.searchCount}회 · 페이지 열람 {stream.fetchCount}건
                </div>
              )}
              {!searching && stream.retrievalRound > 0 && (
                <div style={{ marginTop: 4 }}>
                  로컬 검색 {stream.retrievalRound}라운드 · 읽은 페이지{" "}
                  {stream.retrievalPagesRead}쪽
                </div>
              )}
            </span>
          )}
        </div>
      </div>
      </div>

        </>
      )}

      {activeTab === "result" && !job && restoring && (
        <div className="card empty">
          <strong>
            <span className="spinner" /> 직전 결과를 불러오는 중…
          </strong>
          <div>새로고침 전에 보던 보고서를 다시 읽고 있습니다.</div>
        </div>
      )}

      {activeTab === "result" && !job && !restoring && (
        <div className="card empty">
          <strong>
            {searching ? "아직 검색 결과가 없습니다." : "아직 분석 결과가 없습니다."}
          </strong>
          <div>
            {searching ? "검색 준비" : "분석 준비"} 탭에서 청구항을 넣고 실행하면
            이곳으로 자동 이동합니다.
          </div>
        </div>
      )}

      {activeTab === "result" && job && (
        <div className="card result-card">
          <div className="split" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>
              {job.job_kind === "similarity_search"
                ? "검토 후보 탐색 결과"
                : "분석 결과"}
            </h2>
            <div className="btn-row no-print">
              <StatusPill status={job.status} errorCode={job.error_code} />
              <button
                type="button"
                className="btn small"
                onClick={() => window.print()}
                disabled={running || !(job.result_text ?? "").trim()}
              >
                인쇄 / PDF
              </button>
              <button className="btn small danger" onClick={cancel} disabled={!running}>
                중단
              </button>
              {job.job_kind !== "similarity_search" && !running && !lineage && (
                <>
                  <button
                    className="btn small primary"
                    onClick={openGapSearch}
                    disabled={eligibleGapComponents.length === 0}
                    title={
                      eligibleGapComponents.length > 0
                        ? "유사도 80% 미만 또는 대응 문헌을 찾지 못한 구성만 골라 웹 검색합니다."
                        : job.analysis_manifest_error
                          ? `구성별 결과를 읽지 못했습니다: ${job.analysis_manifest_error}`
                          : "웹 검색이 필요한 미대응 구성이 없습니다."
                    }
                  >
                    미대응 구성 검색
                  </button>
                  <button
                    className="btn small"
                    onClick={() => startFollowUp("MAPPED")}
                    disabled={!job.citation_mapping}
                    title={
                      job.citation_mapping
                        ? "인용발명 번호와 이전 청구항만 물려받습니다. 이전 보고서는 전달하지 않으므로 유사도와 발췌문은 자료에서 다시 판단합니다."
                        : job.citation_mapping_error
                          ? `문헌 매핑을 읽지 못했습니다: ${job.citation_mapping_error}`
                          : "이 프롬프트는 문헌 매핑을 출력하지 않습니다."
                    }
                  >
                    {RELATION_LABEL.MAPPED}
                  </button>
                  <button
                    className="btn small"
                    onClick={() => startFollowUp("CONTINUED")}
                    disabled={!(job.result_text ?? "").trim()}
                    title="이전 보고서 전체를 전달합니다. 보고서 자체를 고치거나 보완할 때만 쓰십시오."
                  >
                    {RELATION_LABEL.CONTINUED}
                  </button>
                  <button
                    className="btn small"
                    onClick={() => startFollowUp("REANALYZED")}
                    title="같은 자료로 처음부터 다시 판단합니다. 인용발명 번호가 이 보고서와 달라질 수 있습니다."
                  >
                    {RELATION_LABEL.REANALYZED}
                  </button>
                </>
              )}
              {job.job_kind !== "similarity_search" && !running && lineage && (
                <>
                  <span className="faint">
                    <strong>{RELATION_LABEL[lineage.relationType]}</strong> 준비 중이라
                    후속 분석 버튼을 잠갔습니다. 이어서 쓰려면 분석 준비 탭으로
                    가십시오.
                  </span>
                  <button
                    type="button"
                    className="btn small"
                    onClick={clearLineage}
                    title="후속 분석 준비를 취소하고 이 보고서에서 다시 고릅니다."
                  >
                    연결 해제
                  </button>
                </>
              )}
            </div>
          </div>

          {job.error_code && (
            <div className="notice danger">
              <strong>{ERROR_LABEL[job.error_code] ?? job.error_code}</strong>
              {errors.length > 0 && (
                <ul>
                  {errors.map((message, i) => (
                    <li key={i}>{message}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {stream.events.length > 0 && running && (
            <div className="event-log no-print" style={{ marginBottom: 14 }}>
              {stream.events
                .filter((e) => e.type !== "result_stream")
                .slice(-40)
                .map((e) => (
                  <div key={e.seq}>
                    <span className="t">{new Date(e.ts).toLocaleTimeString()}</span>
                    <span className="k">{e.type}</span>
                    <span>
                      {String(
                        e.payload.message ??
                          e.payload.stage ??
                          e.payload.status ??
                          "",
                      )}
                    </span>
                  </div>
                ))}
            </div>
          )}

          {job.job_kind === "patent_analysis" &&
            !running &&
            job.analysis_manifest && (
              <AnalysisDegreeOverview components={job.analysis_manifest.items} />
            )}

          {(displayText || running) && (
            <ResultView
              text={displayText}
              outputMode={job.output_mode}
              streaming={running}
            />
          )}

          {gapSearchOpen && job.job_kind === "patent_analysis" && !running && (
            <GapSearchPanel
              job={job}
              components={eligibleGapComponents}
              selectedIds={selectedGapIds}
              providerLabel={selectedProvider?.display_name ?? providerId}
              searchAvailable={searchAvailable}
              submitting={submitting}
              onSelectionChange={setSelectedGapIds}
              onRun={runGapSearch}
              onClose={() => setGapSearchOpen(false)}
            />
          )}

          {job.job_kind === "similarity_search" && !running && (
            <SearchManifestView job={job} />
          )}

          {job.job_kind === "patent_analysis" &&
            !running &&
            isNarrowed(job.delivery_plan) && <RetrievalManifestView job={job} />}

          <details className="no-print" style={{ marginTop: 16 }}>
            <summary className="faint" style={{ cursor: "pointer" }}>
              실행 정보
            </summary>
            <div className="table-scroll" style={{ marginTop: 10 }}>
              <table>
                <tbody>
                  <tr>
                    <th>프롬프트</th>
                    <td>{job.prompt_name}</td>
                  </tr>
                  <tr>
                    <th>실행 도구 / 모델</th>
                    <td>
                      {job.provider} / {job.model ?? "기본값"}
                    </td>
                  </tr>
                  <tr>
                    <th>CLI</th>
                    <td className="break mono-text">
                      {job.cli_path ?? "-"} {job.cli_version ? `(${job.cli_version})` : ""}
                    </td>
                  </tr>
                  {job.job_kind === "patent_analysis" && (
                    <tr>
                      <th>인용발명 전달 방식</th>
                      <td>
                        <DeliverySummary
                          plan={job.delivery_plan}
                          provider={job.provider}
                          manifest={job.delivery_manifest}
                          retrieval={job.retrieval_manifest}
                        />
                      </td>
                    </tr>
                  )}
                  <tr>
                    <th>최종 프롬프트</th>
                    <td className="break mono-text">
                      {job.final_prompt_chars.toLocaleString()}자 · sha256{" "}
                      {job.final_prompt_sha256?.slice(0, 16) ?? "-"}…{" "}
                      <a href={`/api/jobs/${job.id}/final-prompt`} target="_blank" rel="noreferrer">
                        보기
                      </a>
                    </td>
                  </tr>
                  <tr>
                    <th>종료</th>
                    <td>
                      exit={String(job.exit_code)} · terminal_reason=
                      {job.terminal_reason ?? "-"} ·{" "}
                      {job.duration_ms ? `${(job.duration_ms / 1000).toFixed(1)}초` : "-"}
                    </td>
                  </tr>
                  {job.usage && (
                    <tr>
                      <th>사용량</th>
                      <td className="mono-text break">{JSON.stringify(job.usage)}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}

    </div>
  );
}
