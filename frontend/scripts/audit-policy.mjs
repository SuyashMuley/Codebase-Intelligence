// The rule for `npm audit` on the whole dependency tree (development tools included).
//
// Production dependencies are held to a stricter, simpler rule in CI (`npm audit --omit=dev`
// must be clean). For the development tools, a HIGH or CRITICAL advisory fails the build unless it
// is listed below with a reason and an expiry date. An exception that has expired fails the build
// too, so it has to be looked at again instead of being forgotten.

/** Advisories accepted for now. Add one only with a real reason, and a date to look again. */
export const ALLOWED = {
  "GHSA-fc5q-39xc-vjpf": {
    reason:
      "picomatch ReDoS (<=4.0.7). No patched release exists: 4.0.7 is the latest. It is used only " +
      "by the test runner and the linter on our own files in development and CI, never by " +
      "the deployed app (`npm audit --omit=dev` is clean) and never on user input.",
    expires: "2026-12-31",
  },
};

const FAILING = new Set(["high", "critical"]);

/**
 * @param {{vulnerabilities?: Record<string, {via: unknown[]}>}} report parsed `npm audit --json`
 * @param {Record<string, {reason: string, expires: string}>} allowed
 * @param {string} today an ISO date (YYYY-MM-DD)
 * @returns {{ok: boolean, problems: string[], accepted: string[]}}
 */
export function evaluate(report, allowed, today) {
  const advisories = new Map(); // advisory id -> {severity, title}
  for (const entry of Object.values(report.vulnerabilities ?? {})) {
    for (const via of entry.via ?? []) {
      if (typeof via !== "object" || via === null || !("url" in via)) continue;
      const id = String(via.url).split("/").pop() ?? String(via.url);
      advisories.set(id, { severity: String(via.severity), title: String(via.title) });
    }
  }
  const problems = [];
  const accepted = [];
  for (const [id, { severity, title }] of advisories) {
    if (!FAILING.has(severity)) continue;
    const exception = allowed[id];
    if (!exception) {
      problems.push(`${severity.toUpperCase()} ${id}: ${title}`);
    } else if (exception.expires < today) {
      problems.push(`exception for ${id} expired on ${exception.expires}: review it again`);
    } else {
      accepted.push(`${id} (until ${exception.expires}): ${exception.reason}`);
    }
  }
  return { ok: problems.length === 0, problems, accepted };
}
