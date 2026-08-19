export type JobStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type ResultQuality = "SUCCESS" | "SUCCESS_WITH_WARNINGS";

export interface Prompt {
  id: string;
  name: string;
  description: string;
  body: string;
  version: number;
  enabled: boolean;
  archived: boolean;
  output_mode: "markdown" | "text";
  default_provider: string | null;
  default_model: string | null;
  tags: string[];
  accepted_file_types: string[];
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
  capabilities: Record<string, boolean | null>;
  notes: string[];
  install_hint: string;
  usable: boolean;
  /** 설치/실행/인증만 본 상태. 안전 정책은 반영하지 않음. */
  runnable: boolean;
  experimental: boolean;
  opted_in: boolean;
  risks: string[];
}

export interface AttachmentAnalysis {
  attachment_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
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
  user_input: string;
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
    provider_paths: Record<string, string>;
    default_models: Record<string, string>;
    keep_raw_output: boolean;
    fail_on_tool_use: boolean;
    enabled_experimental_providers: string[];
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
