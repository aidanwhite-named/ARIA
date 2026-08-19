import { useCallback, useEffect, useState } from "react";

import ResultView from "../components/ResultView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import type { HistoryItem, Job } from "../lib/types";

export default function HistoryPage() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [detail, setDetail] = useState<Job | null>(null);
  const [finalPrompt, setFinalPrompt] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .history({ status: statusFilter, provider: providerFilter })
      .then(setItems)
      .catch((e) => setError(e.message));
  }, [statusFilter, providerFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const open = async (id: string) => {
    try {
      const job = await api.historyItem(id);
      setDetail(job);
      setFinalPrompt("");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("이 실행 이력과 작업 폴더를 삭제합니다. 계속할까요?")) return;
    try {
      await api.deleteHistory(id);
      if (detail?.id === id) setDetail(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      <div className="page-head">
        <h1>History</h1>
        <p>
          각 실행을 다시 확인하는 데 필요한 정보를 보존합니다. 프롬프트 스냅샷, 최종
          프롬프트 해시, CLI 경로와 버전, 첨부 전달 방식, 경고와 오류가 포함됩니다.
        </p>
      </div>

      {error && <div className="notice danger">{error}</div>}

      <div className="card no-print">
        <div className="btn-row">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ maxWidth: 180 }}>
            <option value="">모든 상태</option>
            <option value="SUCCEEDED">성공</option>
            <option value="FAILED">실패</option>
            <option value="CANCELLED">취소됨</option>
            <option value="RUNNING">실행 중</option>
          </select>
          <select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)} style={{ maxWidth: 180 }}>
            <option value="">모든 Provider</option>
            <option value="mock">mock</option>
            <option value="claude">claude</option>
            <option value="codex">codex</option>
            <option value="gemini">gemini</option>
          </select>
          <button className="btn small" onClick={load}>
            새로고침
          </button>
        </div>
      </div>

      <div className="card">
        {items.length === 0 ? (
          <div className="empty">실행 이력이 없습니다.</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>실행 시각</th>
                  <th>상태</th>
                  <th>Prompt</th>
                  <th>Provider</th>
                  <th>첨부</th>
                  <th>소요</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>{new Date(item.created_at).toLocaleString()}</td>
                    <td>
                      <StatusPill
                        status={item.status}
                        quality={item.result_quality}
                        errorCode={item.error_code}
                      />
                    </td>
                    <td>
                      {item.prompt_name}
                      {item.prompt_version ? ` (v${item.prompt_version})` : ""}
                    </td>
                    <td>
                      {item.provider}
                      {item.model ? ` / ${item.model}` : ""}
                    </td>
                    <td>{item.attachment_count}</td>
                    <td>
                      {item.duration_ms ? `${(item.duration_ms / 1000).toFixed(1)}초` : "-"}
                    </td>
                    <td>
                      <div className="btn-row">
                        <button className="btn small" onClick={() => open(item.id)}>
                          상세
                        </button>
                        <button className="btn small danger" onClick={() => remove(item.id)}>
                          삭제
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {detail && (
        <div className="modal-backdrop no-print" onClick={() => setDetail(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="split" style={{ marginBottom: 14 }}>
              <h2 style={{ margin: 0 }}>{detail.prompt_name}</h2>
              <StatusPill
                status={detail.status}
                quality={detail.result_quality}
                errorCode={detail.error_code}
              />
            </div>

            {detail.error_code && (
              <div className="notice danger">
                <strong>{ERROR_LABEL[detail.error_code] ?? detail.error_code}</strong>
                <ul>
                  {detail.errors.map((message, i) => (
                    <li key={i}>{message}</li>
                  ))}
                </ul>
              </div>
            )}
            {detail.warnings.length > 0 && (
              <div className="notice warn">
                <strong>경고</strong>
                <ul>
                  {detail.warnings.map((message, i) => (
                    <li key={i}>{message}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="table-scroll">
              <table>
                <tbody>
                  <tr>
                    <th>실행 시각</th>
                    <td>{new Date(detail.created_at).toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th>Provider / 모델</th>
                    <td>
                      {detail.provider} / {detail.model ?? "기본값"}
                    </td>
                  </tr>
                  <tr>
                    <th>CLI 경로 / 버전</th>
                    <td className="break mono-text">
                      {detail.cli_path ?? "-"}
                      <br />
                      {detail.cli_version ?? "-"}
                    </td>
                  </tr>
                  <tr>
                    <th>CLI 인수</th>
                    <td className="break mono-text">
                      {detail.cli_args.join(" ") || "-"}
                    </td>
                  </tr>
                  <tr>
                    <th>최종 프롬프트</th>
                    <td className="break mono-text">
                      {detail.final_prompt_chars.toLocaleString()}자 · sha256{" "}
                      {detail.final_prompt_sha256 ?? "-"}
                    </td>
                  </tr>
                  <tr>
                    <th>종료</th>
                    <td>
                      exit={String(detail.exit_code)} · {detail.terminal_reason ?? "-"} ·{" "}
                      {detail.duration_ms
                        ? `${(detail.duration_ms / 1000).toFixed(1)}초`
                        : "-"}
                    </td>
                  </tr>
                  <tr>
                    <th>전처리 도구</th>
                    <td className="mono-text">
                      {Object.entries(detail.preprocessing_versions)
                        .map(([k, v]) => `${k} ${v}`)
                        .join(", ") || "-"}
                    </td>
                  </tr>
                  {detail.usage && (
                    <tr>
                      <th>사용량</th>
                      <td className="mono-text break">{JSON.stringify(detail.usage)}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {detail.attachments.length > 0 && (
              <>
                <h3>첨부 자료</h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>파일</th>
                        <th>필수</th>
                        <th>전달 방식</th>
                        <th>추출</th>
                        <th>sha256</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.attachments.map((a) => (
                        <tr key={a.attachment_id}>
                          <td>
                            {a.original_filename}
                            {a.page_count ? ` (${a.page_count}p)` : ""}
                            {a.error && (
                              <div className="faint" style={{ color: "var(--danger)" }}>
                                {a.error}
                              </div>
                            )}
                          </td>
                          <td>{a.required ? "필수" : "선택"}</td>
                          <td>
                            <span className={`pill ${a.read_ok ? "ok" : "danger"}`}>
                              {a.delivery_mode}
                            </span>
                          </td>
                          <td>{a.extraction_method}</td>
                          <td className="mono-text">{a.sha256.slice(0, 16)}…</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            <h3>사용한 Master Prompt (실행 시점 스냅샷)</h3>
            <pre className="result-raw" style={{ maxHeight: 220 }}>
              {detail.prompt_snapshot}
            </pre>

            <div className="btn-row" style={{ margin: "12px 0" }}>
              <button
                className="btn small"
                onClick={() =>
                  api.finalPrompt(detail.id).then(setFinalPrompt).catch(() => undefined)
                }
              >
                최종 프롬프트 원문 보기
              </button>
            </div>
            {finalPrompt && (
              <pre className="result-raw" style={{ maxHeight: 300 }}>
                {finalPrompt}
              </pre>
            )}

            {detail.result_text && (
              <>
                <h3>결과</h3>
                <ResultView
                  jobId={detail.id}
                  text={detail.result_text}
                  outputMode={detail.output_mode}
                />
              </>
            )}

            <div className="btn-row" style={{ marginTop: 16 }}>
              <button className="btn" onClick={() => setDetail(null)}>
                닫기
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
