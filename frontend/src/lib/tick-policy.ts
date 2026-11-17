import type { TickResult } from "./types";

export type NextStep =
  | { action: "tick"; delayMs: number }
  | { action: "stop"; reason: StopReason };

export type StopReason = "ready" | "failed" | "not_implemented" | "no_job";

const MIN_DELAY_MS = 250;
const IDLE_DELAY_MS = 3_000;
const MAX_WAIT_MS = 5 * 60_000;

/**
 * Decides what the browser should do after one tick. The backend does one bounded step per
 * call and has no background worker, so the open page drives indexing forward.
 */
export function decideNext(result: TickResult, now: Date = new Date()): NextStep {
  const { status, ran, reason } = result;

  if (status.state === "ready") return { action: "stop", reason: "ready" };
  if (status.state === "failed") return { action: "stop", reason: "failed" };
  // Later phases have no handler yet: ticking again would change nothing.
  if (reason?.startsWith("phase_not_implemented")) {
    return { action: "stop", reason: "not_implemented" };
  }

  // Backing off after a transient error (or a GitHub rate limit): wait until it is over.
  if (status.retry_at) {
    const wait = new Date(status.retry_at).getTime() - now.getTime() + 500;
    return { action: "tick", delayMs: Math.min(Math.max(wait, 1_000), MAX_WAIT_MS) };
  }

  if (ran && !status.error) return { action: "tick", delayMs: MIN_DELAY_MS };
  // Another tab holds the lease, or a failed attempt without a scheduled retry.
  return { action: "tick", delayMs: IDLE_DELAY_MS };
}
