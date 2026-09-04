import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import SearchManifestView, { linkableUrl } from "./SearchManifestView";
import type { Job, SearchManifestV14 } from "../lib/types";
afterEach(cleanup);
function current(): SearchManifestV14 {
  return {
    version: 14, status: "complete", provider: "claude", model: "", group_definitions: { A: "test group" },
    input: { claim_text: "", spec_document: null, search_focus: null },
    prompt: { id: "", name: "", sha256: "", runtime_context_sha256: "" },
    started_at: "", completed_at: "", limits: { max_tool_calls: 40, timeout_seconds: 900 },
    tool_availability: { epo: { status: "disabled", detail: "연동 꺼짐" } }, tool_journal: [],
    observed: { tool_calls: [], tool_call_counts: {}, search_queries: ["actual query"], search_call_count: 1, attempted_fetch_urls: [], succeeded_fetch_urls: [], url_lookup_attempts: [], tool_failures: [], unknown_tool_outcomes: [] },
    llm_output: { candidate: "<img src=x onerror=alert(1)>" },
    reported: { candidates: [{
      index: 1, rank: 1, group: "C", doc_type: "patent", doc_number: "EP123A1",
      doi: "", title: "모델 제목", url: "javascript:alert(1)", note: "기술 설명", applicant: "",
      family: "", publication_date: "", reported_publication_date: "",
      evidence_level: "search_snippet_only", verification_issues: ["identifier_unverified"],
      verification_scope: {}, evidence_sources: [], mapping: [],
    }], rounds: [], term_expansions: [], access_failures: [] },
    date_filter: { cutoff: "", applied: false, excluded: [], unknown_publication_date: 1 },
    usage: null, normalization_notes: [], error: null,
  };
}
describe("single-agent audit", () => {
  it("shows C independently of unverified evidence", () => {
    render(<SearchManifestView job={{ search_manifest: current() } as Job} />);
    expect(screen.getByText(/LLM C/)).toBeTruthy();
    expect(screen.getByText("식별 미확인")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/연동 꺼짐/)).toBeTruthy();
    expect(screen.getByText(/actual query/)).toBeTruthy();
  });
  it("shows old versions only as saved audit", () => {
    render(<SearchManifestView job={{ search_manifest: { version: 13, reported: { candidates: [{ group: "C" }] } } } as unknown as Job} />);
    expect(screen.getByText(/재분류·재검증하지 않았습니다/)).toBeTruthy();
  });
  it("keeps incomplete status visible", () => {
    const data = current(); data.status = "incomplete"; data.error = "취소됨"; data.reported = null;
    render(<SearchManifestView job={{ search_manifest: data } as Job} />);
    expect(screen.getByRole("alert").textContent).toContain("취소됨");
  });
  it.each(["javascript:alert(1)", "file:///a", "data:text/html,x", "https://user:pass@example.com", "https://example.com/a b"])("rejects unsafe URL %s", (url) => {
    expect(linkableUrl(url)).toBeNull();
  });
  it("permits https document links", () => expect(linkableUrl("https://doi.org/10.1000/a")).toBe("https://doi.org/10.1000/a"));
});
