import { describe, expect, it } from "vitest";
import { buildCsp, cspMode, newNonce } from "./csp";

const prod = (nonce = "abc123==") => buildCsp({ nonce, isDev: false, secure: true });
const directive = (policy: string, name: string) =>
  policy
    .split("; ")
    .find((d) => d.startsWith(`${name} `))
    ?.slice(name.length + 1)
    .split(" ") ?? [];

describe("the Content-Security-Policy", () => {
  it("lets only nonce-carrying scripts run: no unsafe-inline, no eval, no host list", () => {
    const script = directive(prod("N0nce=="), "script-src");
    expect(script).toEqual(["'self'", "'nonce-N0nce=='", "'strict-dynamic'"]);
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
    expect(script.join(" ")).not.toMatch(/https?:|\*/);
  });

  it("allows nothing the app does not use", () => {
    const p = prod();
    expect(directive(p, "default-src")).toEqual(["'self'"]);
    expect(directive(p, "object-src")).toEqual(["'none'"]);
    expect(directive(p, "frame-ancestors")).toEqual(["'none'"]);
    expect(directive(p, "base-uri")).toEqual(["'self'"]);
    expect(directive(p, "form-action")).toEqual(["'self'"]);
    expect(directive(p, "connect-src")).toEqual(["'self'"]); // the API is proxied through this origin
    expect(p).not.toMatch(/\*(?!:)/); // no wildcard source anywhere (the dev-only port wildcard aside)
  });

  it("allows only GitHub's avatar host as a remote image source", () => {
    const img = directive(prod(), "img-src");
    expect(img.filter((s) => s.startsWith("https:"))).toEqual(["https://avatars.githubusercontent.com"]);
  });

  it("makes room for Monaco's worker, and says where the one deliberate hole is", () => {
    expect(directive(prod(), "worker-src")).toEqual(["'self'", "blob:"]);
    // style-src is the single place 'unsafe-inline' is accepted (run-time <style> from libraries)
    const withInline = prod()
      .split("; ")
      .filter((d) => d.includes("'unsafe-inline'"))
      .map((d) => d.split(" ")[0]);
    expect(withInline).toEqual(["style-src"]);
  });

  it("production gets no eval and no websocket; development gets both, and only there", () => {
    const dev = buildCsp({ nonce: "n", isDev: true, secure: false });
    expect(directive(dev, "script-src")).toContain("'unsafe-eval'");
    expect(directive(dev, "connect-src").some((s) => s.startsWith("ws://"))).toBe(true);
    const p = prod();
    expect(p).not.toContain("unsafe-eval");
    expect(p).not.toContain("ws://");
  });

  it("upgrades insecure requests only for https requests in production", () => {
    expect(prod()).toContain("upgrade-insecure-requests");
    expect(buildCsp({ nonce: "n", isDev: false, secure: false })).not.toContain("upgrade-insecure-requests");
    expect(buildCsp({ nonce: "n", isDev: true, secure: true })).not.toContain("upgrade-insecure-requests");
  });
});

describe("nonces", () => {
  it("are unpredictable, unique and safe to put in a header", () => {
    const seen = new Set(Array.from({ length: 200 }, () => newNonce()));
    expect(seen.size).toBe(200);
    for (const n of seen) expect(n).toMatch(/^[A-Za-z0-9+/]{22}==$/); // 16 random bytes
  });
});

describe("the CSP_MODE switch", () => {
  it("defaults to enforcing, and only the two known alternatives change it", () => {
    expect(cspMode(undefined)).toBe("enforce");
    expect(cspMode("")).toBe("enforce");
    expect(cspMode("enforce")).toBe("enforce");
    expect(cspMode("report-only")).toBe("report-only");
    expect(cspMode("off")).toBe("off");
    expect(cspMode("OFF")).toBe("enforce"); // not an accidental way to switch it off
    expect(cspMode("false")).toBe("enforce");
  });
});
