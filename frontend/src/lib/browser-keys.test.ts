import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import {
  browserKeyHeaders,
  clearBrowserKey,
  getBrowserKey,
  setBrowserKey,
} from "./browser-keys";

beforeEach(() => window.sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("browser-held keys", () => {
  it("round-trips and clears", () => {
    expect(getBrowserKey("chat")).toBeNull();
    expect(setBrowserKey("chat", "sk-abc-0123456789")).toBe(true);
    expect(getBrowserKey("chat")).toBe("sk-abc-0123456789");
    expect(getBrowserKey("embedding")).toBeNull(); // slots are separate
    clearBrowserKey("chat");
    expect(getBrowserKey("chat")).toBeNull();
  });

  it("lives in sessionStorage only, never localStorage", () => {
    setBrowserKey("embedding", "sk-emb-0123456789");
    const writes: string[] = [];
    const recorder = { setItem: (k: string) => writes.push(k), getItem: () => null, removeItem: () => {} };
    vi.spyOn(window, "localStorage", "get").mockReturnValue(recorder as unknown as Storage);
    setBrowserKey("chat", "sk-chat-0123456789");
    expect(writes).toEqual([]);
    expect(window.sessionStorage.getItem("cip.embedding-key")).toBe("sk-emb-0123456789");
    vi.restoreAllMocks();
  });

  it("attaches headers only to repository routes", () => {
    setBrowserKey("embedding", "sk-emb-0123456789");
    setBrowserKey("chat", "sk-chat-0123456789");
    expect(browserKeyHeaders("/api/repositories/r1/search")).toEqual({
      "X-Embedding-Key": "sk-emb-0123456789",
      "X-Chat-Key": "sk-chat-0123456789",
    });
    for (const path of [
      "/api/providers",
      "/api/providers/chat",
      "/api/github/repos",
      "/api/repositories", // the list and connect routes need no key
      "/api/me",
      "https://evil.example/api/repositories/r1",
    ]) {
      expect(browserKeyHeaders(path), path).toEqual({});
    }
  });

  it("sends nothing when no key is held", () => {
    expect(browserKeyHeaders("/api/repositories/r1/index")).toEqual({});
  });

  it("is added to real requests by api(), and only to repository routes", async () => {
    setBrowserKey("chat", "sk-chat-0123456789");
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await api("/api/repositories/r1/chat", { method: "POST", body: "{}" });
    await api("/api/providers");
    const sent = (i: number) =>
      (fetchMock.mock.calls[i] as unknown as [string, RequestInit])[1].headers as Record<string, string>;
    expect(sent(0)["X-Chat-Key"]).toBe("sk-chat-0123456789");
    expect(sent(1)["X-Chat-Key"]).toBeUndefined();
  });

  it("behaves as if there is no key when storage is blocked", () => {
    vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(getBrowserKey("chat")).toBeNull();
    expect(setBrowserKey("chat", "sk-abc-0123456789")).toBe(false);
    expect(browserKeyHeaders("/api/repositories/r1/x")).toEqual({});
    expect(() => clearBrowserKey("chat")).not.toThrow();
    vi.restoreAllMocks();
  });
});
