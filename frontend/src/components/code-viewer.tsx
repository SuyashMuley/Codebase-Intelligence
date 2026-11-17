"use client";

import dynamic from "next/dynamic";
import { formatBytes, formatLanguage, SKIP_EXPLANATIONS, SKIP_LABELS } from "@/lib/format";
import { symbolDepths } from "@/lib/tree";
import type { ApiError } from "@/lib/api";
import type { FileDetail, SymbolInfo } from "@/lib/types";
import { Button, ErrorNotice, Spinner } from "./ui";

// Monaco is large and browser-only: load it on demand, never during server rendering.
const CodeEditor = dynamic(() => import("./code-editor"), {
  ssr: false,
  loading: () => <Spinner label="Loading editor…" />,
});

export type LineRange = { start: number; end: number };

function Outline({
  symbols,
  active,
  onSelect,
}: {
  symbols: SymbolInfo[];
  active: LineRange | null;
  onSelect: (range: LineRange) => void;
}) {
  const depths = symbolDepths(symbols);
  if (symbols.length === 0) {
    return <p className="p-3 text-xs text-zinc-500">No functions, classes or types found.</p>;
  }
  return (
    <ul aria-label="Symbols in this file" className="py-1">
      {symbols.map((s) => {
        const isActive = active?.start === s.start_line && active?.end === s.end_line;
        return (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => onSelect({ start: s.start_line, end: s.end_line })}
              aria-current={isActive ? "true" : undefined}
              title={`${s.signature ?? s.name} (lines ${s.start_line}-${s.end_line})`}
              style={{ paddingLeft: `${(depths.get(s.id) ?? 0) * 12 + 8}px` }}
              className={`flex w-full items-center gap-2 px-2 py-1 text-left text-xs focus-visible:outline-2 focus-visible:outline-emerald-400 ${
                isActive ? "bg-emerald-500/15 text-emerald-100" : "text-zinc-300 hover:bg-zinc-800"
              }`}
            >
              <span className="w-12 shrink-0 text-[10px] uppercase text-zinc-500">{s.kind}</span>
              <span className="min-w-0 flex-1 truncate font-mono">{s.name}</span>
              <span className="shrink-0 text-[10px] text-zinc-600">{s.start_line}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function CodeViewer({
  file,
  loading,
  error,
  range,
  onSelectRange,
  onRetry,
}: {
  file: FileDetail | undefined;
  loading: boolean;
  error: ApiError | null;
  range: LineRange | null;
  onSelectRange: (range: LineRange | null) => void;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Spinner label="Loading file…" />
      </div>
    );
  }
  if (error) {
    const missing = error.status === 404;
    return (
      <div className="space-y-3 p-4">
        <ErrorNotice title={missing ? "That file is not in this snapshot" : "Could not load the file"}>
          {missing ? "It may have been renamed, or the link is out of date." : error.message}
        </ErrorNotice>
        {missing ? null : (
          <Button variant="secondary" onClick={onRetry}>
            Try again
          </Button>
        )}
      </div>
    );
  }
  if (!file) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-zinc-500">
        Select a file to view it.
      </div>
    );
  }

  const skipped = file.skipped_reason !== null;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-zinc-800 p-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="min-w-0 truncate font-mono text-sm text-zinc-100" title={file.path}>
            {file.path}
          </h2>
          <p className="text-xs text-zinc-500">
            {[
              file.language ? formatLanguage(file.language) : null,
              skipped ? null : `${file.line_count} lines`,
              formatBytes(file.size_bytes),
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        {file.redactions > 0 ? (
          <p role="note" className="rounded bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            {file.redactions} secret-looking value{file.redactions === 1 ? " was" : "s were"}{" "}
            replaced with <code>[REDACTED:…]</code> before this file was stored. Line numbers are
            unchanged.
          </p>
        ) : null}
        {file.parse_status === "syntax_errors" || file.parse_status === "truncated" ? (
          <p role="note" className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-200">
            {file.parse_status === "truncated"
              ? "This file was too complex to parse fully, so the symbol list is incomplete."
              : "This file has syntax errors, so the symbol list may be incomplete."}
          </p>
        ) : null}
        {range ? (
          <div className="flex items-center justify-between rounded bg-emerald-500/10 px-3 py-1.5 text-xs text-emerald-200">
            <span>
              {range.start === range.end
                ? `Line ${range.start} highlighted`
                : `Lines ${range.start}–${range.end} highlighted`}
            </span>
            <button
              type="button"
              onClick={() => onSelectRange(null)}
              className="underline focus-visible:outline-2 focus-visible:outline-emerald-400"
            >
              Clear
            </button>
          </div>
        ) : null}
      </div>

      {skipped || file.content === null ? (
        <div className="space-y-2 p-6 text-sm">
          <p className="font-medium text-zinc-200">
            This file isn’t shown ({SKIP_LABELS[file.skipped_reason ?? ""] ?? file.skipped_reason}).
          </p>
          <p className="text-zinc-400">
            {SKIP_EXPLANATIONS[file.skipped_reason ?? ""] ?? "It was not stored."}
          </p>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 lg:grid-cols-[1fr_14rem]">
          <div className="min-h-[24rem] min-w-0">
            <CodeEditor
              path={file.path}
              content={file.content}
              language={file.language}
              highlight={range}
            />
          </div>
          <aside className="max-h-64 overflow-auto border-t border-zinc-800 lg:max-h-none lg:border-l lg:border-t-0">
            <h3 className="px-3 pt-2 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
              Outline
            </h3>
            <Outline symbols={file.symbols} active={range} onSelect={onSelectRange} />
          </aside>
        </div>
      )}
    </div>
  );
}
