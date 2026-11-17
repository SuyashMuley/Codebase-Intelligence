import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, makeStatus, renderWithClient } from "@/test/utils";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  usePathname: () => "/repos/repo-1",
  useSearchParams: () => new URLSearchParams(nav.search),
}));
// Monaco cannot run in jsdom; the editor is replaced by a stand-in that exposes its props.
vi.mock("./code-editor", () => ({
  default: (p: { path: string; content: string; highlight: { start: number; end: number } | null }) => (
    <pre data-testid="editor" data-path={p.path} data-highlight={JSON.stringify(p.highlight)}>
      {p.content}
    </pre>
  ),
}));

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
  nav.search = "";
  nav.replace.mockClear();
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

  const INDEXED = makeStatus({
    state: "running",
    phase: "embed",
    parse: { files: 1, symbols: 2, chunks: 1, refs: 3, syntax_error_files: 0, truncated_files: 0, done: true },
  });
  const STATS = {
    snapshot: { id: "s1", commit_sha: "a".repeat(40), state: "running" },
    parsing_done: true,
    files: { stored: 1, parsed: 1, bytes: 120, by_language: { python: 1 }, skipped: {} },
    symbols: { total: 2, by_kind: { function: 2 } },
    chunks: { total: 1, average_chars: 80, largest_chars: 80 },
    refs: { imports: 1, calls: 2 },
    redactions: { spans: 0, files: 0 },
    syntax_error_files: 0,
  };
  const FILES = {
    snapshot: STATS.snapshot,
    truncated: false,
    items: [
      { path: "pkg/app.py", language: "python", size_bytes: 120, skipped_reason: null, redactions: 0, parse_status: "ok", symbol_count: 2 },
    ],
  };
  const FILE = {
    snapshot: STATS.snapshot,
    path: "pkg/app.py",
    language: "python",
    size_bytes: 120,
    skipped_reason: null,
    redactions: 0,
    parse_status: "ok",
    line_count: 3,
    content: "def run():\n    return 1\n",
    symbols: [{ id: 1, kind: "function", name: "run", qualified_name: "pkg/app.py::run", start_line: 1, end_line: 2, parent_id: null, signature: "def run():" }],
  };
  const indexedRoutes = {
    "GET /api/repositories/repo-1": () => jsonResponse(REPO),
    "GET /api/repositories/repo-1/status": () => jsonResponse(INDEXED),
    "GET /api/repositories/repo-1/stats": () => jsonResponse(STATS),
    "GET /api/repositories/repo-1/files": () => jsonResponse(FILES),
    "GET /api/repositories/repo-1/files/pkg/app.py": () => jsonResponse(FILE),
  };

  it("shows what was indexed on the overview", async () => {
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    expect(await screen.findByRole("heading", { name: "What was indexed" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("id", "panel-overview");
  });

  it("switches tabs by changing the URL, not just local state", async () => {
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    await userEvent.click(await screen.findByRole("tab", { name: "Files" }));
    expect(nav.replace).toHaveBeenCalledWith("/repos/repo-1?tab=files", { scroll: false });
  });

  it("opens the files tab for ?tab=files and lists the files", async () => {
    nav.search = "tab=files";
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    expect(await screen.findByRole("button", { name: /app\.py/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Select a file to view it.")).toBeInTheDocument();
  });

  it("a shared link to a file and lines opens it and highlights those lines", async () => {
    nav.search = "file=pkg%2Fapp.py&line=1-2";
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    const editor = await screen.findByTestId("editor");
    expect(editor).toHaveAttribute("data-path", "pkg/app.py");
    expect(editor).toHaveAttribute("data-highlight", JSON.stringify({ start: 1, end: 2 }));
    expect(editor).toHaveTextContent("def run():");
    expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/lines 1–2 highlighted/i)).toBeInTheDocument();
  });

  it("selecting a file and a symbol writes both into the URL", async () => {
    nav.search = "tab=files";
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /app\.py/ }));
    expect(nav.replace).toHaveBeenLastCalledWith(
      "/repos/repo-1?tab=files&file=pkg%2Fapp.py",
      { scroll: false },
    );
  });

  it("clicking a symbol in the outline highlights its lines", async () => {
    nav.search = "file=pkg%2Fapp.py";
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    const outline = await screen.findByRole("list", { name: "Symbols in this file" });
    await userEvent.click(within(outline).getByRole("button", { name: /run/ }));
    expect(nav.replace).toHaveBeenLastCalledWith(
      "/repos/repo-1?file=pkg%2Fapp.py&line=1-2",
      { scroll: false },
    );
  });

  it("ignores a malformed line parameter instead of breaking", async () => {
    nav.search = "file=pkg%2Fapp.py&line=abc";
    route(indexedRoutes);
    renderWithClient(<RepoDetail id="repo-1" />);
    const editor = await screen.findByTestId("editor");
    expect(editor).toHaveAttribute("data-highlight", "null");
  });

  it("the files tab says to index first when nothing is indexed", async () => {
    nav.search = "tab=files";
    route({
      "GET /api/repositories/repo-1": () => jsonResponse(REPO),
      "GET /api/repositories/repo-1/status": () =>
        jsonResponse(makeStatus({ state: "not_indexed", snapshot_id: null, commit_sha: null, phase: null })),
    });
    renderWithClient(<RepoDetail id="repo-1" />);
    expect(await screen.findByText("Nothing to browse yet")).toBeInTheDocument();
  });
});
