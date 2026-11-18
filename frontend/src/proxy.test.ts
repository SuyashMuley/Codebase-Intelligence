// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { config, proxy } from "./proxy";

afterEach(() => vi.unstubAllEnvs());

const req = (url = "http://localhost:3000/repos", headers: Record<string, string> = {}) =>
  new NextRequest(url, { headers });

function nonceOf(policy: string | null): string {
  return /'nonce-([^']+)'/.exec(policy ?? "")![1];
}

describe("the proxy", () => {
  it("sends a policy whose nonce is forwarded to the app for this request only", () => {
    const res = proxy(req());
    const policy = res.headers.get("Content-Security-Policy");
    expect(policy).toContain("script-src 'self' 'nonce-");
    // The forwarded request headers carry the same nonce and policy (how Next finds the nonce).
    const forwarded = res.headers.get("x-middleware-request-content-security-policy");
    expect(forwarded).toBe(policy);
    expect(res.headers.get("x-middleware-request-x-nonce")).toBe(nonceOf(policy));
    // the nonce is a request detail, not something to hand back to the browser as a header
    expect(res.headers.get("x-nonce")).toBeNull();
  });

  it("uses a different nonce for every request", () => {
    const nonces = new Set(Array.from({ length: 50 }, () => nonceOf(proxy(req()).headers.get("Content-Security-Policy"))));
    expect(nonces.size).toBe(50);
  });

  it("upgrades insecure requests only when the request was https, including behind a proxy", () => {
    const up = (r: NextRequest) => proxy(r).headers.get("Content-Security-Policy")!.includes("upgrade-insecure-requests");
    vi.stubEnv("NODE_ENV", "production");
    expect(up(req("http://localhost:3000/repos"))).toBe(false);
    expect(up(req("https://app.example/repos"))).toBe(true);
    expect(up(req("http://internal/repos", { "x-forwarded-proto": "https" }))).toBe(true);
  });

  it("report-only mode reports without blocking, and still gives Next the nonce", () => {
    vi.stubEnv("CSP_MODE", "report-only");
    const res = proxy(req());
    expect(res.headers.get("Content-Security-Policy")).toBeNull();
    const reported = res.headers.get("Content-Security-Policy-Report-Only");
    expect(reported).toContain("'nonce-");
    expect(res.headers.get("x-middleware-request-content-security-policy")).toBe(reported);
  });

  it("can be switched off, and an unknown value does not switch it off", () => {
    vi.stubEnv("CSP_MODE", "off");
    expect(proxy(req()).headers.get("Content-Security-Policy")).toBeNull();
    vi.stubEnv("CSP_MODE", "no");
    expect(proxy(req()).headers.get("Content-Security-Policy")).not.toBeNull();
  });

  it("applies to pages, not to the API, build assets or prefetches", () => {
    const rule = config.matcher[0] as { source: string; missing: { key: string }[] };
    const re = new RegExp(`^${rule.source}$`);
    for (const page of ["/", "/login", "/repos", "/repos/abc", "/settings/providers"]) {
      expect(re.test(page), page).toBe(true);
    }
    for (const other of ["/api/repositories", "/api/me", "/_next/static/chunks/a.js", "/_next/image", "/favicon.ico"]) {
      expect(re.test(other), other).toBe(false);
    }
    expect(rule.missing.map((m) => m.key)).toEqual(["next-router-prefetch", "purpose"]);
  });
});
