/**
 * 감사 패널이 **실제로 그려지는가** — 세 축을 구분해서.
 *
 * 화면에서 섞이면 안 되는 세 가지가 있다.
 *
 *   page_classification   공식 대조로 덮이기 전의 1차 분류. 지금 분류가 아니다.
 *   발견 경로             웹이 찾았나, EPO 독립 검색이 찾았나, 둘 다인가.
 *   공식 분류 상태        공식 응답에 구성 대응이 실제로 대조됐는가.
 *
 * 셋을 한 줄에 뭉치면 사용자는 "AI 가 그렇게 봤다"와 "ARIA 가 대조했다"를
 * 구별할 수 없다. 이 파일은 그 구별이 DOM 에 남아 있는지 본다.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import SearchManifestView, { linkableUrl } from "./SearchManifestView";
import type { Job, SearchCandidate, SearchManifest } from "../lib/types";

afterEach(cleanup);

function candidate(overrides: Partial<SearchCandidate> = {}): SearchCandidate {
  return {
    index: 1,
    group: null,
    provisional_group: null,
    provisional: true,
    channel: "web",
    doc_type: "patent",
    doc_number: "EP1000000A1",
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

function manifest(overrides: Partial<SearchManifest> = {}): SearchManifest {
  return {
    version: 9,
    generated_at: "2026-08-31T00:00:00+00:00",
    channels: ["web", "patent_db"],
    input: { claim_text: "청구항 1.", claim_boundary_neutralized: false },
    prompt: { id: "search_prompt.md", sha256: "0".repeat(64) },
    policy: {
      name: "web_search",
      allowed_tools: ["WebSearch", "WebFetch"],
      advertised_tools_enforced: true,
      max_rounds: 2,
      search_domain_restriction: false,
    },
    timing: { started_at: null, completed_at: null },
    observed: {
      tool_names: [],
      tool_call_counts: {},
      tool_calls: [],
      search_queries: [],
      attempted_fetch_urls: [],
      succeeded_fetch_urls: [],
      tool_failures: [],
    },
    reported: { rounds: [], candidates: [], access_failures: [] },
    normalization_notes: [],
    error: null,
    ...overrides,
  } as SearchManifest;
}

function panel(value: SearchManifest) {
  const asJob = {
    search_manifest: value,
    search_manifest_error: null,
  } as unknown as Job;
  return <SearchManifestView job={asJob} />;
}

function withCandidates(...items: SearchCandidate[]): SearchManifest {
  return manifest({
    reported: { rounds: [], candidates: items, access_failures: [] },
  });
}

describe("발견 경로", () => {
  it("EPO 독립 검색이 찾은 후보를 웹 후보와 구분해서 표시한다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "B",
            group_eligible: true,
            channel: "patent_db",
            provenance: "official_record_response",
            evidence_status: "official_record_verified",
            classification_basis: "official_record",
            official_supported_rows: 2,
            official_evidence: { artifact_ids: ["a".repeat(64)] },
            discovery_origins: ["epo"],
            epo_discovery: {
              lanes: ["epo:claim_only"],
              artifact_ids: ["a".repeat(64)],
              shortlist: [{ reason: "힘 센서가 그대로 개시됨" }],
            },
          }),
        ),
      ),
    );

    // 요약 줄과 후보 칩 두 곳에 나온다.
    expect(screen.getAllByText("EPO 독립 검색이 발견").length).toBeGreaterThan(0);
    expect(screen.getByText(/EPO 검색 레인/)).toBeTruthy();
    expect(screen.getByText(/힘 센서가 그대로 개시됨/)).toBeTruthy();
    // 열지 않은 웹 페이지를 "확인 실패"로 적지 않는다.
    expect(screen.getByText(/해당 없음\(웹 페이지를 열지 않는 경로\)/)).toBeTruthy();
    expect(
      screen.getByText(/웹 페이지 관측이 없으므로 페이지 근거 분류는 만들지 않으며/),
    ).toBeTruthy();
  });

  it("두 채널이 모두 찾은 후보는 출처를 둘 다 보여 준다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "A",
            group_eligible: true,
            page_fetch_succeeded: true,
            identifier_url_matched: true,
            page_supported_rows: 1,
            discovery_origins: ["web", "epo"],
            epo_discovery: { lanes: ["epo:spec_assisted"] },
          }),
        ),
      ),
    );

    expect(screen.getByText("웹 검색이 발견")).toBeTruthy();
    expect(screen.getAllByText("EPO 독립 검색이 발견").length).toBeGreaterThan(0);
    // 요약은 두 채널에서 나온 후보를 "웹에 없던 후보"로 세지 않는다.
    expect(screen.getByText(/웹에 없던 후보 0건/)).toBeTruthy();
  });
});

describe("page_classification", () => {
  it("공식 분류로 덮이기 전의 1차 분류를 별도 줄로 보존해 보여 준다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "C",
            group_eligible: true,
            evidence_status: "official_record_verified",
            classification_basis: "official_record",
            official_supported_rows: 1,
            official_evidence: { artifact_ids: ["a".repeat(64)] },
            page_classification: {
              group: "A",
              classification_basis: "page_observed",
              page_supported_rows: 2,
            },
          }),
        ),
      ),
    );

    // 지금 분류는 공식 대조 결과다.
    expect(screen.getByText("공식 기록 대조가 있는 AI 분류")).toBeTruthy();
    // 덮인 1차 분류는 별도 줄에 그 사실과 함께 남는다.
    expect(
      screen.getByText(/공식 대조 결과와 달라 공식 분류를 채택했습니다/),
    ).toBeTruthy();
    expect(screen.getByText(/페이지 관측 근거가 있는 AI 분류/)).toBeTruthy();
  });
});

describe("공식 분류 상태", () => {
  it("공식 범위에서 A/B를 더 확인하지 못한 페이지 분류를 강등하지 않고 표시한다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "A",
            group_eligible: true,
            provisional: false,
            classification_basis: "page_observed",
            evidence_status: "source_page_reviewed",
            page_fetch_succeeded: true,
            official_ab_confirmation: "not_confirmed",
            official_ab_confirmation_detail:
              "공식 문헌에서 확보한 범위에서는 A/B 근거를 추가 확인하지 못했습니다.",
          }),
        ),
      ),
    );

    expect(
      screen.getByRole("heading", {
        name: "A · 전체 구조와 핵심 특징이 모두 강하게 유사",
      }),
    ).toBeTruthy();
    const badge = screen.getByText("공식 추가 확인 못함");
    expect(badge.getAttribute("title")).toContain(
      "공식 문헌에서 확보한 범위에서는",
    );
  });

  it("공식 기록으로 분류된 후보 수를 따로 센다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "B",
            group_eligible: true,
            evidence_status: "official_record_verified",
            classification_basis: "official_record",
            official_supported_rows: 3,
            matched_feature_rows: 3,
            official_evidence: { artifact_ids: ["a".repeat(64)] },
            discovery_origins: ["epo"],
          }),
          candidate({
            index: 2,
            doc_number: "US9876543B2",
            provisional_group: "C",
            classification_basis: "search_result",
            group_eligible: false,
          }),
        ),
      ),
    );

    expect(screen.getByText(/공식 기록 대조로 분류/)).toBeTruthy();
    expect(screen.getByText(/공식 기록에서 대조된 구성 행 3개/)).toBeTruthy();
  });

  it("아티팩트 재사용과 상한 제외 사유를 검증 패널에 남긴다", () => {
    render(
      panel(
        manifest({
          reported: {
            rounds: [],
            candidates: [candidate()],
            access_failures: [],
          },
          verification: {
            attempted: true,
            reason: "",
            counts: {
              targets: 1,
              verified: 1,
              fetch_failed: 0,
              not_attempted: 1,
            },
            usage: {
              official_fetch_calls: 0,
              reused_artifact_calls: 2,
              classification_runs: 1,
            },
            excluded_candidates: [
              {
                index: 2,
                doc_number: "US9876543B2",
                reason_code: "verification_target_limit",
                detail: "공식 검증 후보 상한(1건)을 넘어 시도하지 않았습니다.",
              },
            ],
          },
        }),
      ),
    );

    expect(screen.getByText(/같은 자료를 다시 내려받지 않았습니다/)).toBeTruthy();
    expect(screen.getByText("상한 때문에 처리하지 않은 것")).toBeTruthy();
    expect(
      screen.getByText(/공식 검증 후보 상한\(1건\)을 넘어 시도하지 않았습니다/),
    ).toBeTruthy();
  });

  it("일부만 재사용한 문헌을 추가 조회 0회로 적지 않는다", () => {
    render(
      panel(
        manifest({
          reported: {
            rounds: [],
            candidates: [candidate()],
            access_failures: [],
          },
          verification: {
            attempted: true,
            reason: "",
            counts: {
              targets: 2,
              verified: 2,
              fetch_failed: 0,
              not_attempted: 0,
            },
            usage: {
              official_fetch_calls: 2,
              reused_artifact_calls: 2,
              fully_reused_documents: 1,
              partially_reused_documents: 1,
              reuse_plan_unknown_documents: 0,
              reused_without_fresh_fetch_documents: 1,
              reused_with_fresh_fetch_documents: 1,
              planned_fetch_calls: 2,
              classification_runs: 1,
            },
          },
        }),
      ),
    );

    expect(
      screen.getByText(/선택 당시 계획은 완전 재사용 1건 · 부분 재사용 1건이며/),
    ).toBeTruthy();
    expect(screen.getByText(/실제 추가 호출 없이 끝난 재사용 문헌은 1건/)).toBeTruthy();
  });
});

describe("계획 턴의 도구 호출", () => {
  it("감지된 레인과 격리 수준을 접지 않고 보여 준다", () => {
    render(
      panel(
        manifest({
          epo: {
            enabled: true,
            lanes: [
              {
                id: "epo:claim_only",
                status: "ok",
                termination_reason: "unauthorized_tool_use",
                tool_violations: [
                  {
                    provider: "codex",
                    tools: ["web_search"],
                    isolation: "post_hoc_detection",
                  },
                ],
              },
            ],
          },
        }),
      ),
    );

    expect(
      screen.getByText("EPO 계획 턴에서 도구 호출이 감지되었습니다"),
    ).toBeTruthy();
    expect(screen.getByText(/하나도 실행하지 않고 폐기/)).toBeTruthy();
    // 사후 탐지는 차단이 아니다. 외부 호출이 이미 나갔다는 사실이 화면에
    // 그대로 남아야 한다 — 여기서 문구가 약해지면 기록이 보증하지 못하는
    // 것을 보증하는 것처럼 읽힌다.
    expect(screen.getByText("사후 탐지는 차단이 아닙니다.")).toBeTruthy();
    expect(screen.getByText(/이미 나갔고 되돌릴 수 없으며/)).toBeTruthy();
    expect(
      screen.getByText(/사후 탐지 — 호출을 막지 못했고 이미 나간 외부 호출은/),
    ).toBeTruthy();
  });
});

describe("EPO 검색 전략", () => {
  it("첫 응답의 청구항 분석을 검색 기록 패널에서 볼 수 있다", () => {
    render(
      panel(
        manifest({
          epo: {
            enabled: true,
            lanes: [
              {
                id: "epo:claim_only",
                status: "ok",
                termination_reason: "llm_finished",
                claim_analysis: {
                  elements: [
                    {
                      id: "E1",
                      text: "로봇 팔",
                      essential: true,
                      synonyms: ["robot arm"],
                    },
                    // 필수 여부를 적지 않은 구성. 없는 판단을 만들지 않는다.
                    { id: "E2", text: "제어부" },
                  ],
                  relations: [
                    {
                      source: "E2",
                      target: "E1",
                      kind: "제어",
                      description: "제어부가 로봇 팔을 제어한다",
                    },
                  ],
                  concept_combinations: [
                    {
                      elements: ["E1", "E2"],
                      terms: ["robot arm", "controller"],
                      reason: "핵심 조합",
                    },
                  ],
                  search_conditions: [
                    { kind: "ipc", value: "B25J 9/16", reason: "로봇 제어 분류" },
                  ],
                },
              },
            ],
          },
        }),
      ),
    );

    // 감사 패널은 접혀 있다가 펼쳐진다.
    fireEvent.click(screen.getByText("실제 검색 기록 보기"));

    expect(
      screen.getByText("EPO 검색 전략 (청구항 분석 — 모델 판단)"),
    ).toBeTruthy();
    expect(screen.getByText("로봇 팔")).toBeTruthy();
    expect(screen.getByText("robot arm")).toBeTruthy();
    expect(screen.getByText("필수")).toBeTruthy();
    expect(screen.getByText("판단 없음")).toBeTruthy();
    expect(screen.getByText(/제어부가 로봇 팔을 제어한다/)).toBeTruthy();
    expect(screen.getByText(/핵심 조합/)).toBeTruthy();
    expect(screen.getByText("B25J 9/16")).toBeTruthy();
  });
});

describe("미확인 검색 단서", () => {
  it("EPO 단독 후보를 웹 게이트 문구로 설명하지 않는다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            channel: "patent_db",
            provenance: "official_record_response",
            discovery_origins: ["epo"],
            url: "https://ops.example.test/EP1000000A1",
            verification: {
              status: "not_attempted",
              reason_code: "verification_target_limit",
              detail: "공식 검증 후보 상한(1건)을 넘었습니다.",
              backend_id: "epo",
              artifact_ids: [],
            },
          }),
        ),
      ),
    );

    expect(screen.getByText("공식 근거 대조 전")).toBeTruthy();
    expect(screen.queryByText("그룹 제외")).toBeNull();
    expect(
      screen.getByText(/공식 검증 미시도 — 공식 검증 후보 상한\(1건\)을 넘었습니다/),
    ).toBeTruthy();
    expect(screen.getByText(/공식 응답의 주소:/)).toBeTruthy();
    expect(screen.queryByText(/모델이 제시한 주소/)).toBeNull();
  });
});

describe("웹 채널 실패와 EPO 결과의 공존", () => {
  it("EPO 후보만으로 만든 목록임을 결과보다 먼저 알린다", () => {
    render(
      panel(
        manifest({
          reported: {
            rounds: [],
            candidates: [
              candidate({
                channel: "patent_db",
                provenance: "official_record_response",
                discovery_origins: ["epo"],
              }),
            ],
            access_failures: [],
            web_report_error: "감사 블록을 읽지 못했습니다",
          },
        }),
      ),
    );

    expect(screen.getByText("웹 채널의 검색 결과를 읽지 못했습니다")).toBeTruthy();
    expect(
      screen.getByText(/웹 검색이 찾은 문헌은 하나도 들어 있지 않습니다/),
    ).toBeTruthy();
    expect(screen.getByText(/감사 블록을 읽지 못했습니다/)).toBeTruthy();
  });

  it("웹이 성공한 실행에는 그 경고를 띄우지 않는다", () => {
    render(panel(withCandidates(candidate())));
    expect(
      screen.queryByText("웹 채널의 검색 결과를 읽지 못했습니다"),
    ).toBeNull();
  });
});

describe("검증 대상 선택 순서", () => {
  it("무엇을 왜 골랐는지와 무엇이 잘렸는지를 함께 보여 준다", () => {
    render(
      panel(
        manifest({
          reported: {
            rounds: [],
            candidates: [candidate()],
            access_failures: [],
          },
          verification: {
            attempted: true,
            reason: "",
            counts: {
              targets: 1,
              verified: 1,
              fetch_failed: 0,
              not_attempted: 1,
            },
            selection_order: [
              {
                position: 1,
                index: 4,
                doc_number: "EP1000000A1",
                selection_reason: "reusable_official_artifact",
                selection_bucket: "page_ab",
                detail:
                  "이미 받아 둔 공식 응답이 있어 추가 조회 없이 대조할 수 있음",
              },
            ],
            excluded_candidates: [
              {
                index: 1,
                doc_number: "US9876543B2",
                reason_code: "verification_target_limit",
                detail: "공식 검증 후보 상한(1건)을 넘었습니다.",
              },
            ],
          },
        }),
      ),
    );

    expect(screen.getByText("공식 검증 대상 선택 순서")).toBeTruthy();
    expect(screen.getByText("페이지 A/B 공식 근거 보강")).toBeTruthy();
    expect(
      screen.getByText(/이미 받아 둔 공식 응답이 있어 추가 조회 없이/),
    ).toBeTruthy();
    expect(screen.getByText("상한 때문에 처리하지 않은 것")).toBeTruthy();
  });
});

describe("최종 선택 턴", () => {
  it("검색 라운드와 따로, 거절된 검색 action까지 보여 준다", () => {
    render(
      panel(
        manifest({
          epo: {
            enabled: true,
            lanes: [
              {
                id: "epo:claim_only",
                status: "ok",
                termination_reason: "llm_finished",
                selection: {
                  attempted: true,
                  status: "ok",
                  candidates_reviewed: 2,
                  shortlist_added: 1,
                  rejected_actions: 2,
                },
              },
            ],
          },
        }),
      ),
    );

    fireEvent.click(screen.getByText("실제 검색 기록 보기"));

    expect(screen.getByText("최종 선택 턴 (검색 없음)")).toBeTruthy();
    expect(screen.getByText(/OPS 를 부르지 않습니다/)).toBeTruthy();
    expect(screen.getByText(/추가된 shortlist 1건/)).toBeTruthy();
    expect(screen.getByText(/거절된 검색 action 2건/)).toBeTruthy();
  });

  it("돌리지 않은 턴은 사유를 남긴다", () => {
    render(
      panel(
        manifest({
          epo: {
            enabled: true,
            lanes: [
              {
                id: "epo:claim_only",
                status: "ok",
                selection: {
                  attempted: false,
                  reason: "EPO 채널 제한시간(180초)을 넘겨 돌리지 않았습니다.",
                },
              },
            ],
          },
        }),
      ),
    );

    fireEvent.click(screen.getByText("실제 검색 기록 보기"));
    expect(screen.getByText(/돌리지 않음 — EPO 채널 제한시간/)).toBeTruthy();
  });
});

describe("미검증 제목", () => {
  it("페이지를 열지 못한 후보도 제목·링크·상태를 함께 보여 준다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            reported_title: "HumanRig: Learning Automatic Rigging",
            url: "https://arxiv.org/abs/2412.02317",
            quarantined: true,
            quarantine_reason: "이 주소로 성공한 페이지 열람 기록이 없습니다.",
          }),
        ),
      ),
    );

    // 라벨 없이 제목만 나가면 검증된 명칭으로 읽힌다. 셋은 항상 함께 나간다.
    expect(screen.getByText("검색 결과 기반 · 미검증")).toBeTruthy();
    expect(
      screen.getByText(/HumanRig: Learning Automatic Rigging/),
    ).toBeTruthy();
    expect(
      screen.getByText(/페이지 직접 확인 안 됨 — 사용자가 수동 확인 필요/),
    ).toBeTruthy();
    // 링크는 클릭할 수 있어야 한다. 사용자가 직접 확인하는 경로가 이것뿐이다.
    const link = screen.getByRole("link", {
      name: "https://arxiv.org/abs/2412.02317",
    });
    expect(link.getAttribute("href")).toBe("https://arxiv.org/abs/2412.02317");
  });

  it("검증된 명칭이 있으면 미검증 제목을 보여 주지 않는다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            group: "A",
            group_eligible: true,
            page_fetch_succeeded: true,
            identifier_url_matched: true,
            page_supported_rows: 1,
            title: "페이지에서 확인한 명칭",
            reported_title: "검색 결과에서 본 제목",
          }),
        ),
      ),
    );

    expect(screen.getByText("페이지에서 확인한 명칭")).toBeTruthy();
    expect(screen.queryByText("검색 결과 기반 · 미검증")).toBeNull();
    expect(screen.queryByText(/검색 결과에서 본 제목/)).toBeNull();
  });

  it("제목이 없으면 빈 라벨만 남기지 않는다", () => {
    render(panel(withCandidates(candidate({ url: "https://example.com/x" }))));

    expect(screen.queryByText("검색 결과 기반 · 미검증")).toBeNull();
  });
});

describe("링크로 만들 수 있는 주소", () => {
  // 후보의 url 은 모델이 적은 값이고, 모델의 입력에는 검색 결과와 페이지 본문이
  // 섞여 있다. 즉 비신뢰 데이터가 도달할 수 있는 자리다. 렌더러의 sanitize 는
  // 마지막 방어선이지 유일한 방어선이 아니어야 한다.
  const dangerous = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///C:/Windows/System32/drivers/etc/hosts",
    "vbscript:msgbox(1)",
  ];

  it.each(dangerous)("%s 는 링크로 만들지 않는다", (url) => {
    expect(linkableUrl(url)).toBeNull();
  });

  it.each([
    "",
    "   ",
    "확인 필요",
    "patents.example.com/AB1234",
    "://broken",
    "https://",
  ])("파싱할 수 없거나 절대 주소가 아닌 %s 도 링크가 아니다", (url) => {
    expect(linkableUrl(url)).toBeNull();
  });

  it.each(["https://arxiv.org/abs/2412.02317", "http://www.kipris.or.kr/AB1234"])(
    "%s 는 링크로 만든다",
    (url) => {
      expect(linkableUrl(url)).toBe(url);
    },
  );

  it("위험한 스킴은 anchor 없이 평문으로만 보여 준다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            reported_title: "미검증 제목",
            url: "javascript:alert(1)",
            quarantined: true,
          }),
        ),
      ),
    );

    // 값은 지우지 않는다. 모델이 무엇을 적었는지도 기록이다.
    expect(screen.getAllByText("javascript:alert(1)").length).toBeGreaterThan(0);
    // 다만 클릭할 수 있는 자리에 놓지 않는다.
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("http 주소는 새 탭으로 여는 링크가 된다", () => {
    render(
      panel(
        withCandidates(
          candidate({
            reported_title: "미검증 제목",
            url: "https://arxiv.org/abs/2412.02317",
            quarantined: true,
          }),
        ),
      ),
    );

    const link = screen.getByRole("link", {
      name: "https://arxiv.org/abs/2412.02317",
    });
    expect(link.getAttribute("href")).toBe("https://arxiv.org/abs/2412.02317");
    expect(link.getAttribute("rel")).toContain("noopener");
  });
});
