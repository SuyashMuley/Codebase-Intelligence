import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { FileEntry } from "@/lib/types";
import { FileExplorer } from "./file-explorer";

function entry(path: string, over: Partial<FileEntry> = {}): FileEntry {
  return {
    path,
    language: "python",
    size_bytes: 10,
    skipped_reason: null,
    redactions: 0,
    parse_status: "ok",
    symbol_count: 1,
    ...over,
  };
}

function setup(entries: FileEntry[], selected: string | null = null) {
  const onSelect = vi.fn();
  render(<FileExplorer entries={entries} selected={selected} onSelect={onSelect} />);
  return onSelect;
}

describe("FileExplorer", () => {
  it("shows folders and files, and selects a file on click", async () => {
    const onSelect = setup([entry("src/app.py"), entry("README.py"), entry("src/util/helpers.py")]);
    expect(screen.getByRole("button", { name: /^src/ })).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(screen.getByRole("button", { name: /app\.py/ }));
    expect(onSelect).toHaveBeenCalledWith("src/app.py");
  });

  it("marks the selected file", () => {
    setup([entry("a.py"), entry("b.py")], "b.py");
    expect(screen.getByRole("button", { name: /b\.py/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /a\.py/ })).not.toHaveAttribute("aria-current");
  });

  it("opens and closes a folder", async () => {
    setup([entry("src/app.py"), entry("src/more.py")]);
    const folder = screen.getByRole("button", { name: /^src/ });
    expect(screen.getByRole("button", { name: /app\.py/ })).toBeInTheDocument();
    await userEvent.click(folder);
    expect(folder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /app\.py/ })).not.toBeInTheDocument();
    await userEvent.click(folder);
    expect(screen.getByRole("button", { name: /app\.py/ })).toBeInTheDocument();
  });

  it("joins single-folder chains into one row", () => {
    setup([entry("src/components/ui/button.tsx", { language: "typescript" })]);
    expect(screen.getByRole("button", { name: /^src\/components\/ui/ })).toBeInTheDocument();
  });

  it("in a large repository only the top level starts open, except around the selected file", () => {
    const entries = Array.from({ length: 60 }, (_, i) => entry(`top${i % 3}/sub${i % 4}/file${i}.py`));
    setup(entries);
    expect(screen.getByRole("button", { name: /^top0/ })).toHaveAttribute("aria-expanded", "true");
    // top0 contains sub0..sub3, each a deeper folder that stays closed
    expect(screen.getAllByRole("button", { name: /^sub/ })[0]).toHaveAttribute("aria-expanded", "false");
  });

  it("opens the folders that contain the selected file", () => {
    const entries = Array.from({ length: 60 }, (_, i) => entry(`top/sub${i % 5}/deep/file${i}.py`));
    setup(entries, "top/sub2/deep/file2.py");
    expect(screen.getByRole("button", { name: /file2\.py/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /file7\.py/ })).toBeInTheDocument(); // same folder
    expect(screen.queryByRole("button", { name: /file3\.py/ })).not.toBeInTheDocument(); // sibling folder, closed
  });

  it("badges languages, redactions, syntax errors and skipped files", () => {
    setup([
      entry("a.py", { redactions: 2 }),
      entry("b.ts", { language: "typescript", parse_status: "syntax_errors" }),
      entry("c.go", { language: "go", skipped_reason: "too_large", parse_status: null }),
    ]);
    expect(screen.getByRole("button", { name: /a\.py.*2 secret-looking values redacted/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /b\.ts.*has syntax errors/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /c\.go.*Too large/ })).toBeInTheDocument();
    expect(screen.getByText("TS")).toBeInTheDocument();
    expect(screen.getByText("GO")).toBeInTheDocument();
  });

  it("filters to a flat list of full paths, and says when nothing matches", async () => {
    setup([entry("src/app.py"), entry("src/other.py"), entry("lib/util.js", { language: "javascript" })]);
    await userEvent.type(screen.getByLabelText("Filter files"), "util");
    const list = screen.getByRole("list", { name: "Matching files" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByRole("button", { name: /lib\/util\.js/ })).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Filter files"));
    await userEvent.type(screen.getByLabelText("Filter files"), "nope");
    expect(screen.getByText(/no files match/i)).toBeInTheDocument();
  });

  it("caps very long search results", async () => {
    setup(Array.from({ length: 250 }, (_, i) => entry(`f${i}.py`)));
    await userEvent.type(screen.getByLabelText("Filter files"), "f");
    expect(screen.getAllByRole("listitem").filter((li) => li.querySelector("button"))).toHaveLength(200);
    expect(screen.getByText(/first 200 of 250/i)).toBeInTheDocument();
  });
});
