import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import type { Prompt, PromptVersion } from "../lib/types";

const BLANK = {
  name: "",
  description: "",
  body: "",
  output_mode: "markdown" as "markdown" | "text",
  tags: [] as string[],
};

type Draft = typeof BLANK & { id?: string };

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [versions, setVersions] = useState<PromptVersion[] | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const importInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api
      .listPrompts({ search })
      .then(setPrompts)
      .catch((e) => setError(e.message));
  }, [search]);

  useEffect(() => {
    load();
  }, [load]);

  const notify = (text: string) => {
    setMessage(text);
    setError("");
    setTimeout(() => setMessage(""), 2600);
  };

  const save = async () => {
    if (!draft) return;
    try {
      if (draft.id) {
        await api.updatePrompt(draft.id, {
          name: draft.name,
          description: draft.description,
          body: draft.body,
          output_mode: draft.output_mode,
          tags: draft.tags,
        });
        notify("prompt 폴더에 저장했습니다. 본문이 바뀌었으면 버전이 올라갑니다.");
      } else {
        await api.createPrompt(draft);
        notify("prompt 폴더에 새 프롬프트 파일을 만들었습니다.");
      }
      setDraft(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const act = async (fn: () => Promise<unknown>, text: string) => {
    try {
      await fn();
      notify(text);
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const exportAll = async () => {
    const data = await api.exportPrompts();
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "aria-prompts.json";
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const items = Array.isArray(parsed) ? parsed : parsed.prompts;
      if (!Array.isArray(items)) throw new Error("prompts 배열을 찾을 수 없습니다.");
      const result = await api.importPrompts(items, false);
      notify(`가져오기 완료: 생성 ${result.created}건, 갱신 ${result.updated}건`);
      load();
    } catch (e) {
      setError(`가져오기 실패: ${(e as Error).message}`);
    } finally {
      if (importInput.current) importInput.current.value = "";
    }
  };

  return (
    <div>
      <div className="page-head">
        <h1>Prompt Library</h1>
        <p>
          프로젝트의 prompt 폴더에 있는 Markdown 또는 텍스트 파일이 Master Prompt의
          유일한 출처입니다. 편집 내용과 버전 이력도 파일로 저장되며, 실행 시점의
          원문은 작업 이력에 별도 스냅샷으로 남습니다.
        </p>
      </div>

      {message && <div className="notice ok">{message}</div>}
      {error && <div className="notice danger">{error}</div>}

      <div className="card">
        <div className="split">
          <div className="btn-row" style={{ flex: 1 }}>
            <input
              type="text"
              placeholder="이름, 설명, 본문 검색"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ maxWidth: 320 }}
            />
          </div>
          <div className="btn-row">
            <button className="btn small" onClick={exportAll}>
              내보내기
            </button>
            <button
              className="btn small"
              onClick={() => importInput.current?.click()}
            >
              가져오기
            </button>
            <input
              ref={importInput}
              type="file"
              accept=".json"
              hidden
              onChange={(e) => importFile(e.target.files?.[0])}
            />
            <button className="btn primary small" onClick={() => setDraft({ ...BLANK })}>
              새 프롬프트
            </button>
          </div>
        </div>
      </div>

      <div className="card">
        {prompts.length === 0 ? (
          <div className="empty">프롬프트가 없습니다.</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>이름</th>
                  <th>버전</th>
                  <th>출력</th>
                  <th>태그</th>
                  <th>상태</th>
                  <th style={{ width: 260 }}>작업</th>
                </tr>
              </thead>
              <tbody>
                {prompts.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{p.name}</div>
                      <div className="faint">{p.description || "설명 없음"}</div>
                    </td>
                    <td>v{p.version}</td>
                    <td>{p.output_mode}</td>
                    <td>
                      {(p.tags ?? []).map((t) => (
                        <span className="pill neutral" key={t} style={{ marginRight: 4 }}>
                          {t}
                        </span>
                      ))}
                    </td>
                    <td>
                      {p.enabled ? (
                        <span className="pill ok">활성</span>
                      ) : (
                        <span className="pill warn">비활성</span>
                      )}
                    </td>
                    <td>
                      <div className="btn-row">
                        <button
                          className="btn small"
                          onClick={() =>
                            setDraft({
                              id: p.id,
                              name: p.name,
                              description: p.description,
                              body: p.body,
                              output_mode: p.output_mode,
                              tags: p.tags ?? [],
                            })
                          }
                        >
                          편집
                        </button>
                        <button
                          className="btn small"
                          onClick={() =>
                            act(
                              () => api.updatePrompt(p.id, { enabled: !p.enabled }),
                              p.enabled ? "비활성화했습니다." : "활성화했습니다.",
                            )
                          }
                        >
                          {p.enabled ? "비활성" : "활성"}
                        </button>
                        <button
                          className="btn small"
                          onClick={() =>
                            api.promptVersions(p.id).then(setVersions).catch((e) => setError(e.message))
                          }
                        >
                          이력
                        </button>
                        <button
                          className="btn small danger"
                          onClick={() => {
                            if (
                              window.confirm(
                                `"${p.name}" 을(를) 삭제합니다. 과거 실행 이력의 스냅샷은 남습니다. 계속할까요?`,
                              )
                            ) {
                              act(() => api.deletePrompt(p.id), "삭제했습니다.");
                            }
                          }}
                        >
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

      {draft && (
        <div className="modal-backdrop" onClick={() => setDraft(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2 style={{ marginTop: 0 }}>
              {draft.id ? "프롬프트 편집" : "새 프롬프트"}
            </h2>
            <div className="field">
              <label>이름</label>
              <input
                type="text"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </div>
            <div className="field">
              <label>설명</label>
              <input
                type="text"
                value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              />
            </div>
            <div className="card-row">
              <div className="field">
                <label>출력 형식</label>
                <select
                  value={draft.output_mode}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      output_mode: e.target.value as "markdown" | "text",
                    })
                  }
                >
                  <option value="markdown">markdown</option>
                  <option value="text">text</option>
                </select>
              </div>
              <div className="field">
                <label>태그 (쉼표 구분)</label>
                <input
                  type="text"
                  value={draft.tags.join(", ")}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      tags: e.target.value
                        .split(",")
                        .map((t) => t.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </div>
            </div>
            <div className="field">
              <label>본문 (업무 로직은 전부 여기에 씁니다)</label>
              <textarea
                className="mono"
                rows={16}
                value={draft.body}
                onChange={(e) => setDraft({ ...draft, body: e.target.value })}
              />
              <span className="hint">
                저장하면 prompt 폴더의 파일이 즉시 갱신됩니다. ARIA 는 이 본문 앞뒤에
                어떤 업무 지시도 추가하지 않습니다. 첨부 자료의 신뢰 경계에 관한
                규칙만 시스템 프롬프트로 별도 전달됩니다.
              </span>
            </div>
            <div className="btn-row">
              <button
                className="btn primary"
                onClick={save}
                disabled={!draft.name.trim() || !draft.body.trim()}
              >
                저장
              </button>
              <button className="btn" onClick={() => setDraft(null)}>
                취소
              </button>
            </div>
          </div>
        </div>
      )}

      {versions && (
        <div className="modal-backdrop" onClick={() => setVersions(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2 style={{ marginTop: 0 }}>버전 이력</h2>
            {versions.length === 0 && <div className="empty">이력이 없습니다.</div>}
            {versions.map((v) => (
              <details key={v.id} style={{ marginBottom: 10 }}>
                <summary style={{ cursor: "pointer" }}>
                  v{v.version} · {v.name} ·{" "}
                  <span className="faint">
                    {new Date(v.created_at).toLocaleString()}
                  </span>
                </summary>
                <pre className="result-raw" style={{ marginTop: 8 }}>
                  {v.body}
                </pre>
              </details>
            ))}
            <button className="btn" onClick={() => setVersions(null)}>
              닫기
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
