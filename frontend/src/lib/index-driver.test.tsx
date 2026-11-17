import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, makeTick, renderWithClient } from "@/test/utils";
import { useIndexDriver } from "./hooks";

function Harness({ active = true }: { active?: boolean }) {
  const { stopped, lastError } = useIndexDriver("repo-1", active);
  return (
    <div>
      <span data-testid="stopped">{stopped ?? "running"}</span>
      <span data-testid="error">{lastError?.code ?? "none"}</span>
    </div>
  );
}

let fetchMock: ReturnType<typeof vi.fn>;
const ticks = () =>
  fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/index/tick")).length;

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useIndexDriver", () => {
  it("keeps ticking until a phase has no handler, then stops", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(makeTick({ phase: "fetch" })))
      .mockResolvedValueOnce(jsonResponse(makeTick({ phase: "parse" })))
      .mockResolvedValueOnce(
        jsonResponse(makeTick({ phase: "parse" }, false, "phase_not_implemented:parse")),
      );
    renderWithClient(<Harness />);
    await advance(0);
    expect(ticks()).toBe(1);
    await advance(300);
    await advance(300);
    expect(ticks()).toBe(3);
    expect(screen.getByTestId("stopped")).toHaveTextContent("not_implemented");
    await advance(60_000);
    expect(ticks()).toBe(3); // no polling once stopped
  });

  it("stops when the job is ready", async () => {
    fetchMock.mockResolvedValue(jsonResponse(makeTick({ state: "ready", phase: "done" })));
    renderWithClient(<Harness />);
    await advance(0);
    expect(screen.getByTestId("stopped")).toHaveTextContent("ready");
    await advance(10_000);
    expect(ticks()).toBe(1);
  });

  it("waits out a server-side backoff before ticking again", async () => {
    const retryAt = new Date(Date.now() + 10_000).toISOString();
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(
          makeTick({ retry_at: retryAt, attempts: 1, error: { code: "github_unavailable", message: "x" } }),
        ),
      )
      .mockResolvedValue(jsonResponse(makeTick({ state: "ready", phase: "done" })));
    renderWithClient(<Harness />);
    await advance(0);
    expect(ticks()).toBe(1);
    await advance(9_000);
    expect(ticks()).toBe(1); // still backing off: do not hammer the API
    await advance(2_000);
    expect(ticks()).toBe(2);
  });

  it("does not tick when inactive", async () => {
    renderWithClient(<Harness active={false} />);
    await advance(10_000);
    expect(ticks()).toBe(0);
  });

  it("stops and asks the app to re-check the session on 401", async () => {
    fetchMock.mockResolvedValue(apiError(401, "reauth_required"));
    const { client } = renderWithClient(<Harness />);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await advance(0);
    expect(screen.getByTestId("stopped")).toHaveTextContent("error");
    expect(screen.getByTestId("error")).toHaveTextContent("reauth_required");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["me"] });
    await advance(60_000);
    expect(ticks()).toBe(1);
  });

  it("stops when there is no job to run", async () => {
    fetchMock.mockResolvedValue(apiError(409, "no_active_job"));
    renderWithClient(<Harness />);
    await advance(0);
    expect(screen.getByTestId("stopped")).toHaveTextContent("no_job");
    expect(ticks()).toBe(1);
  });

  it("backs off on repeated network errors and gives up after five", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    renderWithClient(<Harness />);
    await advance(0);
    expect(ticks()).toBe(1);
    await advance(4_000);
    expect(ticks()).toBe(2); // 2s * 2^1
    await advance(8_000);
    expect(ticks()).toBe(3);
    await advance(16_000);
    expect(ticks()).toBe(4);
    await advance(30_000);
    expect(ticks()).toBe(5);
    expect(screen.getByTestId("stopped")).toHaveTextContent("error");
    expect(screen.getByTestId("error")).toHaveTextContent("network_error");
    await advance(120_000);
    expect(ticks()).toBe(5);
  });

  it("recovers from a transient error and resets the error counter", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue(jsonResponse(makeTick({ state: "ready", phase: "done" })));
    renderWithClient(<Harness />);
    await advance(0);
    expect(screen.getByTestId("error")).toHaveTextContent("network_error");
    await advance(4_000);
    expect(screen.getByTestId("stopped")).toHaveTextContent("ready");
    expect(screen.getByTestId("error")).toHaveTextContent("none");
  });

  it("stops ticking when the page unmounts", async () => {
    fetchMock.mockResolvedValue(jsonResponse(makeTick({ phase: "parse" })));
    const { unmount } = renderWithClient(<Harness />);
    await advance(0);
    const before = ticks();
    unmount();
    await advance(30_000);
    expect(ticks()).toBe(before);
  });

  it("refreshes the statistics and file list when a phase finishes and when it stops", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(makeTick({ phase: "fetch" })))
      .mockResolvedValueOnce(jsonResponse(makeTick({ phase: "fetch" })))
      .mockResolvedValueOnce(jsonResponse(makeTick({ phase: "parse" })))
      .mockResolvedValueOnce(
        jsonResponse(makeTick({ phase: "embed" }, false, "phase_not_implemented:embed")),
      );
    const { client } = renderWithClient(<Harness />);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const refreshed = () =>
      invalidate.mock.calls.filter(([f]) => (f as { queryKey: string[] }).queryKey[0] === "stats").length;

    await advance(0);
    expect(refreshed()).toBe(1); // the first tick reports a phase
    await advance(300);
    expect(refreshed()).toBe(1); // same phase again: no extra refresh
    await advance(300);
    expect(refreshed()).toBe(2); // fetch -> parse
    await advance(300);
    expect(refreshed()).toBe(3); // parse -> embed, and the driver stops
    const keysTouched = invalidate.mock.calls.map(([f]) => (f as { queryKey: string[] }).queryKey.join("/"));
    expect(keysTouched).toContain("stats/repo-1");
    expect(keysTouched).toContain("files/repo-1");
  });
});
