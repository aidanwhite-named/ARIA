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
   *  실패시킨다. 원문은 절대 자르지 않는다. */
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
  | "raw_original_verified"
  /** ARIA 가 EPO OPS 를 직접 불러 받은 응답에서 후보를 만들었다. 모델이 주장할
   *  수 없는 값이며, "원문 대조 완료"와 다르다 — 응답을 받아 보존했다는 뜻이지
   *  그 문장이 특허 원문의 직접 인용이라는 뜻이 아니다. */
  | "official_record_response";

/** 이 후보를 데려온 검색 경로. search_origins(무엇을 입력으로 검색했나)와
 *  다른 축이다. 값이 없는 옛 기록은 web 하나로 읽는다. */
export type SearchDiscoveryOrigin = "web" | "epo";

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
  support_source?: "page_text" | "snippet" | "official_record" | "none";
  support_text?: string;
  support_scope?: "claims" | "full_text" | "abstract" | "unknown";
  support_url?: string;
  page_supported?: boolean;
  official_supported?: boolean;
  support_match_kind?: string;
  support_field?: string;
  support_artifact_id?: string;
  source_location: string;
  verbatim_excerpt: string;
  translation: string;
  similar: string;
  different: string;
}

export interface SearchCandidate {
  index: number;
  /**
   * 정식 그룹. 새 실행은 A/B 만 만들고, C 는 과거 매니페스트에서만 온다.
   * 그룹 자격을 얻지 못한 후보(참고 후보)는 null 이다.
   */
  group: "A" | "B" | "C" | null;
  /**
   * 왜 정식 A/B 가 아닌가. group 이 차 있으면 빈 문자열이다.
   * below_threshold 는 공식 검증한 결론이고, unverified 는 아직 안 본 것이다.
   */
  classification_outcome?: "" | "below_threshold" | "unverified" | "legacy_c";
  /**
   * 페이지 관측으로 정식 A/B 를 받은 후보를 공식 응답으로 추가 확인했는가.
   * not_confirmed 라도 분류는 내리지 않는다 — OPS 가 주는 것은 초록·청구항
   * 뿐이라 명세서에만 있는 구성은 여기서 대조될 수 없기 때문이다.
   */
  official_ab_confirmation?: "confirmed" | "not_confirmed";
  official_ab_confirmation_detail?: string;
  /** 검색 결과 기반 AI 제안. 정식 group 과 동시에 값이 있으면 안 된다. */
  provisional_group?: "A" | "B" | "C" | null;
  /** ARIA가 저장된 관측/공식 근거로 계산한 분류 근거 등급. */
  classification_basis?:
    | "none"
    | "legacy_unknown"
    | "search_result"
    | "page_observed"
    | "official_record"
    | "original_text";
  provisional: boolean;
  /** 검색 경로. 현재는 web 뿐이고 향후 epo 등이 추가된다. */
  channel: string;
  doc_type: string;
  doc_number: string;
  doi: string;
  title: string;
  /** 검증되지 않은 제목. 문헌번호-주소 대조를 통과하지 못하면 title 이 비고
   *  이 칸에만 남는다. 표시할 때는 반드시 "검색 결과 기반·미검증" 이라고
   *  밝히며, 이 값만으로는 A/B 등급도 구성 대응표도 만들어지지 않는다.
   *  옛 매니페스트에는 없다. */
  reported_title?: string;
  applicant: string;
  url: string;
  /** 관측 기록과 대조하기 위해 정규화한 URL. */
  canonical_url: string;
  family: string;
  provenance: SearchProvenance;
  evidence_status:
    | "candidate_only"
    | "source_page_reviewed"
    | "official_record_verified";
  original_verified: boolean;
  /** ARIA 가 관측한 사실. 이 URL 로 성공한 WebFetch 호출이 있었는가. */
  page_fetch_succeeded: boolean;
  /** 후보 식별 게이트의 결과. 모두 ARIA 가 계산하며 모델이 정하지 않는다.
   *
   *  옛 매니페스트에는 없다. 없으면 저장된 다른 검증 근거가 없는 한 잠정이다.
   */
  url_is_document?: boolean;
  identifier_url_matched?: boolean;
  quarantined?: boolean;
  quarantine_reason?: string;
  group_eligible?: boolean;
  page_supported_rows?: number;
  official_supported_rows?: number;
  matched_feature_rows?: number;
  official_identity_matched?: boolean;
  official_evidence?: {
    status?: string;
    reason?: string;
    backend_id?: string;
    artifact_ids?: string[];
    fields?: string[];
  };
  verification?: {
    status:
      | "not_attempted"
      | "fetch_failed"
      | "record_fetched"
      | "classification_failed"
      | "evidence_mismatch"
      | "promoted";
    reason_code: string;
    detail: string;
    backend_id: string;
    artifact_ids: string[];
  };
  verbatim_excerpt: string;
  source_location: string;
  mapping: SearchMappingRow[];
  note: string;
  /** 공식 대조로 덮이기 전의 1차(페이지 관측) 분류. 이 후보의 분류가 아니라
   *  대체되기 전의 기록이다. 두 값을 같은 칸에 두면 위계가 사라진다. */
  page_classification?: {
    group: "A" | "B" | "C";
    classification_basis: string;
    page_supported_rows?: number;
    evidence_status?: string;
    url?: string;
    mapping?: SearchMappingRow[];
  };
  /** 이 후보를 데려온 검색 경로. 없으면 web 하나로 읽는다. */
  discovery_origins?: SearchDiscoveryOrigin[];
  /** EPO 독립 검색이 이 문헌을 찾았을 때의 기록. 비어 있으면 나오지 않았다. */
  epo_discovery?: {
    lanes?: string[];
    doc_number?: string;
    first_seen_round?: number;
    artifact_ids?: string[];
    evidence_fields?: string[];
    shortlist?: {
      lane?: string;
      round?: number;
      reason?: string;
      matched_elements?: string[];
    }[];
  };
  /** EPO 후보의 백엔드 id. 웹 후보에는 없다. */
  backend_id?: string;
  /** ARIA가 붙인 독립 검색 출처. 모델이 정하는 값이 아니다. */
  search_origins?: ("claim_only" | "spec_assisted")[];
  /** 같은 문헌을 각 경로가 어느 그룹으로 분류했는지. */
  origin_groups?: Partial<
    Record<"claim_only" | "spec_assisted", "A" | "B" | "C" | null>
  >;
  origin_provisional_groups?: Partial<
    Record<"claim_only" | "spec_assisted", "A" | "B" | "C" | null>
  >;
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

/** EPO 검색 레인 하나의 기록. 후보를 다른 레인과 합치지 않는다. */
export interface EpoLaneRecord {
  id: string;
  channel?: string;
  origin?: "claim_only" | "spec_assisted";
  status: string;
  error?: string;
  termination_reason?: string;
  termination_detail?: string;
  search_calls?: number;
  detail_fetches?: number;
  /** 검색 전에 청구항을 어떻게 나눠 읽었는가. 모델의 판단이다. */
  claim_analysis?: {
    round?: number;
    notes?: string;
    elements?: {
      id: string;
      text: string;
      /** 세 상태다. null 은 "판단 없음"이며 "필수 아님"이 아니다. */
      essential?: boolean | null;
      synonyms?: string[];
    }[];
    relations?: {
      source?: string;
      target?: string;
      kind?: string;
      description?: string;
    }[];
    concept_combinations?: {
      elements?: string[];
      terms?: string[];
      reason?: string;
    }[];
    search_conditions?: { kind?: string; value?: string; reason?: string }[];
  };
  /** 최종 대응표로 넘긴 유망 후보. */
  shortlist?: {
    doc_number: string;
    reason?: string;
    matched_elements?: string[];
    round?: number;
  }[];
  /** 상한·대조 실패로 넘기지 않은 것과 그 사유. */
  excluded?: {
    kind: string;
    value?: string;
    reason_code: string;
    detail: string;
  }[];
  /** NO_TOOLS 계획 턴에서 감지된 도구 호출. 비어 있어야 정상이다. */
  tool_violations?: {
    provider?: string;
    lane?: string;
    tools?: string[];
    /** post_hoc_detection 은 차단이 아니다. 외부 호출은 이미 나갔다. */
    isolation?: "provider_enforced" | "post_hoc_detection" | "unknown";
    /** 어느 턴에서 감지됐는가. 없으면 검색 라운드다. */
    phase?: string;
    detail?: string;
  }[];
  tool_isolation?: string;
  /** 검색하지 않는 최종 선택 턴. 돌리지 않았으면 빈 객체다. */
  selection?: {
    attempted?: boolean;
    status?: string;
    reason?: string;
    candidates_reviewed?: number;
    shortlist_added?: number;
    /** 이 턴에서 모델이 보냈지만 실행하지 않은 검색·조회 action 수. */
    rejected_actions?: number;
    /** Provider 가 알려준 이 턴의 사용량. 알려주지 않으면 빈 객체다 —
     *  0 으로 적으면 "안 썼다"로 읽힌다. */
    provider_usage?: Record<
      string,
      number | string | boolean | Record<string, number>
    >;
  };
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
  prompt: {
    id: string;
    /** 템플릿 파일의 해시. 모델에게 실제로 간 프롬프트가 아니다(호환용 별칭). */
    sha256: string;
    /** 위와 같은 값. 이름으로 무엇의 해시인지 알 수 있게 둔 것. */
    template_sha256?: string;
    /** 런타임 컨텍스트의 해시. 템플릿이 같아도 이것이 다르면 프롬프트가 다르다. */
    runtime_context_sha256?: string;
    /** 레인별로 모델에게 실제로 간 프롬프트의 해시. */
    effective_prompt_sha256?: Record<string, string>;
  };
  /** 요청한 추론강도. 실제 적용값은 CLI 가 알려주지 않아 기록하지 않는다. */
  reasoning_effort?: { requested: string; model_default: boolean };
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
  verification?: {
    attempted?: boolean;
    reason?: string;
    backend_id?: string;
    classification_error?: string;
    counts?: {
      targets: number;
      verified: number;
      fetch_failed: number;
      not_attempted: number;
    };
    promotion_policy?: {
      minimum_official_supported_rows: number;
      coverage_ratio_threshold: number | null;
      group_assignment: string;
    };
    /** 상한 때문에 공식 대조를 시도하지 않은 후보. 조용히 누락하지 않는다. */
    excluded_candidates?: {
      index: number;
      doc_number: string;
      reason_code: string;
      detail: string;
      selection_reason?: string;
      selection_bucket?: "page_ab" | "epo_shortlist" | "other";
      expected_fetches?: number | null;
      missing_constituents?: string[];
    }[];
    limits?: Record<string, number>;
    /** 무엇을 어떤 순서로 왜 골랐는가. 상한이 무엇을 잘랐는지와 함께 읽는다. */
    selection_order?: {
      position: number;
      index: number;
      doc_number: string;
      selection_reason: string;
      selection_bucket?: "page_ab" | "epo_shortlist" | "other";
      detail: string;
      /** 이 후보를 확인하려고 OPS 를 몇 번 더 부를 것인가. null 이면 세어 보지
       *  않았다는 뜻이고 0 과 다르다. */
      expected_fetches?: number | null;
      /** 아직 손에 없는 구성요소. 위 횟수의 근거다. */
      missing_constituents?: string[];
    }[];
    selection_policy?: {
      ranking?: string[];
      labels?: Record<string, string>;
      /** 후보 하나를 확인하는 데 필요한 구성요소. */
      constituents?: string[];
      /** 고르는 시점에 예상한 추가 조회 횟수의 합. */
      planned_fetch_calls?: number;
      unknown_fetch_plans?: number;
    };
    usage?: {
      official_fetch_calls?: number;
      /** 이번 단계가 다시 받지 않고 재사용한 EPO 검색 응답 수. */
      reused_artifact_calls?: number;
      /** 선택 시점 계획에서 필요한 구성요소가 모두 있었던 문헌. */
      fully_reused_documents?: number;
      /** 선택 시점 계획에서 일부 구성요소가 부족했던 문헌. */
      partially_reused_documents?: number;
      /** 재사용 계획을 계산하지 못한 문헌. 0회와 구분한다. */
      reuse_plan_unknown_documents?: number;
      /** 재사용 문헌 중 이번 검증 단계에서 실제 추가 호출이 없었던 문헌. */
      reused_without_fresh_fetch_documents?: number;
      /** 재사용 문헌 중 이번 검증 단계에서 실제 추가 호출이 발생한 문헌. */
      reused_with_fresh_fetch_documents?: number;
      /** 선택 시점의 예상 추가 조회 횟수. 실제와 어긋나면 상한·취소·실패다. */
      planned_fetch_calls?: number;
      classification_runs?: number;
    };
  };
  /** EPO 채널 기록. 웹과 섞지 않는다. */
  epo?: {
    enabled?: boolean;
    backend_id?: string;
    reason?: string;
    error?: string;
    channel_budget?: Record<string, unknown>;
    lane_budget?: Record<string, number>;
    lanes?: EpoLaneRecord[];
  };
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
    /** 검색 호출 수. 한 호출이 질의 여러 개를 묶어 보내므로 질의 수와 다르다. */
    search_call_count?: number;
    search_queries_by_origin?: Partial<
      Record<"claim_only" | "spec_assisted", string[]>
    >;
    /** 검색어가 아니라 URL 로 부른 호출. 시도일 뿐 열람이 아니다. */
    url_lookup_attempts?: string[];
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
    /** 성공도 실패도 관측할 수 없었던 호출. 성공으로 읽으면 안 된다. */
    unknown_tool_outcomes?: {
      name: string;
      ts: string;
      input: Record<string, unknown>;
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
    /** 웹 채널의 구조화 결과를 읽지 못한 사유. 비어 있지 않으면 이 후보
     *  목록은 EPO 독립 검색만으로 만들어졌다는 뜻이다. */
    web_report_error?: string;
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
  /** 유사 문헌 검색의 감사 기록. 분석 실행에서는 null. */
  search_manifest: SearchManifest | null;
  /** 모델 보고 블록을 읽지 못한 사유. 관측 기록은 이 경우에도 남는다. */
  search_manifest_error: string | null;
  /** 구성대비 결과에서 시작한 검색의 선택 구성 스냅샷. */
  search_focus: GapSearchFocus | null;
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
    /** EPO OPS 연동 토글. 켜고 자격증명을 넣어도 아직 검색 경로에는 연결되지 않는다. */
    epo_integration_enabled: boolean;
    epo_consumer_key: string;
    /**
     * 응답에서는 **항상 빈 문자열**이다. 저장은 되지만 되돌려주지 않는다.
     * 저장 여부는 secrets_set 을 봐야 한다.
     */
    epo_consumer_secret: string;
    /** OPS HTTP 대기 시간의 총합. EPO 채널 전체 벽시계와 다른 축이다. */
    epo_http_budget_seconds: number;
    /** 0 = 시간당 사용량을 관측·표시만 하고 차단하지 않음. 주간 한도는 계약값이라 별도. */
    epo_hourly_quota_bytes: number;
    epo_max_detail_fetches: number;
    /** 질의 하나가 받아 오는 결과 건수 상한. OPS 자체 상한은 20건이다. */
    epo_max_results_per_query: number;
    /** 최종 A/B/C 대응표까지 끌고 갈 EPO 유망 후보 수 상한. */
    epo_shortlist_limit: number;
    /** 공식 문헌 대조를 시도할 후보 수 상한. 조회 예산과 다른 축이다. */
    epo_verification_targets: number;
    /** ARIA 가 관측해 적는 값. 사용자가 PUT 으로 못 고친다(사용량 되돌리기 방지). */
    epo_quota_state: Record<string, unknown>;
    /**
     * 비특허문헌(Crossref·Europe PMC) 연동. 자격증명이 필요 없어 켜기만 하면
     * 동작한다. 웹 검색이 식별하지 못한 논문을 ARIA 가 직접 찾아 온다.
     */
    literature_integration_enabled: boolean;
    /** Crossref 예의 풀 표시용 연락처. 비워 둬도 동작한다. */
    literature_contact_email: string;
    /** 한 실행에서 ARIA 가 직접 보낼 서지 질의 수 상한. */
    literature_max_queries: number;
    /** 질의 하나가 받아 오는 결과 건수 상한. 두 DB 각각에 적용된다. */
    literature_max_results_per_query: number;
    /** 서지 API HTTP 대기 시간의 총합(초). */
    literature_http_budget_seconds: number;
    /** 공식 서지 대조를 시도할 논문 후보 수 상한. EPO 예산과 다른 축이다. */
    literature_verification_targets: number;
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
  message: string;
  error: string | null;
};
