import type {
  AppSettings,
  HistoryItem,
  Job,
  Prompt,
  PromptVersion,
  ProviderInfo,
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

  listPrompts: (params: { search?: string; includeArchived?: boolean } = {}) => {
    const query = new URLSearchParams();
    if (params.search) query.set("search", params.search);
    if (params.includeArchived) query.set("include_archived", "true");
    const suffix = query.toString();
    return request<Prompt[]>(`/api/prompts${suffix ? `?${suffix}` : ""}`);
  },
  getPrompt: (id: string) => request<Prompt>(`/api/prompts/${id}`),
  createPrompt: (body: Partial<Prompt>) =>
    request<Prompt>("/api/prompts", { method: "POST", body: JSON.stringify(body) }),
  updatePrompt: (id: string, body: Partial<Prompt>) =>
    request<Prompt>(`/api/prompts/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deletePrompt: (id: string) =>
    request<void>(`/api/prompts/${id}`, { method: "DELETE" }),
  clonePrompt: (id: string) =>
    request<Prompt>(`/api/prompts/${id}/clone`, { method: "POST" }),
  promptVersions: (id: string) =>
    request<PromptVersion[]>(`/api/prompts/${id}/versions`),
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

  upload: (files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    return request<UploadResponse>("/api/uploads", { method: "POST", body: form });
  },

  createJob: (body: {
    prompt_id: string;
    provider: string;
    model?: string | null;
    user_input?: string;
    batch_id?: string | null;
    required_map?: Record<string, boolean>;
  }) => request<Job>("/api/jobs", { method: "POST", body: JSON.stringify(body) }),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),
  cancelJob: (id: string) =>
    request<{ cancelled: boolean; reason?: string }>(`/api/jobs/${id}/cancel`, {
      method: "POST",
    }),
  finalPrompt: (id: string) =>
    fetch(`/api/jobs/${id}/final-prompt`).then((r) => r.text()),
  rawOutput: (id: string, which: "stdout" | "stderr") =>
    fetch(`/api/jobs/${id}/raw?which=${which}`).then((r) => r.text()),

  history: (params: { provider?: string; status?: string } = {}) => {
    const query = new URLSearchParams();
    if (params.provider) query.set("provider", params.provider);
    if (params.status) query.set("status", params.status);
    const suffix = query.toString();
    return request<HistoryItem[]>(`/api/history${suffix ? `?${suffix}` : ""}`);
  },
  historyItem: (id: string) => request<Job>(`/api/history/${id}`),
  deleteHistory: (id: string) =>
    request<void>(`/api/history/${id}`, { method: "DELETE" }),

  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (values: Record<string, unknown>) =>
    request<AppSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ values }),
    }),
  resetRuntimeContext: () =>
    request<AppSettings>("/api/settings/runtime-context/reset", { method: "POST" }),
};

export function downloadUrl(jobId: string, fmt: "md" | "txt" | "json"): string {
  return `/api/jobs/${jobId}/result?fmt=${fmt}`;
}
