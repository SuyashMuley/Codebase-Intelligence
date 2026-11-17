// Mirrors the backend's response models (backend/app/schemas). Keep in sync with /openapi.json.

export interface Me {
  id: string;
  login: string;
  avatar_url: string | null;
}

export interface GithubRepo {
  owner: string;
  name: string;
  description: string | null;
  language: string | null;
  stars: number;
  pushed_at: string | null;
  fork: boolean;
  archived: boolean;
}

export interface GithubRepoList {
  items: GithubRepo[];
}

export interface Repository {
  id: string;
  owner: string;
  name: string;
  url: string;
  default_branch: string | null;
  created_at: string;
}

export interface RepositoryList {
  items: Repository[];
  total: number;
  limit: number;
  offset: number;
}

export type IndexState = "not_indexed" | "pending" | "running" | "ready" | "failed";

export interface FetchProgress {
  files_kept: number;
  bytes_kept: number;
  skipped: Record<string, number>;
  secrets_redacted: number;
  files_redacted: number;
}

export interface ParseProgress {
  files: number;
  symbols: number;
  chunks: number;
  refs: number;
  syntax_error_files: number;
  truncated_files: number;
  done: boolean;
}

export interface EmbedProgress {
  embedded: number;
  total: number;
  model: string;
  done: boolean;
  neurons: number;
}

export interface GraphProgress {
  files: number;
  symbols: number;
  static_imports: number;
  unresolved_imports: number;
  static_calls: number;
  unresolved_calls: number;
  truncated: boolean;
  done: boolean;
}

export interface JobError {
  code: string;
  message: string;
}

export interface IndexStatus {
  state: IndexState;
  snapshot_id: string | null;
  commit_sha: string | null;
  phase: string | null;
  error: JobError | null;
  attempts: number;
  max_attempts: number;
  retry_at: string | null;
  can_resume: boolean;
  progress: FetchProgress | null;
  parse: ParseProgress | null;
  embed: EmbedProgress | null;
  graph: GraphProgress | null;
  updated_at: string | null;
}

export interface IndexStartResult {
  created: boolean;
  status: IndexStatus;
}

export interface TickResult {
  ran: boolean;
  reason: string | null;
  status: IndexStatus;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    request_id: string | null;
  };
}

export interface SnapshotRef {
  id: string;
  commit_sha: string;
  state: string;
}

export interface FileEntry {
  path: string;
  language: string | null;
  size_bytes: number;
  skipped_reason: string | null;
  redactions: number;
  parse_status: string | null;
  symbol_count: number;
}

export interface FileList {
  snapshot: SnapshotRef;
  items: FileEntry[];
  truncated: boolean;
}

export interface SymbolInfo {
  id: number;
  kind: string;
  name: string;
  qualified_name: string;
  start_line: number;
  end_line: number;
  parent_id: number | null;
  signature: string | null;
}

export interface FileDetail {
  snapshot: SnapshotRef;
  path: string;
  language: string | null;
  size_bytes: number;
  skipped_reason: string | null;
  redactions: number;
  parse_status: string | null;
  line_count: number;
  content: string | null;
  symbols: SymbolInfo[];
}

export interface Stats {
  snapshot: SnapshotRef;
  parsing_done: boolean;
  files: {
    stored: number;
    parsed: number;
    bytes: number;
    by_language: Record<string, number>;
    skipped: Record<string, number>;
  };
  symbols: { total: number; by_kind: Record<string, number> };
  chunks: {
    total: number;
    average_chars: number;
    largest_chars: number;
    embedded: number;
    embedding_model: string | null;
  };
  refs: { imports: number; calls: number };
  graph: {
    static_imports: number;
    unresolved_imports: number;
    static_calls: number;
    unresolved_calls: number;
    truncated: boolean;
  } | null;
  redactions: { spans: number; files: number };
  syntax_error_files: number;
}

export type SearchMode = "vector" | "hybrid" | "keyword";

export interface SearchHit {
  chunk_id: number;
  path: string;
  start_line: number;
  end_line: number;
  symbol: string | null;
  text: string;
  score: number;
  keyword_rank: number | null;
  vector_rank: number | null;
}

export interface SearchResponse {
  mode_used: SearchMode;
  warnings: string[];
  hits: SearchHit[];
}

export interface GraphNodeData {
  id: string;
  kind: "file" | "symbol" | "external";
  label: string;
  path: string | null;
  language: string | null;
  symbol_kind: string | null;
  start_line: number | null;
  in_degree: number;
  out_degree: number;
  focus: boolean;
}

export interface GraphEdgeData {
  source: string;
  target: string;
  kind: "imports" | "calls";
  resolution: "static" | "unresolved";
  count: number;
  line: number | null;
  reason: string | null;
}

export interface GraphData {
  snapshot: SnapshotRef;
  level: "file" | "symbol";
  nodes: GraphNodeData[];
  edges: GraphEdgeData[];
  truncated: boolean;
  total_nodes: number;
  note: string | null;
}

export interface ImpactHop {
  source: string;
  target: string;
  kind: "imports" | "calls";
}

export interface ImpactDependent {
  path: string;
  symbol: string | null;
  depth: number;
  hops: ImpactHop[];
}

export interface UnverifiedCaller {
  path: string;
  symbol: string | null;
  line: number | null;
  called_as: string;
  reason: string | null;
}

export interface SuggestedTest {
  path: string;
  basis: "static_dependency" | "naming_convention";
  confidence: "high" | "medium" | "low";
  reason: string;
  heuristic: boolean;
}

export interface Impact {
  snapshot: SnapshotRef;
  target: { type: "file" | "symbol"; path: string; symbol: string | null; symbol_id: number | null };
  max_depth: number;
  dependents: ImpactDependent[];
  direct: number;
  transitive: number;
  files_affected: number;
  truncated: boolean;
  unverified_callers: UnverifiedCaller[];
  suggested_tests: SuggestedTest[];
  limitations: string[];
}

export interface AiReview {
  notice: string;
  model: string | null;
  summary: string;
  risks: { description: string; confidence: "low" | "medium" | "high"; paths: string[] }[];
  tests_to_consider: { path: string; reason: string }[];
  open_questions: string[];
  dropped: number;
  static_dependents_considered: number;
}
