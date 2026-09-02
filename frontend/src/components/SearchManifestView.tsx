/** 검색 감사 기록 표시.
 *
 *  보고서 본문과 따로 둔다. 여기 있는 것은 "무엇을 검색했고, 각 후보를 무엇으로
 *  알게 되었는가"이고, 그건 보고서의 결론과 다른 종류의 정보다.
 *
 *  두 층을 화면에서도 섞지 않는다.
 *    ARIA 가 관측한 것 : 실제로 나간 검색어, 연 URL, 실패한 호출
 *    모델이 보고한 것   : 후보 목록과 라운드 설명
 *  전자만 ARIA 가 보증한다.
 */

import { useState } from "react";

import type {
  Job,
  SearchCandidate,
  SearchDiscoveryOrigin,
  SearchManifest,
  SearchProvenance,
} from "../lib/types";

const PROVENANCE_LABEL: Record<SearchProvenance, string> = {
  search_snippet: "검색 스니펫만 확인 (페이지 미열람)",
  webfetch_summary: "페이지 요약 확인 (원문 아님)",
  raw_original_verified: "원문 대조 완료",
  official_record_response: "EPO 공식 응답에서 발견 (원문 인용 아님)",
};

const PROVENANCE_CLASS: Record<SearchProvenance, string> = {
  search_snippet: "neutral",
  webfetch_summary: "warn",
  raw_original_verified: "ok",
  official_record_response: "accent",
};

/** 이 후보를 데려온 검색 경로. 값이 없는 옛 기록은 web 하나로 읽는다.
 *
 *  ORIGIN_LABEL(청구항 단독 / 명세서 확장)과 **다른 축**이다. 저쪽은 무엇을
 *  입력으로 검색했는가이고 이쪽은 어느 경로가 이 문헌을 찾았는가다.
 */
const DISCOVERY_LABEL: Record<SearchDiscoveryOrigin, string> = {
  web: "웹 검색이 발견",
  epo: "EPO 독립 검색이 발견",
};

export function discoveryOrigins(
  item: SearchCandidate,
): SearchDiscoveryOrigin[] {
  const raw = item.discovery_origins;
  const found = (["web", "epo"] as const).filter(
    (origin) => Array.isArray(raw) && raw.includes(origin),
  );
  return found.length > 0 ? [...found] : ["web"];
}

// 사후 탐지는 **차단이 아니다.** 이 값이 붙은 실행에서 외부 호출은 이미 나갔고,
// ARIA 가 한 일은 그 응답을 검색 계획으로 쓰지 않기로 한 것뿐이다.
const ISOLATION_LABEL: Record<string, string> = {
  provider_enforced: "CLI 단계에서 도구 차단(그런데도 호출이 관측됨)",
  post_hoc_detection:
    "사후 탐지 — 호출을 막지 못했고 이미 나간 외부 호출은 되돌릴 수 없음",
  unknown: "도구 통제 수준 확인 불가",
};

const EVIDENCE_LABEL: Record<string, string> = {
  candidate_only: "후보 단계",
  source_page_reviewed: "페이지 열람 성공",
  official_record_verified: "EPO 공식 기록 대조",
};

const CLASSIFICATION_LABEL: Record<string, string> = {
  none: "분류 없음",
  legacy_unknown: "과거 분류(검증 근거 미기록)",
  search_result: "검색 결과 기반 AI 잠정 분류",
  page_observed: "페이지 관측 근거가 있는 AI 분류",
  official_record: "공식 기록 대조가 있는 AI 분류",
  original_text: "원문 직접 대조가 있는 AI 분류",
};

const VERIFICATION_LABEL: Record<string, string> = {
  not_attempted: "공식 검증 미시도",
  fetch_failed: "공식 문헌 확보 실패",
  record_fetched: "공식 문헌 확보 완료",
  classification_failed: "2차 분류 실패",
  evidence_mismatch: "근거 문장 대조 실패",
  promoted: "공식 근거 분류 완료",
};

const VERIFICATION_BUCKET_LABEL: Record<string, string> = {
  page_ab: "페이지 A/B 공식 근거 보강",
  epo_shortlist: "EPO 검색 후보 검증",
  other: "남은 자리 보충",
};

function verificationDetail(value?: string): string {
  const text = (value ?? "").trim();
  if (!/<fault|<\?xml/i.test(text)) {
    return text.length > 600 ? `${text.slice(0, 600)}…` : text;
  }
  const decode = (part: string) =>
    part
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&")
      .replace(/\s+/g, " ")
      .trim();
  const codes = [...text.matchAll(/<code>([\s\S]*?)<\/code>/gi)].map((match) =>
    decode(match[1]),
  );
  const messages = [...text.matchAll(/<message>([\s\S]*?)<\/message>/gi)].map(
    (match) => decode(match[1]).slice(0, 240),
  );
  const status = text.match(/HTTP\s+(\d+)/i)?.[1];
  return [
    status ? `EPO OPS HTTP ${status}` : "EPO OPS 조회 오류",
    ...Array.from(new Set([...codes, ...messages])),
  ]
    .join(" · ")
    .slice(0, 600);
}

const ORIGIN_LABEL: Record<string, string> = {
  claim_only: "청구항 단독",
  spec_assisted: "명세서 확장",
};

// 근거 칸을 결론 칸보다 왼쪽에 둔다. 대응 정도를 읽기 전에 그 판단이 무엇에
// 기대고 있는지를 먼저 보게 하려는 것이다.
const MAPPING_HEADERS = [
  "청구항의 기술적 특징",
  "근거 출처",
  "관측 근거 텍스트",
  "검토 범위",
  "대응 정도",
  "대응 내용",
  "원문 위치",
  "원문 직접 발췌",
  "한국어 번역",
  "유사한 점",
  "차이가 있는 점",
];

/** 그룹 정의가 실려 오지 않는 옛 매니페스트용 fallback.
 *
 *  정의의 단일 출처는 backend/app/search_manifest.py 의 GROUP_DEFINITIONS 이고
 *  실행마다 매니페스트에 실려 온다. 렌더러가 자기 표를 들고 있으면 정의를 고친
 *  뒤 갱신되지 않은 렌더러가 옛 제목으로 인쇄한다 — 2026-08-25 실행에서 실제로
 *  B 와 C 가 뒤바뀌어 나갔다. 이 표는 정의를 싣지 않는 옛 기록에만 쓴다.
 */
const LEGACY_GROUP_TITLE: Record<string, string> = {
  A: "전체 구조와 핵심 특징이 모두 강하게 유사",
  B: "전체 구조는 다르지만 핵심 특징 또는 핵심 관계가 강하게 유사",
  C: "전체 구조는 유사하지만 핵심 대응은 부분적",
};

function groupTitle(manifest: SearchManifest, group: string): string {
  const stored = manifest.group_definitions?.[group];
  return `${group} · ${stored || LEGACY_GROUP_TITLE[group] || ""}`;
}

const SUPPORT_LABEL: Record<string, string> = {
  page_text: "페이지 관측",
  official_record: "공식 문헌 대조",
  snippet: "검색 스니펫",
  none: "근거 없음",
};

/** 새 실행이 만드는 그룹. C 는 과거 기록에서만 온다. */
const WRITE_GROUPS = ["A", "B"] as const;
/** 읽을 수 있는 그룹. 과거 매니페스트의 C 를 계속 표시한다. */
const READ_GROUPS = ["A", "B", "C"] as const;

type SearchGroup = "A" | "B" | "C";
type ClassificationOutcome = "" | "below_threshold" | "unverified" | "legacy_c";
type ClassificationView = {
  group: SearchGroup | null;
  provisionalGroup: SearchGroup | null;
  basis: string;
  outcome: ClassificationOutcome;
};

/** 정식 A/B 가 아니라면 왜인가. 저장값을 고치지 않고 읽기만 한다. */
function outcomeFor(
  item: SearchCandidate,
  group: string | null,
  fallback: ClassificationOutcome,
): ClassificationOutcome {
  if (group === "C") return "legacy_c";
  const stored = item.classification_outcome;
  if (stored === "below_threshold" || stored === "unverified") return stored;
  return fallback;
}

/** 과거 group을 새 기준의 정식 분류로 자동 승격하지 않는 읽기 규칙. */
export function classificationView(item: SearchCandidate): ClassificationView {
  const rawGroup = item.group && ["A", "B", "C"].includes(item.group)
    ? item.group
    : null;
  const rawProvisional =
    item.provisional_group && ["A", "B", "C"].includes(item.provisional_group)
      ? item.provisional_group
      : null;
  const officialSaved = Boolean(
    item.evidence_status === "official_record_verified" &&
      (item.official_evidence?.artifact_ids?.length ?? 0) > 0 &&
      (item.official_supported_rows ?? 0) > 0,
  );
  const originalSaved = Boolean(item.original_verified && rawGroup);
  const pageSaved = Boolean(
    item.identifier_url_matched &&
      item.page_fetch_succeeded &&
      (item.page_supported_rows ?? 0) > 0,
  );
  const formal =
    item.group_eligible === true || officialSaved || originalSaved || pageSaved;
  if (rawGroup && formal) {
    const stored = item.classification_basis;
    const basis =
      stored === "page_observed" ||
      stored === "official_record" ||
      stored === "original_text"
        ? stored
        : originalSaved
          ? "original_text"
          : officialSaved || item.evidence_status === "official_record_verified"
            ? "official_record"
            : "page_observed";
    return {
      group: rawGroup,
      provisionalGroup: null,
      basis,
      outcome: outcomeFor(item, rawGroup, ""),
    };
  }
  const proposed = rawProvisional || rawGroup;
  if (proposed) {
    const basis =
      item.classification_basis === "search_result" ||
      item.classification_basis === "legacy_unknown"
        ? item.classification_basis
        : item.group_eligible === false || rawProvisional
          ? "search_result"
          : "legacy_unknown";
    return {
      group: null,
      provisionalGroup: proposed,
      basis,
      outcome: outcomeFor(item, proposed, "unverified"),
    };
  }
  return {
    group: null,
    provisionalGroup: null,
    basis: "none",
    outcome: outcomeFor(item, null, "unverified"),
  };
}

const SCOPE_LABEL: Record<string, string> = {
  claims: "청구항",
  full_text: "전문",
  abstract: "초록",
  unknown: "확인 필요",
};

function CandidateRow({
  item,
  gapSearch = false,
}: {
  item: SearchCandidate;
  gapSearch?: boolean;
}) {
  const identity = item.doc_number || item.doi || "문헌번호 확인 필요";
  const origins = item.search_origins ?? [];
  const classification = classificationView(item);
  const verification = item.verification;
  const discovered = discoveryOrigins(item);
  // 웹 페이지를 한 번도 열지 않는 경로로 온 후보. 웹 게이트의 문구를 그대로
  // 쓰면 "확인 실패"로 읽히지만, 이 후보에는 애초에 열어 볼 페이지가 없었다.
  const epoOnly = !discovered.includes("web");
  const pagePrior = item.page_classification;
  return (
    <li className="search-candidate">
      <div className="search-candidate-head">
        <span className="mono-text">{identity}</span>
        {!item.original_verified && (
          <span className="pill warn">원문 발췌 미검증</span>
        )}
        <span className={`pill ${PROVENANCE_CLASS[item.provenance] ?? "neutral"}`}>
          {PROVENANCE_LABEL[item.provenance] ?? item.provenance}
        </span>
        <span
          className={`pill ${item.page_fetch_succeeded ? "ok" : "neutral"}`}
          title={
            item.page_fetch_succeeded
              ? "이 주소의 페이지 본문을 실제로 읽은 기록을 ARIA 가 확인했습니다."
              : "이 주소의 페이지 본문을 읽은 기록이 없습니다. 페이지를 가져오기만 하고 본문을 읽지 않은 경우도 여기에 들어갑니다."
          }
        >
          {EVIDENCE_LABEL[item.evidence_status] ?? item.evidence_status}
        </span>
        <span className="pill neutral">{item.channel}</span>
        <span className="pill ok">
          {CLASSIFICATION_LABEL[classification.basis] ?? classification.basis}
        </span>
        {/* 공식 응답을 받아 봤지만 A/B 근거를 더 찾지 못한 후보. 분류는
            내리지 않되 그 사실을 숨기지도 않는다 — OPS 는 초록·청구항만
            주므로 명세서에만 있는 구성은 여기서 대조될 수 없다. */}
        {item.official_ab_confirmation === "not_confirmed" && (
          <span
            className="pill warn"
            title={item.official_ab_confirmation_detail || undefined}
          >
            공식 추가 확인 못함
          </span>
        )}
        {discovered.map((origin) => (
          <span
            key={`discovery-${origin}`}
            className={`pill ${origin === "epo" ? "accent" : "neutral"}`}
            title={
              origin === "epo"
                ? "ARIA 가 EPO OPS 를 직접 검색해 이 문헌을 찾았습니다."
                : "모델의 웹 검색 보고에서 이 문헌이 나왔습니다."
            }
          >
            {DISCOVERY_LABEL[origin]}
          </span>
        ))}
        {origins.map((origin) => (
          <span
            key={origin}
            className={`pill ${origin === "spec_assisted" ? "accent" : "neutral"}`}
          >
            {gapSearch && origin === "claim_only"
              ? "미대응 구성 보완"
              : ORIGIN_LABEL[origin] ?? origin}
          </span>
        ))}
      </div>
      {item.title && <div className="search-candidate-title">{item.title}</div>}
      <div className="faint">
        {item.applicant && <>{item.applicant} · </>}
        {item.family && <>패밀리 {item.family} · </>}
        원문 위치 {item.source_location}
      </div>
      {/* 공식 대조로 덮이기 전의 1차 분류. 지금 분류와 나란히 두지 않는다 —
          같은 줄에 두면 같은 위계로 읽히고, 그러면 등급을 나눈 의미가 없다. */}
      {pagePrior?.group && (
        <div className="faint">
          대체된 1차 분류: <strong>{pagePrior.group}</strong>{" "}
          {CLASSIFICATION_LABEL[pagePrior.classification_basis] ??
            pagePrior.classification_basis}{" "}
          · 페이지 근거 행 {pagePrior.page_supported_rows ?? 0}개
          {pagePrior.group === item.group
            ? " (공식 대조 결과와 같음)"
            : " — 공식 대조 결과와 달라 공식 분류를 채택했습니다"}
        </div>
      )}
      {item.epo_discovery?.lanes?.length ? (
        <div className="faint">
          EPO 검색 레인: {item.epo_discovery.lanes.join(", ")}
          {(item.epo_discovery.shortlist ?? []).map((entry, i) =>
            entry.reason ? (
              <div key={i}>EPO 선정 이유: {entry.reason}</div>
            ) : null,
          )}
          {(item.epo_discovery.artifact_ids ?? []).length > 0 && (
            <div>
              재사용한 EPO 응답 아티팩트{" "}
              {(item.epo_discovery.artifact_ids ?? []).length}건 — 공식 검증에서
              다시 내려받지 않았습니다.
            </div>
          )}
        </div>
      ) : null}
      {item.matched_feature_rows !== undefined && (
        <div className="faint">
          공식 기록에서 대조된 구성 행 {item.matched_feature_rows}개
        </div>
      )}
      {verification && (
        <div className="faint">
          후보별 공식 검증: {VERIFICATION_LABEL[verification.status] ?? verification.status}
          {verificationDetail(verification.detail)
            ? ` — ${verificationDetail(verification.detail)}`
            : ""}
        </div>
      )}
      <div className="faint">직접 발췌: {item.verbatim_excerpt}</div>
      <div className="faint">
        ARIA 관측: 페이지 본문{" "}
        {item.page_fetch_succeeded ? "읽음" : "읽은 기록 없음"} · 원문 대조{" "}
        {item.original_verified ? "완료" : "안 됨"} · 문헌번호-주소 대조{" "}
        {epoOnly
          ? "해당 없음(웹 페이지를 열지 않는 경로)"
          : item.identifier_url_matched
            ? "완료"
            : "안 됨"}
      </div>
      {epoOnly ? (
        <div className="faint">
          EPO 독립 검색이 데려온 후보입니다. 웹 페이지 관측이 없으므로 페이지
          근거 분류는 만들지 않으며, 정식 A/B는 공식 응답에 구성 대응이
          대조된 경우에만 붙습니다.
        </div>
      ) : (
        item.identifier_url_matched === false && (
          <div className="faint">
            문헌번호가 위 주소에서 확인되지 않아 명칭·출원인·패밀리를 표시하지
            않았습니다.
          </div>
        )
      )}
      {item.note && <div className="search-candidate-note">{item.note}</div>}
      {item.url && (
        <a
          className="break"
          href={item.url}
          target="_blank"
          rel="noreferrer noopener"
        >
          {item.url}
        </a>
      )}
      {(item.mapping ?? []).length > 0 && (
        <div className="table-scroll" style={{ marginTop: 8 }}>
          <table className="search-mapping-table">
            <thead>
              <tr>
                {MAPPING_HEADERS.map((header) => (
                  <th key={header}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(item.mapping ?? []).map((row, i) => (
                <tr key={i}>
                  <td>{row.feature || "-"}</td>
                  <td
                    className={
                      row.page_supported || row.official_supported ? "" : "faint"
                    }
                  >
                    {SUPPORT_LABEL[row.support_source ?? "none"] ?? "근거 없음"}
                  </td>
                  <td
                    className={
                      row.page_supported || row.official_supported ? "" : "faint"
                    }
                  >
                    {row.support_text || "-"}
                  </td>
                  <td className="faint">
                    {SCOPE_LABEL[row.support_scope ?? "unknown"] ?? "확인 필요"}
                  </td>
                  <td>{row.degree}</td>
                  <td>{row.counterpart || "-"}</td>
                  <td className={row.verified ? "" : "faint"}>
                    {row.support_source === "official_record" && row.support_field
                      ? `공식 응답 필드: ${row.support_field}`
                      : row.source_location}
                  </td>
                  <td className={row.verified ? "" : "faint"}>
                    {row.verbatim_excerpt}
                  </td>
                  <td className={row.verified ? "" : "faint"}>
                    {row.translation}
                  </td>
                  <td>{row.similar || "-"}</td>
                  <td>{row.different || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </li>
  );
}

/** 명세서 보조 실행에서 실제로 보고된 검색어 확장과 제외한 한정사항. */
function ExpansionPanel({ manifest }: { manifest: SearchManifest }) {
  const spec = manifest.input?.spec_document ?? null;
  const newRows = manifest.reported?.term_expansions ?? [];
  const legacyRows = manifest.reported?.claim_interpretation ?? [];
  const rows =
    newRows.length > 0
      ? newRows
      : legacyRows.map((row) => ({
          claim_term: row.term,
          alternative_meanings: row.reading ? [row.reading] : [],
          expanded_terms: [] as string[],
          basis: row.basis,
          excluded_limitations: row.narrowed
            ? ["이전 실행에서 문언보다 좁게 읽었다고 보고됨"]
            : ([] as string[]),
        }));
  if (!spec && rows.length === 0) return null;

  return (
    <div className="search-interpretation">
      <h4>출원발명 문서를 이용한 별도 검색 확장</h4>
      {spec ? (
        <p className="faint">
          {spec.filename} · {spec.char_count.toLocaleString()}자 — 이 문서는 청구항
          단독 검색에는 전달되지 않았습니다. 별도 확장 검색의 검색어 생성에만
          사용하고 후보는 합집합으로 병합했습니다.
        </p>
      ) : (
        <p className="faint">
          이 실행에는 출원발명 문서를 넣지 않았습니다. 아래 해석 기록에는 대조할
          명세서가 없습니다.
        </p>
      )}

      {rows.length === 0 ? (
        <p className="faint">
          모델이 보고한 용어 확장 기록이 없습니다. 명세서를 검색어에 어떻게
          반영했는지는 확인할 수 없습니다.
        </p>
      ) : (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>청구항 문언</th>
                  <th>가능한 의미</th>
                  <th>추가 검색어</th>
                  <th>명세서 근거</th>
                  <th>검색 제한에 쓰지 않은 한정</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    <td>{row.claim_term || "-"}</td>
                    <td>{row.alternative_meanings.join(", ") || "-"}</td>
                    <td>{row.expanded_terms.join(", ") || "-"}</td>
                    <td className="faint">{row.basis || "-"}</td>
                    <td>{row.excluded_limitations.join(", ") || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="notice info">
            근거 위치는 모델의 자기보고라 원문 대조가 필요합니다. 명세서 확장
            결과가 청구항 단독 후보를 삭제하는 것은 병합 단계에서 금지됩니다.
          </div>
        </>
      )}
    </div>
  );
}

export default function SearchManifestView({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const manifest = job.search_manifest;
  if (!manifest) return null;

  // 감사 기록은 DB 에 저장된 JSON 이라 이 화면보다 오래 산다. 스키마가 바뀐 뒤
  // 예전 실행을 열었을 때 화면이 죽지 않도록 배열 필드는 항상 받아 낸다.
  const observed = manifest.observed ?? ({} as SearchManifest["observed"]);
  const attempted = observed.attempted_fetch_urls ?? [];
  const succeeded = observed.succeeded_fetch_urls ?? [];
  const toolFailures = observed.tool_failures ?? [];
  const searchQueries = observed.search_queries ?? [];
  const queriesByOrigin = observed.search_queries_by_origin ?? {};
  const urlLookups = observed.url_lookup_attempts ?? [];
  const unknownOutcomes = observed.unknown_tool_outcomes ?? [];
  // 도구 이름 개수로 세지 않는다. Codex 는 web_search 하나로 검색과 URL 조회를
  // 겸하므로 이름을 세면 URL 조회가 검색으로 잡히고, 예전 코드는 web_search 를
  // 아예 빼먹어서 Codex 실행이 "실제 검색 0회" 로 보였다.
  //
  // 질의 수로 세지도 않는다. 한 호출이 질의 여러 개를 묶어 보낸다. 옛 기록에는
  // search_call_count 가 없고, 그때는 호출당 질의 하나라 둘이 같았다.
  const searchCount = observed.search_call_count ?? searchQueries.length;
  const exposureEnforced = manifest.policy.advertised_tools_enforced !== false;
  const reported = manifest.reported;
  const candidates = reported?.candidates ?? [];
  const classified = candidates.map((item) => ({
    item,
    classification: classificationView(item),
  }));
  // group_eligible 이 없는 옛 기록은 정식으로 간주하지 않는다. 저장된 검증
  // 흔적이 없으면 잠정 등급으로 보이되 후보 자체는 숨기지 않는다.
  const grouped = classified.filter(({ classification }) => classification.group);
  // 공식 검증했는데 A/B 기준에 못 미친 후보. 아직 검증하지 못한 후보와 다른
  // 사실이므로 같은 칸에 두지 않는다.
  const belowThreshold = classified.filter(
    ({ classification }) =>
      !classification.group && classification.outcome === "below_threshold",
  );
  const belowIndexes = new Set(belowThreshold.map(({ item }) => item.index));
  const provisional = classified.filter(
    ({ item, classification }) =>
      classification.provisionalGroup && !belowIndexes.has(item.index),
  );
  const isolated = classified
    .filter(
      ({ item, classification }) =>
        !classification.group &&
        !classification.provisionalGroup &&
        !belowIndexes.has(item.index),
    )
    .map(({ item }) => item);
  const hasOriginData = candidates.some(
    (candidate) => (candidate.search_origins ?? []).length > 0,
  );
  const baseCandidates = candidates.filter((candidate) =>
    (candidate.search_origins ?? []).includes("claim_only"),
  ).length || (hasOriginData ? 0 : candidates.length);
  const assistedOnly = candidates.filter(
    (candidate) =>
      (candidate.search_origins ?? []).length === 1 &&
      candidate.search_origins?.[0] === "spec_assisted",
  ).length;
  const foundByBoth = candidates.filter(
    (candidate) => (candidate.search_origins ?? []).length > 1,
  ).length;
  // 발견 경로별 집계. search_origins 와 다른 축이라 따로 센다.
  const epoDiscovered = candidates.filter((candidate) =>
    discoveryOrigins(candidate).includes("epo"),
  );
  const epoOnlyCount = epoDiscovered.filter(
    (candidate) => !discoveryOrigins(candidate).includes("web"),
  ).length;
  const officialClassified = classified.filter(
    ({ classification }) => classification.basis === "official_record",
  ).length;
  const focus = manifest.input?.search_focus ?? null;
  const epoLanes = manifest.epo?.lanes ?? [];
  const toolViolations = epoLanes.flatMap((lane) =>
    (lane.tool_violations ?? []).map((violation) => ({ lane, violation })),
  );
  const epoExclusions = epoLanes.flatMap((lane) =>
    (lane.excluded ?? []).map((row) => ({ lane, row })),
  );
  const verificationExclusions =
    manifest.verification?.excluded_candidates ?? [];
  const selectionOrder = manifest.verification?.selection_order ?? [];
  // 검색하지 않는 최종 선택 턴. 레인마다 최대 한 번이다.
  const selectionTurns = epoLanes
    .map((lane) => ({ lane, selection: lane.selection }))
    .filter(({ selection }) => selection && Object.keys(selection).length > 0);
  const webReportError = reported?.web_report_error ?? "";

  return (
    <section className="search-panel no-print">
      {job.search_manifest_error && (
        <div className="notice danger">
          <strong>후보 목록을 구조화하지 못했습니다</strong>
          <div style={{ marginTop: 4 }}>{job.search_manifest_error}</div>
          <div className="faint" style={{ marginTop: 4 }}>
            검증되지 않은 모델 출력을 보고서로 내보내지 않습니다. 모델 원문과
            아래 실제 검색 기록은 그대로 남아 있습니다.
          </div>
        </div>
      )}

      {manifest.policy.search_strategy === "isolated_union" && (
        <div className="notice info">
          <strong>격리된 이중 검색</strong>
          <div style={{ marginTop: 4 }}>
            청구항 단독 검색에는 명세서를 전달하지 않고, 명세서 보조 검색을 별도
            실행한 뒤 후보를 합집합으로 병합했습니다.
          </div>
        </div>
      )}

      {focus && (
        <div className="notice info">
          <strong>미대응 구성 보완 검색</strong>
          <div style={{ marginTop: 4 }}>
            유사도 {focus.threshold}% 미만 또는 대응 문헌 미발견 구성{" "}
            {focus.components.length}개를 대상으로{" "}
            <strong>1차 조합 검색 → 2차 개별 검색</strong> 순서로 실행했습니다.
          </div>
        </div>
      )}

      <ExpansionPanel manifest={manifest} />

      <div className="search-summary">
        <span>
          <strong>실제 검색</strong> {searchCount}회
          {searchQueries.length !== searchCount && (
            <> (질의 {searchQueries.length}개)</>
          )}
        </span>
        {urlLookups.length > 0 && (
          <span>
            <strong>URL 조회 시도</strong> {urlLookups.length}건
          </span>
        )}
        <span>
          <strong>페이지 열람 시도</strong>{" "}
          {attempted.length}건
        </span>
        <span>
          <strong>페이지 열람 성공</strong>{" "}
          {succeeded.length}건
        </span>
        <span>
          <strong>후보</strong> {candidates.length}건
        </span>
        {focus ? (
          <span>
            <strong>검색 대상 구성</strong> {focus.components.length}개
          </span>
        ) : (
          <>
            <span>
              <strong>청구항 단독 후보</strong> {baseCandidates}건
            </span>
            <span>
              <strong>명세서로 추가</strong> {assistedOnly}건
            </span>
            <span>
              <strong>양쪽에서 발견</strong> {foundByBoth}건
            </span>
          </>
        )}
        {epoDiscovered.length > 0 && (
          <span>
            <strong>EPO 독립 검색이 발견</strong> {epoDiscovered.length}건 (웹에
            없던 후보 {epoOnlyCount}건)
          </span>
        )}
        <span>
          <strong>정식 그룹</strong> {grouped.length}건 ·{" "}
          <strong>잠정 그룹</strong> {provisional.length}건 ·{" "}
          <strong>미분류 단서</strong> {isolated.length}건
        </span>
        <span>
          <strong>공식 기록 대조로 분류</strong> {officialClassified}건
        </span>
        <span>
          <strong>본문 읽은 것이 확인된 후보</strong>{" "}
          {candidates.filter((c) => c.page_fetch_succeeded).length}건
        </span>
        <span>
          <strong>원문 대조 확인됨</strong>{" "}
          {candidates.filter((c) => c.original_verified).length}건
        </span>
      </div>

      {(manifest.version ?? 0) < 4 && (
        <p className="faint">
          <strong>이 기록은 후보 식별·행별 근거 게이트가 적용되기 전에
          생성되었습니다.</strong> 아래 후보의 문헌번호·명칭·출원인이 같은
          페이지에서 확인되었는지, 각 대응 행이 실제 관측에 근거하는지는 검증되지
          않았습니다. 사용하기 전에 각 문헌을 직접 확인하십시오.
        </p>
      )}

      {(manifest.version ?? 0) < 8 && (
        <div className="notice info">
          <strong>과거 분류 안전 해석</strong>
          <div style={{ marginTop: 4 }}>
            정식 분류 근거가 저장되지 않은 과거 등급은 잠정 등급으로 표시합니다.
            원본 매니페스트 값은 수정하지 않았습니다.
          </div>
        </div>
      )}

      {/* 웹 채널의 출력을 읽지 못한 실행. 후보가 EPO 하나에서만 나왔다는
          사실은 결과를 읽기 전에 알아야 한다. */}
      {webReportError && (
        <div className="notice danger">
          <strong>웹 채널의 검색 결과를 읽지 못했습니다</strong>
          <div style={{ marginTop: 4 }}>
            아래 후보는 EPO 독립 검색만으로 만들어졌으며, 웹 검색이 찾은 문헌은
            하나도 들어 있지 않습니다.
          </div>
          <div className="faint" style={{ marginTop: 4 }}>
            사유: {webReportError}
          </div>
        </div>
      )}

      {/* 계획 턴에서 도구가 감지된 실행. 접지 않고 맨 위에 둔다 — 그 응답의
          검색·조회 지시를 하나도 실행하지 않았다는 사실이 제일 중요하다. */}
      {toolViolations.length > 0 && (
        <div className="notice danger">
          <strong>EPO 계획 턴에서 도구 호출이 감지되었습니다</strong>
          <div style={{ marginTop: 4 }}>
            EPO 검색 계획 턴은 도구 없는 실행입니다. 아래 레인에서는 모델이 외부
            도구를 호출한 것이 관측되어, <strong>그 응답의 검색·조회 지시를
            하나도 실행하지 않고 폐기</strong>했습니다.
          </div>
          {toolViolations.some(
            ({ violation }) => violation.isolation === "post_hoc_detection",
          ) && (
            <div style={{ marginTop: 4 }}>
              <strong>사후 탐지는 차단이 아닙니다.</strong> 격리 수준이{" "}
              <span className="mono-text">post_hoc_detection</span> 인 레인에서는
              ARIA 가 도구 호출을 막을 수단이 없습니다. 그 외부 호출은{" "}
              <strong>이미 나갔고 되돌릴 수 없으며</strong>, ARIA 가 한 일은 그
              응답을 검색 계획으로 쓰지 않기로 한 것뿐입니다. 모델이 무엇을
              읽었는지는 ARIA 가 알지 못합니다.
            </div>
          )}
          <ul className="search-query-list" style={{ marginTop: 4 }}>
            {toolViolations.map(({ lane, violation }, i) => (
              <li key={i}>
                <span className="mono-text">{lane.id}</span> ·{" "}
                {violation.provider || "provider 미기록"} · 감지된 도구{" "}
                <span className="mono-text">
                  {(violation.tools ?? []).join(", ") || "-"}
                </span>{" "}
                — {ISOLATION_LABEL[violation.isolation ?? "unknown"] ??
                  violation.isolation}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(manifest.verification?.attempted || manifest.verification?.reason) && (
        <div className="notice info">
          <strong>공식 문헌 2차 검증</strong>
          {manifest.verification.reason && (
            <div style={{ marginTop: 4 }}>{manifest.verification.reason}</div>
          )}
          <div className="faint" style={{ marginTop: 4 }}>
            대상 {manifest.verification.counts?.targets ?? 0}건 · 공식 문헌 확보{" "}
            {manifest.verification.counts?.verified ?? 0}건 · 확보 실패{" "}
            {manifest.verification.counts?.fetch_failed ?? 0}건 · 미시도{" "}
            {manifest.verification.counts?.not_attempted ?? 0}건
          </div>
          {(manifest.verification.usage?.reused_artifact_calls ?? 0) > 0 && (
            <div className="faint" style={{ marginTop: 4 }}>
              EPO 검색 레인이 이미 받아 둔 응답{" "}
              {manifest.verification.usage?.reused_artifact_calls}건을 재사용해
              같은 자료를 다시 내려받지 않았습니다 (이번 단계의 OPS 호출{" "}
              {manifest.verification.usage?.official_fetch_calls ?? 0}건).
              {/* 계획상 완전/부분 재사용과 실제 추가 호출 여부는 다른 축이다.
                  예산 부족으로 호출이 없었다고 부분 재사용이 완전해지지 않는다. */}
              {((manifest.verification.usage?.fully_reused_documents ?? 0) > 0 ||
                (manifest.verification.usage?.partially_reused_documents ?? 0) >
                  0) && (
                <>
                  {" "}
                  선택 당시 계획은 완전 재사용{" "}
                  {manifest.verification.usage?.fully_reused_documents ?? 0}건 · 부분
                  재사용{" "}
                  {manifest.verification.usage?.partially_reused_documents ?? 0}건이며,
                  실제 추가 호출 없이 끝난 재사용 문헌은{" "}
                  {manifest.verification.usage
                    ?.reused_without_fresh_fetch_documents ?? 0}
                  건 · 추가 호출이 발생한 재사용 문헌은{" "}
                  {manifest.verification.usage?.reused_with_fresh_fetch_documents ?? 0}
                  건입니다.
                </>
              )}
            </div>
          )}
          {/* 무엇을 왜 골랐는가. 상한이 무엇을 잘랐는지는 아래 목록이 말하고,
              왜 그것이 잘렸는지는 이 순서와 함께 읽어야 알 수 있다. */}
          {selectionOrder.length > 0 && (
            <div className="faint" style={{ marginTop: 4 }}>
              <strong>공식 검증 대상 선택 순서</strong>
              <ol className="search-query-list">
                {selectionOrder.map((row) => (
                  <li key={`${row.index}-${row.doc_number}`}>
                    <span className="mono-text">{row.doc_number}</span>{" "}
                    {row.selection_bucket && (
                      <span className="pill neutral">
                        {VERIFICATION_BUCKET_LABEL[row.selection_bucket] ??
                          row.selection_bucket}
                      </span>
                    )}{" "}
                    —{" "}
                    {row.detail}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {/* 상한에 걸려 빠진 후보를 조용히 누락하지 않는다. */}
          {(verificationExclusions.length > 0 || epoExclusions.length > 0) && (
            <div className="faint" style={{ marginTop: 4 }}>
              <strong>상한 때문에 처리하지 않은 것</strong>
              <ul className="search-query-list">
                {verificationExclusions.map((row, i) => (
                  <li key={`v-${i}`}>
                    <span className="mono-text">
                      {row.doc_number || `후보 ${row.index}`}
                    </span>{" "}
                    — {row.detail}
                  </li>
                ))}
                {epoExclusions.map(({ lane, row }, i) => (
                  <li key={`e-${i}`}>
                    <span className="mono-text">
                      {lane.id} · {row.value || row.kind}
                    </span>{" "}
                    — {row.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="faint" style={{ marginTop: 4 }}>
            A/B는 AI 분류입니다. ARIA는 공식 응답에서 실제로 대조된 구성 행
            수를 표시하며, 안정적인 특징 분모가 없어 임의의 커버리지 백분율은
            계산하지 않습니다.
          </div>
        </div>
      )}

      {(grouped.length > 0 || provisional.length > 0) && (
        <div className="search-groups">
          {provisional.length > 0 && (
            <p className="faint">
              <strong>잠정 분류 안내:</strong> 검색 결과를 바탕으로 모델이
              제안했지만 페이지 본문 또는 공식 문헌 근거로 정식 승격되지 않은
              후보입니다. 문헌번호와 주소는 재검토 단서로만 사용하십시오.
            </p>
          )}
          {READ_GROUPS.map((group) => {
            const formalRows = grouped.filter(
              ({ classification }) => classification.group === group,
            );
            const provisionalRows = provisional.filter(
              ({ classification }) => classification.provisionalGroup === group,
            );
            if (formalRows.length === 0 && provisionalRows.length === 0) return null;
            return (
              <div key={group}>
                <h4>
                  {groupTitle(manifest, group)}
                  {!WRITE_GROUPS.includes(group as "A" | "B") && (
                    <span className="pill neutral"> 과거 분류</span>
                  )}
                </h4>
                {formalRows.length > 0 && (
                  <>
                    <h5>정식 분류</h5>
                    <ul className="search-candidate-list">
                      {formalRows.map(({ item }) => (
                        <CandidateRow
                          key={item.index}
                          item={item}
                          gapSearch={Boolean(focus)}
                        />
                      ))}
                    </ul>
                  </>
                )}
                {provisionalRows.length > 0 && (
                  <>
                    <h5>잠정 분류</h5>
                    <ul className="search-candidate-list">
                      {provisionalRows.map(({ item, classification }) => (
                        <li className="search-candidate" key={item.index}>
                          <div className="search-candidate-head">
                            <span className="mono-text">
                              {item.doc_number || item.doi || "문헌번호 확인 필요"}
                            </span>
                            <span className="pill warn">잠정 {group}</span>
                            <span className="pill neutral">
                              {CLASSIFICATION_LABEL[classification.basis] ??
                                classification.basis}
                            </span>
                            {discoveryOrigins(item).map((origin) => (
                              <span
                                key={origin}
                                className={`pill ${
                                  origin === "epo" ? "accent" : "neutral"
                                }`}
                              >
                                {DISCOVERY_LABEL[origin]}
                              </span>
                            ))}
                          </div>
                          {item.verification && (
                            <div className="faint">
                              정식 승격되지 않은 이유:{" "}
                              {VERIFICATION_LABEL[item.verification.status] ??
                                item.verification.status}
                              {verificationDetail(item.verification.detail)
                                ? ` — ${verificationDetail(item.verification.detail)}`
                                : ""}
                            </div>
                          )}
                          {!item.verification && item.quarantine_reason && (
                            <div className="faint">
                              정식 승격되지 않은 이유: {item.quarantine_reason}
                            </div>
                          )}
                          {item.url && (
                            <div className="faint break">
                              모델이 제시한 주소: {item.url}
                            </div>
                          )}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}

      {belowThreshold.length > 0 && (
        <div className="search-groups">
          <h4>공식 검증했으나 A/B 기준 미달</h4>
          <p className="faint">
            공식 문헌을 확보해 대조했지만 A 에도 B 에도 해당하지 않는다고 판단한
            후보입니다. 상세 구성 대응표는 만들지 않고 사유만 남깁니다.
          </p>
          <ul className="search-candidate-list">
            {belowThreshold.map(({ item }) => (
              <li className="search-candidate" key={item.index}>
                <div className="search-candidate-head">
                  <span className="mono-text">
                    {item.doc_number || item.doi || "문헌번호 확인 필요"}
                  </span>
                  <span className="pill neutral">A/B 기준 미달</span>
                </div>
                {(item.verification?.detail || item.note) && (
                  <p className="faint">
                    {item.verification?.detail || item.note}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {isolated.length > 0 && (
        <div className="search-groups">
          <h4>미검증 참고 후보</h4>
          <p className="faint">
            아직 그룹 분류와 구성 대응표에 들어가지 못한 후보입니다. 웹 후보는
            문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 없어서이고,
            EPO 독립 검색 후보는 공식 응답에 구성 대응이 아직 대조되지
            않아서입니다. 문헌번호는 다시 확인해 볼 단서로 남기며, 검증되지 않은
            대응 내용은 표시하지 않습니다.
          </p>
          <ul className="search-candidate-list">
            {isolated.map((item) => {
              const discovered = discoveryOrigins(item);
              const epoOnly = !discovered.includes("web");
              return (
                <li className="search-candidate" key={item.index}>
                  <div className="search-candidate-head">
                    <span className="mono-text">
                      {item.doc_number || item.doi || "문헌번호 확인 필요"}
                    </span>
                    <span className="pill warn">
                      {epoOnly ? "공식 근거 대조 전" : "그룹 제외"}
                    </span>
                    <span
                      className={`pill ${
                        PROVENANCE_CLASS[item.provenance] ?? "neutral"
                      }`}
                    >
                      {PROVENANCE_LABEL[item.provenance] ?? item.provenance}
                    </span>
                    {discovered.map((origin) => (
                      <span
                        key={origin}
                        className={`pill ${
                          origin === "epo" ? "accent" : "neutral"
                        }`}
                      >
                        {DISCOVERY_LABEL[origin]}
                      </span>
                    ))}
                  </div>
                  {/* EPO 후보에는 웹 게이트 문구를 쓰지 않는다. 격리된 것이
                      아니라 아직 공식 근거가 대조되지 않았을 뿐이다. */}
                  <div className="faint">
                    {epoOnly ? "상태: " : "제외 사유: "}
                    {epoOnly
                      ? `${
                          VERIFICATION_LABEL[
                            item.verification?.status ?? "not_attempted"
                          ] ?? "공식 근거 대조 전"
                        }${
                          verificationDetail(item.verification?.detail)
                            ? ` — ${verificationDetail(item.verification?.detail)}`
                            : ""
                        }`
                      : item.quarantine_reason ||
                        "페이지 관측에 근거한 대응표 행이 없습니다."}
                  </div>
                  {item.url && (
                    <div className="faint break">
                      {epoOnly ? "공식 응답의 주소: " : "모델이 제시한 주소: "}
                      {item.url}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <button
        type="button"
        className="btn small"
        onClick={() => setOpen((prev) => !prev)}
      >
        {open ? "검색 기록 접기" : "실제 검색 기록 보기"}
      </button>

      {open && (
        <div className="search-log">
          {/* EPO 레인이 검색어를 만들기 전에 적은 청구항 분해. 모델의 판단이지
              ARIA 의 관측이 아니므로 그렇게 읽히도록 문구를 붙인다. */}
          {epoLanes.some((lane) => lane.claim_analysis?.elements?.length) && (
            <>
              <h4>EPO 검색 전략 (청구항 분석 — 모델 판단)</h4>
              <p className="faint">
                검색어를 만들기 전에 EPO 레인이 적은 청구항 분해입니다. ARIA 가
                대조한 사실이 아니며, 실제로 어떤 검색식이 되었는지는 아래 질의
                기록과 대조하십시오.
              </p>
              {epoLanes
                .filter((lane) => lane.claim_analysis?.elements?.length)
                .map((lane) => (
                  <div key={lane.id}>
                    <strong>{lane.id}</strong>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>구성요소</th>
                            <th>청구항 문언</th>
                            <th>필수 여부</th>
                            <th>동의어·유사 표현</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(lane.claim_analysis?.elements ?? []).map((el) => (
                            <tr key={el.id}>
                              <td className="mono-text">{el.id}</td>
                              <td>{el.text}</td>
                              <td className="faint">
                                {/* 세 상태다. 적지 않은 것을 '필수 아님'으로
                                    인쇄하면 없는 판단을 만들어 낸다. */}
                                {el.essential === true
                                  ? "필수"
                                  : el.essential === false
                                    ? "필수 아님"
                                    : "판단 없음"}
                              </td>
                              <td>{(el.synonyms ?? []).join(", ") || "-"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {(lane.claim_analysis?.relations ?? []).length > 0 && (
                      <ul className="search-query-list">
                        {(lane.claim_analysis?.relations ?? []).map((rel, i) => (
                          <li key={i}>
                            <span className="mono-text">
                              {rel.source} → {rel.target}
                            </span>{" "}
                            ({rel.kind || "관계"}) {rel.description}
                          </li>
                        ))}
                      </ul>
                    )}
                    {(lane.claim_analysis?.concept_combinations ?? []).length >
                      0 && (
                      <ul className="search-query-list">
                        {(lane.claim_analysis?.concept_combinations ?? []).map(
                          (combo, i) => (
                            <li key={i}>
                              개념 조합{" "}
                              <span className="mono-text">
                                {(combo.elements ?? []).join(", ")}
                              </span>{" "}
                              → {(combo.terms ?? []).join(", ") || "-"}
                              {combo.reason ? ` — ${combo.reason}` : ""}
                            </li>
                          ),
                        )}
                      </ul>
                    )}
                    {(lane.claim_analysis?.search_conditions ?? []).length >
                      0 && (
                      <ul className="search-query-list">
                        {(lane.claim_analysis?.search_conditions ?? []).map(
                          (cond, i) => (
                            <li key={i}>
                              {cond.kind || "조건"}:{" "}
                              <span className="mono-text">{cond.value}</span>
                              {cond.reason ? ` — ${cond.reason}` : ""}
                            </li>
                          ),
                        )}
                      </ul>
                    )}
                  </div>
                ))}
            </>
          )}

          {/* 검색하지 않는 최종 선택 턴. 마지막 검색 결과가 shortlist 평가를
              받았는지는 "왜 이 문헌이 후보에 없나"에 답할 때 필요하다. */}
          {selectionTurns.length > 0 && (
            <>
              <h4>최종 선택 턴 (검색 없음)</h4>
              <p className="faint">
                이 턴은 OPS 를 부르지 않습니다. 마지막 검색이 데려온 문헌까지
                포함해 shortlist 를 한 번 더 고르는 자리이며, 검색 라운드와
                사용량을 따로 기록합니다.
              </p>
              <ul className="search-query-list">
                {selectionTurns.map(({ lane, selection }) => (
                  <li key={lane.id}>
                    <span className="mono-text">{lane.id}</span> —{" "}
                    {selection?.attempted
                      ? `${selection.status || "ok"} · 검토한 후보 ${
                          selection.candidates_reviewed ?? 0
                        }건 · 추가된 shortlist ${selection.shortlist_added ?? 0}건`
                      : `돌리지 않음 — ${selection?.reason || "사유 미기록"}`}
                    {(selection?.rejected_actions ?? 0) > 0 && (
                      <> · 거절된 검색 action {selection?.rejected_actions}건</>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>ARIA 가 관측한 검색어</h4>
          {Object.keys(queriesByOrigin).length > 0 ? (
            Object.entries(queriesByOrigin).map(([origin, queries]) => (
              <div key={origin}>
                <strong>
                  {focus && origin === "claim_only"
                    ? "미대응 구성 보완"
                    : ORIGIN_LABEL[origin] ?? origin}
                </strong>
                <ol className="search-query-list">
                  {(queries ?? []).map((query, i) => (
                    <li key={i} className="mono-text break">
                      {query}
                    </li>
                  ))}
                </ol>
              </div>
            ))
          ) : searchQueries.length === 0 ? (
            <p className="faint">기록된 검색 호출이 없습니다.</p>
          ) : (
            <ol className="search-query-list">
              {searchQueries.map((query, i) => (
                <li key={i} className="mono-text break">
                  {query}
                </li>
              ))}
            </ol>
          )}

          <h4>열람을 시도한 주소</h4>
          {attempted.length === 0 ? (
            <p className="faint">페이지 열람을 시도한 기록이 없습니다.</p>
          ) : (
            <ul className="search-query-list">
              {attempted.map((url, i) => {
                const ok = succeeded.includes(url);
                return (
                  <li key={i} className="break">
                    <span className={`pill ${ok ? "ok" : "danger"}`}>
                      {ok ? "열람 성공" : "열람 실패"}
                    </span>{" "}
                    <span className="mono-text">{url}</span>
                  </li>
                );
              })}
            </ul>
          )}

          {(toolFailures.length > 0 || unknownOutcomes.length > 0) && (
            <p className="faint">
              확인된 도구 실패 {toolFailures.length}건 · 결과 확인 불가{" "}
              {unknownOutcomes.length}건. <strong>결과 확인 불가</strong>는
              실패가 아니라, 이 Provider 가 성공·실패를 구조화된 형태로 알려주지
              않아 ARIA 가 판정할 수 없었다는 뜻입니다. 성공으로 읽지 마십시오.
            </p>
          )}

          {toolFailures.length > 0 && (
            <>
              <h4>확인된 접근 실패</h4>
              <ul className="search-query-list">
                {toolFailures.map((failure, i) => (
                  <li key={i} className="break">
                    <span className="mono-text">
                      {String(failure.input?.url ?? failure.name)}
                    </span>{" "}
                    — {failure.error}
                  </li>
                ))}
              </ul>
            </>
          )}

          {reported && reported.access_failures.length > 0 && (
            <>
              <h4>모델이 보고한 원문 확보 필요 문헌</h4>
              <ul className="search-query-list">
                {reported.access_failures.map((failure, i) => (
                  <li key={i} className="break">
                    <span className="mono-text">{failure.url}</span> —{" "}
                    {failure.reason}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>실행 조건</h4>
          <div className="table-scroll">
            <table>
              <tbody>
                <tr>
                  <th>검색 프롬프트</th>
                  <td className="break mono-text">
                    {manifest.prompt.id} · sha256{" "}
                    {manifest.prompt.sha256.slice(0, 16)}…
                  </td>
                </tr>
                <tr>
                  <th>허용된 도구</th>
                  <td className="mono-text">
                    {manifest.policy.allowed_tools.join(", ")}
                  </td>
                </tr>
                <tr>
                  <th>도구 노출 통제</th>
                  <td>
                    {exposureEnforced
                      ? "CLI 단계에서 허용 목록으로 제한"
                      : "제한 불가 — 실제 호출을 사후 탐지"}
                  </td>
                </tr>
                <tr>
                  <th>검색 도메인 제한</th>
                  <td>
                    없음 — 검색 질의의 대상 도메인을 ARIA가 기술적으로 강제하지
                    않습니다.
                  </td>
                </tr>
                <tr>
                  <th>검색 확장 상한</th>
                  <td>독립 실행별 {manifest.policy.max_rounds}라운드</td>
                </tr>
                {manifest.search_lanes?.map((lane) => (
                  <tr key={lane.id}>
                    <th>{ORIGIN_LABEL[lane.id] ?? lane.id}</th>
                    <td>
                      명세서 컨텍스트 {lane.spec_in_context ? "포함" : "없음"} · 도구
                      호출 상한 {lane.max_tool_calls}회 · {lane.status}
                    </td>
                  </tr>
                ))}
                {manifest.input.claim_boundary_neutralized && (
                  <tr>
                    <th>청구항 경계</th>
                    <td>
                      입력한 청구항에서 경계 표시를 중화했습니다. 청구항 안에
                      <code> &lt;/CLAIM_TEXT&gt; </code>가 들어 있었습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
