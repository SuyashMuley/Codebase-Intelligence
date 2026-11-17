import { describe, expect, it } from "vitest";
import { makeTick } from "@/test/utils";
import { decideNext } from "./tick-policy";

const NOW = new Date("2026-01-01T00:00:00Z");

describe("decideNext", () => {
  it("stops when the job is ready", () => {
    expect(decideNext(makeTick({ state: "ready", phase: "done" }), NOW)).toEqual({
      action: "stop",
      reason: "ready",
    });
  });

  it("stops when the job has failed", () => {
    const result = decideNext(makeTick({ state: "failed", phase: "failed", can_resume: true }), NOW);
    expect(result).toEqual({ action: "stop", reason: "failed" });
  });

  it("stops (instead of polling forever) when a later phase has no handler", () => {
    const result = decideNext(makeTick({ phase: "parse" }, false, "phase_not_implemented:parse"), NOW);
    expect(result).toEqual({ action: "stop", reason: "not_implemented" });
  });

  it("ticks again quickly after a successful step", () => {
    expect(decideNext(makeTick({ phase: "parse" }), NOW)).toEqual({ action: "tick", delayMs: 250 });
  });

  it("waits until retry_at while backing off, plus a small margin", () => {
    const retryAt = new Date(NOW.getTime() + 30_000).toISOString();
    const result = decideNext(
      makeTick({ retry_at: retryAt, error: { code: "github_unavailable", message: "x" } }),
      NOW,
    );
    expect(result).toEqual({ action: "tick", delayMs: 30_500 });
  });

  it("never waits less than a second or more than five minutes", () => {
    const soon = new Date(NOW.getTime() - 5_000).toISOString();
    expect(decideNext(makeTick({ retry_at: soon }), NOW)).toEqual({ action: "tick", delayMs: 1_000 });
    const far = new Date(NOW.getTime() + 3_600_000).toISOString();
    expect(decideNext(makeTick({ retry_at: far }), NOW)).toEqual({ action: "tick", delayMs: 300_000 });
  });

  it("polls gently when another tab holds the lease", () => {
    expect(decideNext(makeTick({}, false, "busy_or_backing_off_or_finished"), NOW)).toEqual({
      action: "tick",
      delayMs: 3_000,
    });
  });

  it("polls gently after a failed attempt with no scheduled retry", () => {
    const result = decideNext(makeTick({ error: { code: "x", message: "y" } }), NOW);
    expect(result).toEqual({ action: "tick", delayMs: 3_000 });
  });
});
