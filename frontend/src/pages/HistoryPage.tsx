import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import StatusPill from "../components/StatusPill";
import { api } from "../lib/api";
import { DELIVERY_LABEL, isNarrowed } from "../lib/types";
import type { HistoryItem, JobKind, RelationType } from "../lib/types";

/** 두 축은 결과물이 다르다. 목록에서도 한 눈에 갈라 보이게 한다. */
const KIND_LABEL: Record<JobKind, string> = {
  patent_analysis: "구성대비 분석",
  similarity_search: "유사문헌 검색",
};

function relationLabel(relation: RelationType | null): string {
  if (relation === "MAPPED") return "종속항 추가 — 번호 유지";
  if (relation === "CONTINUED") return "보고서 수정·보완";
  if (relation === "REANALYZED") return "같은 자료로 재분석";
  return "";
}

export default function HistoryPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [kindFilter, setKindFilter] = useState<"" | JobKind>("");
  const [statusFilter, setStatusFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [error, setError] = useState("");
  const [deletingAll, setDeletingAll] = useState(false);

  const load = useCallback(() => {
    api
      .history({ status: statusFilter, provider: providerFilter })
      .then(setItems)
      .catch((e) => setError(e.message));
  }, [statusFilter, providerFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const visible = kindFilter
    ? items.filter((item) => item.job_kind === kindFilter)
    : items;

  const open = (id: string) => {
    navigate(`/run?job=${encodeURIComponent(id)}`);
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
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const removeAll = async () => {
    const ok = window.confirm(
      "모든 실행 이력과 실행 폴더에 저장된 파일을 한 번에 삭제합니다.\n되돌릴 수 없습니다.\n\n계속할까요?",
    );
    if (!ok) return;

    setDeletingAll(true);
    setError("");
    try {
      await api.deleteAllHistory();
      setItems([]);
    } catch (e) {
      setError((e as Error).message);
      load();
    } finally {
      setDeletingAll(false);
    }
  };

  return (
    <div className="page page-history">
      <div className="page-head">
        <span className="eyebrow">04 / 실행 기록</span>
        <h1>분석 결과와 과정을 확인합니다</h1>
        <p>
          입력의 지문부터 프롬프트 스냅샷, 실행 환경과 결과까지 판단의 근거를 다시 확인합니다.
        </p>
      </div>

      {error && <div className="notice danger">{error}</div>}

      <div className="card no-print history-filter">
        <h2>기록 필터</h2>
        <div className="btn-row">
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as "" | JobKind)}
            aria-label="작업 유형"
            style={{ maxWidth: 180 }}
          >
            <option value="">두 작업 모두</option>
            <option value="patent_analysis">구성대비 분석</option>
            <option value="similarity_search">유사문헌 검색</option>
          </select>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ maxWidth: 180 }}>
            <option value="">모든 상태</option>
            <option value="SUCCEEDED">성공</option>
            <option value="FAILED">실패</option>
            <option value="CANCELLED">취소됨</option>
            <option value="RUNNING">실행 중</option>
          </select>
          <select value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)} style={{ maxWidth: 180 }}>
            <option value="">모든 실행 도구</option>
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
        <div className="split history-list-head">
          <h2>실행 목록</h2>
          <button
            type="button"
            className="btn small danger"
            onClick={removeAll}
            disabled={deletingAll}
          >
            {deletingAll ? "전체 삭제 중…" : "전체 삭제"}
          </button>
        </div>
        {visible.length === 0 ? (
          <div className="empty">
            {kindFilter
              ? `${KIND_LABEL[kindFilter]} 실행 이력이 없습니다.`
              : "실행 이력이 없습니다."}
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>실행 시각</th>
                  <th>작업</th>
                  <th>상태</th>
                  <th>프롬프트</th>
                  <th>계보</th>
                  <th>실행 도구</th>
                  <th>첨부</th>
                  <th>소요</th>
                  <th>관리</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((item) => (
                  <tr
                    key={item.id}
                    className="history-row"
                    tabIndex={0}
                    aria-label={`${new Date(item.created_at).toLocaleString()} ${item.prompt_name} 결과 보기`}
                    onClick={() => open(item.id)}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return;
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        open(item.id);
                      }
                    }}
                  >
                    <td>{new Date(item.created_at).toLocaleString()}</td>
                    <td>
                      <span
                        className={`pill ${item.job_kind === "similarity_search" ? "warn" : "accent"}`}
                      >
                        {KIND_LABEL[item.job_kind] ?? item.job_kind}
                      </span>
                    </td>
                    <td>
                      <StatusPill status={item.status} errorCode={item.error_code} />
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
                    <td>
                      {item.attachment_count}
                      {isNarrowed(item.delivery_plan) && (
                        <div
                          className="faint"
                          title="인용발명 문헌 전체가 아니라 검색으로 확인한 구간과 그 페이지 전문만 전달했습니다."
                        >
                          {DELIVERY_LABEL[item.delivery_plan]}
                        </div>
                      )}
                    </td>
                    <td>
                      {item.duration_ms ? `${(item.duration_ms / 1000).toFixed(1)}초` : "-"}
                    </td>
                    <td>
                      <div className="btn-row">
                        <button
                          className="btn small danger"
                          onClick={(event) => {
                            event.stopPropagation();
                            void remove(item);
                          }}
                        >
                          삭제
                        </button>
                        {item.descendant_count > 0 && (
                          <button
                            className="btn small danger"
                            onClick={(event) => {
                              event.stopPropagation();
                              void removeThread(item);
                            }}
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
    </div>
  );
}
