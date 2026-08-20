export type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type ResultQuality = "SUCCESS" | "SUCCESS_WITH_WARNINGS";

export type AttachmentRole = "APPLICATION" | "CITATION" | "SUPPLEMENTAL";

/** 후속 실행이 원본에서 무엇을 물려받았는지. null 이면 독립 실행.
 *
 *  MAPPED     인용발명 번호 + 이전 청구항. 이전 보고서는 받지 않는다.
 *  CONTINUED  거기에 이전 보고서 전체를 더한다. 보고서 수정·보완용.
 *  REANALYZED 첨부만. 번호도 이전 판단도 물려받지 않는다.
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
  capabilities: Record<string, boolean | string[] | null>;
  notes: string[];
  install_hint: string;
  usable: boolean;
  /** 설치/실행/인증만 본 상태. 안전 정책은 반영하지 않음. */
  runnable: boolean;
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
}

export interface UploadResponse {
  batch_id: string;
  files: AttachmentAnalysis[];
  rejected: { filename: string; reason: string }[];
  total_chars: number;
  max_inline_chars: number;
}

export interface JobAttachment extends AttachmentAnalysis {
  required: boolean;
}

export interface Job {
  id: string;
  status: JobStatus;
  result_quality: ResultQuality | null;
  error_code: string | null;
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
  warnings: string[];
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
  result_quality: ResultQuality | null;
  error_code: string | null;
  prompt_name: string;
  prompt_version: number | null;
  provider: string;
  model: string | null;
  created_at: string;
  duration_ms: number | null;
  attachment_count: number;
  warning_count: number;
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
