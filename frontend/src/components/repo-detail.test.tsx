import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, makeStatus, renderWithClient } from "@/test/utils";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import { RepoDetail } from "./repo-detail";

const REPO = {
  id: "repo-1",
  owner: "octocat",
  name: "Hello-World",
  url: "https://github.com/octocat/Hello-World",
  default_branch: "master",
  created_at: "2026-01-01T00:00:00Z",
};

let fetchMock: ReturnType<typeof vi.fn>;

function route(handlers: Record<string, () => Response>) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    const handler = handlers[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler();
  });
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("RepoDetail", () => {
  it("shows the repository and its not-indexed state", async () => {
    route({
      "GET /api/repositories/repo-1": () => jsonResponse(REPO),
      "GET /api/repositories/repo-1/status": () =>
        jsonResponse(makeStatus({ state: "not_indexed", snapshot_id: null, commit_sha: null, phase: null })),
    });
    renderWithClient(<RepoDetail id="repo-1" />);
    expect(await screen.findByRole("heading", { name: "octocat/Hello-World" })).toBeInTheDocument();
    expect(await screen.findByText(/not indexed yet/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view on github/i })).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("says so, with a way back, when the repository is not in the account", async () => {
    route({
      "GET /api/repositories/repo-1": () => apiError(404, "repo_not_found", "not found"),
      "GET /api/repositories/repo-1/status": () => apiError(404, "repo_not_found", "not found"),
    });
    renderWithClient(<RepoDetail id="repo-1" />);
    expect(await screen.findByText(/repository not found/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to your repositories/i })).toHaveAttribute("href", "/repos");
  });

  it("shows the server's reason when indexing cannot start", async () => {
    route({
      "GET /api/repositories/repo-1": () => jsonResponse(REPO),
      "GET /api/repositories/repo-1/status": () =>
        jsonResponse(makeStatus({ state: "not_indexed", snapshot_id: null, commit_sha: null, phase: null })),
      "POST /api/repositories/repo-1/index": () =>
        apiError(429, "quota_exceeded", "Daily indexing quota reached (10 new snapshots per 24 hours)."),
    });
    renderWithClient(<RepoDetail id="repo-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /index latest commit/i }));
    expect(await screen.findByText(/daily indexing quota reached/i)).toBeInTheDocument();
  });

  it("offers to sign in again when GitHub no longer honors the authorization", async () => {
    route({
      "GET /api/repositories/repo-1": () => jsonResponse(REPO),
      "GET /api/repositories/repo-1/status": () =>
        jsonResponse(makeStatus({ state: "not_indexed", snapshot_id: null, commit_sha: null, phase: null })),
      "POST /api/repositories/repo-1/index": () =>
        apiError(401, "reauth_required", "Please sign in with GitHub."),
    });
    renderWithClient(<RepoDetail id="repo-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /index latest commit/i }));
    expect(await screen.findByText(/your github authorization expired/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in again/i })).toHaveAttribute(
      "href",
      "/api/auth/github/login?next=%2Frepos%2Frepo-1",
    );
    // The generic "could not start" notice is not shown on top of it.
    expect(screen.queryByText(/could not start indexing/i)).not.toBeInTheDocument();
  });
});
