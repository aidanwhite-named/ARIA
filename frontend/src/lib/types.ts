export type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type AttachmentRole = "APPLICATION" | "CITATION" | "SUPPLEMENTAL";

/** 실행 종류. 입력 화면과 도구 정책이 여기서 갈린다.
 *
 *  patent_analysis   첨부한 PDF 를 인라인으로 넣고 도구를 전부 끈 채 구성대비
 *  similarity_search 청구항만 넣고 Provider의 웹 도구로 검토 후보 탐색
 */
export type JobKind = "patent_analysis" | "similarity_search";

/** 인용발명 문헌을 최종 분석 모델에게 어떻게 전달했는가.
 *
 *  full_inline      정규화 텍스트 전체를 프롬프트에 넣었다.
 *  local_retrieval  ARIA 가 로컬 색인하고, AI 가 구조화된 검색으로 찾은 구간을
 *                   근거 패키지로 넣었다. 근거 패키지에는 찾은 구간뿐 아니라
 *                   **그 구간이 있는 페이지 전문과 앞뒤 페이지**가 예산이
 *                   허락하는 만큼 함께 들어간다.
 *
 *  폐기: focused_pages. 한때 「페이지 단위」를 독립 전달 모드로 두었는데, 같은
 *  검색을 돌리고 담는 단위만 다른 것이라 전달 방식이 아니라 근거 패키지의 확장
 *  방식이 맞았다. 옛 실행 기록의 그 값은 local_retrieval 로 읽는다 —
 *  full_inline 으로 읽으면 「문헌 전체를 모델이 봤다」가 되어 거짓이 된다.
 *
 *  첨부 하나의 상태(delivery_mode)와 축이 다르다.
 */
export type DeliveryPlan = "full_inline" | "local_retrieval";

/** 저장된 값을 읽는다. 모르는 값은 좁은 쪽으로 해석한다. */
export function toDeliveryPlan(value: string | null | undefined): DeliveryPlan {
  if (!value) return "full_inline";
  if (value === "full_inline") return "full_inline";
  return "local_retrieval";
}

/** 화면에 그대로 쓰는 전달 방식 이름. 세 곳이 각자 문자열을 만들면 같은 실행이
 *  화면마다 다르게 불린다. */
export const DELIVERY_LABEL: Record<DeliveryPlan, string> = {
  full_inline: "전체 인라인 전달",
  local_retrieval: "로컬 검색 전달",
};

/** 원문 전체가 들어가지 않은 전달인가. */
export function isNarrowed(plan: DeliveryPlan | string): boolean {
  return toDeliveryPlan(plan as string) !== "full_inline";
}

/** 모델 컨텍스트 기반 입력 예산.
 *
 *  Provider 전송 하드 한도(agy 의 180,000 bytes)와 **다른 축**이다. 앞쪽은 CLI 가
 *  자르는 지점이고 이쪽은 모델이 거절하는 지점이라, 사용자가 할 일이 다르다.
 *
 *  source 가 "fallback" 이면 모델 한도를 확인하지 못하고 보수적 대체값을 쓴
 *  것이다. 화면은 그 사실을 반드시 보여 준다.
 */
export type ModelTokenBudget = {
  model: string;
  context_tokens: number;
  reserve_tokens: number;
  input_tokens: number;
  source: "configured" | "fallback";
};

/** 전달 판정 한 벌. 화면·History·감사 기록이 같은 값을 쓴다. */
export type DeliveryManifest = {
  provider: string;
  selected_delivery_mode: DeliveryPlan;
  selection_reason: string;
  full_inline_chars: number;
  full_inline_bytes: number;
  full_inline_tokens: number;
  actual_payload_chars: number;
  actual_payload_bytes: number;
  /** Provider 전송 하드 한도. 선언하지 않은 Provider 는 null. */
  provider_byte_limit: number | null;
  /** 모델 컨텍스트 입력 예산. 하드 한도가 있는 Provider 는 null. */
  model_token_budget: ModelTokenBudget | null;
  /** 이 크기가 실측인가 예산 상한인가. 준비 화면은 상한을 보여 준다. */
  payload_is_budget_ceiling: boolean;
  /** 사건 규모 기준 때문에 좁혔는가. 전송 한도와 다른 축이다. */
  scale_downgraded: boolean;
};

/** 근거 패키지에서 구성 하나에 ARIA 가 확정한 상태.
 *
 *  matched 가 아닌 것을 "문헌에 없음"으로 읽으면 안 된다. 그 구분이 이 타입의
 *  존재 이유다.
 */
export type EvidenceStatus =
  | "matched"
  | "not_found_in_reviewed_scope"
  | "coverage_insufficient"
  | "extraction_unreadable"
  | "visual_review_required";

/** 문헌 하나의 색인·추출 상태. ARIA 가 관측한 사실이며 모델이 정하지 않는다. */
export interface RetrievalDocument {
  alias: string;
  attachment_id: string;
  filename: string;
  pdf_sha256: string;
  role: string;
  index_rebuilt: boolean;
  index: {
    index_version: number;
    extractor_version: string;
    chunk_count: number;
    page_count: number;
    source_page_count: number;
    trigram_enabled: boolean;
    built_at: string;
  };
  extraction: {
    source_page_count: number;
    processed_page_count: number;
    page_count_mismatch: boolean;
    ok_pages: number;
    empty_or_low_text_pages: number[];
    extraction_failed_pages: number[];
    visual_review_required_pages: number[];
    extraction_divergence_pages: number[];
    chunk_count: number;
    chunk_failures: number;
    status: "complete" | "review_required" | "unusable";
    open_error: string | null;
  };
}

/** 로컬 검색 실행의 감사 기록. 전체 인라인 실행에서는 null 이다. */
export interface RetrievalManifest {
  version: number;
  delivery_mode: "local_retrieval";
  generated_at: string;
  claim_sha256: string;
  agent_prompt_sha256: string;
  ocr_performed: false;
  budget: {
    max_rounds: number;
    max_page_reads: number;
    max_evidence_chars: number;
    max_evidence_bytes?: number;
    hits_per_document: number;
    max_round_result_chars: number;
  };
  sqlite: {
    fts5: boolean;
    trigram: boolean;
    sqlite_version: string;
    error: string;
  };
  /** 의미 검색이 실제로 돌았는가. enabled 와 active 는 다른 축이다. */
  semantic: {
    enabled: boolean;
    active: boolean;
    model: string | null;
    revision: string | null;
    cache_state: string;
    reason: string;
    notes: string[];
  };
  libraries: Record<string, string>;
  documents: RetrievalDocument[];
  not_indexed: { alias: string; filename: string; reason: string }[];
  rounds: {
    round: number;
    started_at: string;
    completed_at: string;
    status: string;
    input_sha256: string;
    output_sha256: string;
    input_chars: number;
    input_bytes?: number;
    output_chars: number;
    actions: number;
    error: string;
  }[];
  pages_read: number;
  /** 이미 읽은 페이지를 다시 요청한 횟수. 막지는 않고 기록만 남긴다. */
  repeat_page_reads: number;
  /** 실제로 최종 프롬프트에 들어간 근거 패키지의 문자 수.
   *
   *  예산(budget.max_evidence_chars)은 이 값의 상한이다. 넘으면 ARIA 가 서지
   *  발췌 → 구성 메타데이터 → 근거 구간 순으로 줄이고, 그래도 안 되면 실행을
   *  실패시킨다. 페이지 확장의 부분 수록은 page_truncations에 별도로 기록한다. */
  evidence_chars: number;
  components: {
    id: string;
    label: string;
    queries: string[];
    channels_used: string[];
    channels_failed: string[];
    candidates: number;
    /** 문헌별 검색 실행 기록. 결과가 0건이었던 검색도 들어 있다.
     *
     *  "찾지 못했다"와 "찾아보지 않았다"를 가르는 유일한 근거다. 이 기록이
     *  없으면 한 문헌만 뒤지고 나머지를 건너뛴 실행이 「검토 범위에서 미발견」
     *  으로 보인다. */
    searched_documents: {
      attachment: string;
      attachment_id: string;
      queries: string[];
      channels_used: string[];
      channels_failed: string[];
      hits: number;
    }[];
    /** 이 구성에 대해 검색 자체를 하지 않은 문헌. 비어 있어야 정상이다. */
    unsearched_documents: string[];
  }[];
  action_errors: { round?: number; action?: string; reason: string }[];
  notes: string[];
  budget_exhausted: boolean;
  /** 근거 패키지를 예산에 맞추려고 줄인 내역. 비어 있어야 정상이다.
   *
   *  원문은 절대 자르지 않는다. 여기 적히는 것은 서지 발췌 제거, 구성
   *  메타데이터 축약, 근거 구간 제거뿐이며 전부 검토 범위 제한으로도 올라간다. */
  package_reductions: string[];
  /** 예산 때문에 뺀 페이지. package_reductions 와 **다른 채널**이다 — 페이지를
   *  뺀 것은 근거를 뺀 것이 아니므로 구성 판정을 흔들지 않는다. */
  page_reductions?: string[];
  /** 일부만 수록한 페이지. 전문 확인 페이지와 구분한다. */
  page_truncations?: {
    attachment: string;
    pdf_page: number;
    source_chars: number;
    included_chars: number;
    omitted_chars: number;
  }[];
  error: string;
  error_code: string;
  status: "complete" | "partial" | "failed";
}

export type AnalysisComponentStatus =
  | "matched"
  | "below_threshold"
  | "not_found"
  | "unreadable";

export interface AnalysisComponent {
  id: string;
  claim: string;
  symbol: string;
  feature: string;
  similarity: number | null;
  status: AnalysisComponentStatus;
  difference: string;
  search_eligible: boolean;
}

export interface AnalysisManifest {
  version: number;
  threshold: number;
  items: AnalysisComponent[];
}

export interface GapSearchFocus {
  version: number;
  mode: "gap";
  source_job_id: string;
  source_job_label: string;
  threshold: number;
  components: AnalysisComponent[];
}

/** 이 후보를 무엇으로 알게 되었는가.
 *
 *  search_snippet        검색 결과 제목·스니펫만 봤다
 *  webfetch_summary      WebFetch 로 페이지를 열어 요약을 받았다 (원문 아님)
 *  raw_original_verified 공식 원문 텍스트를 확보해 대조했다
 */
export type SearchGroup = "A" | "B" | "C" | null;
export type SearchEvidenceLevel = "search_snippet_only" | "source_page_reviewed" |
  "official_bibliographic" | "official_abstract" | "official_claims" | "official_full_text";
export type SearchVerificationIssue = "identifier_unverified" | "identifier_invalid" |
  "identifier_mismatch" | "source_not_read" | "quote_unverified" | "support_unverified" |
  "duplicate_group_conflict" | "publication_date_conflict" | "source_conflict";
export interface SearchMappingRow {
  feature: string; degree: string; counterpart: string; similar: string; different: string;
  support_text: string; support_verified: boolean; quote_verified: boolean;
  verbatim_excerpt: string; translation: string; source_location: string;
  evidence_ref: { artifact_id: string; field_path: string; profile_id: string } | null;
}
export interface SearchCandidate {
  index: number; rank: number; group: SearchGroup; doc_type: string;
  doc_number: string; doi: string; title: string; url: string; note: string;
  applicant: string; family: string; publication_date: string; reported_publication_date: string;
  evidence_level: SearchEvidenceLevel; verification_issues: SearchVerificationIssue[];
  verification_scope: Record<string, "not_requested" | "verified" | "unavailable">;
  evidence_sources: unknown[]; mapping: SearchMappingRow[];
}
export interface SearchManifestV14 {
  version: 14; status: "complete" | "incomplete"; provider: string; model: string;
  group_definitions: Record<string, string>;
  input: { claim_text: string; spec_document: unknown; search_focus: GapSearchFocus | null };
  prompt: { id: string; name: string; sha256: string; runtime_context_sha256: string };
  started_at: string; completed_at: string;
  limits: { max_tool_calls: number; timeout_seconds: number };
  tool_availability: Record<string, { status: "available" | "disabled" | "not_configured" |
    "not_implemented" | "unsupported_transport"; detail: string }>;
  tool_journal: Record<string, unknown>[];
  observed: { tool_calls: Record<string, unknown>[]; tool_call_counts: Record<string, number>;
    search_queries: string[]; search_call_count: number; attempted_fetch_urls: string[]; succeeded_fetch_urls: string[]; url_lookup_attempts: string[]; tool_failures: unknown[]; unknown_tool_outcomes: unknown[] };
  llm_output: unknown;
  reported: { candidates: SearchCandidate[]; term_expansions: unknown[]; rounds: unknown[]; access_failures: unknown[] } | null;
  date_filter: { cutoff: string; applied: boolean; excluded: { doc_number: string; doi: string;
    title: string; publication_date: string; detail: string; reason_code: string }[];
    unknown_publication_date: number };
  usage: unknown; normalization_notes: string[]; error: string | null;
}
export interface LegacySearchManifest {
  version: 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13;
  // Retained only as a read-only audit object. Never interpreted as the new schema.
  [key: string]: unknown;
}
export type SearchManifest = SearchManifestV14 | LegacySearchManifest;

export type RelationType = "MAPPED" | "CONTINUED" | "REANALYZED";

export interface CitationMappingItem {
  citation_number: number;
  /** 이 실행의 첨부를 가리킨다. 복제될 때마다 바뀐다. */
  attachment_id: string;
  /** 같은 자료라는 근거. 복제해도 바뀌지 않는다. */
  attachment_sha256: string;
  filename: string;
  document_number: string;
}

export interface CitationMapping {
  version: number;
  items: CitationMappingItem[];
}

export type PromptKind = "analysis" | "search";

export interface Prompt {
  id: string;
  name: string;
  description: string;
  body: string;
  enabled: boolean;
  accepted_file_types: string[];
  /** 프롬프트 파일 메타데이터에서만 정하는 ARIA 확장 선언. */
  capabilities: string[];
  /**
   * 어느 작업의 프롬프트인가. 분석 프롬프트와 검색 전략 프롬프트는 계약이
   * 다르므로 목록도 선택도 섞지 않는다. 파일이 정하며 화면에서 바꿀 수 없다.
   */
  kind: PromptKind;
  created_at: string;
  updated_at: string;
}

export interface PromptCatalogItem extends Prompt {
  editable: boolean;
  deletable: boolean;
}

export interface ProviderInfo {
  provider: string;
  display_name: string;
  installed: boolean;
  executable_path: string | null;
  executable_kind: string | null;
  executable_ok: boolean;
  version: string | null;
  auth_state: "OK" | "NOT_LOGGED_IN" | "UNKNOWN" | "NOT_APPLICABLE";
  capabilities: Record<
    string,
    | boolean
    | string
    | string[]
    | Record<string, string>
    | Record<string, string[]>
    | null
  >;
  notes: string[];
  install_hint: string;
  /** ARIA에 실제 분석 실행 Adapter가 구현되어 있는가. */
  execution_supported: boolean;
  /** 실행 허용 여부. 설치·인증에 더해 안전 정책까지 반영. */
  usable: boolean;
  /** 설치/실행/인증만 본 상태. 안전 정책은 반영하지 않음. */
  runnable: boolean;
  /** ARIA 의 안전 원칙(도구 없는 실행)을 충족하지 못하는 Provider. */
  experimental: boolean;
  risks: string[];
}

export type ProviderLoginState =
  | "STARTING"
  | "WAITING_FOR_USER"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

/**
 * 메모리에만 존재하는 CLI 인증 진행 상태. 인증정보는 포함하지 않는다.
 * 로그인과 로그아웃이 같은 수명주기를 쓰고 intent 로만 구분된다.
 */
export interface ProviderLoginSession {
  session_id: string;
  provider: string;
  intent: "login" | "logout";
  method: string;
  mode: "browser" | "helper_window";
  state: ProviderLoginState;
  message: string;
  started_at: string;
  completed_at: string | null;
  can_cancel: boolean;
}

/** CLI가 실제 자격증명을 지운 뒤 다시 확인한 로그아웃 결과. */
export interface ProviderLogoutImmediate {
  provider: string;
  mode: "immediate";
  ok: boolean;
  auth_state: "NOT_LOGGED_IN";
  message: string;
}

/**
 * 로그아웃 요청 결과. 전용 logout 명령이 있는 CLI(claude, codex)는 즉시 끝나고,
 * agy 처럼 대화형 창에서만 로그아웃할 수 있는 CLI 는 세션을 돌려준다.
 */
export type ProviderLogoutResult = ProviderLogoutImmediate | ProviderLoginSession;

/** 도우미 창에서 진행되는 로그아웃인지 판별한다. */
export function isLogoutSession(
  result: ProviderLogoutResult,
): result is ProviderLoginSession {
  return "session_id" in result;
}

export interface AttachmentAnalysis {
  attachment_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  role: AttachmentRole;
  page_count: number | null;
  char_count: number;
  extraction_method: string;
  delivery_mode: string;
  read_ok: boolean;
  error: string | null;
  /** 「분석에 포함」의 초기 체크 상태. 업로드 응답에서는 정상 처리된 자료만
   *  true 다. 실행 기록에서는 그 실행이 실제로 분석 자료로 썼는지를 뜻한다. */
  included: boolean;
}

export interface UploadResponse {
  batch_id: string;
  files: AttachmentAnalysis[];
  rejected: { filename: string; reason: string }[];
  total_chars: number;
  /** ARIA 자체 글자 수 한도. null 이면 제한 없음(기본값). */
  max_inline_chars: number | null;
}

export interface JobAttachment extends AttachmentAnalysis {
  required: boolean;
}

/** backend/app/analysis_completeness.py 의 check() 결과. */
export interface AnalysisCompleteness {
  process_succeeded: boolean;
  manifest_parsed: boolean;
  manifest_error: string | null;
  declared_components: number;
  reported_components: number;
  /** 검색이 선언한 구성 이름과 보고서의 구성 이름을 대조할 수 있었는가. */
  comparable: boolean;
  missing_components: string[];
  inferred_components: string[];
  scope: {
    status?: string;
    rounds?: number;
    max_rounds?: number;
    budget_exhausted?: boolean;
    pending_actions?: number;
    pages_read?: number;
    limited_components?: string[];
    limited?: boolean;
  };
  complete: boolean;
}

export interface Job {
  id: string;
  status: JobStatus;
  error_code: string | null;
  job_kind: JobKind;
  prompt_id: string | null;
  prompt_name: string;
  prompt_snapshot: string;
  output_mode: "markdown" | "text";
  claim_text: string;
  source_job_id: string | null;
  source_job_label: string;
  relation_type: RelationType | null;
  followup_instruction: string;
  prior_claim_text: string;
  prior_report: string;
  /** 이 실행의 보고서에서 읽어 검증한 매핑. null 이면 번호를 물려줄 수 없다. */
  citation_mapping: CitationMapping | null;
  /** 원본에서 물려받아 이 실행의 자료에 다시 묶은 고정 매핑. */
  prior_citation_mapping: CitationMapping | null;
  prompt_capabilities: string[];
  citation_mapping_error: string | null;
  /** 구성별 유사도와 미발견 상태를 검증한 보완 검색 입력. */
  analysis_manifest: AnalysisManifest | null;
  analysis_manifest_error: string | null;
  /**
   * 분석 완전성 점검. 저장하지 않고 조회 시점에 retrieval_manifest 와
   * analysis_manifest 에서 계산한 파생값이다. 검색 실행에서는 null.
   */
  analysis_completeness: AnalysisCompleteness | null;
  /** 유사 문헌 검색의 감사 기록. 분석 실행에서는 null. */
  search_manifest: SearchManifest | null;
  /** 모델 보고 블록을 읽지 못한 사유. 관측 기록은 이 경우에도 남는다. */
  search_manifest_error: string | null;
  /** 구성대비 결과에서 시작한 검색의 선택 구성 스냅샷. */
  search_focus: GapSearchFocus | null;
  /** 이 실행에 적용한 검색 기준일(YYYY-MM-DD). null 이면 날짜 조건 없이
   *  검색했다는 뜻이며, 이 기능 이전의 실행도 모두 null 이다. */
  search_cutoff_date?: string | null;
  search_depth?: "quick" | "standard" | "deep";
  /** 인용발명 문헌을 어떻게 전달했는가. 값이 없는 과거 실행은 full_inline. */
  delivery_plan: DeliveryPlan;
  delivery_manifest?: DeliveryManifest | null;
  /** 로컬 검색 실행의 감사 기록. 전체 인라인 실행에서는 null. */
  retrieval_manifest: RetrievalManifest | null;
  /** 로컬 검색이 근거 패키지를 만들지 못한 사유. */
  retrieval_manifest_error: string | null;
  provider: string;
  model: string | null;
  cli_path: string | null;
  cli_version: string | null;
  cli_args: string[];
  system_prompt_snapshot: string;
  final_prompt_sha256: string | null;
  final_prompt_chars: number;
  terminal_reason: string | null;
  exit_code: number | null;
  errors: string[];
  permission_denials: unknown[];
  usage: Record<string, unknown> | null;
  result_text: string | null;
  attachments: JobAttachment[];
  preprocessing_versions: Record<string, string>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
}

export interface HistoryItem {
  id: string;
  status: JobStatus;
  error_code: string | null;
  job_kind: JobKind;
  prompt_name: string;
  provider: string;
  model: string | null;
  created_at: string;
  duration_ms: number | null;
  attachment_count: number;
  source_job_id: string | null;
  source_job_label: string;
  relation_type: RelationType | null;
  /** 이 실행을 원본 삼아 번호를 이어받을 수 있는지. */
  has_citation_mapping: boolean;
  /** 이 실행에서 이어진 후속 실행 수. 스레드 일괄 삭제 대상 건수. */
  descendant_count: number;
  /** 인용발명 문헌을 어떻게 전달했는가. */
  delivery_plan: DeliveryPlan;
  delivery_manifest?: DeliveryManifest | null;
}

export interface AppSettings {
  values: {
    max_file_size_bytes: number;
    max_total_upload_bytes: number;
    max_files_per_job: number;
    /** 0 = 제한 없음(기본값). */
    max_inline_chars: number;
    default_timeout_seconds: number;
    max_concurrency_per_provider: number;
    runtime_context: string;
    runtime_context_enabled: boolean;
    default_prompt_id: string;
    /** 검색 화면이 처음 고르는 검색 전략 프롬프트. 비어 있으면 배포본. */
    default_search_prompt_id: string;
    default_provider: string;
    provider_paths: Record<string, string>;
    default_models: Record<string, string>;
    /** provider -> 추론강도. 키가 없으면 모델 기본값이다. */
    reasoning_effort?: Record<string, string>;
    keep_raw_output: boolean;
    fail_on_tool_use: boolean;
    max_search_tool_calls: number;
    /** 인용발명 전달 방식 정책. auto = 넣을 수 있는 만큼 넓게. */
    retrieval_mode: "auto" | "full" | "retrieval";
    retrieval_max_rounds: number;
    retrieval_max_page_reads: number;
    retrieval_evidence_chars: number;
    retrieval_hits_per_document: number;
    /** 근거 구간이 있는 페이지의 앞뒤로 더 담을 페이지 수. */
    retrieval_neighbor_pages: number;
    /**
     * 모델 컨텍스트 한도 재정의. `provider:model` 또는 `model` 이 키다.
     * 비어 있으면 아래 대체값을 쓴다 — ARIA 는 모델 한도를 추측하지 않는다.
     */
    model_context_tokens: Record<string, number>;
    model_output_reserve_tokens: number;
    unknown_model_context_tokens: number;
    /**
     * 사건 규모 품질 기준. **전송 한도가 아니다.** 전송 하드 한도를 선언하지
     * 않은 Provider 에서만 판정에 쓰이고, 0 이면 쓰지 않는다.
     */
    delivery_scale_documents: number;
    delivery_scale_pages: number;
    delivery_scale_claim_elements: number;
    /** 임베딩 캐시 상한(MB). 0 = 정리하지 않음. */
    embedding_cache_max_mb: number;
    /** 기본 꺼짐. 켜도 라이브러리·모델이 없으면 키워드 검색만으로 진행한다. */
    retrieval_semantic_enabled: boolean;
    kiwee_integration_enabled: boolean;
    /** EPO OPS 도구 연동. 실행별 MCP를 지원하는 Provider에서 사용한다. */
    epo_integration_enabled: boolean;
    epo_consumer_key: string;
    /**
     * 응답에서는 **항상 빈 문자열**이다. 저장은 되지만 되돌려주지 않는다.
     * 저장 여부는 secrets_set 을 봐야 한다.
     */
    epo_consumer_secret: string;
    /** OPS HTTP 대기 시간의 총합. 실행 전체 시간과 별개인 내부 안전 한도. */
    epo_http_budget_seconds: number;
    /** 0 = 시간당 사용량을 관측·표시만 하고 차단하지 않음. 주간 한도는 계약값이라 별도. */
    epo_hourly_quota_bytes: number;
    epo_max_detail_fetches: number;
    /** ARIA 가 관측해 적는 값. 사용자가 PUT 으로 못 고친다(사용량 되돌리기 방지). */
    epo_quota_state: Record<string, unknown>;
    /**
     * 비특허문헌(Crossref·Europe PMC) 연동. 자격증명이 필요 없어 켜기만 하면
     * MCP를 지원하는 Provider의 LLM이 필요할 때 도구로 호출한다.
     */
    literature_integration_enabled: boolean;
    /** Crossref 예의 풀 표시용 연락처. 비워 둬도 동작한다. */
    literature_contact_email: string;
    /** 질의 하나가 받아 오는 결과 건수 상한. 두 DB 각각에 적용된다. */
    literature_max_results_per_query: number;
    /** 서지 API HTTP 대기 시간의 총합(초). */
    literature_http_budget_seconds: number;
  };
  warnings: string[];
  data_dir: string;
  runs_dir: string;
  env_filtering: {
    allowlist: string[];
    blocked_prefixes: string[];
    removed_count: number;
    removed_sample: string[];
  };
  /** 비밀 값이 저장되어 있는가. values 의 빈 문자열로는 구별할 수 없다. */
  secrets_set: Record<string, boolean>;
  /** EPO OPS 사용량. 백엔드가 한도·남은 양까지 계산해서 준다. */
  epo_quota: EpoQuotaSnapshot;
  /** agy 의 페이지 열람 허용 목록. ARIA 설정값이 아니라 다른 도구의 설정
   *  파일에서 읽은 사실이라 values 가 아니라 이 칸으로 온다. 옛 백엔드는
   *  보내지 않으므로 선택 값이다. */
  agy_permissions?: AgyPermissionState;
}

/** agy settings.json 의 read_url 허용 목록 상태. */
export interface AgyPermissionState {
  path: string;
  exists: boolean;
  /** 지금 열 수 있는 호스트 전부. 사용자가 직접 넣은 것을 포함한다. */
  allowed_hosts: string[];
  /** ARIA 가 권장하는 논문 출처. */
  recommended: string[];
  /** 권장 목록 중 실제로 적용된 것. */
  applied: string[];
  /** 권장 목록 중 아직 없는 것. */
  missing: string[];
  /** read_url(*) 가 이미 들어 있는가. ARIA 는 이 값을 만들지 않는다. */
  wildcard: boolean;
  /** 읽지 못한 이유. 비어 있지 않으면 다른 칸은 신뢰할 수 없다. */
  error: string;
}

/** EPO OPS 사용량 스냅샷.
 *
 *  `ops_*` 는 OPS 가 헤더로 알려준 권위 있는 값이고, `local_bytes` 는 ARIA 가
 *  센 값이다. 둘을 합치지 않는 것은 의도다 — 어긋나면 그 사실이 신호다.
 */
export interface EpoQuotaSnapshot {
  week?: string;
  weekly_limit_bytes?: number;
  hourly_limit_bytes?: number;
  local_bytes?: number;
  ops_weekly_bytes?: number | null;
  ops_hourly_bytes?: number | null;
  effective_weekly_bytes?: number;
  remaining_weekly_bytes?: number;
  requests?: number;
  /** 지금 날아가 있는 요청들이 잡아 둔 최대 응답량. 한도 계산에 포함된다. */
  reserved_bytes?: number;
  /** 아직 DB 에 저장되지 않은 증분. 저장이 실패하면 여기 남는다. */
  pending_bytes?: number;
  /** 마지막 저장 실패 사유. 빈 문자열이면 정상. */
  persist_error?: string;
  warn?: boolean;
  throttle?: {
    raw?: string;
    system_state?: string;
    services?: Record<string, string>;
    dangerous?: boolean;
  };
  observed_at?: string;
}

/** 외부 데이터 소스 자격증명 확인 결과. 토큰 값은 오지 않는다. */
export interface CredentialCheck {
  ok: boolean;
  detail: string;
  http_status: number | null;
  expires_in: number | null;
}

export interface StreamEvent {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

/** 실행 전에 백엔드가 잰 최종 조립 프롬프트의 크기.
 *
 *  화면이 원본 첨부의 글자 수를 세는 것으로는 이 값을 맞힐 수 없다. 실제로
 *  나가는 본문에는 런타임 컨텍스트·경계 표시·명세서 절이 모두 붙고, Provider
 *  한도는 문자가 아니라 UTF-8 바이트로 걸린다. runner 와 같은 조립 함수가
 *  계산한 값이다.
 */
export type PreflightLane = {
  id: string;
  chars: number;
  bytes: number;
};

export type Preflight = {
  job_kind: JobKind;
  provider: string;
  lanes: PreflightLane[];
  chars: number;
  bytes: number;
  /** 사용자가 환경설정에서 스스로 건 글자 수 한도. null 이면 제한 없음. */
  char_budget: number | null;
  /**
   * 이 Provider 가 자료 전체를 손실 없이 모델에 전달할 수 있는 바이트 한도.
   * 사용자 입력 제한이 아니라 전달 경로의 한계이며 끌 수 없다. 한도를
   * 선언하지 않은 Provider 는 null.
   */
  byte_budget: number | null;
  over_chars: boolean;
  over_bytes: boolean;
  blocked: boolean;
  /** 이 입력이 실제로 어떻게 전달되는가. runner 와 같은 판정 함수를 쓴다. */
  delivery_plan: DeliveryPlan;
  /** 왜 그 방식을 골랐는가. 화면이 문장을 새로 만들지 않고 이 값을 그대로 쓴다. */
  selection_reason: string;
  /** 전체 인라인으로 넣었을 때의 크기. auto 가 왜 좁혔는지 설명한다. */
  full_inline_bytes: number;
  full_inline_chars: number;
  delivery_manifest: DeliveryManifest | null;
  /**
   * local_retrieval 일 때 위 chars/bytes 는 근거 패키지 예산으로 계산한
   * **최댓값**이다. 실제 실행은 이 값을 넘지 못한다. full_inline 이면 null.
   */
  evidence_budget_chars: number | null;
  evidence_budget_bytes?: number | null;
  message: string;
  error: string | null;
};
