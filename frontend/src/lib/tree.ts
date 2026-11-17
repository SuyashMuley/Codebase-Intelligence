import type { FileEntry } from "./types";

export interface FileNode {
  type: "file";
  name: string;
  path: string;
  entry: FileEntry;
}

export interface DirNode {
  type: "dir";
  name: string; // "src" or, for a compacted chain, "src/components"
  path: string; // the deepest directory of the chain
  children: TreeNode[];
  fileCount: number; // files anywhere below
}

export type TreeNode = FileNode | DirNode;

const byName = (a: TreeNode, b: TreeNode): number => {
  if (a.type !== b.type) return a.type === "dir" ? -1 : 1; // folders first
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; // bytewise: same everywhere
};

/**
 * Turns a flat list of paths into a folder tree. Chains of folders that each hold only one
 * folder are shown as one row ("src/components/ui"), like most code editors do.
 */
export function buildTree(entries: FileEntry[]): DirNode {
  const root: DirNode = { type: "dir", name: "", path: "", children: [], fileCount: 0 };
  const dirs = new Map<string, DirNode>([["", root]]);

  for (const entry of entries) {
    const parts = entry.path.split("/");
    let parent = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const path = parts.slice(0, i + 1).join("/");
      let dir = dirs.get(path);
      if (!dir) {
        dir = { type: "dir", name: parts[i], path, children: [], fileCount: 0 };
        dirs.set(path, dir);
        parent.children.push(dir);
      }
      dir.fileCount += 1;
      parent = dir;
    }
    parent.children.push({ type: "file", name: parts[parts.length - 1], path: entry.path, entry });
    root.fileCount += 1;
  }
  return compact(sortTree(root), true);
}

function sortTree(dir: DirNode): DirNode {
  dir.children.sort(byName);
  for (const child of dir.children) if (child.type === "dir") sortTree(child);
  return dir;
}

function compact(dir: DirNode, isRoot: boolean): DirNode {
  dir.children = dir.children.map((c) => (c.type === "dir" ? compact(c, false) : c));
  if (!isRoot) {
    while (dir.children.length === 1 && dir.children[0].type === "dir") {
      const only = dir.children[0];
      dir = { ...only, name: `${dir.name}/${only.name}`, fileCount: only.fileCount };
    }
  }
  return dir;
}

/** Files whose path, language or skip reason contains the query (case-insensitive). */
export function filterEntries(entries: FileEntry[], query: string): FileEntry[] {
  const q = query.trim().toLowerCase();
  if (!q) return entries;
  return entries.filter(
    (e) =>
      e.path.toLowerCase().includes(q) ||
      (e.language ?? "").toLowerCase().includes(q) ||
      (e.skipped_reason ?? "").toLowerCase().includes(q),
  );
}

/** Whether `dir` contains `filePath` (so a selected file's folders can be shown open). */
export function containsPath(dir: DirNode, filePath: string): boolean {
  return dir.path !== "" && filePath.startsWith(`${dir.path}/`);
}

/** "12" -> {12,12}; "12-20" -> {12,20}; anything else -> null. Reversed ranges are fixed. */
export function parseLineRange(value: string | null): { start: number; end: number } | null {
  if (!value) return null;
  const m = /^(\d{1,7})(?:-(\d{1,7}))?$/.exec(value);
  if (!m) return null;
  const a = Number(m[1]);
  const b = m[2] ? Number(m[2]) : a;
  if (a < 1 || b < 1) return null;
  return { start: Math.min(a, b), end: Math.max(a, b) };
}

export function formatLineRange(range: { start: number; end: number } | null): string | null {
  if (!range) return null;
  return range.start === range.end ? String(range.start) : `${range.start}-${range.end}`;
}

/** Indentation level of each symbol, from its parent chain (cycles cannot loop forever). */
export function symbolDepths(symbols: { id: number; parent_id: number | null }[]): Map<number, number> {
  const parents = new Map(symbols.map((s) => [s.id, s.parent_id]));
  const depths = new Map<number, number>();
  for (const s of symbols) {
    let depth = 0;
    let cur = s.parent_id;
    while (cur !== null && cur !== undefined && depth < 20) {
      depth += 1;
      cur = parents.get(cur) ?? null;
    }
    depths.set(s.id, depth);
  }
  return depths;
}
