"use client";

import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useSearch } from "@/lib/hooks";
import type { SearchHit, SearchMode } from "@/lib/types";
import { Button, Card, EmptyState, ErrorNotice } from "./ui";

const MODES: { value: SearchMode; label: string }[] = [
  { value: "vector", label: "Meaning (default)" },
  { value: "hybrid", label: "Meaning + keywords" },
  { value: "keyword", label: "Keywords only" },
];

const WARNINGS: Record<string, string> = {
  embeddings_not_ready:
    "Embeddings are not ready yet, so this used keyword search. Meaning-based search works once indexing has finished.",
};

function warningText(code: string): string {
  if (WARNINGS[code]) return WARNINGS[code];
  if (code.startsWith("embedding_failed")) {
    return "The embedding service could not be reached, so this used keyword search. Try again shortly.";
  }
  return code;
}

export function lineLabel(hit: Pick<SearchHit, "start_line" | "end_line">): string {
  return hit.start_line === hit.end_line
    ? `${hit.start_line}`
    : `${hit.start_line}-${hit.end_line}`;
}

export function SearchTab({
  repoId,
  hasSnapshot,
  onOpen,
}: {
  repoId: string;
  hasSnapshot: boolean;
  onOpen: (path: string, line: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("vector");
  const search = useSearch(repoId);

  if (!hasSnapshot) {
    return (
      <EmptyState title="Nothing to search yet">
        Index this repository from the Overview tab first.
      </EmptyState>
    );
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (q) search.mutate({ query: q, mode });
  }

  const error = search.error instanceof ApiError ? search.error : null;
  const result = search.data;

  return (
    <div className="space-y-4">
      <form onSubmit={submit} className="flex flex-wrap gap-2" role="search">
        <input
          type="search"
          value={query}
          maxLength={500}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search the code"
          placeholder="Ask about the code, or type a function name"
          className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-emerald-400"
        />
        <select
          aria-label="Search method"
          value={mode}
          onChange={(e) => setMode(e.target.value as SearchMode)}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-2 text-sm"
        >
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
        <Button type="submit" disabled={search.isPending || !query.trim()}>
          {search.isPending ? "Searching…" : "Search"}
        </Button>
      </form>

      {error ? (
        <ErrorNotice title="Search failed">
          {error.code === "rate_limited"
            ? "You are searching too fast. Wait a moment and try again."
            : error.message}
        </ErrorNotice>
      ) : null}

      {result?.warnings.map((w) => (
        <p key={w} role="note" className="rounded bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {warningText(w)}
        </p>
      ))}

      {result && result.hits.length === 0 ? (
        <EmptyState title="No matches">Try different words, or another search method.</EmptyState>
      ) : null}

      {result && result.hits.length > 0 ? (
        <ol aria-label="Search results" className="space-y-3">
          {result.hits.map((hit) => (
            <li key={hit.chunk_id}>
              <Card className="space-y-2 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => onOpen(hit.path, lineLabel(hit))}
                    className="max-w-full break-all text-left font-mono text-sm text-emerald-400 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
                  >
                    {hit.path}:{lineLabel(hit)}
                  </button>
                  <span className="text-xs text-zinc-500">
                    {hit.symbol ? `${hit.symbol} · ` : ""}score {hit.score.toFixed(3)}
                  </span>
                </div>
                <pre className="max-h-40 overflow-auto rounded bg-zinc-950 p-2 text-xs text-zinc-300">
                  {hit.text}
                </pre>
              </Card>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}
