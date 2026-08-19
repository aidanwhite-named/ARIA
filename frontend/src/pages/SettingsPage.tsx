import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { AppSettings, ProviderInfo } from "../lib/types";

const AUTH_LABEL: Record<string, { text: string; cls: string }> = {
  OK: { text: "로그인됨", cls: "ok" },
  NOT_LOGGED_IN: { text: "로그인 필요", cls: "danger" },
  UNKNOWN: { text: "확인 불가", cls: "neutral" },
  NOT_APPLICABLE: { text: "해당 없음", cls: "neutral" },
};

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [probing, setProbing] = useState(false);
  const [smoke, setSmoke] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [paths, setPaths] = useState<Record<string, string>>({});

  useEffect(() => {
    api.settings().then((s) => {
      setSettings(s);
      setPaths(s.values.provider_paths ?? {});
    }).catch((e) => setError(e.message));
    api.listProviders().then(setProviders).catch(() => undefined);
  }, []);

  const notify = (text: string) => {
    setMessage(text);
    setError("");
    setTimeout(() => setMessage(""), 2600);
  };

  const probe = async () => {
    setProbing(true);
    setError("");
    try {
      setProviders(await api.probeProviders());
      notify("Provider 를 다시 검사했습니다.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setProbing(false);
    }
  };

  const saveValue = async (key: string, value: unknown) => {
    try {
      const updated = await api.updateSettings({ [key]: value });
      setSettings(updated);
      notify("저장했습니다.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggleExperimental = async (id: string, on: boolean) => {
    if (!settings) return;
    const current = settings.values.enabled_experimental_providers ?? [];
    const next = on
      ? Array.from(new Set([...current, id]))
      : current.filter((x) => x !== id);
    try {
      const updated = await api.updateSettings({
        enabled_experimental_providers: next,
      });
      setSettings(updated);
      setProviders(await api.listProviders());
      notify(on ? "활성화했습니다." : "비활성화했습니다.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const runSmoke = async (id: string) => {
    if (
      !window.confirm(
        "실제 모델을 호출합니다. 계정 사용량이 발생할 수 있습니다. 계속할까요?",
      )
    ) {
      return;
    }
    setSmoke(null);
    try {
      setSmoke(await api.smokeTest(id));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!settings) {
    return (
      <div>
        <div className="page-head">
          <h1>Settings</h1>
        </div>
        {error ? <div className="notice danger">{error}</div> : <p className="faint">불러오는 중…</p>}
      </div>
    );
  }

  const v = settings.values;

  return (
    <div>
      <div className="page-head">
        <h1>Settings</h1>
        <p>
          ARIA 는 API Key 를 입력받거나 저장하지 않습니다. 각 CLI 에 이미 저장된 로컬
          로그인 세션만 사용합니다.
        </p>
      </div>

      {message && <div className="notice ok">{message}</div>}
      {error && <div className="notice danger">{error}</div>}
      {settings.warnings.map((w, i) => (
        <div className="notice warn" key={i}>
          {w}
        </div>
      ))}

      <div className="card">
        <div className="split" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Provider Capability Matrix</h2>
          <button className="btn small" onClick={probe} disabled={probing}>
            {probing ? "검사 중…" : "다시 검사"}
          </button>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>설치</th>
                <th>실행</th>
                <th>버전</th>
                <th>인증</th>
                <th>스트리밍</th>
                <th>도구 차단</th>
                <th>종합</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => {
                const auth = AUTH_LABEL[p.auth_state] ?? AUTH_LABEL.UNKNOWN;
                const cap = (key: string) =>
                  p.capabilities[key] === true ? "○" : p.capabilities[key] === false ? "×" : "?";
                return (
                  <tr key={p.provider}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                      <div className="faint break mono-text">
                        {p.executable_path ?? "경로 없음"}
                      </div>
                    </td>
                    <td>{p.installed ? "○" : "×"}</td>
                    <td>{p.executable_ok ? "○" : "×"}</td>
                    <td className="mono-text">{p.version ?? "-"}</td>
                    <td>
                      <span className={`pill ${auth.cls}`}>{auth.text}</span>
                    </td>
                    <td>{cap("stream_json")}</td>
                    <td>{cap("tools_disabled")}</td>
                    <td>
                      {p.experimental && !p.opted_in ? (
                        <span className="pill warn">실험적 · 비활성</span>
                      ) : p.usable ? (
                        <span className={`pill ${p.experimental ? "warn" : "ok"}`}>
                          {p.experimental ? "실험적 · 활성" : "사용 가능"}
                        </span>
                      ) : (
                        <span className="pill danger">사용 불가</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div style={{ marginTop: 16 }}>
          {providers.map((p) => (
            <details key={p.provider} style={{ marginBottom: 8 }}>
              <summary style={{ cursor: "pointer" }}>
                {p.display_name} — 상세 및 설치/로그인 안내
              </summary>
              <div style={{ padding: "10px 0 4px" }}>
                {p.experimental && (
                  <div className="notice warn">
                    <strong>
                      실험적 Provider — ARIA 의 안전 원칙(도구 없는 실행)을 충족하지
                      못합니다
                    </strong>
                    <ul>
                      {p.risks.map((risk, i) => (
                        <li key={i}>{risk}</li>
                      ))}
                    </ul>
                    <label className="checkbox" style={{ marginTop: 10 }}>
                      <input
                        type="checkbox"
                        checked={p.opted_in}
                        onChange={(e) => toggleExperimental(p.provider, e.target.checked)}
                      />
                      위 내용을 확인했으며 이 Provider 를 활성화합니다
                    </label>
                    {!p.runnable && (
                      <div className="faint" style={{ marginTop: 6 }}>
                        활성화해도 설치 또는 인증이 완료되어야 실행됩니다.
                      </div>
                    )}
                  </div>
                )}
                {p.notes.length > 0 && (
                  <ul style={{ margin: "0 0 10px", paddingLeft: 18 }}>
                    {p.notes.map((n, i) => (
                      <li key={i} className="muted">
                        {n}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="faint" style={{ marginTop: 0 }}>
                  {p.install_hint}
                </p>
                <div className="field" style={{ maxWidth: 560 }}>
                  <label>실행 파일 경로 직접 지정 (비우면 자동 탐색)</label>
                  <input
                    type="text"
                    value={paths[p.provider] ?? ""}
                    placeholder="예: C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"
                    onChange={(e) =>
                      setPaths((prev) => ({ ...prev, [p.provider]: e.target.value }))
                    }
                  />
                </div>
                <div className="btn-row">
                  <button
                    className="btn small"
                    onClick={() => saveValue("provider_paths", paths)}
                  >
                    경로 저장
                  </button>
                  {p.provider !== "mock" && (
                    <button
                      className="btn small"
                      onClick={() => runSmoke(p.provider)}
                      disabled={!p.executable_ok}
                    >
                      실제 호출 테스트 (사용량 발생)
                    </button>
                  )}
                </div>
              </div>
            </details>
          ))}
        </div>

        {smoke && (
          <pre className="result-raw" style={{ marginTop: 12, maxHeight: 260 }}>
            {JSON.stringify(smoke, null, 2)}
          </pre>
        )}
      </div>

      <div className="card">
        <h2>실행 한도</h2>
        <div className="card-row">
          <NumberField
            label="파일 1개 최대 크기 (bytes)"
            value={v.max_file_size_bytes}
            onSave={(n) => saveValue("max_file_size_bytes", n)}
          />
          <NumberField
            label="총 업로드 최대 크기 (bytes)"
            value={v.max_total_upload_bytes}
            onSave={(n) => saveValue("max_total_upload_bytes", n)}
          />
          <NumberField
            label="작업당 최대 파일 수"
            value={v.max_files_per_job}
            onSave={(n) => saveValue("max_files_per_job", n)}
          />
        </div>
        <div className="card-row">
          <NumberField
            label="인라인 전달 최대 문자 수"
            value={v.max_inline_chars}
            hint="초과하면 INPUT_TOO_LARGE 로 중단합니다. 임의로 자르지 않습니다."
            onSave={(n) => saveValue("max_inline_chars", n)}
          />
          <NumberField
            label="실행 제한 시간 (초)"
            value={v.default_timeout_seconds}
            onSave={(n) => saveValue("default_timeout_seconds", n)}
          />
          <NumberField
            label="Provider 당 동시 실행"
            value={v.max_concurrency_per_provider}
            hint="2 이상이면 메모리와 계정 사용량 제한에 주의하십시오."
            onSave={(n) => saveValue("max_concurrency_per_provider", n)}
          />
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={v.keep_raw_output}
            onChange={(e) => saveValue("keep_raw_output", e.target.checked)}
          />
          raw stdout/stderr 를 파일로 보존
        </label>
        <label className="checkbox" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={v.fail_on_tool_use}
            onChange={(e) => saveValue("fail_on_tool_use", e.target.checked)}
          />
          도구 호출이 감지되면 실패로 처리
        </label>
        <div className="hint">
          도구를 끌 수 있는 Provider(Claude)와 끌 수 없는 Provider(agy)에서는 이
          설정과 무관하게 항상 실패로 처리합니다.
        </div>
      </div>

      <div className="card">
        <h2>런타임 컨텍스트</h2>
        <p className="faint" style={{ marginTop: 0 }}>
          시스템 프롬프트로 전달되는 실행 안전 규칙입니다. 특허 분석 같은 업무 지시가
          아니라, 첨부 자료의 신뢰 경계를 정하는 내용만 들어갑니다.
        </p>
        <label className="checkbox" style={{ marginBottom: 10 }}>
          <input
            type="checkbox"
            checked={v.runtime_context_enabled}
            onChange={(e) => saveValue("runtime_context_enabled", e.target.checked)}
          />
          런타임 컨텍스트 사용
        </label>
        {!v.runtime_context_enabled && (
          <div className="notice warn">
            비활성화하면 첨부 문서 안의 지시문이 실행 지시로 해석될 위험이 커집니다.
          </div>
        )}
        <TextAreaField
          value={v.runtime_context}
          onSave={(text) => saveValue("runtime_context", text)}
          onReset={() =>
            api.resetRuntimeContext().then((s) => {
              setSettings(s);
              notify("기본값으로 되돌렸습니다.");
            })
          }
        />
      </div>

      <div className="card">
        <h2>저장 위치와 실행 환경</h2>
        <div className="table-scroll">
          <table>
            <tbody>
              <tr>
                <th>데이터 폴더</th>
                <td className="break mono-text">{settings.data_dir}</td>
              </tr>
              <tr>
                <th>실행 폴더</th>
                <td className="break mono-text">{settings.runs_dir}</td>
              </tr>
              <tr>
                <th>자식 프로세스 환경변수</th>
                <td>
                  allowlist {settings.env_filtering.allowlist.length}개만 전달, 그 외{" "}
                  {settings.env_filtering.removed_count}개 제거
                  <div className="faint">
                    차단 접두사: {settings.env_filtering.blocked_prefixes.join(", ")}
                  </div>
                  <div className="faint">
                    ARIA 를 Claude Code 세션 안에서 실행할 때 부모의 ANTHROPIC_* /
                    CLAUDE_* 변수가 자식 CLI 로 새어 들어가 인증이 깨지는 것을 막습니다.
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function NumberField(props: {
  label: string;
  value: number;
  hint?: string;
  onSave: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(props.value));
  useEffect(() => setDraft(String(props.value)), [props.value]);
  const dirty = draft !== String(props.value);
  return (
    <div className="field">
      <label>{props.label}</label>
      <div className="btn-row">
        <input
          type="number"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          style={{ maxWidth: 200 }}
        />
        <button
          className="btn small"
          disabled={!dirty}
          onClick={() => props.onSave(Number(draft))}
        >
          저장
        </button>
      </div>
      {props.hint && <span className="hint">{props.hint}</span>}
    </div>
  );
}

function TextAreaField(props: {
  value: string;
  onSave: (value: string) => void;
  onReset: () => void;
}) {
  const [draft, setDraft] = useState(props.value);
  useEffect(() => setDraft(props.value), [props.value]);
  return (
    <div>
      <textarea
        className="mono"
        rows={12}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="btn-row" style={{ marginTop: 8 }}>
        <button
          className="btn small primary"
          disabled={draft === props.value}
          onClick={() => props.onSave(draft)}
        >
          저장
        </button>
        <button className="btn small" onClick={props.onReset}>
          기본값으로
        </button>
      </div>
    </div>
  );
}
