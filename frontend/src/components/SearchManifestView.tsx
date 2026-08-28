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
  SearchManifest,
  SearchProvenance,
} from "../lib/types";

const PROVENANCE_LABEL: Record<SearchProvenance, string> = {
  search_snippet: "검색 스니펫만 확인 (페이지 미열람)",
  webfetch_summary: "페이지 요약 확인 (원문 아님)",
  raw_original_verified: "원문 대조 완료",
};

const PROVENANCE_CLASS: Record<SearchProvenance, string> = {
  search_snippet: "neutral",
  webfetch_summary: "warn",
  raw_original_verified: "ok",
};

const EVIDENCE_LABEL: Record<string, string> = {
  candidate_only: "후보 단계",
  source_page_reviewed: "페이지 열람 성공",
};

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
  snippet: "검색 스니펫",
  none: "근거 없음",
};

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
  return (
    <li className="search-candidate">
      <div className="search-candidate-head">
        <span className="mono-text">{identity}</span>
        {item.provisional && <span className="pill warn">잠정 분류</span>}
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
      <div className="faint">직접 발췌: {item.verbatim_excerpt}</div>
      <div className="faint">
        ARIA 관측: 페이지 본문{" "}
        {item.page_fetch_succeeded ? "읽음" : "읽은 기록 없음"} · 원문 대조{" "}
        {item.original_verified ? "완료" : "안 됨"} · 문헌번호-주소 대조{" "}
        {item.identifier_url_matched ? "완료" : "안 됨"}
      </div>
      {item.identifier_url_matched === false && (
        <div className="faint">
          문헌번호가 위 주소에서 확인되지 않아 명칭·출원인·패밀리를 표시하지
          않았습니다.
        </div>
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
                  <td className={row.page_supported ? "" : "faint"}>
                    {SUPPORT_LABEL[row.support_source ?? "none"] ?? "근거 없음"}
                  </td>
                  <td className={row.page_supported ? "" : "faint"}>
                    {row.support_text || "-"}
                  </td>
                  <td className="faint">
                    {SCOPE_LABEL[row.support_scope ?? "unknown"] ?? "확인 필요"}
                  </td>
                  <td>{row.degree}</td>
                  <td>{row.counterpart || "-"}</td>
                  <td className={row.verified ? "" : "faint"}>
                    {row.source_location}
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
  const toolCounts = observed.tool_call_counts ?? {};
  const searchCount = (toolCounts.WebSearch ?? 0) + (toolCounts.search_web ?? 0);
  const exposureEnforced = manifest.policy.advertised_tools_enforced !== false;
  const reported = manifest.reported;
  const candidates = reported?.candidates ?? [];
  // group_eligible 이 없는 옛 매니페스트는 그대로 그룹에 둔다. 지난 기록을 다시
  // 열었을 때 화면이 비어 버리면 안 된다.
  const grouped = candidates.filter((c) => c.group_eligible !== false);
  const isolated = candidates.filter((c) => c.group_eligible === false);
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
  const focus = manifest.input?.search_focus ?? null;

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
        </span>
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
        <span>
          <strong>그룹 분류</strong> {grouped.length}건 ·{" "}
          <strong>미확인 단서</strong> {isolated.length}건
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

      {grouped.length > 0 && (
        <div className="search-groups">
          {(["A", "B", "C"] as const).map((group) => {
            const rows = grouped.filter((c) => c.group === group);
            if (rows.length === 0) return null;
            return (
              <div key={group}>
                <h4>{groupTitle(manifest, group)}</h4>
                <ul className="search-candidate-list">
                  {rows.map((item) => (
                    <CandidateRow
                      key={item.index}
                      item={item}
                      gapSearch={Boolean(focus)}
                    />
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}

      {isolated.length > 0 && (
        <div className="search-groups">
          <h4>미확인 검색 단서</h4>
          <p className="faint">
            문헌 식별이 확인되지 않았거나 페이지 관측에 근거한 대응이 없어 그룹
            분류와 구성 대응표에서 제외한 후보입니다. 문헌번호는 다시 확인해 볼
            단서로만 남깁니다. 명칭·출원인·대응 내용은 검증되지 않았으므로
            표시하지 않습니다.
          </p>
          <ul className="search-candidate-list">
            {isolated.map((item) => (
              <li className="search-candidate" key={item.index}>
                <div className="search-candidate-head">
                  <span className="mono-text">
                    {item.doc_number || item.doi || "문헌번호 확인 필요"}
                  </span>
                  <span className="pill warn">그룹 제외</span>
                  <span
                    className={`pill ${
                      PROVENANCE_CLASS[item.provenance] ?? "neutral"
                    }`}
                  >
                    {PROVENANCE_LABEL[item.provenance] ?? item.provenance}
                  </span>
                </div>
                <div className="faint">
                  제외 사유:{" "}
                  {item.quarantine_reason ||
                    "페이지 관측에 근거한 대응표 행이 없습니다."}
                </div>
                {item.url && (
                  <div className="faint break">
                    모델이 제시한 주소: {item.url}
                  </div>
                )}
              </li>
            ))}
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

          {toolFailures.length > 0 && (
            <>
              <h4>접근 실패</h4>
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
                    {manifest.prompt.id} v{manifest.prompt.version ?? "-"} · sha256{" "}
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
