import { describe, expect, it } from "vitest";

import type { SearchCandidate } from "../lib/types";
import { classificationView } from "./SearchManifestView";

function candidate(
  overrides: Partial<SearchCandidate> = {},
): SearchCandidate {
  return {
    index: 1,
    group: "A",
    provisional: true,
    channel: "web",
    doc_type: "patent",
    doc_number: "EP1234567A1",
    doi: "",
    title: "",
    applicant: "",
    url: "",
    canonical_url: "",
    family: "",
    provenance: "search_snippet",
    evidence_status: "candidate_only",
    original_verified: false,
    page_fetch_succeeded: false,
    verbatim_excerpt: "원문에서 확인되지 않음",
    source_location: "확인 필요",
    mapping: [],
    note: "",
    ...overrides,
  };
}

describe("safe A/B interpretation", () => {
  it("treats a legacy group without saved evidence as provisional", () => {
    expect(classificationView(candidate())).toEqual({
      group: null,
      provisionalGroup: "A",
      basis: "legacy_unknown",
      outcome: "unverified",
    });
  });

  it("keeps an explicitly rejected group as a search proposal", () => {
    expect(
      classificationView(candidate({ group: "B", group_eligible: false })),
    ).toEqual({
      group: null,
      provisionalGroup: "B",
      basis: "search_result",
      outcome: "unverified",
    });
  });

  it("keeps a past C readable and marks it as a legacy classification", () => {
    expect(
      classificationView(
        candidate({
          group: "C",
          group_eligible: true,
          page_fetch_succeeded: true,
          identifier_url_matched: true,
          page_supported_rows: 1,
        }),
      ),
    ).toEqual({
      group: "C",
      provisionalGroup: null,
      basis: "page_observed",
      outcome: "legacy_c",
    });
  });

  it("keeps a page-supported A/B group formal", () => {
    expect(
      classificationView(
        candidate({
          group: "B",
          group_eligible: true,
          page_fetch_succeeded: true,
          identifier_url_matched: true,
          page_supported_rows: 1,
        }),
      ),
    ).toEqual({
      group: "B",
      provisionalGroup: null,
      basis: "page_observed",
      outcome: "",
    });
  });

  it("separates a verified below-threshold candidate from an unverified one", () => {
    expect(
      classificationView(
        candidate({ group: null, classification_outcome: "below_threshold" }),
      ).outcome,
    ).toBe("below_threshold");
    expect(classificationView(candidate({ group: null })).outcome).toBe(
      "unverified",
    );
  });
});
