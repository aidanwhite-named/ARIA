import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ResultView from "../components/ResultView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import type {
  AttachmentAnalysis,
  AttachmentRole,
  Job,
  Prompt,
  ProviderInfo,
  UploadResponse,
} from "../lib/types";
import { useJobStream } from "../lib/useJobStream";

type RunTab = "input" | "result";
const PROVIDER_IDS = new Set(["agy", "claude", "codex"]);

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
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [promptId, setPromptId] = useState("");
  const [providerId, setProviderId] = useState("agy");
  const [model, setModel] = useState("");
  const [claimText, setClaimText] = useState("");
  const [applicationFile, setApplicationFile] = useState<File | null>(null);
  const [citationFiles, setCitationFiles] = useState<File[]>([]);
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [required, setRequired] = useState<Record<string, boolean>>({});
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<RunTab>("input");
  const applicationFileInput = useRef<HTMLInputElement>(null);
  const citationFileInput = useRef<HTMLInputElement>(null);

  const stream = useJobStream(
    job && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.status) ? job.id : null,
  );

  useEffect(() => {
    Promise.all([api.listPrompts(), api.listProviders(), api.settings()])
      .then(([promptList, providerList, appSettings]) => {
        setPrompts(promptList);
        setProviders(providerList);
        const configuredPromptId = appSettings.values.default_prompt_id;
        const fallbackPrompt = promptList.find((p) => p.enabled) ?? promptList[0];
        const configuredPrompt = promptList.find(
          (p) => p.id === configuredPromptId && p.enabled,
        );
        setPromptId(configuredPrompt?.id || fallbackPrompt?.id || "");

        const configuredProvider = appSettings.values.default_provider;
        const nextProvider = PROVIDER_IDS.has(configuredProvider)
          ? configuredProvider
          : "agy";
        setProviderId(nextProvider);
        setModel(appSettings.values.default_models?.[nextProvider] ?? "");
      })
      .catch((e) => setError(String(e.message)));
  }, []);

  // 실행이 끝나면 최종 상태를 다시 읽어 온다.
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

  const selectedPrompt = useMemo(
    () => prompts.find((p) => p.id === promptId) ?? null,
    [prompts, promptId],
  );
  const selectedProvider = useMemo(
    () => providers.find((p) => p.provider === providerId) ?? null,
    [providers, providerId],
  );

  const running = Boolean(
    job && ["QUEUED", "RUNNING"].includes(job.status) && !stream.finished,
  );

  const totalChars = useMemo(() => {
    const attachmentChars =
      upload?.files.reduce(
        (sum, f) => sum + (f.read_ok ? f.char_count + 200 : 0),
        0,
      ) ?? 0;
    return (
      attachmentChars +
      claimText.length +
      (selectedPrompt?.body.length ?? 0)
    );
  }, [upload, claimText, selectedPrompt]);

  const budget = upload?.max_inline_chars ?? 300000;
  const overBudget = totalChars > budget;

  const selectedUploadItems = useMemo(
    () => [
      ...(applicationFile
        ? [{ file: applicationFile, role: "APPLICATION" as const }]
        : []),
      ...citationFiles.map((file) => ({ file, role: "CITATION" as const })),
    ],
    [applicationFile, citationFiles],
  );

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

  const run = async () => {
    if (!promptId) return;
    setSubmitting(true);
    setError("");
    try {
      const preparedUpload = upload ?? (await uploadSelectedFiles());
      const created = await api.createJob({
        claim_text: claimText,
        batch_id: preparedUpload?.batch_id ?? null,
        required_map: required,
      });
      setJob(created);
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

  const reset = () => {
    setJob(null);
    setUpload(null);
    setRequired({});
    setClaimText("");
    setApplicationFile(null);
    setCitationFiles([]);
    setActiveTab("input");
    if (applicationFileInput.current) applicationFileInput.current.value = "";
    if (citationFileInput.current) citationFileInput.current.value = "";
  };

  const displayText = job?.result_text ?? stream.streamText;
  const warnings = job?.warnings ?? stream.warnings;
  const errors = job?.errors ?? stream.errors;

  return (
    <div>
      <div className="page-head">
        <h1>실행</h1>
        <p>
          Settings 에 저장된 Master Prompt, Provider, 모델로 특허 분석을 실행합니다.
        </p>
      </div>

      {error && <div className="notice danger">{error}</div>}

      <div className="run-tabs no-print" role="tablist" aria-label="실행 화면">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "input"}
          className={`run-tab ${activeTab === "input" ? "active" : ""}`}
          onClick={() => setActiveTab("input")}
        >
          전용 입력
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "result"}
          className={`run-tab ${activeTab === "result" ? "active" : ""}`}
          onClick={() => setActiveTab("result")}
        >
          분석 결과
          {running && <span className="spinner" aria-label="실행 중" />}
        </button>
      </div>

      {activeTab === "input" && (
        <>
      <div className="card no-print">
        <h2>1. 특허 분석 전용 입력</h2>
        <div className="run-config-summary">
          <span>
            <strong>Prompt</strong> {selectedPrompt ? `${selectedPrompt.name} (v${selectedPrompt.version})` : "설정 필요"}
          </span>
          <span>
            <strong>Provider</strong> {selectedProvider?.display_name ?? providerId}
          </span>
          <span>
            <strong>모델</strong> {model || "CLI 기본값"}
          </span>
          <a href="#/settings">Settings에서 변경</a>
        </div>
        <div className="patent-input-grid">
          <section className="input-panel application claim-panel">
            <div className="input-panel-head">
              <span className="input-step">A</span>
              <div>
                <strong>출원발명 청구항</strong>
                <div className="hint">분석할 청구항을 그대로 붙여넣으십시오.</div>
              </div>
            </div>
            <textarea
              id="claimText"
              className="claim-input"
              aria-label="출원발명 청구항"
              value={claimText}
              onChange={(e) => setClaimText(e.target.value)}
              placeholder={
                "예: 청구항 1. ...\n\n여러 청구항을 한 번에 입력할 수 있습니다."
              }
              disabled={running}
            />
          </section>

          <section className="input-panel citation">
            <div className="input-panel-head">
              <span className="input-step">B</span>
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
                {citationFiles.map((file, index) => (
                  <div className="selected-file" key={`${file.name}-${index}`}>
                    <span className="pill warn">인용 후보 {index + 1}</span>
                    <span>{file.name}</span>
                    <span className="faint">{formatBytes(file.size)}</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="input-panel application">
            <div className="input-panel-head">
              <span className="input-step">C</span>
              <div>
                <strong>출원발명 문서</strong>
                <div className="hint">명세서 PDF가 있으면 1개를 추가하십시오.</div>
              </div>
            </div>
            <input
              ref={applicationFileInput}
              type="file"
              accept=".pdf,application/pdf"
              aria-label="출원발명 PDF"
              onChange={(e) => {
                setApplicationFile(e.target.files?.[0] ?? null);
                setUpload(null);
                setRequired({});
              }}
              disabled={running || uploading}
            />
            {applicationFile && (
              <div className="selected-file">
                <span className="pill accent">출원발명</span>
                <span>{applicationFile.name}</span>
                <span className="faint">{formatBytes(applicationFile.size)}</span>
              </div>
            )}
          </section>
        </div>

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
                : "선택한 PDF 업로드 및 확인"}
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
              예상 입력 크기 {totalChars.toLocaleString()}자 / 허용{" "}
              {budget.toLocaleString()}자
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
            현재 텍스트 입력 예상 크기 {totalChars.toLocaleString()}자 / 허용{" "}
            {budget.toLocaleString()}자
          </div>
        )}
      </div>

      <div className="card no-print">
        <h2>2. 분석 실행</h2>
        <div className="btn-row">
          <button
            className="btn primary"
            onClick={run}
            disabled={
              running ||
              uploading ||
              submitting ||
              !promptId ||
              !selectedProvider?.usable
            }
          >
            {submitting ? "제출 중…" : "분석 실행"}
          </button>
          <button className="btn danger" onClick={cancel} disabled={!running}>
            중단
          </button>
          {job && !running && (
            <button className="btn" onClick={reset}>
              새 실행
            </button>
          )}
          {running && (
            <span className="faint">
              <span className="spinner" /> {stream.stage || "진행 중"}
            </span>
          )}
        </div>
      </div>

        </>
      )}

      {activeTab === "result" && !job && (
        <div className="card empty">
          <strong>아직 분석 결과가 없습니다.</strong>
          <div>전용 입력 탭에서 자료를 넣고 실행하면 이곳으로 자동 이동합니다.</div>
        </div>
      )}

      {activeTab === "result" && job && (
        <div className="card">
          <div className="split" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>분석 결과</h2>
            <div className="btn-row no-print">
              <StatusPill
                status={job.status}
                quality={job.result_quality}
                errorCode={job.error_code}
              />
              <button className="btn small danger" onClick={cancel} disabled={!running}>
                중단
              </button>
              {job && !running && (
                <button className="btn small" onClick={reset}>
                  새 분석
                </button>
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

          {warnings.length > 0 && (
            <div className="notice warn">
              <strong>경고 {warnings.length}건</strong>
              <ul>
                {warnings.map((message, i) => (
                  <li key={i}>{message}</li>
                ))}
              </ul>
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
              jobId={job.id}
              text={displayText}
              outputMode={job.output_mode}
              streaming={running}
            />
          )}

          <details className="no-print" style={{ marginTop: 16 }}>
            <summary className="faint" style={{ cursor: "pointer" }}>
              실행 정보
            </summary>
            <div className="table-scroll" style={{ marginTop: 10 }}>
              <table>
                <tbody>
                  <tr>
                    <th>Prompt</th>
                    <td>
                      {job.prompt_name} (v{job.prompt_version})
                    </td>
                  </tr>
                  <tr>
                    <th>Provider / 모델</th>
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
