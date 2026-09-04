/** Read-only audit. Technical groups never depend on evidence levels. */
import type { Job, SearchManifestV14 } from "../lib/types";

export function linkableUrl(raw?: string): string | null {
  const text = (raw ?? "").trim();
  if (!text || /[\s<>"\u0000-\u001f]/.test(text)) return null;
  try {
    const url = new URL(text);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? text : null;
  } catch { return null; }
}
const LEVELS: Record<string, string> = {
  search_snippet_only: "검색 스니펫·모델 판단 / 미검증",
  source_page_reviewed: "페이지 열람 확인 / 인용 미검증",
  official_bibliographic: "공식 서지 확보", official_abstract: "공식 초록 확보",
  official_claims: "공식 청구항 확보", official_full_text: "공식 전문 확보",
};
const ISSUES: Record<string, string> = {
  identifier_unverified: "식별 미확인", identifier_invalid: "식별자 형식 오류",
  identifier_mismatch: "문헌 식별자 불일치", source_not_read: "본문 열람 미확인",
  quote_unverified: "직접 인용 검증 불가", support_unverified: "근거 문장 대조 미확인",
  duplicate_group_conflict: "중복 후보 그룹 충돌", publication_date_conflict: "공개일 출처 충돌",
  source_conflict: "출처 간 필드 내용 차이",
};
function Current({ data }: { data: SearchManifestV14 }) {
  return <>
    <p>A/B/C는 LLM의 기술적 판단입니다. 증거 확보 수준은 별도로 표시합니다.</p>
    {data.error && <p role="alert">미완료: {data.error}</p>}
    <dl>{Object.entries(data.group_definitions).map(([key, value]) =>
      <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
    <h4>도구 상태</h4>
    <ul>{Object.entries(data.tool_availability).map(([name, value]) =>
      <li key={name}>{name}: {value.detail}</li>)}</ul>
    <p>검색 기준일: {data.date_filter.cutoff || "없음"} · 공개일 불명: {data.date_filter.unknown_publication_date || 0}건</p>
    {(data.reported?.candidates ?? []).map((item) => {
      const url = linkableUrl(item.url);
      return <section key={item.index} className="card">
        <h4>{item.rank}. {item.doc_number || item.doi || item.title} · LLM {item.group || "미분류"}</h4>
        <p>LLM 제목: {item.title}</p>
        {url ? <a href={url} target="_blank" rel="noreferrer">문헌 보기</a> : <span>링크 미확인</span>}
        <p>{LEVELS[item.evidence_level]}</p><p>{item.note}</p>
        {item.verification_issues.length > 0 && <p>{item.verification_issues.map(x => ISSUES[x]).join(" / ")}</p>}
        <details><summary>확보 범위 및 구성 대응</summary>
          <pre>{JSON.stringify({ scope: item.verification_scope, mapping: item.mapping }, null, 2)}</pre>
        </details>
      </section>;
    })}
    {data.date_filter.excluded.length > 0 && <details><summary>기준일 이후 공개로 제외된 문헌</summary>
      <pre>{JSON.stringify(data.date_filter.excluded, null, 2)}</pre></details>}
    <details><summary>실제 도구 호출·검색어 (ARIA 관측)</summary>
      <pre>{JSON.stringify({ observed: data.observed, journal: data.tool_journal }, null, 2)}</pre>
    </details>
    <details><summary>LLM 원출력 (미검증)</summary><pre>{JSON.stringify(data.llm_output, null, 2)}</pre></details>
  </>;
}
export default function SearchManifestView({ job }: { job: Job }) {
  const data = job.search_manifest;
  if (!data) return job.search_manifest_error ? <p role="alert">{job.search_manifest_error}</p> : null;
  return <details className="card search-manifest"><summary>검색 감사 기록</summary>
    {data.version === 14 ? <Current data={data} /> : <>
      <p>이전 형식(v{data.version})의 저장 기록입니다. 재분류·재검증하지 않았습니다. 저장된 보고서를 함께 확인하십시오.</p>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </>}
  </details>;
}
