import { describe, expect, it } from "vitest";
import nextConfig from "../../next.config";

async function headerMap(): Promise<Record<string, string>> {
  const rules = await nextConfig.headers!();
  const all = rules.find((r) => r.source === "/:path*");
  expect(all, "a rule that applies to every path").toBeDefined();
  return Object.fromEntries(all!.headers.map((h) => [h.key, h.value]));
}

describe("security response headers", () => {
  it("keeps the existing protections", async () => {
    const h = await headerMap();
    expect(h["X-Content-Type-Options"]).toBe("nosniff");
    expect(h["X-Frame-Options"]).toBe("DENY");
    expect(h["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
  });

  it("denies browser powers the app does not use", async () => {
    const policy = (await headerMap())["Permissions-Policy"];
    for (const feature of ["camera", "microphone", "geolocation", "payment", "usb"]) {
      expect(policy).toContain(`${feature}=()`);
    }
  });

  it("isolates the window from pages that open it", async () => {
    expect((await headerMap())["Cross-Origin-Opener-Policy"]).toBe("same-origin");
  });

  it("proxies only /api to the backend", async () => {
    const rewrites = (await nextConfig.rewrites!()) as { source: string }[];
    expect(rewrites.map((r) => r.source)).toEqual(["/api/:path*"]);
  });
});
