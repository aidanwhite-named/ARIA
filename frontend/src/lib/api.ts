import type {
  AppSettings,
  CredentialCheck,
  HistoryItem,
  Job,
  JobKind,
  Preflight,
  Prompt,
  PromptCatalogItem,
  PromptVersion,
  ProviderInfo,
  ProviderLoginSession,
  ProviderLogoutResult,
  AttachmentRole,
  RelationType,
  UploadResponse,
} from "./types";

// 백엔드 CSRF 가드가 변경 요청에 요구하는 헤더.
// 커스텀 헤더는 preflight 를 강제하므로 외부 사이트가 붙일 수 없다.
const CLIENT_HEADER = { "X-ARIA-Client": "1" } as const;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...CLIENT_HEADER,
      ...(init?.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      // 응답 본문이 JSON 이 아닌 경우 상태 코드만 쓴다.
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; version: string }>("/api/health"),

  listPrompts: (params: { search?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.search) query.set("search", params.search);
    const suffix = query.toString();
    return request<Prompt[]>(`/api/prompts${suffix ? `?${suffix}` : ""}`);
  },
  listPromptCatalog: (params: { search?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.search) query.set("search", params.search);
    const suffix = query.toString();
    return request<PromptCatalogItem[]>(
      `/api/prompts/catalog${suffix ? `?${suffix}` : ""}`,
    );
  },
  getPrompt: (id: string) => request<Prompt>(`/api/prompts/${id}`),
  createPrompt: (body: Partial<Prompt>) =>
    request<Prompt>("/api/prompts", { method: "POST", body: JSON.stringify(body) }),
  updatePrompt: (id: string, body: Partial<Prompt>) =>
    request<Prompt>(`/api/prompts/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateReservedPrompt: (id: string, body: Partial<Prompt>) =>
    request<PromptCatalogItem>(`/api/prompts/reserved/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deletePrompt: (id: string) =>
    request<void>(`/api/prompts/${id}`, { method: "DELETE" }),
  promptVersions: (id: string) =>
    request<PromptVersion[]>(`/api/prompts/${id}/versions`),
  reservedPromptVersions: (id: string) =>
    request<PromptVersion[]>(`/api/prompts/reserved/${id}/versions`),
  exportPrompts: () =>
    request<{ version: number; prompts: unknown[] }>("/api/prompts/export"),
  importPrompts: (prompts: unknown[], replaceExisting: boolean) =>
    request<{ created: number; updated: number }>("/api/prompts/import", {
      method: "POST",
      body: JSON.stringify({ prompts, replace_existing: replaceExisting }),
    }),

  listProviders: () =>
    request<{ providers: ProviderInfo[] }>("/api/providers").then((r) => r.providers),
  probeProviders: () =>
    request<{ providers: ProviderInfo[] }>("/api/providers/probe", {
      method: "POST",
    }).then((r) => r.providers),
  smokeTest: (id: string) =>
    request<Record<string, unknown>>(`/api/providers/${id}/smoke-test`, {
      method: "POST",
    }),
  startProviderLogin: (id: string, method?: string) =>
    request<ProviderLoginSession>(`/api/providers/${id}/login`, {
      method: "POST",
      body: JSON.stringify({ method: method ?? null }),
    }),
  providerLoginStatus: (id: string, sessionId: string) =>
    request<ProviderLoginSession>(`/api/providers/${id}/login/${sessionId}`),
  cancelProviderLogin: (id: string, sessionId: string) =>
    request<ProviderLoginSession>(`/api/providers/${id}/login/${sessionId}`, {
      method: "DELETE",
    }),
  logoutProvider: (id: string) =>
    request<ProviderLogoutResult>(`/api/providers/${id}/logout`, {
      method: "POST",
    }),
  providerLogoutStatus: (id: string, sessionId: string) =>
    request<ProviderLoginSession>(`/api/providers/${id}/logout/${sessionId}`),
  cancelProviderLogout: (id: string, sessionId: string) =>
    request<ProviderLoginSession>(`/api/providers/${id}/logout/${sessionId}`, {
      method: "DELETE",
    }),

  upload: (items: { file: File; role: AttachmentRole }[]) => {
    const form = new FormData();
    items.forEach(({ file }) => form.append("files", file));
    form.append("roles", JSON.stringify(items.map(({ role }) => role)));
    return request<UploadResponse>("/api/uploads", { method: "POST", body: form });
  },

  createJob: (body: {
    job_kind?: JobKind;
    prompt_id?: string | null;
    provider?: string | null;
    model?: string | null;
    claim_text?: string;
    batch_id?: string | null;
    /** 넣기로 한 자료를 못 읽었을 때 실행을 실패시킬지. 실행 화면은 보내지
     *  않으며(모두 필수), 백엔드 기본값도 필수다. 「분석에 포함」과 다른 축이라
     *  남겨 둔다. */
    required_map?: Record<string, boolean>;
    /** 「분석에 포함」을 체크한 첨부 id. 생략하면 서버에 저장된 포함 여부를
     *  그대로 쓴다(= 새 업로드는 전부 포함). */
    selected_attachment_ids?: string[] | null;
    source_job_id?: string | null;
    relation_type?: RelationType | null;
    followup_instruction?: string;
    search_component_ids?: string[];
  }) => request<Job>("/api/jobs", { method: "POST", body: JSON.stringify(body) }),
  /** 실행하지 않고 최종 조립 프롬프트의 크기만 받아 온다. 작업을 만들지 않고
   *  Provider 도 부르지 않는다. */
  preflight: (body: {
    job_kind?: JobKind;
    prompt_id?: string | null;
    provider?: string | null;
    claim_text?: string;
    batch_id?: string | null;
    /** createJob 과 같은 목록을 보내야 안내한 크기와 실제 실행이 일치한다. */
    selected_attachment_ids?: string[] | null;
    source_job_id?: string | null;
    relation_type?: RelationType | null;
    followup_instruction?: string;
  }) =>
    request<Preflight>("/api/jobs/preflight", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),
  cancelJob: (id: string) =>
    request<{ cancelled: boolean; reason?: string }>(`/api/jobs/${id}/cancel`, {
      method: "POST",
    }),
  finalPrompt: (id: string) =>
    fetch(`/api/jobs/${id}/final-prompt`).then((r) => r.text()),
  /** 실행 원문. "model" 은 검색 작업에서 모델이 쓴 산문(감사 자료)이다. */
  rawOutput: (id: string, which: "stdout" | "stderr" | "model") =>
    fetch(`/api/jobs/${id}/raw?which=${which}`).then((r) => r.text()),
  /** 로컬 검색 실행의 감사 자료. 파일 원문을 그대로 받는다. */
  retrievalArtifactUrl: (
    id: string,
    which: "evidence" | "manifest" | "extraction" | "trace",
  ) => `/api/jobs/${id}/retrieval?which=${which}`,

  history: (params: { provider?: string; status?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.provider) query.set("provider", params.provider);
    if (params.status) query.set("status", params.status);
    const suffix = query.toString();
    return request<HistoryItem[]>(`/api/history${suffix ? `?${suffix}` : ""}`);
  },
  historyItem: (id: string) => request<Job>(`/api/history/${id}`),
  deleteAllHistory: () =>
    request<{ deleted: number }>("/api/history", { method: "DELETE" }),
  deleteHistory: (id: string) =>
    request<void>(`/api/history/${id}`, { method: "DELETE" }),
  /** 이 실행과 그로부터 이어진 후속 실행 전부. 일괄 삭제 전 확인용. */
  historyThread: (id: string) =>
    request<HistoryItem[]>(`/api/history/${id}/thread`),
  deleteHistoryThread: (id: string) =>
    request<{ deleted: number }>(`/api/history/${id}/thread`, {
      method: "DELETE",
    }),

  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (values: Record<string, unknown>) =>
    request<AppSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),
  resetRuntimeContext: () =>
    request<AppSettings>("/api/settings/runtime-context/reset", { method: "POST" }),
  // 저장된 자격증명으로 토큰 발급을 한 번 시도한다. 키를 본문으로 보내지
  // 않는다 — 백엔드가 저장된 값을 읽는다.
  checkEpoCredentials: () =>
    request<CredentialCheck>("/api/settings/epo/check", { method: "POST" }),
};
