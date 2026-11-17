import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { FileDetail } from "@/lib/types";

vi.mock("./code-editor", () => ({
  default: (p: { path: string; content: string; language: string | null; highlight: unknown }) => (
    <pre
      data-testid="editor"
      data-path={p.path}
      data-language={p.language}
      data-highlight={JSON.stringify(p.highlight)}
    >
      {p.content}
    </pre>
  ),
}));

import { CodeViewer } from "./code-viewer";

const SNAP = { id: "s", commit_sha: "a".repeat(40), state: "running" };

function file(over: Partial<FileDetail> = {}): FileDetail {
  return {
    snapshot: SNAP,
    path: "pkg/app.py",
    language: "python",
    size_bytes: 2048,
    skipped_reason: null,
    redactions: 0,
    parse_status: "ok",
    line_count: 12,
    content: "class A:\n    def m(self): ...\n",
    symbols: [
      { id: 1, kind: "class", name: "A", qualified_name: "pkg/app.py::A", start_line: 1, end_line: 2, parent_id: null, signature: "class A:" },
      { id: 2, kind: "method", name: "m", qualified_name: "pkg/app.py::A.m", start_line: 2, end_line: 2, parent_id: 1, signature: "def m(self): ..." },
    ],
    ...over,
  };
}

function setup(props: Partial<Parameters<typeof CodeViewer>[0]> = {}) {
  const onSelectRange = vi.fn();
  const onRetry = vi.fn();
  render(
    <CodeViewer
      file={file()}
      loading={false}
      error={null}
      range={null}
      onSelectRange={onSelectRange}
      onRetry={onRetry}
      {...props}
    />,
  );
  return { onSelectRange, onRetry };
}

describe("CodeViewer", () => {
  it("shows the file with its details and passes text and language to the editor", async () => {
    setup();
    expect(screen.getByRole("heading", { name: "pkg/app.py" })).toBeInTheDocument();
    expect(screen.getByText("Python · 12 lines · 2.0 KB")).toBeInTheDocument();
    const editor = await screen.findByTestId("editor");
    expect(editor).toHaveAttribute("data-language", "python");
    expect(editor).toHaveTextContent("class A:");
  });

  it("lists symbols in an outline, nested by parent, and selects their lines", async () => {
    const { onSelectRange } = setup();
    const outline = screen.getByRole("list", { name: "Symbols in this file" });
    const items = within(outline).getAllByRole("button");
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveStyle({ paddingLeft: "20px" }); // the method is indented under the class
    await userEvent.click(items[1]);
    expect(onSelectRange).toHaveBeenCalledWith({ start: 2, end: 2 });
  });

  it("marks the symbol whose lines are highlighted", () => {
    setup({ range: { start: 1, end: 2 } });
    const outline = screen.getByRole("list", { name: "Symbols in this file" });
    expect(within(outline).getAllByRole("button")[0]).toHaveAttribute("aria-current", "true");
  });

  it("says when there are no symbols", () => {
    setup({ file: file({ symbols: [] }) });
    expect(screen.getByText(/no functions, classes or types found/i)).toBeInTheDocument();
  });

  it("shows and clears the highlighted range", async () => {
    const { onSelectRange } = setup({ range: { start: 5, end: 9 } });
    expect(screen.getByText("Lines 5–9 highlighted")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onSelectRange).toHaveBeenCalledWith(null);
  });

  it("describes a single highlighted line", () => {
    setup({ range: { start: 7, end: 7 } });
    expect(screen.getByText("Line 7 highlighted")).toBeInTheDocument();
  });

  it("tells the reader when secrets were redacted, with the right grammar", () => {
    const { unmount } = render(
      <CodeViewer file={file({ redactions: 1 })} loading={false} error={null} range={null} onSelectRange={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByRole("note")).toHaveTextContent("1 secret-looking value was replaced");
    unmount();
    setup({ file: file({ redactions: 3 }) });
    expect(screen.getByRole("note")).toHaveTextContent("3 secret-looking values were replaced");
    expect(screen.getByRole("note")).toHaveTextContent("Line numbers are unchanged");
  });

  it.each([
    ["syntax_errors", /syntax errors/i],
    ["truncated", /too complex to parse fully/i],
  ])("warns that symbols may be incomplete (%s)", (status, text) => {
    setup({ file: file({ parse_status: status }) });
    expect(screen.getByRole("note")).toHaveTextContent(text);
  });

  it("explains a skipped file instead of showing an empty editor", () => {
    setup({
      file: file({ content: null, skipped_reason: "too_large", symbols: [], line_count: 0, parse_status: null }),
    });
    expect(screen.getByText(/this file isn’t shown \(Too large\)/i)).toBeInTheDocument();
    expect(screen.getByText(/larger than the 256 KB limit/i)).toBeInTheDocument();
    expect(screen.queryByTestId("editor")).not.toBeInTheDocument();
  });

  it("shows loading, an empty prompt, and errors", async () => {
    const { unmount } = render(
      <CodeViewer file={undefined} loading error={null} range={null} onSelectRange={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/loading file/i);
    unmount();

    const none = render(
      <CodeViewer file={undefined} loading={false} error={null} range={null} onSelectRange={vi.fn()} onRetry={vi.fn()} />,
    );
    expect(screen.getByText("Select a file to view it.")).toBeInTheDocument();
    none.unmount();

    const onRetry = vi.fn();
    render(
      <CodeViewer file={undefined} loading={false} error={new ApiError(502, "x", "Server hiccup.")} range={null} onSelectRange={vi.fn()} onRetry={onRetry} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Server hiccup.");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("explains a missing file without offering a pointless retry", () => {
    setup({ file: undefined, error: new ApiError(404, "file_not_found", "No such file.") });
    expect(screen.getByRole("alert")).toHaveTextContent(/not in this snapshot/i);
    expect(screen.queryByRole("button", { name: /try again/i })).not.toBeInTheDocument();
  });

  it("never renders file content as HTML", async () => {
    setup({ file: file({ content: '<img src=x onerror="alert(1)">\n' }) });
    const editor = await screen.findByTestId("editor");
    expect(editor.querySelector("img")).toBeNull();
    expect(editor).toHaveTextContent('<img src=x onerror="alert(1)">');
  });
});
