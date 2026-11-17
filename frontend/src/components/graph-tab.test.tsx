import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphData, GraphNodeData } from "@/lib/types";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";
import { GraphTab } from "./graph-tab";

// React Flow measures the DOM, which jsdom cannot do. The canvas is replaced by buttons so the
// tab's own behaviour (what it asks for, what selecting does) is what is tested.
vi.mock("./graph-canvas", () => ({
  GraphCanvas: ({
    graph,
    selectedId,
    onSelect,
  }: {
    graph: GraphData;
    selectedId: string | null;
    onSelect: (n: GraphNodeData) => void;
  }) => (
    <div data-testid="canvas" data-selected={selectedId ?? ""}>
      {graph.nodes.map((n) => (
        <button key={n.id} onClick={() => onSelect(n)}>
          node {n.label}
        </button>
      ))}
    </div>
  ),
  GraphLegend: () => <p>legend</p>,
}));

const node = (over: Partial<GraphNodeData>): GraphNodeData => ({
  id: "file:1",
  kind: "file",
  label: "a.py",
  path: "a.py",
  language: "python",
  symbol_kind: null,
  start_line: null,
  in_degree: 0,
  out_degree: 0,
  focus: false,
  ...over,
});

const snapshot = { id: "s", commit_sha: "abc", state: "ready" };
const fileGraph: GraphData = {
  snapshot,
  level: "file",
  nodes: [node({ id: "file:1", label: "a.py", path: "a.py" }), node({ id: "file:2", label: "b.py", path: "b.py" })],
  edges: [{ source: "file:1", target: "file:2", kind: "imports", resolution: "static", count: 1, line: 1, reason: null }],
  truncated: false,
  total_nodes: 2,
  note: null,
};
const symbolGraph: GraphData = {
  ...fileGraph,
  level: "symbol",
  nodes: [
    node({ id: "sym:7", kind: "symbol", label: "run", path: "a.py" }),
    node({ id: "ext:os.getcwd", kind: "external", label: "os.getcwd", path: null }),
  ],
  edges: [],
};
const fileList = {
  snapshot,
  truncated: false,
  items: [
    { path: "src/deep/builder.py", language: "python", size_bytes: 1, skipped_reason: null, redactions: 0, parse_status: "ok", symbol_count: 3 },
    { path: "src/other.py", language: "python", size_bytes: 1, skipped_reason: null, redactions: 0, parse_status: "ok", symbol_count: 1 },
    { path: "src/builder.bin", language: null, size_bytes: 1, skipped_reason: "binary", redactions: 0, parse_status: null, symbol_count: 0 },
  ],
};
const impact = {
  snapshot,
  target: { type: "file", path: "a.py", symbol: null, symbol_id: null },
  max_depth: 3,
  dependents: [],
  direct: 0,
  transitive: 0,
  files_affected: 0,
  truncated: false,
  unverified_callers: [],
  suggested_tests: [],
  limitations: [],
};

afterEach(() => vi.unstubAllGlobals());

function route(handlers: Record<string, () => Response>) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.startsWith("/api/repositories/r1/files")) return jsonResponse(fileList);
    for (const [prefix, make] of Object.entries(handlers)) if (url.startsWith(prefix)) return make();
    throw new Error(`unexpected request ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function setup(over: Partial<Parameters<typeof GraphTab>[0]> = {}) {
  const onChange = vi.fn();
  const onOpenInFiles = vi.fn();
  renderWithClient(
    <GraphTab
      repoId="r1"
      hasSnapshot
      ready
      file={null}
      view="files"
      symbol={null}
      onChange={onChange}
      onOpenInFiles={onOpenInFiles}
      {...over}
    />,
  );
  return { onChange, onOpenInFiles };
}

describe("GraphTab", () => {
  it("says to index first without a snapshot, and waits while indexing is unfinished", () => {
    setup({ hasSnapshot: false });
    expect(screen.getByText(/nothing to graph yet/i)).toBeInTheDocument();
    setup({ ready: false });
    expect(screen.getByText(/built when indexing finishes/i)).toBeInTheDocument();
  });

  it("loads the whole-repository file graph first", async () => {
    const fetchMock = route({ "/api/repositories/r1/graph": () => jsonResponse(fileGraph) });
    setup();
    expect(await screen.findByRole("button", { name: "node a.py" })).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls).toContain("/api/repositories/r1/graph?level=file&depth=1");
    expect(screen.getByText(/select a file in the graph/i)).toBeInTheDocument();
  });

  it("selecting a file node reports it", async () => {
    route({ "/api/repositories/r1/graph": () => jsonResponse(fileGraph) });
    const { onChange } = setup();
    await userEvent.click(await screen.findByRole("button", { name: "node b.py" }));
    expect(onChange).toHaveBeenCalledWith({ file: "b.py", sym: null });
  });

  it("shows the impact of the selected file and marks it in the graph", async () => {
    const fetchMock = route({
      "/api/repositories/r1/graph": () => jsonResponse(fileGraph),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    setup({ file: "a.py" });
    await screen.findByRole("button", { name: "node a.py" });
    expect(screen.getByTestId("canvas")).toHaveAttribute("data-selected", "file:1");
    await screen.findByText(/If you change this/);
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("impact?depth=3&file=a.py"))).toBe(true);
  });

  it("shows just the neighbourhood of the selected file by default, and everything on request", async () => {
    const fetchMock = route({
      "/api/repositories/r1/graph": () => jsonResponse(fileGraph),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    setup({ file: "a.py" });
    await screen.findByRole("button", { name: "node a.py" });
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("graph?level=file&depth=1&path=a.py"))).toBe(true);
    const box = screen.getByRole("checkbox", { name: /only around the selected file/i });
    expect(box).toBeChecked();
    await userEvent.click(box);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u) === "/api/repositories/r1/graph?level=file&depth=1"),
      ).toBe(true),
    );
  });

  it("hides unresolved calls in the symbol view until asked, and says how many", async () => {
    const withUnresolved: GraphData = {
      ...symbolGraph,
      edges: [
        { source: "sym:7", target: "ext:os.getcwd", kind: "calls", resolution: "unresolved", count: 1, line: 3, reason: "not_found" },
      ],
    };
    route({
      "/api/repositories/r1/graph": () => jsonResponse(withUnresolved),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    setup({ file: "a.py", view: "symbols" });
    await screen.findByRole("button", { name: "node run" });
    expect(screen.queryByRole("button", { name: "node os.getcwd" })).not.toBeInTheDocument();
    const toggle = screen.getByRole("checkbox", { name: /show 1 unresolved call/i });
    await userEvent.click(toggle);
    expect(await screen.findByRole("button", { name: "node os.getcwd" })).toBeInTheDocument();
  });

  it("disables the symbol view until a file is selected", () => {
    route({ "/api/repositories/r1/graph": () => jsonResponse(fileGraph) });
    setup();
    expect(screen.getByRole("button", { name: "Symbols" })).toBeDisabled();
  });

  it("switches to the symbol view and asks for that file's symbols", async () => {
    route({
      "/api/repositories/r1/graph": () => jsonResponse(symbolGraph),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    const { onChange } = setup({ file: "a.py", view: "symbols" });
    await screen.findByRole("button", { name: "node run" });
    await userEvent.click(screen.getByRole("button", { name: "Files" }));
    expect(onChange).toHaveBeenCalledWith({ view: "files", sym: null });
  });

  it("selecting a symbol asks for the symbol's impact, and unresolved nodes cannot be selected", async () => {
    const fetchMock = route({
      "/api/repositories/r1/graph": () => jsonResponse({ ...symbolGraph, edges: [{ source: "sym:7", target: "ext:os.getcwd", kind: "calls", resolution: "unresolved", count: 1, line: 3, reason: "not_found" }] }),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    const { onChange } = setup({ file: "a.py", view: "symbols" });
    await userEvent.click(await screen.findByRole("button", { name: "node run" }));
    expect(onChange).toHaveBeenCalledWith({ file: "a.py", sym: 7 });
    onChange.mockClear();
    await userEvent.click(screen.getByRole("checkbox", { name: /show 1 unresolved call/i }));
    await userEvent.click(await screen.findByRole("button", { name: "node os.getcwd" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("graph?level=symbol&depth=1&path=a.py"))).toBe(true);
  });

  it("uses the symbol id for impact when one is selected", async () => {
    const fetchMock = route({
      "/api/repositories/r1/graph": () => jsonResponse(symbolGraph),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    setup({ file: "a.py", view: "symbols", symbol: 7 });
    await screen.findByText(/If you change this/);
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("impact?depth=3&symbol=7"))).toBe(true);
    expect(screen.getByTestId("canvas")).toHaveAttribute("data-selected", "sym:7");
  });

  it("offers the same nodes as a keyboard-friendly list", async () => {
    route({ "/api/repositories/r1/graph": () => jsonResponse(symbolGraph) });
    const { onChange } = setup({ file: "a.py", view: "symbols" });
    await screen.findByRole("button", { name: "node run" });
    const list = screen.getByLabelText("Nodes in this graph");
    expect(within(list).queryByText(/os\.getcwd/)).not.toBeInTheDocument(); // not selectable
    await userEvent.click(within(list).getByRole("button", { name: /a\.py › run/ }));
    expect(onChange).toHaveBeenCalledWith({ file: "a.py", sym: 7 });
  });

  it("says when the graph was cut to stay readable", async () => {
    route({
      "/api/repositories/r1/graph": () => jsonResponse({ ...fileGraph, truncated: true, total_nodes: 900 }),
    });
    setup();
    expect(await screen.findByRole("note")).toHaveTextContent(/2 most connected of 900 files/i);
  });

  it("shows an empty graph honestly", async () => {
    route({
      "/api/repositories/r1/graph": () =>
        jsonResponse({ ...fileGraph, nodes: [], edges: [], note: "3 files with no static links are not drawn." }),
    });
    setup();
    expect(await screen.findByText("No links found")).toBeInTheDocument();
    expect(screen.getByText(/3 files with no static links/)).toBeInTheDocument();
  });

  it("shows an error with a retry", async () => {
    route({ "/api/repositories/r1/graph": () => apiError(500, "internal_error", "boom") });
    setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("can open the selected file in the Files tab", async () => {
    route({
      "/api/repositories/r1/graph": () => jsonResponse(fileGraph),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    const { onOpenInFiles } = setup({ file: "a.py" });
    await userEvent.click(await screen.findByRole("button", { name: "Open in Files" }));
    expect(onOpenInFiles).toHaveBeenCalledWith("a.py");
  });

  it("finds any file by name, even one the overview does not draw", async () => {
    route({ "/api/repositories/r1/graph": () => jsonResponse(fileGraph) });
    const { onChange } = setup();
    await screen.findByRole("button", { name: "node a.py" });
    await userEvent.type(screen.getByRole("searchbox", { name: /find a file to graph/i }), "BUILD");
    const matches = await screen.findByRole("list", { name: "Matching files" });
    expect(within(matches).getAllByRole("button").map((b) => b.textContent)).toEqual(["src/deep/builder.py"]);
    await userEvent.click(within(matches).getByRole("button"));
    expect(onChange).toHaveBeenCalledWith({ file: "src/deep/builder.py", sym: null });
    expect(screen.getByRole("searchbox", { name: /find a file to graph/i })).toHaveValue("");
  });

  it("says when no file matches", async () => {
    route({ "/api/repositories/r1/graph": () => jsonResponse(fileGraph) });
    setup();
    await screen.findByRole("button", { name: "node a.py" });
    await userEvent.type(screen.getByRole("searchbox", { name: /find a file to graph/i }), "zzz");
    expect(await screen.findByText("No file matches.")).toBeInTheDocument();
  });

  it("does not describe a cut symbol graph as a cut file list", async () => {
    route({
      "/api/repositories/r1/graph": () => jsonResponse({ ...symbolGraph, truncated: true }),
      "/api/repositories/r1/impact": () => jsonResponse(impact),
    });
    setup({ file: "a.py", view: "symbols" });
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(/some links are not drawn/i);
    expect(note).not.toHaveTextContent(/most connected/i);
  });
});
