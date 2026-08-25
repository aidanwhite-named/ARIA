import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import GapSearchPanel from "../components/GapSearchPanel";
import ResultView from "../components/ResultView";
import SearchManifestView from "../components/SearchManifestView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import { useRunSession } from "../lib/runSession";
import type {
  AttachmentAnalysis,
  AttachmentRole,
  Job,
  JobKind,
  Prompt,
  ProviderInfo,
  RelationType,
} from "../lib/types";

const PROVIDER_IDS = new Set(["agy", "claude", "codex"]);

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
  const version = job.prompt_version ? ` v${job.prompt_version}` : "";
  return `${stamp} · ${job.prompt_name}${version}`;
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

export default function RunPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const historyJobId = searchParams.get("job");
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [promptId, setPromptId] = useState("");
  // 빈 문자열 = 지정 안 함. 제한된 안전성 Provider 가 자동으로 선택되면
  // 사용자가 위험을 확인하지 않은 채 실행하게 된다.
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  // 환경설정의 「모델에 전달할 최대 글자 수」. 화면에 상수를 박아 두면 설정을
  // 바꿔도 옛 숫자가 남아, 사용자가 틀린 한도를 믿고 입력을 줄이게 된다.
  // 아직 못 읽었으면 null 로 두고 짐작한 숫자를 보여 주지 않는다.
  const [inlineCharBudget, setInlineCharBudget] = useState<number | null>(null);
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
  } = useRunSession();

  useEffect(() => {
    Promise.all([api.listPrompts(), api.listProviders(), api.settings()])
      .then(([promptList, providerList, appSettings]) => {
        setPrompts(promptList);
        setProviders(providerList);
        setInlineCharBudget(appSettings.values.max_inline_chars);
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
        setJob(storedJob);
        setLineage(null);
        setFollowupInstruction("");
        // 어떤 종류의 실행을 열었는지에 따라 준비 화면도 그 모드로 맞춘다.
        setJobKind(storedJob.job_kind);
        if (storedJob.job_kind === "similarity_search") {
          setSearchClaimText(storedJob.claim_text);
        } else {
          setClaimText(storedJob.claim_text);
        }
        setUpload(null);
        setRequired({});
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
  }, [historyJobId]);

  const selectedPrompt = useMemo(
    () => prompts.find((p) => p.id === promptId) ?? null,
    [prompts, promptId],
  );
  const selectedProvider = useMemo(
    () => providers.find((p) => p.provider === providerId) ?? null,
    [providers, providerId],
  );

  const searching = jobKind === "similarity_search";
  const jobKindLabel = JOB_KIND_LABEL[jobKind];
  const searchAvailable = supportsSearch(selectedProvider);
  const detectOnlySearch =
    searching && selectedProvider?.capabilities?.search_tool_control === "detect_only";
  const eligibleGapComponents = useMemo(
    () =>
      job?.job_kind === "patent_analysis"
        ? (job.analysis_manifest?.items ?? []).filter((item) => item.search_eligible)
        : [],
    [job],
  );

  const totalChars = useMemo(() => {
    const newAttachmentChars =
      upload?.files.reduce(
        (sum, f) => sum + (f.read_ok ? f.char_count + 200 : 0),
        0,
      ) ?? 0;
    // 물려받는 첨부도 자식 실행의 프롬프트에 그대로 다시 들어간다.
    const inheritedChars =
      lineage?.inheritedAttachments.reduce(
        (sum, f) => sum + (f.read_ok ? f.char_count + 200 : 0),
        0,
      ) ?? 0;
    return (
      newAttachmentChars +
      inheritedChars +
      claimText.length +
      followupInstruction.length +
      (lineage?.priorClaimChars ?? 0) +
      (lineage?.priorReportChars ?? 0) +
      (selectedPrompt?.body.length ?? 0)
    );
  }, [upload, lineage, claimText, followupInstruction, selectedPrompt]);

  // 업로드를 마쳤으면 서버가 그 응답에 실어 준 값이 가장 최신이다. 아직
  // 안 올렸으면 화면을 열 때 읽어 둔 설정값을 쓴다.
  const budget = upload?.max_inline_chars ?? inlineCharBudget;
  const overBudget = budget !== null && totalChars > budget;

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
      setUpload(response);
      const map: Record<string, boolean> = {};
      response.files.forEach((f) => {
        map[f.attachment_id] = true;
      });
      setRequired(map);
      return response;
    } catch (e) {
      throw e;
    } finally {
      setUploading(false);
    }
  }, [selectedUploadItems]);

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
        claim_text: searchClaimText,
        batch_id: prepared?.batch_id ?? null,
      });
      setJob(created);
      navigate("/run", { replace: true });
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
        source_job_id: job.id,
        search_component_ids: selectedGapIds,
      });
      setJobKind("similarity_search");
      setSearchClaimText(created.claim_text);
      setJob(created);
      setGapSearchOpen(false);
      setSelectedGapIds([]);
      setActiveTab("result");
      navigate("/run", { replace: true });
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
    setSubmitting(true);
    setError("");
    try {
      const preparedUpload = upload ?? (await uploadSelectedFiles());
      const preparedAttachmentCount =
        (preparedUpload?.files.length ?? 0) + inheritedAnalysisAttachmentCount;
      if (preparedAttachmentCount === 0) {
        setError(
          "구성대비 분석에 사용할 수 있는 인용발명 문헌이 없습니다. 선택한 PDF의 처리 결과를 확인하십시오.",
        );
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
        required_map: required,
        source_job_id: lineage?.sourceJobId ?? null,
        relation_type: lineage?.relationType ?? null,
        followup_instruction: lineage ? followupInstruction : "",
      });
      setJob(created);
      navigate("/run", { replace: true });
      // 이 batch 는 방금 만든 작업에 귀속됐다. 그대로 다시 보내면 백엔드가
      // "이미 다른 작업에 사용된 업로드입니다" 로 거절한다. 고른 File 자체는
      // 남겨 두므로, 청구항만 고쳐 다시 실행하면 새 batch 로 다시 올라간다.
      setUpload(null);
      setRequired({});
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
    setRequired({});
    if (citationFileInput.current) citationFileInput.current.value = "";
  };

  const clearSelectedFiles = () => {
    setUpload(null);
    setRequired({});
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
    navigate("/run", { replace: true });
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
      inheritedAttachments: job.attachments,
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

  return (
    <div className="page page-run" data-mode={jobKind}>
      {error && <div className="notice danger">{error}</div>}

      {!providerId && (
        <div className="notice danger">
          <strong>실행할 AI 도구가 지정되지 않았습니다</strong>
          <div style={{ marginTop: 4 }}>
            <a href="#/settings">환경 설정</a>에서 사용할 도구를 지정하십시오.
          </div>
        </div>
      )}

      <div className="run-tabs no-print" role="tablist" aria-label="실행 화면">
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

      {activeTab === "input" && (
        <>
      <div className="card no-print run-input-card">
        <h2 className="card-head-mode">
          {searching ? "검색 준비" : "분석 자료 준비"}
          <span className="mode-chip">
            <i aria-hidden="true" />
            {jobKindLabel}
          </span>
        </h2>

        <div className="run-config-summary">
          <span>
            <strong>프롬프트</strong>{" "}
            {searching
              ? "검색 프롬프트 (search_prompt.md)"
              : selectedPrompt
                ? `${selectedPrompt.name} (v${selectedPrompt.version})`
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

            <section className="input-panel claim-panel search-panel-input">
              <div className="input-panel-head">
                <span className="input-step">1</span>
                <div>
                  <strong>검색할 청구항</strong>
                  <div className="hint">
                    청구항 전문을 그대로 붙여넣으십시오. 입력한 문장은 실행 지시가
                    아니라 검색 대상 데이터로만 전달됩니다. 검색 범위는 여기 적은
                    청구항이 정합니다.
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

            <section className="input-panel application search-spec-panel">
              <div className="input-panel-head">
                <span className="input-step">2</span>
                <div>
                  <strong>출원발명 문서 (선택)</strong>
                  <div className="hint">
                    명세서 PDF 를 1건 넣으면 청구항 단독 검색과 완전히 분리된 확장
                    검색을 추가로 실행합니다. 가능한 의미·동의어·영문 용어를 넓힌
                    뒤 두 후보 집합을 합집합으로 병합합니다.
                  </div>
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
                      {searchSpec.page_count
                        ? ` · ${searchSpec.page_count}페이지`
                        : ""}{" "}
                      · 청구항 {searchClaimText.trim().length.toLocaleString()}자
                      {specOutweighsClaim && (
                        <div style={{ marginTop: 4 }}>
                          명세서가 청구항보다 훨씬 깁니다. 청구항 단독 검색에는
                          영향이 없지만 보조 검색의 용어 확장이 실시예에 쏠릴 수
                          있으니, 결과의 「출원발명 문서를 이용한 별도 검색 확장」
                          절을 확인하십시오.
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      <strong>본문을 읽지 못했습니다</strong>
                      <div style={{ marginTop: 4 }}>
                        {searchSpec.error ?? "알 수 없는 오류"} — 이 상태로는
                        실행이 거절됩니다. 텍스트가 들어 있는 PDF 로 바꾸거나
                        명세서를 빼고 검색하십시오.
                      </div>
                    </>
                  )}
                </div>
              )}
            </section>

            <div className="notice info" style={{ marginTop: 12 }}>
              <strong>실행 방식</strong>
              <ul className="lineage-inherits">
                <li>
                  {detectOnlySearch
                    ? "agy의 search_web와 read_url_content만 정상 호출로 인정합니다. 다른 실제 도구 호출은 탐지되는 즉시 작업을 실패 처리합니다."
                    : "웹 검색과 페이지 열람(WebSearch, WebFetch)만 허용합니다. 파일 읽기·쓰기와 명령 실행은 허용하지 않으며, 다른 도구가 호출되면 실행이 실패합니다."}
                </li>
                <li>
                  각 독립 검색의 확장은 최대 2라운드입니다. 검색을 한 번도 하지
                  않고 작성된 답변은 실패로 처리합니다.
                </li>
                <li>
                  페이지 열람 결과는 원문이 아니라 요약입니다. 후보의 발췌와 대응
                  관계는 <strong>원문에서 직접 확인해야</strong> 합니다.
                </li>
                {searchSpecFile && (
                  <li>
                    먼저 명세서가 전혀 없는 컨텍스트에서 청구항 단독 검색을
                    실행합니다. 그 다음 명세서 보조 검색을 별도로 실행하고 후보를
                    합집합으로 병합하므로 단독 검색 후보는 삭제되지 않습니다.{" "}
                    <strong>
                      명세서에서 얻은 대안 의미·추가 검색어·사용하지 않은 한정은
                      결과 보고서에 표시됩니다.
                    </strong>
                  </li>
                )}
              </ul>
            </div>
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
                setRequired({});
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

        <div className="btn-row file-prepare-row">
          <button
            type="button"
            className="btn"
            onClick={prepareFiles}
            disabled={
              running || uploading || selectedUploadItems.length === 0 || Boolean(upload)
            }
          >
            {upload
              ? "PDF 처리 완료"
              : uploading
                ? "자료 처리 중…"
                : "선택한 PDF 미리 확인"}
          </button>
          <span className="hint">
            실행 버튼을 눌러도 아직 처리하지 않은 PDF는 자동으로 업로드됩니다.
          </span>
        </div>

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
                      checked={required[file.attachment_id] ?? false}
                      onChange={(e) =>
                        setRequired((prev) => ({
                          ...prev,
                          [file.attachment_id]: e.target.checked,
                        }))
                      }
                      disabled={running}
                    />
                    필수
                  </label>
                </div>
              );
            })}

            <div
              className={`notice ${overBudget ? "danger" : "info"}`}
              style={{ marginTop: 12 }}
            >
              예상 입력 크기 {totalChars.toLocaleString()}자 /{" "}
              {budget === null ? "허용 한도 확인 중" : `허용 ${budget.toLocaleString()}자`}
              {overBudget &&
                " — 예산을 초과하면 실행이 INPUT_TOO_LARGE 로 중단됩니다. ARIA 는 내용을 임의로 자르거나 요약하지 않습니다."}
            </div>
          </div>
        )}

        {!upload && (
          <div
            className={`notice ${overBudget ? "danger" : "info"}`}
            style={{ marginTop: 12 }}
          >
            현재 텍스트 입력 예상 크기 {totalChars.toLocaleString()}자 /{" "}
            {budget === null ? "허용 한도 확인 중" : `허용 ${budget.toLocaleString()}자`}
          </div>
        )}
          </>
        )}
      </div>

      <div className="card no-print run-action-card">
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

        <div className="btn-row">
          <button
            className="btn primary"
            onClick={run}
            disabled={
              running ||
              uploading ||
              submitting ||
              !providerId ||
              !selectedProvider?.usable ||
              (searching
                ? !searchClaimText.trim() || !searchAvailable
                : !promptId || !claimText.trim() || !hasAnalysisAttachments)
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
            </span>
          )}
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

          <details className="no-print" style={{ marginTop: 16 }}>
            <summary className="faint" style={{ cursor: "pointer" }}>
              실행 정보
            </summary>
            <div className="table-scroll" style={{ marginTop: 10 }}>
              <table>
                <tbody>
                  <tr>
                    <th>프롬프트</th>
                    <td>
                      {job.prompt_name} (v{job.prompt_version})
                    </td>
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
