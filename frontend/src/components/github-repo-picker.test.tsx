import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import { GithubRepoPicker } from "./github-repo-picker";

function gh(name: string, over: Record<string, unknown> = {}) {
  return {
    owner: "alice",
    name,
    description: `${name} description`,
    language: "Python",
    stars: 5,
    pushed_at: "2026-01-02T00:00:00Z",
    fork: false,
    archived: false,
    ...over,
  };
}

const CONNECTED = {
  items: [{ id: "r1", owner: "alice", name: "already", url: "u", default_branch: null, created_at: "2026-01-01T00:00:00Z" }],
  total: 1,
  limit: 100,
  offset: 0,
};

let fetchMock: ReturnType<typeof vi.fn>;
type Handler = (init?: RequestInit) => Response;

function route(github: Handler, extra: Record<string, Handler> = {}) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    if (key === "GET /api/github/repos") return github(init);
    if (key === "GET /api/repositories?limit=100") return jsonResponse(CONNECTED);
    const handler = extra[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler(init);
  });
}

beforeEach(() => {
  push.mockClear();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const list = (items: unknown[]) => () => jsonResponse({ items });

describe("GithubRepoPicker", () => {
  it("lists your public repositories with their details", async () => {
    route(list([gh("alpha", { stars: 12 }), gh("beta", { language: null, stars: 0 })]));
    renderWithClient(<GithubRepoPicker />);
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("alice/alpha");
    expect(items[0]).toHaveTextContent("alpha description");
    expect(items[0]).toHaveTextContent("Python · ★ 12");
    expect(items[1]).not.toHaveTextContent("★");
  });

  it("shows a loading state first", () => {
    route(() => new Response(null));
    fetchMock.mockImplementation(() => new Promise(() => {}));
    renderWithClient(<GithubRepoPicker />);
    expect(screen.getByRole("status")).toHaveTextContent(/loading your github repositories/i);
  });

  it("hides forks by default and can show them", async () => {
    route(list([gh("mine"), gh("forked", { fork: true })]));
    renderWithClient(<GithubRepoPicker />);
    await screen.findByText("alice/mine");
    expect(screen.queryByText("alice/forked")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/hide forks/i));
    const forked = (await screen.findByText("alice/forked")).closest("li");
    expect(forked).toHaveTextContent("fork");
  });

  it("filters by name, description or language, and searching also finds forks", async () => {
    route(list([gh("api-server", { language: "Go" }), gh("website", { language: "TypeScript" }), gh("old-fork", { fork: true, language: "Go" })]));
    renderWithClient(<GithubRepoPicker />);
    await screen.findByText("alice/api-server");
    await userEvent.type(screen.getByLabelText(/filter your github repositories/i), "go");
    expect(screen.getByText("alice/api-server")).toBeInTheDocument();
    expect(screen.queryByText("alice/website")).not.toBeInTheDocument();
    expect(screen.getByText("alice/old-fork")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText(/filter your github repositories/i));
    await userEvent.type(screen.getByLabelText(/filter your github repositories/i), "zzz");
    expect(screen.getByText("Nothing matches")).toBeInTheDocument();
  });

  it("marks already-connected repositories and disables them", async () => {
    route(list([gh("already"), gh("fresh")]));
    renderWithClient(<GithubRepoPicker />);
    const done = await screen.findByRole("button", { name: /already is already connected/i });
    expect(done).toBeDisabled();
    expect(done).toHaveTextContent("Connected");
    expect(screen.getByRole("button", { name: "Connect fresh" })).toBeEnabled();
  });

  it("connects a repository and opens it", async () => {
    route(list([gh("fresh")]), {
      "POST /api/repositories": (init) => {
        expect(init?.body).toBe(JSON.stringify({ url: "https://github.com/alice/fresh" }));
        return jsonResponse({ id: "new-id", owner: "alice", name: "fresh", url: "u", default_branch: null, created_at: "2026-01-01T00:00:00Z" }, 201);
      },
    });
    renderWithClient(<GithubRepoPicker />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect fresh" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/repos/new-id"));
  });

  it("explains why a connect failed", async () => {
    route(list([gh("fresh")]), {
      "POST /api/repositories": () => apiError(400, "invalid_github_url", "Not a valid GitHub repository URL."),
    });
    renderWithClient(<GithubRepoPicker />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect fresh" }));
    expect(await screen.findByText(/could not connect that repository/i)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("pages a long list", async () => {
    route(list(Array.from({ length: 45 }, (_, i) => gh(`repo-${String(i).padStart(2, "0")}`))));
    renderWithClient(<GithubRepoPicker />);
    expect(await screen.findAllByRole("listitem")).toHaveLength(30);
    await userEvent.click(screen.getByRole("button", { name: /show more \(15 left\)/i }));
    expect(screen.getAllByRole("listitem")).toHaveLength(45);
    expect(screen.queryByRole("button", { name: /show more/i })).not.toBeInTheDocument();
  });

  it("has a friendly empty state", async () => {
    route(list([]));
    renderWithClient(<GithubRepoPicker />);
    expect(await screen.findByText(/no public repositories on your github account/i)).toBeInTheDocument();
    expect(screen.getByText(/pasting its url/i)).toBeInTheDocument();
  });

  it("shows an error with a retry", async () => {
    let calls = 0;
    route(() => {
      calls += 1;
      return calls === 1 ? apiError(503, "github_rate_limited", "GitHub API rate limit reached.") : jsonResponse({ items: [gh("back")] });
    });
    renderWithClient(<GithubRepoPicker />);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/rate limit reached/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText("alice/back")).toBeInTheDocument();
  });

  it("offers to sign in again when GitHub no longer accepts the authorization", async () => {
    route(() => apiError(401, "reauth_required", "Please sign in with GitHub."));
    renderWithClient(<GithubRepoPicker />);
    const link = await screen.findByRole("link", { name: /sign in again/i });
    expect(link).toHaveAttribute("href", "/api/auth/github/login?next=%2Frepos");
  });
});
