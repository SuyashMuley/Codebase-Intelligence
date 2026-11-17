import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import type { IndexStatus, TickResult } from "@/lib/types";

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function apiError(status: number, code: string, message = "msg"): Response {
  return jsonResponse({ error: { code, message, details: {}, request_id: "req-1" } }, status);
}

export function makeStatus(overrides: Partial<IndexStatus> = {}): IndexStatus {
  return {
    state: "running",
    snapshot_id: "snap-1",
    commit_sha: "0123456789abcdef0123456789abcdef01234567",
    phase: "fetch",
    error: null,
    attempts: 0,
    max_attempts: 3,
    retry_at: null,
    can_resume: false,
    progress: null,
    parse: null,
    embed: null,
    updated_at: null,
    ...overrides,
  };
}

export function makeTick(
  status: Partial<IndexStatus> = {},
  ran = true,
  reason: string | null = null,
): TickResult {
  return { ran, reason, status: makeStatus(status) };
}
