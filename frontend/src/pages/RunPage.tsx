import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ResultView from "../components/ResultView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import type {
  AttachmentAnalysis,
  Job,
  Prompt,
  ProviderInfo,
  UploadResponse,
} from "../lib/types";
import { useJobStream } from "../lib/useJobStream";

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

export default function RunPage() {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [promptId, setPromptId] = useState("");
  const [providerId, setProviderId] = useState("mock");
  const [model, setModel] = useState("");
  const [userInput, setUserInput] = useState("");
  const [upload, setUpload] = useState<UploadResponse | null>(null);
  const [required, setRequired] = useState<Record<string, boolean>>({});
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const stream = useJobStream(
    job && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.status) ? job.id : null,
  );

  useEffect(() => {
    api.listPrompts().then(setPrompts).catch((e) => setError(String(e.message)));
    api.listProviders().then(setProviders).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!promptId && prompts.length > 0) {
      const first = prompts.find((p) => p.enabled) ?? prompts[0];
      setPromptId(first.id);
      if (first.default_provider) setProviderId(first.default_provider);
      if (first.default_model) setModel(first.default_model);
    }
  }, [prompts, promptId]);

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
    return attachmentChars + userInput.length + (selectedPrompt?.body.length ?? 0);
  }, [upload, userInput, selectedPrompt]);

  const budget = upload?.max_inline_chars ?? 300000;
  const overBudget = totalChars > budget;

  const handleFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      const response = await api.upload(Array.from(files));
      setUpload(response);
      const map: Record<string, boolean> = {};
      response.files.forEach((f) => {
        map[f.attachment_id] = f.read_ok;
      });
      setRequired(map);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }, []);

  const run = async () => {
    if (!promptId) return;
    setSubmitting(true);
    setError("");
    try {
      const created = await api.createJob({
        prompt_id: promptId,
        provider: providerId,
        model: model.trim() || null,
        user_input: userInput,
        batch_id: upload?.batch_id ?? null,
        required_map: required,
      });
      setJob(created);
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
    setUserInput("");
  };

  const displayText = job?.result_text ?? stream.streamText;
  const warnings = job?.warnings ?? stream.warnings;
  const errors = job?.errors ?? stream.errors;

  return (
    <div>
      <div className="page-head">
        <h1>실행</h1>
        <p>
          선택한 Master Prompt 를 선택한 Provider 에서 실행합니다. 업무 로직은
          전부 Master Prompt 안에 있으며 ARIA 는 어떤 지시도 추가하지 않습니다.
        </p>
      </div>

      {error && <div className="notice danger">{error}</div>}

      <div className="card no-print">
        <h2>1. 프롬프트와 Provider</h2>
        <div className="card-row">
          <div className="field">
            <label htmlFor="prompt">Master Prompt</label>
            <select
              id="prompt"
              value={promptId}
              onChange={(e) => setPromptId(e.target.value)}
              disabled={running}
            >
              {prompts.length === 0 && <option value="">프롬프트가 없습니다</option>}
              {prompts.map((p) => (
                <option key={p.id} value={p.id} disabled={!p.enabled}>
                  {p.name} (v{p.version}){p.enabled ? "" : " · 비활성"}
                </option>
              ))}
            </select>
            {selectedPrompt && (
              <span className="hint">
                출력 형식: {selectedPrompt.output_mode} ·{" "}
                {selectedPrompt.description || "설명 없음"}
              </span>
            )}
          </div>

          <div className="field">
            <label htmlFor="provider">Provider</label>
            <select
              id="provider"
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              disabled={running}
            >
              {providers.map((p) => (
                <option key={p.provider} value={p.provider} disabled={!p.usable}>
                  {p.display_name}
                  {p.usable ? "" : " · 사용 불가"}
                </option>
              ))}
            </select>
            {selectedProvider && !selectedProvider.usable && (
              <span className="hint" style={{ color: "var(--danger)" }}>
                {selectedProvider.auth_state === "NOT_LOGGED_IN"
                  ? "CLI 에 로그인되어 있지 않습니다. Settings 에서 안내를 확인하십시오."
                  : "이 Provider 는 현재 사용할 수 없습니다."}
              </span>
            )}
          </div>

          <div className="field">
            <label htmlFor="model">모델 (비우면 기본값)</label>
            <input
              id="model"
              type="text"
              value={model}
              placeholder="예: sonnet"
              onChange={(e) => setModel(e.target.value)}
              disabled={running}
            />
          </div>
        </div>
      </div>

      <div className="card no-print">
        <h2>2. 추가 입력과 첨부 자료</h2>
        <div className="field">
          <label htmlFor="userInput">사용자 추가 입력 (선택)</label>
          <textarea
            id="userInput"
            value={userInput}
            onChange={(e) => setUserInput(e.target.value)}
            placeholder="Master Prompt 에 더해 전달할 내용을 입력합니다."
            disabled={running}
          />
        </div>

        <div className="field">
          <label>첨부 파일</label>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".txt,.md,.markdown,.json,.csv,.pdf"
            onChange={(e) => handleFiles(e.target.files)}
            disabled={running || uploading}
          />
          <span className="hint">
            지원 형식: TXT, MD, JSON, CSV, 텍스트 레이어가 있는 PDF. ARIA 가 텍스트로
            정규화해서 프롬프트에 직접 넣습니다. 모델이 파일을 읽을지 여부에 의존하지
            않습니다.
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
                    <div className="file-name">{file.original_filename}</div>
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
      </div>

      <div className="card no-print">
        <div className="btn-row">
          <button
            className="btn primary"
            onClick={run}
            disabled={
              running || submitting || !promptId || !selectedProvider?.usable
            }
          >
            {submitting ? "제출 중…" : "실행"}
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

      {job && (
        <div className="card">
          <div className="split" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>실행 결과</h2>
            <StatusPill
              status={job.status}
              quality={job.result_quality}
              errorCode={job.error_code}
            />
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
