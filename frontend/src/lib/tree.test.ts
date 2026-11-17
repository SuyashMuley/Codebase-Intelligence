import { describe, expect, it } from "vitest";
import type { FileEntry } from "./types";
import {
  buildTree,
  containsPath,
  filterEntries,
  formatLineRange,
  parseLineRange,
  symbolDepths,
  type DirNode,
} from "./tree";

function entry(path: string, over: Partial<FileEntry> = {}): FileEntry {
  return {
    path,
    language: "python",
    size_bytes: 10,
    skipped_reason: null,
    redactions: 0,
    parse_status: "ok",
    symbol_count: 0,
    ...over,
  };
}

const names = (dir: DirNode) => dir.children.map((c) => `${c.type}:${c.name}`);

describe("buildTree", () => {
  it("nests files under folders, folders first, each group sorted", () => {
    const tree = buildTree(
      ["z.py", "b/y.py", "a.py", "b/x.py", "a/b/c.py", "a/other.py"].map((p) => entry(p)),
    );
    expect(names(tree)).toEqual(["dir:a", "dir:b", "file:a.py", "file:z.py"]);
    const b = tree.children.find((c) => c.name === "b") as DirNode;
    expect(names(b)).toEqual(["file:x.py", "file:y.py"]);
    expect(tree.fileCount).toBe(6);
  });

  it("sorts bytewise so the order is the same on every machine", () => {
    const tree = buildTree(["pkg/_x.py", "pkg/Z.py", "pkg/a.py", "pkg/B.py"].map((p) => entry(p)));
    const pkg = tree.children[0] as DirNode;
    expect(names(pkg)).toEqual(["file:B.py", "file:Z.py", "file:_x.py", "file:a.py"]);
  });

  it("joins chains of single-folder directories into one row", () => {
    const tree = buildTree([entry("src/components/ui/button.tsx"), entry("src/components/ui/card.tsx")]);
    expect(names(tree)).toEqual(["dir:src/components/ui"]);
    const ui = tree.children[0] as DirNode;
    expect(ui.path).toBe("src/components/ui");
    expect(names(ui)).toEqual(["file:button.tsx", "file:card.tsx"]);
  });

  it("does not join a folder that also holds files or several folders", () => {
    const tree = buildTree([entry("src/a.py"), entry("src/sub/b.py"), entry("lib/x/y/z.py")]);
    expect(names(tree)).toEqual(["dir:lib/x/y", "dir:src"]);
    const src = tree.children[1] as DirNode;
    expect(names(src)).toEqual(["dir:sub", "file:a.py"]);
  });

  it("keeps the file entries so the UI can show badges", () => {
    const tree = buildTree([entry("a.py", { redactions: 2, skipped_reason: null })]);
    const file = tree.children[0];
    expect(file.type === "file" && file.entry.redactions).toBe(2);
  });

  it("copes with no files and with one file", () => {
    expect(buildTree([]).children).toEqual([]);
    expect(names(buildTree([entry("only.go")]))).toEqual(["file:only.go"]);
  });
});

describe("filterEntries", () => {
  const all = [
    entry("src/app.py"),
    entry("web/Button.tsx", { language: "typescript" }),
    entry("src/big.py", { skipped_reason: "too_large", parse_status: null }),
  ];
  it("matches path, language and skip reason, ignoring case", () => {
    expect(filterEntries(all, "BUTTON").map((e) => e.path)).toEqual(["web/Button.tsx"]);
    expect(filterEntries(all, "typescript").map((e) => e.path)).toEqual(["web/Button.tsx"]);
    expect(filterEntries(all, "too_large").map((e) => e.path)).toEqual(["src/big.py"]);
    expect(filterEntries(all, "src/").map((e) => e.path)).toEqual(["src/app.py", "src/big.py"]);
  });
  it("returns everything for a blank query and nothing for no match", () => {
    expect(filterEntries(all, "  ")).toHaveLength(3);
    expect(filterEntries(all, "zzz")).toEqual([]);
  });
});

describe("containsPath", () => {
  const dir = { type: "dir", name: "src/a", path: "src/a", children: [], fileCount: 0 } as DirNode;
  it("is true only for files below the folder", () => {
    expect(containsPath(dir, "src/a/x.py")).toBe(true);
    expect(containsPath(dir, "src/a/b/x.py")).toBe(true);
    expect(containsPath(dir, "src/ab/x.py")).toBe(false);
    expect(containsPath(dir, "src/a")).toBe(false);
  });
});

describe("line ranges", () => {
  it.each([
    ["12", { start: 12, end: 12 }],
    ["12-20", { start: 12, end: 20 }],
    ["20-12", { start: 12, end: 20 }],
    ["1", { start: 1, end: 1 }],
  ])("parses %s", (value, expected) => expect(parseLineRange(value)).toEqual(expected));

  it.each([null, "", "0", "0-5", "abc", "1-", "-5", "1-2-3", "1.5", "99999999", " 5", "5;drop"])(
    "rejects %j",
    (value) => expect(parseLineRange(value)).toBeNull(),
  );

  it("formats back to the URL form", () => {
    expect(formatLineRange({ start: 7, end: 7 })).toBe("7");
    expect(formatLineRange({ start: 7, end: 9 })).toBe("7-9");
    expect(formatLineRange(null)).toBeNull();
  });
});

describe("symbolDepths", () => {
  it("indents by the parent chain", () => {
    const depths = symbolDepths([
      { id: 1, parent_id: null },
      { id: 2, parent_id: 1 },
      { id: 3, parent_id: 2 },
      { id: 4, parent_id: null },
    ]);
    expect([1, 2, 3, 4].map((id) => depths.get(id))).toEqual([0, 1, 2, 0]);
  });
  it("cannot loop forever on bad data", () => {
    const depths = symbolDepths([
      { id: 1, parent_id: 2 },
      { id: 2, parent_id: 1 },
    ]);
    expect(depths.get(1)).toBeLessThanOrEqual(20);
  });
});
