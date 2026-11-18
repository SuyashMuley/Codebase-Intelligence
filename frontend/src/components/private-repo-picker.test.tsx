import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import { PrivateRepoPicker } from "./private-repo-picker";

const entry = (name: string) => ({
  owner: "acme",
  name,
  description: `${name} description`,
  language: "Python",
  stars: 0,
  pushed_at: null,
  fork: false,
  archived: false,
});
const MINE = {
  items: [
    { id: "r1", owner: "acme", name: "linked", url: "u", default_branch: null, created_at: "2026-01-01T00:00:00Z", is_private: true },
  ],
  total: 1,
  limit: 100,
  offset: 0,
};

const NO_PROVIDERS = {
  chat_providers: [{ id: "openai", label: "OpenAI", default_model: "gpt-4o-mini" }],
  embedding_providers: [{ id: "openai", label: "OpenAI", models: ["text-embedding-3-small"], needs_account_id: false }],
  embedding: null,
  chat: null,
};

let fetchMock: ReturnType<typeof vi.fn>;
function route(privateList: () => Response, extra: Record<string, () => Response> = {}) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    if (key === "GET /api/github/private-repos") return privateList();
    if (key === "GET /api/repositories?limit=100") return jsonResponse(MINE);
    if (key === "GET /api/providers") return jsonResponse(NO_PROVIDERS);
    const handler = extra[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler();
  });
}

beforeEach(() => {
  push.mockClear();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("PrivateRepoPicker", () => {
  it("lists private repositories with a private badge and connects one", async () => {
    route(
      () => jsonResponse({ items: [entry("secret"), entry("linked")], install_url: "https://github.com/apps/x/installations/new", retention_days: 30 }),
      {
        "POST /api/repositories": () =>
          jsonResponse({ id: "r2", owner: "acme", name: "secret", url: "u", default_branch: "main", created_at: "2026-01-01T00:00:00Z", is_private: true }, 201),
      },
    );
    renderWithClient(<PrivateRepoPicker />);
    const list = await screen.findByRole("list", { name: "Your private repositories" });
    expect(list).toHaveTextContent("acme/secret");
    expect(screen.getAllByText("private")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "linked is already connected" })).toBeDisabled();

    await userEvent.click(screen.getByLabelText(/i understand where my private code goes/i));
    await userEvent.click(screen.getByRole("button", { name: "Connect secret" }));
    const [, init] = fetchMock.mock.calls.find(([u, i]) => u === "/api/repositories" && i?.method === "POST")!;
    expect(JSON.parse(String(init.body))).toEqual({ url: "https://github.com/acme/secret" });
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith("/repos/r2"));
  });

  it("links to the app's install page when there is nothing to list", async () => {
    route(() => jsonResponse({ items: [], install_url: "https://github.com/apps/x/installations/new", retention_days: 30 }));
    renderWithClient(<PrivateRepoPicker />);
    expect(await screen.findByText(/no private repositories available/i)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /install the github app/i });
    expect(link).toHaveAttribute("href", "https://github.com/apps/x/installations/new");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("still explains what to do when the install link is not configured", async () => {
    route(() => jsonResponse({ items: [], install_url: null, retention_days: null }));
    renderWithClient(<PrivateRepoPicker />);
    expect(await screen.findByText(/install the github app on a repository from its page/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /install/i })).not.toBeInTheDocument();
  });

  it("shows the server's reason when connecting fails", async () => {
    route(() => jsonResponse({ items: [entry("secret")], install_url: null, retention_days: null }), {
      "POST /api/repositories": () => apiError(404, "repo_not_found", "Repository was not found."),
    });
    renderWithClient(<PrivateRepoPicker />);
    await userEvent.click(await screen.findByLabelText(/i understand where my private code goes/i));
    await userEvent.click(await screen.findByRole("button", { name: "Connect secret" }));
    expect(await screen.findByText("Repository was not found.")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("asks to sign in again when GitHub authorization is gone, and can retry other failures", async () => {
    route(() => apiError(401, "reauth_required", "Please sign in with GitHub."));
    renderWithClient(<PrivateRepoPicker />);
    expect(await screen.findByRole("link", { name: /sign in again/i })).toBeInTheDocument();
  });

  it("reports a failure to load and offers to retry", async () => {
    route(() => apiError(503, "github_unavailable", "Could not reach GitHub."));
    renderWithClient(<PrivateRepoPicker />);
    expect(await screen.findByText("Could not reach GitHub.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("cannot connect until the notice is acknowledged", async () => {
    route(() => jsonResponse({ items: [entry("secret")], install_url: null, retention_days: 30 }));
    renderWithClient(<PrivateRepoPicker />);
    const connect = await screen.findByRole("button", { name: "Connect secret" });
    expect(connect).toBeDisabled();
    expect(screen.getByRole("region", { name: /before you connect a private repository/i })).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/i understand where my private code goes/i));
    expect(connect).toBeEnabled();
    await userEvent.click(screen.getByLabelText(/i understand where my private code goes/i));
    expect(connect).toBeDisabled(); // un-ticking takes it back
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === "POST")).toBe(false);
  });
});
