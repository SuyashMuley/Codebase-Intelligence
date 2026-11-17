"use client";

import { useMemo, useState } from "react";
import { LANGUAGE_BADGES, SKIP_LABELS } from "@/lib/format";
import { buildTree, containsPath, filterEntries, type DirNode, type TreeNode } from "@/lib/tree";
import type { FileEntry } from "@/lib/types";

const MAX_SEARCH_RESULTS = 200;

function LanguageBadge({ language }: { language: string | null }) {
  const badge = LANGUAGE_BADGES[language ?? ""];
  if (!badge) return null;
  return (
    <span className={`rounded px-1 text-[10px] font-semibold ${badge.color}`} aria-hidden>
      {badge.short}
    </span>
  );
}

function FileRow({
  entry,
  label,
  selected,
  onSelect,
  indent,
}: {
  entry: FileEntry;
  label: string;
  selected: boolean;
  onSelect: (path: string) => void;
  indent: number;
}) {
  const skipped = entry.skipped_reason !== null;
  return (
    <button
      type="button"
      onClick={() => onSelect(entry.path)}
      aria-current={selected ? "true" : undefined}
      title={entry.path}
      style={{ paddingLeft: `${indent * 14 + 8}px` }}
      className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm focus-visible:outline-2 focus-visible:outline-emerald-400 ${
        selected ? "bg-emerald-500/15 text-emerald-100" : "text-zinc-300 hover:bg-zinc-800"
      } ${skipped ? "opacity-60" : ""}`}
    >
      <LanguageBadge language={entry.language} />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {skipped ? (
        <span className="shrink-0 text-[10px] text-zinc-500">
          {SKIP_LABELS[entry.skipped_reason ?? ""] ?? entry.skipped_reason}
        </span>
      ) : null}
      {entry.redactions > 0 ? (
        <span
          className="shrink-0 rounded bg-amber-500/15 px-1 text-[10px] text-amber-300"
          title={`${entry.redactions} secret-looking value${entry.redactions === 1 ? "" : "s"} redacted`}
        >
          <span aria-hidden>redacted</span>
          <span className="sr-only">{entry.redactions} secret-looking values redacted</span>
        </span>
      ) : null}
      {entry.parse_status === "syntax_errors" || entry.parse_status === "truncated" ? (
        <span
          className="shrink-0 rounded bg-red-500/15 px-1 text-[10px] text-red-300"
          title="Syntax errors: symbols may be incomplete"
        >
          <span aria-hidden>errors</span>
          <span className="sr-only">has syntax errors</span>
        </span>
      ) : null}
    </button>
  );
}

function Tree({
  nodes,
  depth,
  selected,
  isOpen,
  toggle,
  onSelect,
}: {
  nodes: TreeNode[];
  depth: number;
  selected: string | null;
  isOpen: (dir: DirNode) => boolean;
  toggle: (dir: DirNode) => void;
  onSelect: (path: string) => void;
}) {
  return (
    <ul>
      {nodes.map((node) =>
        node.type === "dir" ? (
          <li key={node.path}>
            <button
              type="button"
              onClick={() => toggle(node)}
              aria-expanded={isOpen(node)}
              style={{ paddingLeft: `${depth * 14 + 8}px` }}
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm text-zinc-200 hover:bg-zinc-800 focus-visible:outline-2 focus-visible:outline-emerald-400"
            >
              <span aria-hidden className="w-3 text-zinc-500">
                {isOpen(node) ? "▾" : "▸"}
              </span>
              <span className="min-w-0 flex-1 truncate">{node.name}</span>
              <span className="text-[10px] text-zinc-500">{node.fileCount}</span>
            </button>
            {isOpen(node) ? (
              <Tree
                nodes={node.children}
                depth={depth + 1}
                selected={selected}
                isOpen={isOpen}
                toggle={toggle}
                onSelect={onSelect}
              />
            ) : null}
          </li>
        ) : (
          <li key={node.path}>
            <FileRow
              entry={node.entry}
              label={node.name}
              selected={node.path === selected}
              onSelect={onSelect}
              indent={depth}
            />
          </li>
        ),
      )}
    </ul>
  );
}

export function FileExplorer({
  entries,
  selected,
  onSelect,
}: {
  entries: FileEntry[];
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const [query, setQuery] = useState("");
  // Folders the user opened or closed by hand. Everything else uses the default below.
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const tree = useMemo(() => buildTree(entries), [entries]);
  const matches = useMemo(() => filterEntries(entries, query), [entries, query]);
  const searching = query.trim() !== "";
  const small = entries.length <= 40;

  function isOpen(dir: DirNode): boolean {
    if (dir.path in manual) return manual[dir.path];
    if (selected && containsPath(dir, selected)) return true;
    // Small repositories open fully; large ones show only the top level until you dig in.
    return small || !dir.path.includes("/");
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <label className="sr-only" htmlFor="file-filter">
        Filter files
      </label>
      <input
        id="file-filter"
        type="search"
        placeholder="Filter files"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-600 focus-visible:outline-2 focus-visible:outline-emerald-400"
      />
      <nav aria-label="Files" className="min-h-0 flex-1 overflow-auto">
        {searching ? (
          matches.length === 0 ? (
            <p className="px-2 py-4 text-sm text-zinc-500">No files match “{query.trim()}”.</p>
          ) : (
            <ul aria-label="Matching files">
              {matches.slice(0, MAX_SEARCH_RESULTS).map((entry) => (
                <li key={entry.path}>
                  <FileRow
                    entry={entry}
                    label={entry.path}
                    selected={entry.path === selected}
                    onSelect={onSelect}
                    indent={0}
                  />
                </li>
              ))}
              {matches.length > MAX_SEARCH_RESULTS ? (
                <li className="px-2 py-2 text-xs text-zinc-500">
                  Showing the first {MAX_SEARCH_RESULTS} of {matches.length}. Refine the filter.
                </li>
              ) : null}
            </ul>
          )
        ) : (
          <Tree
            nodes={tree.children}
            depth={0}
            selected={selected}
            isOpen={isOpen}
            toggle={(dir) => setManual((m) => ({ ...m, [dir.path]: !isOpen(dir) }))}
            onSelect={onSelect}
          />
        )}
      </nav>
    </div>
  );
}
