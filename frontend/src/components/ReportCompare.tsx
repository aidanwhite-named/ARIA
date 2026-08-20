import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import type { Job } from "../lib/types";

/** 번호 유지 후속 분석은 이전 보고서를 모델에게 주지 않는다. 그래서 보고서가
 *  「이전 분석과 달라진 부분」을 스스로 적을 수 없다. 그 자리를 이 화면이 메운다.
 *
 *  ARIA 는 두 보고서를 나란히 놓고 줄 단위로 어디가 다른지만 표시한다. 무엇이
 *  중요한 차이인지는 판단하지 않는다. 판단은 모델이, 대조는 사람이 한다.
 */

type Op = "same" | "added" | "removed";

interface Row {
  op: Op;
  left: string | null;
  right: string | null;
}

/** 최장 공통 부분수열. 보고서 길이가 수천 줄을 넘지 않으므로 표 전체를 채운다. */
function diffLines(before: string[], after: string[]): Row[] {
  const n = before.length;
  const m = after.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      lcs[i][j] =
        before[i] === after[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const rows: Row[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      rows.push({ op: "same", left: before[i], right: after[j] });
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ op: "removed", left: before[i], right: null });
      i += 1;
    } else {
      rows.push({ op: "added", left: null, right: after[j] });
      j += 1;
    }
  }
  while (i < n) {
    rows.push({ op: "removed", left: before[i], right: null });
    i += 1;
  }
  while (j < m) {
    rows.push({ op: "added", left: null, right: after[j] });
    j += 1;
  }
  return rows;
}

function toLines(text: string | null): string[] {
  return (text ?? "").replace(/\r\n/g, "\n").split("\n");
}

interface Props {
  job: Job;
  onClose: () => void;
}

export default function ReportCompare({ job, onClose }: Props) {
  const [source, setSource] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [onlyChanges, setOnlyChanges] = useState(true);

  useEffect(() => {
    if (!job.source_job_id) {
      setError("이 실행에는 비교할 원본이 없습니다.");
      return;
    }
    let cancelled = false;
    api
      .historyItem(job.source_job_id)
      .then((fresh) => {
        if (!cancelled) setSource(fresh);
      })
      .catch(() =>
        setError(
          "원본 실행을 찾지 못했습니다. 이력에서 삭제되었을 수 있습니다. " +
            "이 보고서 자체는 그대로 남아 있습니다.",
        ),
      );
    return () => {
      cancelled = true;
    };
  }, [job.source_job_id]);

  const rows = useMemo(() => {
    if (!source) return [];
    return diffLines(toLines(source.result_text), toLines(job.result_text));
  }, [source, job.result_text]);

  const changed = rows.filter((row) => row.op !== "same").length;
  const visible = onlyChanges ? rows.filter((row) => row.op !== "same") : rows;

  return (
    <div className="modal-backdrop no-print" onClick={onClose}>
      <div className="modal compare-modal" onClick={(e) => e.stopPropagation()}>
        <div className="split" style={{ marginBottom: 10 }}>
          <h2 style={{ margin: 0 }}>원본 보고서와 비교</h2>
          <button className="btn small" onClick={onClose}>
            닫기
          </button>
        </div>

        {error && <div className="notice danger">{error}</div>}

        {source && (
          <>
            <div className="notice info">
              ARIA 는 두 보고서에서 <strong>줄이 다른 곳만</strong> 표시합니다. 어느 쪽이
              옳은지, 어떤 차이가 중요한지는 판단하지 않습니다. 직접 확인하십시오.
            </div>

            <div className="split compare-head">
              <div>
                <span className="pill">원본</span> {new Date(source.created_at).toLocaleString()}
                <div className="faint">
                  {source.prompt_name}
                  {source.prompt_version ? ` v${source.prompt_version}` : ""} ·{" "}
                  {source.provider}
                  {source.model ? ` / ${source.model}` : ""}
                </div>
              </div>
              <div>
                <span className="pill accent">이번</span>{" "}
                {new Date(job.created_at).toLocaleString()}
                <div className="faint">
                  {job.prompt_name}
                  {job.prompt_version ? ` v${job.prompt_version}` : ""} · {job.provider}
                  {job.model ? ` / ${job.model}` : ""}
                </div>
              </div>
            </div>

            <div className="btn-row" style={{ margin: "10px 0" }}>
              <span className="faint">
                다른 줄 {changed.toLocaleString()}개 / 전체 {rows.length.toLocaleString()}줄
              </span>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={onlyChanges}
                  onChange={(e) => setOnlyChanges(e.target.checked)}
                />
                다른 줄만 보기
              </label>
            </div>

            {changed === 0 ? (
              <div className="empty">두 보고서의 본문이 완전히 같습니다.</div>
            ) : (
              <div className="table-scroll compare-scroll">
                <table className="compare-table">
                  <thead>
                    <tr>
                      <th>원본</th>
                      <th>이번</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((row, index) => (
                      <tr key={index} className={`compare-${row.op}`}>
                        <td>{row.left ?? ""}</td>
                        <td>{row.right ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
