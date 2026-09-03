import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import type { PromptCatalogItem, PromptKind } from "../lib/types";

const BLANK = {
  name: "",
  description: "",
  body: "",
  output_mode: "markdown" as "markdown" | "text",
  tags: [] as string[],
};

type Draft = typeof BLANK & {
  id?: string;
  kind: PromptKind;
  // 배포본인가. 저장 경로가 다르고 삭제할 수 없다.
  reserved?: boolean;
};

//: 새 검색 전략 프롬프트의 시작 본문. 실행·보안·감사 계약은 ARIA 가 갖고
//: 있으므로 여기에는 전략만 적는다 — placeholder 도 경계 표시도 필요 없다.
const SEARCH_STRATEGY_TEMPLATE = `기술적 식별력이 높은 핵심 특징을 중심으로 검색하되 전체 시스템의 유사성도 함께 고려해줘.

검색 범위와 확장 방식
- 후출원·후공개 문헌도 기술적으로 유사하거나 인용문헌 추적에 유용하면 포함해줘.
- 유력 문헌의 실제 용어, IPC·CPC, 패밀리, 인용·피인용 문헌으로 검색을 넓혀줘.

후보 우선순위와 평가 관점
- 전용 페이지에서 문헌 식별정보를 확인했고, 관측한 문장이 핵심 특징을 직접
  뒷받침하는 후보를 먼저 배치해줘.
- 확인하지 못한 후보도 버리지 말고 미확인 단서로 남겨줘.

동의어·영문어·분류코드 활용
- 청구항 용어의 동의어와 영문 대응어를 함께 시도해줘.
- 유력 후보의 IPC·CPC 를 확인해 같은 분류의 문헌으로 넓혀줘.
`;

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptCatalogItem[]>([]);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const importInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    api
      .listPromptCatalog({ search })
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
      const payload = {
        name: draft.name,
        description: draft.description,
        body: draft.body,
        output_mode: draft.output_mode,
        tags: draft.tags,
      };
      if (draft.id) {
        // 배포본(기본 제공)만 예약 경로로 저장한다. 사용자가 만든 검색 전략은
        // 일반 프롬프트와 같은 경로로 저장하며, 백엔드가 종류에 맞는 검증을
        // 건다 — 종류로 경로를 가르면 사용자가 만든 검색 전략을 편집할 수 없다.
        if (draft.reserved) {
          await api.updateReservedPrompt(draft.id, payload);
        } else {
          await api.updatePrompt(draft.id, payload);
        }
        notify("프롬프트를 저장했습니다.");
      } else {
        await api.createPrompt({ ...payload, kind: draft.kind });
        notify(
          draft.kind === "search"
            ? "prompt 폴더에 새 검색 전략 프롬프트를 만들었습니다."
            : "prompt 폴더에 새 프롬프트 파일을 만들었습니다.",
        );
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
    <div className="page page-prompts">
      <div className="page-head">
        <span className="eyebrow">프롬프트</span>
        <h1>프롬프트를 관리합니다</h1>
        <p>
          분석과 검색이 각자의 전용 프롬프트를 사용합니다. 모든 수정의 맥락과
          실행 시점의 원문은 작업 기록에 보존합니다.
        </p>
      </div>

      {message && <div className="notice ok">{message}</div>}
      {error && <div className="notice danger">{error}</div>}

      <div className="card prompt-toolbar">
        <div className="split">
          <div className="btn-row" style={{ flex: 1 }}>
            <input
              type="text"
              aria-label="프롬프트 검색"
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
            <button
              className="btn small"
              onClick={() =>
                setDraft({
                  ...BLANK,
                  kind: "search",
                  name: "새 검색 전략",
                  body: SEARCH_STRATEGY_TEMPLATE,
                })
              }
            >
              새 검색 전략
            </button>
            <button
              className="btn primary small"
              onClick={() => setDraft({ ...BLANK, kind: "analysis" })}
            >
              새 분석 프롬프트
            </button>
          </div>
        </div>
      </div>

      <div className="card prompt-library">
        {prompts.length === 0 ? (
          <div className="empty">프롬프트가 없습니다.</div>
        ) : (
          <div className="table-scroll">
            <table className="prompt-table">
              <thead>
                <tr>
                  <th>용도</th>
                  <th>이름</th>
                  <th>결과 형식</th>
                  <th>태그</th>
                  <th>상태</th>
                  <th style={{ width: 260 }}>작업</th>
                </tr>
              </thead>
              <tbody>
                {prompts.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <span className={`pill ${p.kind === "search" ? "danger" : "accent"}`}>
                        {p.kind === "search" ? "검색" : "분석"}
                      </span>
                    </td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{p.name}</div>
                      <div className="faint">{p.description || "설명 없음"}</div>
                    </td>
                    <td>{p.output_mode === "markdown" ? "서식 포함" : "일반 텍스트"}</td>
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
                              kind: p.kind,
                              reserved: !p.deletable,
                            })
                          }
                        >
                          편집
                        </button>
                        <button
                          className="btn small"
                          onClick={() =>
                            act(
                              () =>
                                p.deletable
                                  ? api.updatePrompt(p.id, { enabled: !p.enabled })
                                  : api.updateReservedPrompt(p.id, {
                                      enabled: !p.enabled,
                                    }),
                              p.enabled ? "비활성화했습니다." : "활성화했습니다.",
                            )
                          }
                        >
                          {p.enabled ? "비활성" : "활성"}
                        </button>
                        {p.deletable ? (
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
                        ) : (
                          <span className="pill neutral">기본 제공</span>
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

      {draft && (
        <div className="modal-backdrop" onClick={() => setDraft(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2 style={{ marginTop: 0 }}>
              {draft.id
                ? draft.kind === "search"
                  ? "검색 전략 프롬프트 편집"
                  : "분석 프롬프트 편집"
                : draft.kind === "search"
                  ? "새 검색 전략 프롬프트"
                  : "새 분석 프롬프트"}
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
                {draft.kind === "search"
                  ? "검색 전략만 적으십시오. 청구항·명세서·미대응 구성은 ARIA가 이 본문 뒤에 경계와 함께 붙이며, 도구 허용·호출 예산·채널 정책·감사 기록·보고서 형식은 이 본문이 바꿀 수 없습니다."
                  : "저장하면 prompt 폴더의 파일이 즉시 갱신됩니다. ARIA는 이 본문 앞뒤에 업무 지시를 추가하지 않습니다."}
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
    </div>
  );
}
