// Mirrors the backend's response models (backend/app/schemas). Keep in sync with /openapi.json.

export interface Me {
  id: string;
  login: string;
  avatar_url: string | null;
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
