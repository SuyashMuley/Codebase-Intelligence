import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Stats } from "@/lib/types";
import { StatsPanel } from "./stats-panel";

function stats(over: Partial<Stats> = {}): Stats {
  return {
    snapshot: { id: "s", commit_sha: "a".repeat(40), state: "running" },
    parsing_done: true,
    files: {
      stored: 12,
      parsed: 12,
      bytes: 29182,
      by_language: { python: 9, typescript: 3 },
      skipped: { unsupported: 35, vendored: 2 },
    },
    symbols: { total: 140, by_kind: { function: 90, class: 20, method: 30 } },
    chunks: { total: 96, average_chars: 640, largest_chars: 1790 },
    refs: { imports: 40, calls: 760 },
    redactions: { spans: 0, files: 0 },
    syntax_error_files: 0,
    ...over,
  };
}

describe("StatsPanel", () => {
  it("shows the headline numbers with context", () => {
    render(<StatsPanel stats={stats()} progress={null} />);
    const card = screen.getByLabelText("Indexing statistics");
    expect(within(card).getByText("Source files").nextSibling).toHaveTextContent("12");
    expect(within(card).getByText("28.5 KB of code")).toBeInTheDocument();
    expect(within(card).getByText("Symbols").nextSibling).toHaveTextContent("140");
    expect(within(card).getByText("Chunks").nextSibling).toHaveTextContent("96");
    expect(within(card).getByText("about 640 characters each")).toBeInTheDocument();
    expect(within(card).getByText("800")).toBeInTheDocument();
    expect(within(card).getByText("40 imports · 760 calls")).toBeInTheDocument();
  });

  it("breaks counts down by language, symbol kind and skip reason, biggest first", () => {
    render(<StatsPanel stats={stats()} progress={null} />);
    const languages = screen.getByRole("heading", { name: "Languages" }).nextElementSibling as HTMLElement;
    const rows = within(languages).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Python");
    expect(rows[0]).toHaveTextContent("9");
    expect(rows[1]).toHaveTextContent("TypeScript");
    const kinds = screen.getByRole("heading", { name: "Symbol kinds" }).nextElementSibling as HTMLElement;
    expect(within(kinds).getAllByRole("listitem")[0]).toHaveTextContent("function");
    const skipped = screen.getByRole("heading", { name: "Not indexed" }).nextElementSibling as HTMLElement;
    expect(within(skipped).getByText("Other file types")).toBeInTheDocument();
    expect(within(skipped).getByText("Vendored or build output")).toBeInTheDocument();
  });

  it("says parsing is still running, with progress", () => {
    render(
      <StatsPanel
        stats={stats({ parsing_done: false })}
        progress={{ files: 4, symbols: 0, chunks: 0, refs: 0, syntax_error_files: 0, truncated_files: 0, done: false }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Parsing in progress · 4 of 12 files");
  });

  it("does not claim progress when parsing has finished", () => {
    render(<StatsPanel stats={stats()} progress={null} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("notes redactions and syntax errors, only when there are some", () => {
    const { rerender } = render(<StatsPanel stats={stats()} progress={null} />);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    rerender(
      <StatsPanel
        stats={stats({ redactions: { spans: 5, files: 3 }, syntax_error_files: 1 })}
        progress={null}
      />,
    );
    const notes = screen.getAllByRole("note");
    expect(notes[0]).toHaveTextContent("5 secret-looking values in 3 files were replaced");
    expect(notes[0]).toHaveTextContent("originals were never saved");
    expect(notes[1]).toHaveTextContent("1 file has syntax errors");
  });

  it("uses singular wording for one", () => {
    render(
      <StatsPanel
        stats={stats({ redactions: { spans: 1, files: 1 }, syntax_error_files: 2 })}
        progress={null}
      />,
    );
    expect(screen.getAllByRole("note")[0]).toHaveTextContent("1 secret-looking value in 1 file was");
    expect(screen.getAllByRole("note")[1]).toHaveTextContent("2 files have syntax errors");
  });

  it("copes with an empty index", () => {
    render(
      <StatsPanel
        stats={stats({
          files: { stored: 0, parsed: 0, bytes: 0, by_language: {}, skipped: {} },
          symbols: { total: 0, by_kind: {} },
          chunks: { total: 0, average_chars: 0, largest_chars: 0 },
          refs: { imports: 0, calls: 0 },
        })}
        progress={null}
      />,
    );
    expect(screen.queryByRole("heading", { name: "Languages" })).not.toBeInTheDocument();
    expect(screen.queryByText(/characters each/)).not.toBeInTheDocument();
  });

  it("counts source files the same way as the status panel, not the explorer rows", () => {
    // 12 files with text, but 14 explorer rows because two skipped files are listed too.
    render(
      <StatsPanel
        stats={stats({
          files: { stored: 14, parsed: 12, bytes: 100, by_language: { python: 9, go: 3 }, skipped: {} },
        })}
        progress={null}
      />,
    );
    expect(screen.getByText("Source files").nextSibling).toHaveTextContent("12");
  });
});
