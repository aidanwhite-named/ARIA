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
  strategy: "combined_then_individual";
  components: AnalysisComponent[];
}

/** 이 후보를 무엇으로 알게 되었는가.
 *
 *  search_snippet        검색 결과 제목·스니펫만 봤다
 *  webfetch_summary      WebFetch 로 페이지를 열어 요약을 받았다 (원문 아님)
 *  raw_original_verified 공식 원문 텍스트를 확보해 대조했다
 */
export type SearchProvenance =
  | "search_snippet"
  | "webfetch_summary"
  | "raw_original_verified";

/** 청구항 구성 대응표 한 줄.
 *
 *  verified 가 false 면 source_location·verbatim_excerpt·translation 은 ARIA 가
 *  확정 표현으로 바꾼 값이며, 모델이 쓴 원래 값은 들어 있지 않다.
 */
export interface SearchMappingRow {
  feature: string;
  counterpart: string;
  degree: string;
  verified: boolean;
  /** 이 행의 대응 주장을 무엇을 읽고 썼는가. verified 와 다른 축이다.
   *
   *  verified 는 후보의 공식 원문을 대조했는지이고 web 채널에서는 늘 false 다.
   *  그래서 그것만으로는 근거 있는 행과 지어낸 행이 화면에서 구분되지 않았다.
   *  none 인 행은 degree 가 "확인되지 않음" 이고 서술 칸이 비어 있다.
   */
  support_source?: "page_text" | "snippet" | "none";
  support_text?: string;
  support_scope?: "claims" | "full_text" | "abstract" | "unknown";
  support_url?: string;
  page_supported?: boolean;
  source_location: string;
  verbatim_excerpt: string;
  translation: string;
  similar: string;
  different: string;
}

export interface SearchCandidate {
  index: number;
  group: "A" | "B" | "C";
  provisional: boolean;
  /** 검색 경로. 현재는 web 뿐이고 향후 epo 등이 추가된다. */
  channel: string;
  doc_type: string;
  doc_number: string;
  doi: string;
  title: string;
  applicant: string;
  url: string;
  /** 관측 기록과 대조하기 위해 정규화한 URL. */
  canonical_url: string;
  family: string;
  provenance: SearchProvenance;
  evidence_status: "candidate_only" | "source_page_reviewed";
  original_verified: boolean;
  /** ARIA 가 관측한 사실. 이 URL 로 성공한 WebFetch 호출이 있었는가. */
  page_fetch_succeeded: boolean;
  /** 후보 식별 게이트의 결과. 모두 ARIA 가 계산하며 모델이 정하지 않는다.
   *
   *  옛 매니페스트에는 없다. 없으면 격리 이전의 기록이므로 그대로 그룹에 둔다.
   */
  url_is_document?: boolean;
  identifier_url_matched?: boolean;
  quarantined?: boolean;
  quarantine_reason?: string;
  group_eligible?: boolean;
  page_supported_rows?: number;
  verbatim_excerpt: string;
  source_location: string;
  mapping: SearchMappingRow[];
  note: string;
  /** ARIA가 붙인 독립 검색 출처. 모델이 정하는 값이 아니다. */
  search_origins?: ("claim_only" | "spec_assisted")[];
  /** 같은 문헌을 각 경로가 어느 그룹으로 분류했는지. */
  origin_groups?: Partial<Record<"claim_only" | "spec_assisted", "A" | "B" | "C">>;
}

/** 청구항 문언을 명세서로 어떻게 읽었는지, 모델이 보고한 한 줄.
 *
 *  ARIA 가 검증하는 값이 아니다. basis 가 가리키는 명세서 위치에 정말 그 내용이
 *  있는지는 사람이 원문에서 확인해야 한다. 그래도 화면에 내보내는 이유는,
 *  검색 범위가 청구항보다 좁아졌다면 그 사실이 어딘가에 보여야 하기 때문이다.
 */
export interface ClaimInterpretation {
  term: string;
  reading: string;
  basis: string;
  /** 청구항 문언 자체보다 좁게 읽었는가. 알 수 없으면 true 로 둔다. */
  narrowed: boolean;
}

/** 명세서 보조 검색에서 모델이 보고한 검색어 확장 근거. */
export interface SearchTermExpansion {
  claim_term: string;
  alternative_meanings: string[];
  expanded_terms: string[];
  basis: string;
  excluded_limitations: string[];
}

/** 검색에 곁들인 출원발명 문서. 넣지 않았으면 null. */
export interface SearchSpecDocument {
  attachment_id: string;
  filename: string;
  sha256: string;
  page_count: number | null;
  char_count: number;
}

export interface SearchManifest {
  version: number;
  generated_at: string;
  channels: string[];
  /** 이 실행이 쓴 A/B/C 정의. 렌더러가 자기 표를 들고 있으면 정의를 고친 뒤
   *  갱신되지 않은 렌더러가 옛 제목으로 인쇄한다. 실제로 그렇게 어긋난 적이
   *  있어서 정의를 기록에 싣는다. 없는 옛 매니페스트만 fallback 을 쓴다. */
  group_schema_version?: number;
  group_definitions?: Record<string, string>;
  input: {
    claim_text: string;
    claim_boundary_neutralized: boolean;
    spec_document?: SearchSpecDocument | null;
    spec_boundary_neutralized?: boolean;
    search_focus?: GapSearchFocus | null;
    focus_boundary_neutralized?: boolean;
  };
  prompt: { id: string; version: number | null; sha256: string };
  policy: {
    name: string;
    allowed_tools: string[];
    advertised_tools_enforced: boolean;
    max_rounds: number;
    search_domain_restriction: boolean;
    search_strategy?:
      | "claim_only"
      | "isolated_union"
      | "combined_then_individual";
    candidate_merge?: "single" | "union";
    max_tool_calls_total?: number | null;
    lane_budgets?: Partial<Record<"claim_only" | "spec_assisted", number>>;
  };
  timing: { started_at: string | null; completed_at: string | null };
  search_lanes?: {
    id: "claim_only" | "spec_assisted";
    spec_in_context: boolean;
    prompt_sha256: string;
    max_tool_calls: number;
    started_at: string;
    completed_at: string;
    status: string;
    error_code: string | null;
  }[];
  /** ARIA 가 스트림에서 직접 관측한 것. 모델의 자기 보고가 아니다. */
  observed: {
    tool_names: string[];
    tool_call_counts: Record<string, number>;
    tool_calls: {
      id: string | null;
      name: string;
      ts: string;
      input: Record<string, unknown>;
      ok: boolean | null;
      error: string | null;
    }[];
    search_queries: string[];
    search_queries_by_origin?: Partial<
      Record<"claim_only" | "spec_assisted", string[]>
    >;
    /** 열려고 시도한 주소. 성공했다는 뜻이 아니다. */
    attempted_fetch_urls: string[];
    /** 실제로 열린 주소. 후보의 열람 주장은 이 목록과 대조해야 인정된다. */
    succeeded_fetch_urls: string[];
    tool_failures: {
      name: string;
      ts: string;
      input: Record<string, unknown>;
      error: string;
      search_origin?: "claim_only" | "spec_assisted";
    }[];
  };
  /** 모델이 보고한 것. 읽지 못했으면 null. */
  reported: {
    rounds: {
      round: number;
      channel: string;
      queries: string[];
      note: string;
      search_origin?: "claim_only" | "spec_assisted";
    }[];
    term_expansions?: SearchTermExpansion[];
    /** v1 저장 기록을 여는 동안만 사용하는 이전 필드. */
    claim_interpretation?: ClaimInterpretation[];
    candidates: SearchCandidate[];
    access_failures: {
      url: string;
      reason: string;
      search_origin?: "claim_only" | "spec_assisted";
    }[];
  } | null;
  /** ARIA 가 증거 등급을 내리거나 고친 내역. */
  normalization_notes: string[];
  error: string | null;
}

/** 후속 실행이 원본에서 무엇을 물려받았는지. null 이면 독립 실행.
 *
 *  MAPPED     인용발명 번호 + 이전 청구항. 이전 보고서는 받지 않는다.
 *  CONTINUED  거기에 이전 보고서 전체를 더한다. 보고서 수정·보완용.
 *  REANALYZED 같은 자료로 재분석. 번호도 이전 판단도 물려받지 않는다.
 */
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

export interface Prompt {
  id: string;
  name: string;
  description: string;
  body: string;
  version: number;
  enabled: boolean;
  output_mode: "markdown" | "text";
  tags: string[];
  accepted_file_types: string[];
  /** 프롬프트 파일 메타데이터에서만 정하는 ARIA 확장 선언. */
  capabilities: string[];
  created_at: string;
  updated_at: string;
}

export type PromptKind = "analysis" | "search";

export interface PromptCatalogItem extends Prompt {
  kind: PromptKind;
  editable: boolean;
  deletable: boolean;
}

export interface PromptVersion {
  id: string;
  version: number;
  name: string;
  description: string;
  body: string;
  output_mode: string;
  created_at: string;
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
  capabilities: Record<string, boolean | string | string[] | null>;
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

export interface Job {
  id: string;
  status: JobStatus;
  error_code: string | null;
  job_kind: JobKind;
  prompt_id: string | null;
  prompt_name: string;
  prompt_version: number | null;
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
  /** 유사 문헌 검색의 감사 기록. 분석 실행에서는 null. */
  search_manifest: SearchManifest | null;
  /** 모델 보고 블록을 읽지 못한 사유. 관측 기록은 이 경우에도 남는다. */
  search_manifest_error: string | null;
  /** 구성대비 결과에서 시작한 검색의 선택 구성 스냅샷. */
  search_focus: GapSearchFocus | null;
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
  prompt_version: number | null;
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
    default_provider: string;
    provider_paths: Record<string, string>;
    default_models: Record<string, string>;
    keep_raw_output: boolean;
    fail_on_tool_use: boolean;
    max_search_tool_calls: number;
    kiwee_integration_enabled: boolean;
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
  message: string;
  error: string | null;
};
