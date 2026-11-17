"use client";

import { useMemo, useState } from "react";
import { ApiError } from "@/lib/api";
import { useFiles, useGraph } from "@/lib/hooks";
import type { GraphData, GraphNodeData } from "@/lib/types";
import { GraphCanvas, GraphLegend } from "./graph-canvas";
import { ImpactPanel } from "./impact-panel";
import { Button, Card, EmptyState, ErrorNotice, Spinner } from "./ui";

export type GraphView = "files" | "symbols";

export interface GraphSelection {
  file?: string | null;
  sym?: number | null;
  view?: GraphView;
}

const MAX_MATCHES = 8;

/** Reach any file, not only the ones the capped overview happens to draw. */
function FileFinder({
  repoId,
  onPick,
}: {
  repoId: string;
  onPick: (path: string) => void;
}) {
  const [text, setText] = useState("");
  const files = useFiles(repoId, true, false);
  const needle = text.trim().toLowerCase();
  const matches = useMemo(() => {
    if (!needle || !files.data) return [];
    return files.data.items
      .filter((f) => f.skipped_reason === null && f.path.toLowerCase().includes(needle))
      .slice(0, MAX_MATCHES);
  }, [needle, files.data]);
  return (
    <div className="space-y-1">
      <input
        type="search"
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label="Find a file to graph"
        placeholder="Find a file…"
        className="w-56 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs focus-visible:outline-2 focus-visible:outline-emerald-400"
      />
      {needle ? (
        matches.length > 0 ? (
          <ul aria-label="Matching files" className="max-h-56 overflow-auto rounded-md border border-zinc-800 bg-zinc-950 p-1">
            {matches.map((f) => (
              <li key={f.path}>
                <button
                  type="button"
                  onClick={() => {
                    onPick(f.path);
                    setText("");
                  }}
                  className="w-full truncate rounded px-2 py-1 text-left font-mono text-xs text-zinc-300 hover:bg-zinc-800 hover:text-emerald-400 focus-visible:outline-2 focus-visible:outline-emerald-400"
                >
                  {f.path}
                </button>
              </li>
            ))}
          </ul>
        ) : files.isPending ? null : (
          <p className="text-xs text-zinc-500">No file matches.</p>
        )
      ) : null}
    </div>
  );
}

export function GraphTab({
  repoId,
  hasSnapshot,
  ready,
  file,
  view,
  symbol,
  onChange,
  onOpenInFiles,
}: {
  repoId: string;
  hasSnapshot: boolean;
  ready: boolean;
  file: string | null;
  view: GraphView;
  symbol: number | null;
  onChange: (next: GraphSelection) => void;
  onOpenInFiles: (path: string) => void;
}) {
  // A whole repository is a hairball; a file's neighbourhood is readable. So selecting a file
  // narrows the graph to it unless the person asks for everything.
  const [near, setNear] = useState(true);
  const [depth, setDepth] = useState(1);
  const [showUnresolved, setShowUnresolved] = useState(false);

  const symbols = view === "symbols" && file !== null;
  const graph = useGraph(
    repoId,
    {
      level: symbols ? "symbol" : "file",
      path: symbols ? file : near ? file : null,
      depth,
    },
    hasSnapshot && ready,
  );

  const hidden = useMemo(
    () => (graph.data ? graph.data.edges.filter((e) => e.resolution === "unresolved").length : 0),
    [graph.data],
  );
  // Facts first: unresolved calls (dashed) are hidden in the symbol view until asked for.
  const shown = useMemo<GraphData | null>(() => {
    if (!graph.data) return null;
    if (!symbols || showUnresolved) return graph.data;
    return {
      ...graph.data,
      nodes: graph.data.nodes.filter((n) => n.kind !== "external"),
      edges: graph.data.edges.filter((e) => e.resolution === "static"),
    };
  }, [graph.data, symbols, showUnresolved]);

  if (!hasSnapshot) {
    return (
      <EmptyState title="Nothing to graph yet">
        Index this repository from the Overview tab first.
      </EmptyState>
    );
  }
  if (!ready) {
    return (
      <EmptyState title="The graph is built when indexing finishes">
        It links files by their imports and calls. Check the Overview tab for progress.
      </EmptyState>
    );
  }

  function select(node: GraphNodeData) {
    if (node.kind === "external" || !node.path) return;
    if (node.kind === "symbol") {
      const id = Number(node.id.replace("sym:", ""));
      onChange({ file: node.path, sym: Number.isFinite(id) ? id : null });
    } else {
      onChange({ file: node.path, sym: null });
    }
  }

  const selectedId = symbols
    ? symbol !== null
      ? `sym:${symbol}`
      : null
    : (graph.data?.nodes.find((n) => n.path === file)?.id ?? null);

  const error = graph.error instanceof ApiError ? graph.error : null;
  const target = { file, symbol: symbols ? symbol : null };

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_24rem]">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div role="group" aria-label="Graph level" className="flex gap-1">
            <Button
              variant={symbols ? "secondary" : "primary"}
              aria-pressed={!symbols}
              onClick={() => onChange({ view: "files", sym: null })}
            >
              Files
            </Button>
            <Button
              variant={symbols ? "primary" : "secondary"}
              aria-pressed={symbols}
              disabled={file === null}
              title={file === null ? "Select a file first" : undefined}
              onClick={() => onChange({ view: "symbols", sym: null })}
            >
              Symbols
            </Button>
          </div>
          {symbols && hidden > 0 ? (
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <input
                type="checkbox"
                checked={showUnresolved}
                onChange={(e) => setShowUnresolved(e.target.checked)}
              />
              Show {hidden} unresolved {hidden === 1 ? "call" : "calls"}
            </label>
          ) : null}
          {!symbols ? (
            <>
              <label className="flex items-center gap-2 text-xs text-zinc-400">
                <input
                  type="checkbox"
                  checked={near}
                  disabled={file === null}
                  onChange={(e) => setNear(e.target.checked)}
                />
                Only around the selected file
              </label>
              {near && file ? (
                <select
                  aria-label="Neighbourhood size"
                  value={depth}
                  onChange={(e) => setDepth(Number(e.target.value))}
                  className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs"
                >
                  {[1, 2, 3].map((n) => (
                    <option key={n} value={n}>
                      {n} {n === 1 ? "step" : "steps"} out
                    </option>
                  ))}
                </select>
              ) : null}
            </>
          ) : null}
          <FileFinder repoId={repoId} onPick={(path) => onChange({ file: path, sym: null })} />
          {file ? (
            <Button variant="ghost" onClick={() => onOpenInFiles(file)}>
              Open in Files
            </Button>
          ) : null}
        </div>

        {graph.isPending ? <Spinner label="Loading the graph…" /> : null}
        {error ? (
          <div className="space-y-3">
            <ErrorNotice title="Could not load the graph">{error.message}</ErrorNotice>
            <Button variant="secondary" onClick={() => graph.refetch()}>
              Try again
            </Button>
          </div>
        ) : null}

        {graph.data && shown ? (
          shown.nodes.length === 0 ? (
            <EmptyState title="No links found">
              {graph.data.note ?? "No imports or calls between files could be resolved."}
            </EmptyState>
          ) : (
            <>
              <GraphCanvas graph={shown} selectedId={selectedId} onSelect={select} />
              <GraphLegend />
              {graph.data.note ? <p className="text-xs text-zinc-500">{graph.data.note}</p> : null}
              {graph.data.truncated ? (
                <p role="note" className="text-xs text-amber-200">
                  {symbols
                    ? "Some links are not drawn because this file has more than the display limit. The most frequent unresolved calls are kept."
                    : `Showing the ${graph.data.nodes.length} most connected of ${graph.data.total_nodes} files. Select a file to see everything around it.`}
                </p>
              ) : null}
              <details className="text-sm">
                <summary className="cursor-pointer text-zinc-400">Show as a list</summary>
                <ul aria-label="Nodes in this graph" className="mt-2 max-h-64 space-y-1 overflow-auto">
                  {shown.nodes
                    .filter((n) => n.kind !== "external")
                    .map((n) => (
                      <li key={n.id}>
                        <button
                          type="button"
                          onClick={() => select(n)}
                          aria-current={n.id === selectedId ? "true" : undefined}
                          className="w-full text-left font-mono text-xs text-zinc-300 hover:text-emerald-400 focus-visible:outline-2 focus-visible:outline-emerald-400"
                        >
                          {n.kind === "symbol" ? `${n.path} › ${n.label}` : (n.path ?? n.label)}
                          <span className="text-zinc-600">
                            {" "}
                            · {n.in_degree} in · {n.out_degree} out
                          </span>
                        </button>
                      </li>
                    ))}
                </ul>
              </details>
            </>
          )
        ) : null}
      </div>

      <div className="min-w-0">
        {file ? (
          <ImpactPanel
            key={`${file}:${target.symbol ?? ""}`}
            repoId={repoId}
            target={target}
            onOpenFile={(path) => onChange({ file: path, sym: null })}
          />
        ) : (
          <Card>
            <p className="text-sm text-zinc-400">
              Select a file in the graph to see what depends on it, which tests to run, and which
              calls could not be verified.
            </p>
          </Card>
        )}
      </div>
    </div>
  );
}
