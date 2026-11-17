import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, post } from "./api";
import { apiError, jsonResponse } from "@/test/utils";

afterEach(() => vi.unstubAllGlobals());

/** Awaits a promise that must reject and returns its ApiError. */
async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected the request to fail");
}

function stubFetch(response: Response | Error) {
  const fn = vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response;
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("api", () => {
  it("returns parsed JSON and sends credentials for the same origin", async () => {
    const fetchMock = stubFetch(jsonResponse({ id: "1" }));
    await expect(api("/api/me")).resolves.toEqual({ id: "1" });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.credentials).toBe("same-origin");
  });

  it("returns undefined for 204", async () => {
    stubFetch(new Response(null, { status: 204 }));
    await expect(post<void>("/api/auth/logout")).resolves.toBeUndefined();
  });

  it("turns the error envelope into a typed ApiError", async () => {
    stubFetch(apiError(404, "repo_not_found", "Repository not found."));
    const err = await failure(api("/api/repositories/x"));
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({
      status: 404,
      code: "repo_not_found",
      message: "Repository not found.",
      requestId: "req-1",
    });
    expect(err.needsSignIn).toBe(false);
  });

  it("flags 401 as needing sign-in", async () => {
    stubFetch(apiError(401, "reauth_required"));
    const err = await failure(api("/api/me"));
    expect(err.needsSignIn).toBe(true);
    expect(err.code).toBe("reauth_required");
  });

  it("does not leak raw server pages for non-JSON errors", async () => {
    stubFetch(new Response("<html>Bad gateway: internal host 10.0.0.5</html>", { status: 502 }));
    const err = await failure(api("/api/me"));
    expect(err.code).toBe("server_error");
    expect(err.message).not.toContain("10.0.0.5");
  });

  it("reports network failures as a friendly error", async () => {
    stubFetch(new TypeError("Failed to fetch"));
    const err = await failure(api("/api/me"));
    expect(err).toMatchObject({ status: 0, code: "network_error" });
  });

  it("posts JSON bodies with a content type", async () => {
    const fetchMock = stubFetch(jsonResponse({ ok: true }));
    await post("/api/repositories", { url: "https://github.com/o/r" });
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ url: "https://github.com/o/r" }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});
