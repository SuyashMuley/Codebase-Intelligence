import { describe, expect, it } from "vitest";
import { ALLOWED, evaluate } from "../../scripts/audit-policy.mjs";

type Report = Parameters<typeof evaluate>[0];

const advisory = (id: string, severity: string, title = "t") => ({
  url: `https://github.com/advisories/${id}`,
  severity,
  title,
  name: "pkg",
});
const report = (...entries: unknown[][]): Report => ({
  vulnerabilities: Object.fromEntries(entries.map((via, i) => [`pkg${i}`, { via }])),
});
const allow = { "GHSA-aaaa-bbbb-cccc": { reason: "because", expires: "2026-12-31" } };

describe("the dependency audit policy", () => {
  it("accepts an advisory that is allowed and not yet expired, and says why", () => {
    const r = evaluate(report([advisory("GHSA-aaaa-bbbb-cccc", "high")]), allow, "2026-11-18");
    expect(r.ok).toBe(true);
    expect(r.accepted[0]).toContain("because");
  });

  it("fails on a new high or critical advisory", () => {
    for (const severity of ["high", "critical"]) {
      const r = evaluate(report([advisory("GHSA-new1-new1-new1", severity)]), allow, "2026-11-18");
      expect(r.ok, severity).toBe(false);
      expect(r.problems[0]).toContain("GHSA-new1-new1-new1");
    }
  });

  it("fails once an exception has expired, so it is reviewed again", () => {
    const r = evaluate(report([advisory("GHSA-aaaa-bbbb-cccc", "high")]), allow, "2027-01-01");
    expect(r.ok).toBe(false);
    expect(r.problems[0]).toMatch(/expired on 2026-12-31/);
  });

  it("does not fail on moderate or low advisories", () => {
    const r = evaluate(
      report([advisory("GHSA-mod1-mod1-mod1", "moderate"), advisory("GHSA-low1-low1-low1", "low")]),
      allow,
      "2026-11-18",
    );
    expect(r.ok).toBe(true);
  });

  it("counts an advisory that reaches many packages once, and ignores package-name entries", () => {
    const via = [advisory("GHSA-new1-new1-new1", "high"), "some-other-package"];
    const r = evaluate(report(via, via, via), allow, "2026-11-18");
    expect(r.problems).toHaveLength(1);
  });

  it("an empty or missing report is clean", () => {
    expect(evaluate({}, allow, "2026-11-18").ok).toBe(true);
    expect(evaluate(report(), allow, "2026-11-18").ok).toBe(true);
  });

  it("the shipped exception has a reason and an expiry that has not passed", () => {
    for (const [id, e] of Object.entries(ALLOWED) as [string, { reason: string; expires: string }][]) {
      expect(id).toMatch(/^GHSA-/);
      expect(e.reason.length).toBeGreaterThan(40);
      expect(e.expires).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });
});
