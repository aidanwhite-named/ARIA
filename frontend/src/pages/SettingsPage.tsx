import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { isLogoutSession } from "../lib/types";
import type {
  AppSettings,
  Prompt,
  ProviderInfo,
  ProviderLoginSession,
} from "../lib/types";

const AUTH_LABEL: Record<string, { text: string; cls: string }> = {
  OK: { text: "로그인됨", cls: "ok" },
  NOT_LOGGED_IN: { text: "로그인 필요", cls: "danger" },
  UNKNOWN: { text: "확인 불가", cls: "neutral" },
  NOT_APPLICABLE: { text: "해당 없음", cls: "neutral" },
};

/** 이 도구를 지금 못 쓰는 이유를 한 마디로. */
function blockedReason(p: ProviderInfo): string {
  if (!p.execution_supported) return "실행 미구현";
  if (!p.installed) return "설치 필요";
  if (!p.executable_ok) return "호출 불가";
  if (p.auth_state === "NOT_LOGGED_IN") return "로그인 필요";
  if (p.auth_state === "UNKNOWN") return "인증 확인 불가";
  return "사용 불가";
}

/** 같은 이유를 한 문장으로. 무엇을 해야 하는지까지 적는다. */
function blockedDetail(p: ProviderInfo): string {
  if (!p.execution_supported)
    return "ARIA 가 이 도구의 실행 경로를 아직 지원하지 않습니다. 설치나 로그인으로 해결되지 않습니다.";
  if (!p.installed)
    return "CLI 를 찾지 못했습니다. 아래 상세의 설치 안내를 따르거나 실행 파일 경로를 지정하십시오.";
  if (!p.executable_ok)
    return "실행 파일은 있으나 호출할 수 없습니다. 아래 상세에서 절대 경로를 지정하고 다시 검사하십시오.";
  if (p.auth_state === "NOT_LOGGED_IN")
    return "로그인이 필요합니다. 아래 표의 로그인 버튼을 사용하십시오.";
  if (p.auth_state === "UNKNOWN")
    return "인증 상태를 확인하지 못했습니다. 다시 검사하거나 로그인을 시도하십시오.";
  return "사용할 수 없습니다. 아래 상세를 확인하십시오.";
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [probing, setProbing] = useState(false);
  const [smoke, setSmoke] = useState<Record<string, unknown> | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [defaultPromptId, setDefaultPromptId] = useState("");
  const [defaultProvider, setDefaultProvider] = useState("agy");
  const [defaultModels, setDefaultModels] = useState<Record<string, string>>({});
  const [loginProvider, setLoginProvider] = useState<ProviderInfo | null>(null);
  const [loginSession, setLoginSession] = useState<ProviderLoginSession | null>(null);
  const [loginStarting, setLoginStarting] = useState(false);
  const [loggingOut, setLoggingOut] = useState<string | null>(null);
  const [logoutProvider, setLogoutProvider] = useState<ProviderInfo | null>(null);
  const [logoutSession, setLogoutSession] = useState<ProviderLoginSession | null>(
    null,
  );
  // Provider 표 옆에 붙여 두는 오류. 페이지 맨 위 배너만 쓰면 표를 보고 있는
  // 사용자에게는 아무 일도 일어나지 않은 것처럼 보인다.
  const [logoutError, setLogoutError] = useState<{
    provider: string;
    message: string;
  } | null>(null);
  // 사용자가 직접 접거나 편 Provider. 여기에 없으면 아래 기본값을 쓴다.
  const [detailsOpen, setDetailsOpen] = useState<Record<string, boolean>>({});

  useEffect(() => {
    Promise.all([api.settings(), api.listPrompts(), api.listProviders()])
      .then(([s, promptList, providerList]) => {
        setSettings(s);
        setPrompts(promptList);
        setProviders(providerList);
        setPaths(s.values.provider_paths ?? {});
        const configuredPromptId = s.values.default_prompt_id ?? "";
        const configuredPrompt = promptList.find(
          (prompt) => prompt.id === configuredPromptId && prompt.enabled,
        );
        const fallbackPrompt = promptList.find((prompt) => prompt.enabled);
        setDefaultPromptId(configuredPrompt?.id ?? fallbackPrompt?.id ?? "");
        setDefaultProvider(s.values.default_provider ?? "agy");
        setDefaultModels(s.values.default_models ?? {});
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!loginSession || !loginSession.can_cancel) return;
    let stopped = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.providerLoginStatus(
          loginSession.provider,
          loginSession.session_id,
        );
        if (stopped) return;
        setLoginSession(next);
        if (next.state === "SUCCEEDED") {
          setProviders(await api.listProviders());
          notify(`${loginProvider?.display_name ?? next.provider} 로그인이 완료되었습니다.`);
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      }
    }, 1200);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [loginSession?.session_id, loginSession?.can_cancel]);

  // 도우미 창 로그아웃(agy)은 사용자가 창을 닫아야 끝난다. 창이 닫히면 백엔드가
  // 인증 상태를 다시 검사하므로, 여기서는 그 결과만 기다린다.
  useEffect(() => {
    if (!logoutSession || !logoutSession.can_cancel) return;
    let stopped = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.providerLogoutStatus(
          logoutSession.provider,
          logoutSession.session_id,
        );
        if (stopped) return;
        setLogoutSession(next);
        const name = logoutProvider?.display_name ?? next.provider;
        if (next.state === "SUCCEEDED") {
          setLogoutError(null);
          setProviders(await api.listProviders());
          notify(`${name}에서 로그아웃했습니다.`);
        } else if (next.state === "FAILED") {
          setLogoutError({ provider: next.provider, message: next.message });
          setProviders(await api.listProviders());
        }
      } catch (e) {
        if (stopped) return;
        const message = (e as Error).message;
        setError(message);
        setLogoutError({ provider: logoutSession.provider, message });
      }
    }, 1200);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [logoutSession?.session_id, logoutSession?.can_cancel]);

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
      notify("AI 실행 도구를 다시 검사했습니다.");
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

  const saveExecutionDefaults = async () => {
    try {
      const updated = await api.updateSettings({
        default_prompt_id: defaultPromptId,
        default_provider: defaultProvider,
        default_models: defaultModels,
      });
      setSettings(updated);
      notify("실행 기본 설정을 저장했습니다.");
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

  const openLogin = (provider: ProviderInfo) => {
    setLoginProvider(provider);
    if (loginSession?.provider !== provider.provider || !loginSession.can_cancel) {
      setLoginSession(null);
    }
    setError("");
  };

  const beginLogin = async (method?: string) => {
    if (!loginProvider) return;
    setLoginStarting(true);
    setError("");
    try {
      setLoginSession(
        await api.startProviderLogin(loginProvider.provider, method),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoginStarting(false);
    }
  };

  const cancelLogin = async () => {
    if (!loginSession) return;
    try {
      const next = await api.cancelProviderLogin(
        loginSession.provider,
        loginSession.session_id,
      );
      setLoginSession(next);
      setProviders(await api.listProviders());
      if (next.state === "SUCCEEDED") {
        notify(
          `${loginProvider?.display_name ?? next.provider} 로그인이 완료되었습니다.`,
        );
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // agy 는 전용 logout 명령이 없어 대화형 도우미 창에서만 로그아웃할 수 있다.
  const usesLogoutHelper = (provider: ProviderInfo) => provider.provider === "agy";

  const logoutBusy = (providerId: string) =>
    loggingOut === providerId ||
    (logoutSession?.provider === providerId && logoutSession.can_cancel);

  const logout = async (provider: ProviderInfo) => {
    const question = usesLogoutHelper(provider)
      ? `${provider.display_name} 로그아웃 도우미 창을 엽니다. 창에 /logout 을 입력한 뒤 창을 닫으면 ARIA가 상태를 다시 검사합니다. 계속할까요?`
      : `${provider.display_name} CLI에 저장된 현재 계정 로그인을 해제합니다. 계속할까요?`;
    if (!window.confirm(question)) return;

    setLoggingOut(provider.provider);
    setError("");
    setLogoutError(null);
    try {
      const result = await api.logoutProvider(provider.provider);
      if (isLogoutSession(result)) {
        // 도우미 창이 닫힐 때까지 폴링이 이어받는다.
        setLogoutProvider(provider);
        setLogoutSession(result);
        return;
      }
      setProviders(await api.listProviders());
      notify(`${provider.display_name}에서 로그아웃했습니다.`);
    } catch (e) {
      const message = (e as Error).message;
      setError(message);
      setLogoutError({ provider: provider.provider, message });
    } finally {
      setLoggingOut(null);
    }
  };

  const cancelLogout = async () => {
    if (!logoutSession) return;
    try {
      const next = await api.cancelProviderLogout(
        logoutSession.provider,
        logoutSession.session_id,
      );
      setLogoutSession(next);
      // 창에서 /logout 을 이미 끝냈다면 백엔드가 재검사 후 SUCCEEDED 로 돌려준다.
      // 어느 쪽이든 표를 갱신해야 화면이 실제 인증 상태와 어긋나지 않는다.
      setProviders(await api.listProviders());
      if (next.state === "SUCCEEDED") {
        setLogoutError(null);
        notify(
          `${logoutProvider?.display_name ?? next.provider}에서 로그아웃했습니다.`,
        );
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const closeLogout = () => {
    setLogoutProvider(null);
    setLogoutSession(null);
  };

  if (!settings) {
    return (
      <div className="page page-settings">
        <div className="page-head">
          <span className="eyebrow">05 / 환경 설정</span>
          <h1>분석 환경을 설정합니다</h1>
        </div>
        {error ? <div className="notice danger">{error}</div> : <p className="faint">불러오는 중…</p>}
      </div>
    );
  }

  const v = settings.values;
  const selectedProvider = providers.find((p) => p.provider === defaultProvider);
  const modelOptions = Array.isArray(selectedProvider?.capabilities.models)
    ? selectedProvider.capabilities.models
    : [];
  const selectedModel = modelOptions.includes(defaultModels[defaultProvider])
    ? defaultModels[defaultProvider]
    : "";

  return (
    <div className="page page-settings">
      <div className="page-head">
        <span className="eyebrow">05 / 환경 설정</span>
        <h1>분석 환경을 설정합니다</h1>
        <p>
          분석에 사용할 기준과 AI 실행 도구, 로컬 실행의 안전 범위를 관리합니다. ARIA는 API Key를 수집하거나 저장하지 않습니다.
        </p>
      </div>

      {message && <div className="notice ok">{message}</div>}
      {error && <div className="notice danger">{error}</div>}
      {settings.warnings.map((w, i) => (
        <div className="notice warn" key={i}>
          {w}
        </div>
      ))}

      <div className="card settings-defaults">
        <h2>실행 기본 설정</h2>
        <p className="faint" style={{ marginTop: -6 }}>
          실행 화면은 아래 설정을 그대로 사용합니다.
        </p>
        <div className="card-row">
          <div className="field">
            <label htmlFor="default-prompt">기본 분석 프롬프트</label>
            <select
              id="default-prompt"
              value={defaultPromptId}
              onChange={(e) => setDefaultPromptId(e.target.value)}
            >
                <option value="">최근 활성 분석 프롬프트 자동 선택</option>
              {prompts.map((prompt) => (
                <option key={prompt.id} value={prompt.id} disabled={!prompt.enabled}>
                  {prompt.name} (v{prompt.version}){prompt.enabled ? "" : " · 비활성"}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="default-provider">AI 실행 도구 (Provider)</label>
            <select
              id="default-provider"
              value={defaultProvider}
              onChange={(e) => setDefaultProvider(e.target.value)}
            >
              <option value="">지정 안 함 (실행 불가)</option>
              {providers.map((provider) => (
                <option key={provider.provider} value={provider.provider}>
                  {provider.display_name}
                  {provider.usable ? "" : ` · ${blockedReason(provider)}`}
                </option>
              ))}
            </select>
            {!defaultProvider && (
              <span className="hint" style={{ color: "var(--danger)" }}>
                AI 실행 도구를 지정하지 않으면 분석을 시작할 수 없습니다. 실행
                화면은 여기에서 저장한 기본값을 사용합니다.
              </span>
            )}
            {selectedProvider && !selectedProvider.usable && (
              <span className="hint" style={{ color: "var(--danger)" }}>
                {blockedDetail(selectedProvider)}
              </span>
            )}
          </div>
          <div className="field">
            <label htmlFor="default-model">모델</label>
            <select
              id="default-model"
              value={selectedModel}
              onChange={(e) =>
                setDefaultModels((current) => {
                  const next = { ...current };
                  if (e.target.value) next[defaultProvider] = e.target.value;
                  else delete next[defaultProvider];
                  return next;
                })
              }
            >
              <option value="">CLI 기본 모델</option>
              {modelOptions.map((model) => (
                <option key={model} value={model}>{model}</option>
              ))}
            </select>
            <span className="hint">
              {modelOptions.length > 0
                ? `${modelOptions.length}개 모델을 선택할 수 있습니다.`
                : "모델 목록을 확인할 수 없습니다."}
            </span>
          </div>
        </div>
        <button className="btn primary" onClick={saveExecutionDefaults}>
          실행 기본 설정 저장
        </button>
      </div>

      <div className="card settings-provider">
        <div className="split" style={{ marginBottom: 12 }}>
          <div>
            <h2 style={{ margin: 0 }}>AI 실행 도구 상태</h2>
            <p className="faint" style={{ margin: "4px 0 0" }}>
              설치·로그인·안전 기능을 확인하고 사용할 도구를 점검합니다.
            </p>
          </div>
          <button className="btn small" onClick={probe} disabled={probing}>
            {probing ? "검사 중…" : "다시 검사"}
          </button>
        </div>
        {logoutError && (
          <div className="notice danger" style={{ marginBottom: 12 }}>
            <strong>
              {providers.find((p) => p.provider === logoutError.provider)
                ?.display_name ?? logoutError.provider}{" "}
              로그아웃 실패
            </strong>
            <div>{logoutError.message}</div>
          </div>
        )}
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>실행 도구</th>
                <th>설치</th>
                <th>실행 파일</th>
                <th>버전</th>
                <th>인증</th>
                <th>실시간 결과</th>
                <th>도구 차단</th>
                <th>종합</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => {
                const auth = AUTH_LABEL[p.auth_state] ?? AUTH_LABEL.UNKNOWN;
                const cap = (key: string) => {
                  const value = p.capabilities[key];
                  if (value === true) return { text: "지원", cls: "ok" };
                  if (value === false) return { text: "미지원", cls: "danger" };
                  return { text: "확인 불가", cls: "neutral" };
                };
                const streaming = cap("stream_json");
                const toolBlocking = cap("tools_disabled");
                return (
                  <tr key={p.provider}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                      <div className="faint break mono-text">
                        {p.executable_path ?? "경로 없음"}
                      </div>
                    </td>
                    <td>
                      <span className={`pill ${p.installed ? "ok" : "danger"}`}>
                        {p.installed ? "설치됨" : "미설치"}
                      </span>
                    </td>
                    <td>
                      <span className={`pill ${p.executable_ok ? "ok" : "danger"}`}>
                        {p.executable_ok ? "확인됨" : "확인 필요"}
                      </span>
                    </td>
                    <td className="mono-text">{p.version ?? "-"}</td>
                    <td>
                      <div className="provider-auth-cell">
                        <span className={`pill ${auth.cls}`}>{auth.text}</span>
                        {p.auth_state === "OK" ? (
                          <button
                            className="btn small danger"
                            onClick={() => logout(p)}
                            disabled={logoutBusy(p.provider)}
                          >
                            {logoutBusy(p.provider)
                              ? "로그아웃 중…"
                              : usesLogoutHelper(p)
                                ? "로그아웃 도우미"
                                : "로그아웃"}
                          </button>
                        ) : (
                          <button
                            className="btn small"
                            onClick={() => openLogin(p)}
                            disabled={!p.executable_ok}
                          >
                            {p.provider === "agy" ? "로그인 도우미" : "로그인"}
                          </button>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className={`pill ${streaming.cls}`}>{streaming.text}</span>
                    </td>
                    <td>
                      <span className={`pill ${toolBlocking.cls}`}>{toolBlocking.text}</span>
                    </td>
                    <td>
                      {p.usable ? (
                        <span className="pill ok">
                          {p.experimental ? "사용 가능 · 안전 제한" : "사용 가능"}
                        </span>
                      ) : (
                        <span className="pill danger">{blockedReason(p)}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div style={{ marginTop: 16 }}>
          {providers.map((p) => {
            // 결정이 필요한 Provider 는 접어 두지 않는다. 이 앱에서 가장 중요한
            // 안전 스위치가 각주처럼 보이면 사용자는 그것을 찾지 못한다.
            const isOpen = detailsOpen[p.provider] ?? false;
            return (
            <details
              key={p.provider}
              className="provider-details"
              open={isOpen}
            >
              <summary
                onClick={(e) => {
                  e.preventDefault();
                  setDetailsOpen((current) => ({
                    ...current,
                    [p.provider]: !isOpen,
                  }));
                }}
              >
                <b>{p.display_name}</b>
                {p.experimental && <span className="pill warn">안전 제한</span>}
                <span className="faint">상세 및 설치/로그인 안내</span>
              </summary>
              <div className="provider-details-body">
                {p.experimental && (
                  <div className="notice warn">
                    <strong>
                      이 실행 도구는 ARIA 의 안전 원칙(도구 없는 실행)을 충족하지
                      못합니다
                    </strong>
                    <ul>
                      {p.risks.map((risk, i) => (
                        <li key={i}>{risk}</li>
                      ))}
                    </ul>
                    <div className="faint" style={{ marginTop: 8 }}>
                      실행을 막지는 않습니다. 다만 도구 호출이 감지되면 그 실행은
                      설정과 무관하게 실패로 기록됩니다.
                    </div>
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
                  <button
                    className="btn small"
                    onClick={() => runSmoke(p.provider)}
                    disabled={!p.executable_ok}
                  >
                    실제 호출 테스트 (사용량 발생)
                  </button>
                </div>
              </div>
            </details>
            );
          })}
        </div>

        {smoke && (
          <pre className="result-raw" style={{ marginTop: 12, maxHeight: 260 }}>
            {JSON.stringify(smoke, null, 2)}
          </pre>
        )}
      </div>

      <div className="card settings-context">
        <h2>안전 지시문 (런타임 컨텍스트)</h2>
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

      <div className="card settings-storage">
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

      <div className="card settings-limits">
        <h2>실행 한도</h2>
        <div className="settings-limit-grid">
          <ByteField
            label="파일 1개 최대 크기"
            value={v.max_file_size_bytes}
            onSave={(n) => saveValue("max_file_size_bytes", n)}
          />
          <ByteField
            label="한 번에 올릴 수 있는 총 파일 크기"
            value={v.max_total_upload_bytes}
            onSave={(n) => saveValue("max_total_upload_bytes", n)}
          />
          <NumberField
            label="분석 1회당 최대 파일 수"
            value={v.max_files_per_job}
            onSave={(n) => saveValue("max_files_per_job", n)}
          />
          <NumberField
            label="ARIA 자체 글자 수 한도 (0 = 제한 없음)"
            value={v.max_inline_chars}
            hint={
              "기본값 0 — ARIA 는 글자 수로 막지 않습니다. 실행을 실제로 막는 " +
              "한도는 선택한 Provider 가 자료 전체를 손실 없이 전달할 수 있는 " +
              "크기와 모델 컨텍스트이며, 이 둘은 끌 수 없습니다. 어느 쪽을 " +
              "넘든 ARIA 는 문서를 자르거나 요약하지 않고 INPUT_TOO_LARGE 로 " +
              "중단합니다. 그때는 문헌을 나눠 여러 번 실행하거나 전송 한도가 " +
              "더 큰 Provider 를 선택하십시오."
            }
            onSave={(n) => saveValue("max_inline_chars", n)}
          />
          <NumberField
            label="실행 제한 시간 (초)"
            value={v.default_timeout_seconds}
            onSave={(n) => saveValue("default_timeout_seconds", n)}
          />
          <NumberField
            label="실행 도구당 동시 분석 수"
            value={v.max_concurrency_per_provider}
            hint="2 이상이면 메모리와 계정 사용량 제한에 주의하십시오."
            onSave={(n) => saveValue("max_concurrency_per_provider", n)}
          />
          <NumberField
            label="검색 1회당 최대 도구 호출 수"
            value={v.max_search_tool_calls}
            hint="유사 문헌 검색에만 적용됩니다. 넘으면 실행을 끊고 실패로 남깁니다."
            onSave={(n) => saveValue("max_search_tool_calls", n)}
          />
        </div>
        <div className="settings-limit-options">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={v.keep_raw_output}
              onChange={(e) => saveValue("keep_raw_output", e.target.checked)}
            />
            raw stdout/stderr 를 파일로 보존
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={v.fail_on_tool_use}
              onChange={(e) => saveValue("fail_on_tool_use", e.target.checked)}
            />
            도구 호출이 감지되면 실패로 처리
          </label>
          <div className="hint">
            특허 구성대비 분석에서는 이 설정과 무관하게 도구 호출을 실패로
            처리합니다. 유사 문헌 검색은 Provider별 검색 도구만 허용하는 별도
            정책을 사용합니다.
          </div>
        </div>
      </div>

      <div className="card settings-kiwee">
        <h2>Kiwee 특허 검색 연동</h2>
        <p className="muted">
          유사 문헌 검색에서 웹 대신(또는 웹과 함께) Kiwee 특허 DB 를 사용할지
          결정합니다. 지금은 연동 지점만 모듈로 준비된 단계이며, 실제 접속·검색은
          공급자 승인과 API 계약이 확정된 뒤 활성화됩니다.
        </p>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={v.kiwee_integration_enabled}
            onChange={(e) =>
              saveValue("kiwee_integration_enabled", e.target.checked)
            }
          />
          Kiwee 특허 검색 연동 사용
        </label>
        {v.kiwee_integration_enabled && (
          <div className="notice info" style={{ marginTop: 10 }}>
            연동이 켜져 있으나 접속·인증이 아직 구현되지 않아 실제 검색은
            수행되지 않습니다. 외부 접속을 시도하지 않습니다.
          </div>
        )}
      </div>

      {loginProvider && (
        <div
          className="modal-backdrop no-print"
          onClick={() => {
            if (!loginSession?.can_cancel) setLoginProvider(null);
          }}
        >
          <div
            className="modal provider-login-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="provider-login-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="split" style={{ marginBottom: 14 }}>
              <div>
                <span className="eyebrow">CLI 계정 연결</span>
                <h2 id="provider-login-title" style={{ margin: "5px 0 0" }}>
                  {loginProvider.display_name} 로그인
                </h2>
              </div>
              <button
                className="btn small"
                onClick={() => setLoginProvider(null)}
                disabled={Boolean(loginSession?.can_cancel)}
              >
                닫기
              </button>
            </div>

            <div className="notice info">
              ARIA는 비밀번호, API Key 또는 OAuth 토큰을 입력받거나 저장하지
              않습니다. 인증은 {loginProvider.display_name} CLI와 공식 로그인
              페이지가 처리합니다.
            </div>

            {!loginSession ? (
              <>
                <p className="muted">
                  브라우저에 이미 로그인된 계정이 자동 선택될 수 있습니다. 다른
                  계정을 연결하려면 공식 로그인 화면에서 <strong>다른 계정 사용</strong>을
                  선택하세요.
                </p>
                {loginProvider.provider === "claude" ? (
                  <div className="login-method-grid">
                    <button
                      className="btn primary"
                      onClick={() => beginLogin("subscription")}
                      disabled={loginStarting}
                    >
                      Claude 구독으로 로그인
                    </button>
                    <button
                      className="btn"
                      onClick={() => beginLogin("console")}
                      disabled={loginStarting}
                    >
                      Anthropic Console로 로그인
                    </button>
                  </div>
                ) : loginProvider.provider === "codex" ? (
                  <button
                    className="btn primary"
                    onClick={() => beginLogin("chatgpt")}
                    disabled={loginStarting}
                  >
                    ChatGPT로 로그인
                  </button>
                ) : (
                  <>
                    <div className="notice warn">
                      agy는 전용 로그인 명령을 제공하지 않습니다. 별도 도우미 창이
                      열리면 Google 로그인, 테마와 약관 설정을 마친 뒤 창을 닫으세요.
                      도우미는 빈 전용 폴더에서 샌드박스 모드로 실행됩니다.
                    </div>
                    <button
                      className="btn primary"
                      onClick={() => beginLogin("google")}
                      disabled={loginStarting}
                    >
                      agy 로그인 도우미 열기
                    </button>
                  </>
                )}
                {loginStarting && (
                  <span className="login-starting muted">
                    <span className="spinner" /> 로그인 준비 중…
                  </span>
                )}
              </>
            ) : (
              <div className="login-session-state">
                <div className="login-state-line">
                  {loginSession.can_cancel && <span className="spinner" />}
                  <span
                    className={`pill ${
                      loginSession.state === "SUCCEEDED"
                        ? "ok"
                        : loginSession.state === "FAILED"
                          ? "danger"
                          : loginSession.state === "CANCELLED"
                            ? "neutral"
                            : "accent"
                    }`}
                  >
                    {loginSession.state === "SUCCEEDED"
                      ? "로그인 완료"
                      : loginSession.state === "FAILED"
                        ? "로그인 실패"
                        : loginSession.state === "CANCELLED"
                          ? "취소됨"
                          : "로그인 진행 중"}
                  </span>
                </div>
                <p>{loginSession.message}</p>
                {loginSession.mode === "browser" && loginSession.can_cancel && (
                  <p className="faint">
                    열린 브라우저에서 로그인을 마치면 이 화면이 자동으로 갱신됩니다.
                  </p>
                )}
                <div className="btn-row">
                  {loginSession.can_cancel ? (
                    <button
                      className={`btn ${
                        loginSession.mode === "helper_window" ? "primary" : "danger"
                      }`}
                      onClick={cancelLogin}
                    >
                      {loginSession.mode === "helper_window"
                        ? "창 닫고 로그인 확인"
                        : "로그인 취소"}
                    </button>
                  ) : (
                    <>
                      {loginSession.state !== "SUCCEEDED" && (
                        <button
                          className="btn primary"
                          onClick={() => setLoginSession(null)}
                        >
                          다시 시도
                        </button>
                      )}
                      <button
                        className="btn"
                        onClick={() => setLoginProvider(null)}
                      >
                        닫기
                      </button>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {logoutProvider && logoutSession && (
        <div
          className="modal-backdrop no-print"
          onClick={() => {
            if (!logoutSession.can_cancel) closeLogout();
          }}
        >
          <div
            className="modal provider-login-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="provider-logout-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="split" style={{ marginBottom: 14 }}>
              <div>
                <span className="eyebrow">CLI 계정 연결 해제</span>
                <h2 id="provider-logout-title" style={{ margin: "5px 0 0" }}>
                  {logoutProvider.display_name} 로그아웃
                </h2>
              </div>
              <button
                className="btn small"
                onClick={closeLogout}
                disabled={logoutSession.can_cancel}
              >
                닫기
              </button>
            </div>

            <div className="notice warn">
              {logoutProvider.display_name}는 비대화식 로그아웃 명령을 제공하지
              않습니다. 열린 창에 <strong>/logout</strong> 을 직접 입력하세요.
              자격증명은 CLI가 지우고, ARIA는 창이 닫힌 뒤 인증 상태를 다시
              검사할 뿐입니다.
            </div>

            <div className="login-session-state">
              <div className="login-state-line">
                {logoutSession.can_cancel && <span className="spinner" />}
                <span
                  className={`pill ${
                    logoutSession.state === "SUCCEEDED"
                      ? "ok"
                      : logoutSession.state === "FAILED"
                        ? "danger"
                        : logoutSession.state === "CANCELLED"
                          ? "neutral"
                          : "accent"
                  }`}
                >
                  {logoutSession.state === "SUCCEEDED"
                    ? "로그아웃 완료"
                    : logoutSession.state === "FAILED"
                      ? "로그아웃 실패"
                      : logoutSession.state === "CANCELLED"
                        ? "취소됨"
                        : "로그아웃 진행 중"}
                </span>
              </div>
              <p>{logoutSession.message}</p>
              <div className="btn-row">
                {logoutSession.can_cancel ? (
                  <button className="btn" onClick={cancelLogout}>
                    창 닫고 상태 확인
                  </button>
                ) : (
                  <>
                    {logoutSession.state !== "SUCCEEDED" && (
                      <button
                        className="btn primary"
                        onClick={() => logout(logoutProvider)}
                      >
                        다시 시도
                      </button>
                    )}
                    <button className="btn" onClick={closeLogout}>
                      닫기
                    </button>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const BYTES_PER_MEBIBYTE = 1024 * 1024;

function ByteField(props: {
  label: string;
  value: number;
  onSave: (value: number) => void;
}) {
  const current = props.value / BYTES_PER_MEBIBYTE;
  const [draft, setDraft] = useState(String(current));
  useEffect(() => setDraft(String(current)), [current]);
  const parsed = Number(draft);
  const dirty = Number.isFinite(parsed) && parsed > 0 && parsed !== current;

  return (
    <div className="field">
      <label>{props.label}</label>
      <div className="number-with-unit">
        <input
          type="number"
          min="1"
          step="1"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <span>MB</span>
        <button
          className="btn small"
          disabled={!dirty}
          onClick={() => props.onSave(Math.round(parsed * BYTES_PER_MEBIBYTE))}
        >
          저장
        </button>
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
