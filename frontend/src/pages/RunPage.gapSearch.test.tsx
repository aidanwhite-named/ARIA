/** 「미대응 구성 검색」이 열리는 자리.
 *
 *  이 버튼은 보고서 맨 위의 버튼 줄에 있고, 보고서 본문은 수천 픽셀이다.
 *  선택 화면을 보고서 뒤에 그리면 누른 사람의 화면 밖에서 열린다 — 눌러도
 *  아무 일이 일어나지 않는 것으로 보이고, 기능이 사라진 것과 구분되지 않는다.
 *  그래서 "열리는가"가 아니라 "보고서보다 앞에 열리는가"를 고정한다.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HashRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Job, ProviderInfo } from "../lib/types";

const JOB_ID = "job-1";

const job = {
  id: JOB_ID,
  status: "SUCCEEDED",
  error_code: null,
  job_kind: "patent_analysis",
  prompt_id: "patent-analysis-master-prompt.md",
  prompt_name: "구성대비 분석",
  output_mode: "markdown",
  claim_text: "청구항 1 ...",
  source_job_id: null,
  relation_type: null,
  citation_mapping: null,
  citation_mapping_error: null,
  analysis_manifest: {
    version: 1,
    threshold: 80,
    items: [
      {
        id: "C001",
        claim: "청구항 1",
        symbol: "(A)",
        feature: "대응된 구성",
        similarity: 92,
        status: "matched",
        difference: "",
        search_eligible: false,
      },
      {
        id: "C002",
        claim: "청구항 1",
        symbol: "(B)",
        feature: "미대응 구성",
        similarity: 45,
        status: "below_threshold",
        difference: "대응 내용 없음",
        search_eligible: true,
      },
    ],
  },
  analysis_manifest_error: null,
  search_manifest: null,
  search_focus: null,
  delivery_plan: "full_inline",
  retrieval_manifest: null,
  retrieval_manifest_error: null,
  provider: "agy",
  model: null,
  cli_path: null,
  cli_version: null,
  cli_args: [],
  prompt_snapshot: "",
  prompt_capabilities: [],
  system_prompt_snapshot: "",
  final_prompt_sha256: null,
  final_prompt_chars: 0,
  terminal_reason: null,
  exit_code: null,
  permission_denials: [],
  usage: null,
  preprocessing_versions: {},
  errors: [],
  // 실제 보고서는 길다. 순서가 어긋나면 화면 밖에서 열린다는 것이 이 길이다.
  result_text: `# 분석 결과\n\n${"본문 문단.\n\n".repeat(200)}`,
  attachments: [],
  created_at: new Date().toISOString(),
} as unknown as Job;

const provider = {
  provider: "agy",
  display_name: "agy",
  capabilities: { web_search: true },
  notes: [],
} as unknown as ProviderInfo;

vi.mock("../lib/api", () => ({
  api: {
    listPrompts: vi.fn(async () => []),
    listProviders: vi.fn(async () => [provider]),
    settings: vi.fn(async () => ({
      values: { default_provider: "agy", max_inline_chars: 0 },
    })),
    historyItem: vi.fn(async () => job),
    getJob: vi.fn(async () => job),
  },
}));

const { RunSessionProvider } = await import("../lib/runSession");
const { default: RunPage } = await import("./RunPage");

afterEach(cleanup);

describe("미대응 구성 검색", () => {
  it("구성 블록 오류를 보고서 앞에 표시하고 본문은 유지한다", async () => {
    const { api } = await import("../lib/api");
    const invalid = {
      ...job,
      analysis_manifest: null,
      analysis_manifest_error: "구성 4의 unreadable 상태에는 유사도를 부여하지 않아야 합니다.",
    };
    vi.mocked(api.historyItem).mockResolvedValueOnce(invalid);
    vi.mocked(api.getJob).mockResolvedValueOnce(invalid);
    window.location.hash = `#/analysis?job=${JOB_ID}`;
    render(
      <RunSessionProvider>
        <HashRouter><RunPage kind="patent_analysis" /></HashRouter>
      </RunSessionProvider>,
    );
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(invalid.analysis_manifest_error);
    const report = document.querySelector(".result");
    expect(report).toBeTruthy();
    expect(alert.compareDocumentPosition(report!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect((screen.getByRole("button", { name: /미대응 구성 검색/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("보고서보다 앞에 선택 화면을 연다", async () => {
    window.location.hash = `#/analysis?job=${JOB_ID}`;
    render(
      <RunSessionProvider>
        <HashRouter>
          <RunPage kind="patent_analysis" />
        </HashRouter>
      </RunSessionProvider>,
    );

    const button = await screen.findByRole("button", { name: /미대응 구성 검색/ });
    await userEvent.click(button);

    const panel = await waitFor(() => {
      const found = document.querySelector(".gap-search-panel");
      if (!found) throw new Error("선택 화면이 열리지 않았습니다.");
      return found;
    });
    const report = document.querySelector(".result");
    expect(report).toBeTruthy();
    // 검색 대상 구성만 고를 수 있어야 한다.
    expect(panel.querySelectorAll("input[type=checkbox]").length).toBe(1);
    expect(
      panel.compareDocumentPosition(report!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
