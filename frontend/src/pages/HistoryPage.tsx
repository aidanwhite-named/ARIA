import { useCallback, useEffect, useState } from "react";

import ReportCompare from "../components/ReportCompare";
import ResultView from "../components/ResultView";
import StatusPill, { ERROR_LABEL } from "../components/StatusPill";
import { api } from "../lib/api";
import type { CitationMapping, HistoryItem, Job, RelationType } from "../lib/types";

function MappingTable({ mapping }: { mapping: CitationMapping }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>인용발명</th>
            <th>고유 문헌번호</th>
            <th>문헌</th>
            <th>sha256</th>
          </tr>
        </thead>
        <tbody>
          {mapping.items.map((item) => (
            <tr key={item.citation_number}>
              <td>인용발명 {item.citation_number}</td>
              <td>{item.document_number}</td>
              <td>{item.filename}</td>
              <td className="mono-text">{item.attachment_sha256.slice(0, 16)}…</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function relationLabel(relation: RelationType | null): string {
  if (relation === "MAPPED") return "종속항 추가 — 번호 유지";
  if (relation === "CONTINUED") return "보고서 수정·보완";
  if (relation === "REANALYZED") return "자료만 물려받음";
  return "";
}

export default function HistoryPage() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [detail, setDetail] = useState<Job | null>(null);
  const [comparing, setComparing] = useState<Job | null>(null);
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

  const remove = async (item: HistoryItem) => {
    // 후속 실행은 첨부와 이전 보고서를 자기 폴더/컬럼에 복제해 두므로 원본이
    // 없어도 온전하다. 함께 지워지지 않는다는 것을 삭제 전에 알려 준다.
    const note =
      item.descendant_count > 0
        ? `\n\n이 실행에서 이어진 후속 분석 ${item.descendant_count}건은 삭제되지 않고 그대로 남습니다.`
        : "";
    if (
      !window.confirm(`이 실행 이력과 작업 폴더를 삭제합니다.${note}\n\n계속할까요?`)
    ) {
      return;
    }
    try {
      await api.deleteHistory(item.id);
      if (detail?.id === item.id) setDetail(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removeThread = async (item: HistoryItem) => {
    try {
      const thread = await api.historyThread(item.id);
      const lines = thread
        .map(
          (t, i) =>
            `${i === 0 ? "원본" : "후속"} · ${new Date(t.created_at).toLocaleString()} · ${t.prompt_name}`,
        )
        .join("\n");
      const ok = window.confirm(
        `아래 ${thread.length}건의 실행 이력과 작업 폴더를 모두 삭제합니다.\n되돌릴 수 없습니다.\n\n${lines}\n\n계속할까요?`,
      );
      if (!ok) return;
      await api.deleteHistoryThread(item.id);
      if (detail && thread.some((t) => t.id === detail.id)) setDetail(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="page page-history">
      <div className="page-head">
        <span className="eyebrow">03 / Audit trail</span>
        <h1>결과보다 먼저, 과정을 남깁니다</h1>
        <p>
          입력의 지문부터 프롬프트 스냅샷, 실행 환경과 결과까지 판단의 근거를 다시 확인합니다.
        </p>
      </div>

      {error && <div className="notice danger">{error}</div>}

      <div className="card no-print history-filter">
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
            <option value="claude">claude</option>
            <option value="codex">codex</option>
            <option value="agy">agy</option>
          </select>
          <button className="btn small" onClick={load}>
            새로고침
          </button>
        </div>
      </div>

      <div className="card history-list">
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
                  <th>계보</th>
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
                      {item.relation_type ? (
                        <>
                          <span
                            className={`pill ${item.relation_type === "CONTINUED" ? "accent" : "warn"}`}
                          >
                            {relationLabel(item.relation_type)}
                          </span>
                          <div className="faint">
                            {item.source_job_label || "원본 미상"}
                          </div>
                        </>
                      ) : (
                        <span className="faint">독립 실행</span>
                      )}
                      {item.descendant_count > 0 && (
                        <div className="faint">후속 {item.descendant_count}건</div>
                      )}
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
                        <button className="btn small danger" onClick={() => remove(item)}>
                          삭제
                        </button>
                        {item.descendant_count > 0 && (
                          <button
                            className="btn small danger"
                            onClick={() => removeThread(item)}
                            title="이 실행과 그로부터 이어진 후속 분석을 모두 삭제합니다."
                          >
                            스레드 삭제 ({item.descendant_count + 1})
                          </button>
                        )}
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
                  {detail.relation_type && (
                    <tr>
                      <th>계보</th>
                      <td>
                        {relationLabel(detail.relation_type)} ·{" "}
                        {detail.source_job_label || "원본 미상"}
                        <div className="faint">
                          원본 실행 id {detail.source_job_id ?? "-"}
                          {detail.prior_claim_text &&
                            ` · 이전 청구항 ${detail.prior_claim_text.length.toLocaleString()}자`}
                          {detail.prior_report
                            ? ` · 이전 보고서 ${detail.prior_report.length.toLocaleString()}자`
                            : " · 이전 보고서 전달 안 함"}
                        </div>
                        <div className="btn-row" style={{ marginTop: 6 }}>
                          <button
                            className="btn small"
                            onClick={() => setComparing(detail)}
                          >
                            원본과 비교
                          </button>
                        </div>
                      </td>
                    </tr>
                  )}
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

            {detail.prior_citation_mapping && (
              <>
                <h3>물려받은 고정 문헌 매핑</h3>
                <MappingTable mapping={detail.prior_citation_mapping} />
              </>
            )}

            {detail.citation_mapping ? (
              <>
                <h3>이 실행이 남긴 문헌 매핑</h3>
                <MappingTable mapping={detail.citation_mapping} />
              </>
            ) : (
              detail.citation_mapping_error && (
                <div className="notice warn">
                  <strong>문헌 매핑을 읽지 못했습니다</strong>
                  <div>{detail.citation_mapping_error}</div>
                  <div>
                    보고서 자체는 정상입니다. 이 실행을 원본으로 삼는 번호 유지 후속
                    분석만 쓸 수 없습니다.
                  </div>
                </div>
              )
            )}

            {detail.followup_instruction && (
              <>
                <h3>사용자 후속 지시</h3>
                <pre className="result-raw" style={{ maxHeight: 160 }}>
                  {detail.followup_instruction}
                </pre>
              </>
            )}

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

      {comparing && (
        <ReportCompare job={comparing} onClose={() => setComparing(null)} />
      )}
    </div>
  );
}
