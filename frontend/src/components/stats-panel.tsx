import { formatBytes, formatLanguage, SKIP_LABELS } from "@/lib/format";
import type { ParseProgress, Stats } from "@/lib/types";
import { Card } from "./ui";

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div>
      <dt className="text-xs text-zinc-500">{label}</dt>
      <dd className="text-xl font-semibold text-zinc-100">{value}</dd>
      {hint ? <p className="text-xs text-zinc-500">{hint}</p> : null}
    </div>
  );
}

function Breakdown({
  title,
  rows,
  label,
}: {
  title: string;
  rows: Record<string, number>;
  label: (key: string) => string;
}) {
  const entries = Object.entries(rows).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  if (entries.length === 0) return null;
  return (
    <div>
      <h3 className="mb-2 text-xs font-medium text-zinc-400">{title}</h3>
      <ul className="space-y-1.5">
        {entries.map(([key, n]) => (
          <li key={key} className="grid grid-cols-[8rem_1fr_3rem] items-center gap-2 text-xs">
            <span className="truncate text-zinc-300">{label(key)}</span>
            <span className="h-1.5 overflow-hidden rounded bg-zinc-800">
              <span
                className="block h-full rounded bg-emerald-500/70"
                style={{ width: `${Math.max(4, (n / max) * 100)}%` }}
              />
            </span>
            <span className="text-right tabular-nums text-zinc-400">{n}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function StatsPanel({
  stats,
  progress,
}: {
  stats: Stats;
  progress: ParseProgress | null;
}) {
  const parsing = !stats.parsing_done;
  // Files whose text is stored (and so parsed). The explorer also lists a few skipped files
  // (too large, ...), which would make this number disagree with the status panel above.
  const sourceFiles = Object.values(stats.files.by_language).reduce((a, b) => a + b, 0);
  return (
    <Card className="space-y-5" aria-label="Indexing statistics">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-zinc-200">What was indexed</h2>
        {parsing ? (
          <p role="status" className="text-xs text-sky-300">
            Parsing in progress
            {progress ? ` · ${progress.files} of ${sourceFiles} files` : ""}…
          </p>
        ) : null}
      </div>

      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat
          label="Source files"
          value={sourceFiles}
          hint={`${formatBytes(stats.files.bytes)} of code`}
        />
        <Stat label="Symbols" value={stats.symbols.total} hint="functions, classes, types" />
        <Stat
          label="Chunks"
          value={stats.chunks.total}
          hint={stats.chunks.total ? `about ${stats.chunks.average_chars} characters each` : undefined}
        />
        <Stat
          label="References"
          value={stats.refs.imports + stats.refs.calls}
          hint={`${stats.refs.imports} imports · ${stats.refs.calls} calls`}
        />
      </dl>

      <div className="grid gap-6 sm:grid-cols-3">
        <Breakdown title="Languages" rows={stats.files.by_language} label={formatLanguage} />
        <Breakdown title="Symbol kinds" rows={stats.symbols.by_kind} label={(k) => k} />
        <Breakdown
          title="Not indexed"
          rows={stats.files.skipped}
          label={(k) => SKIP_LABELS[k] ?? k}
        />
      </div>

      {stats.redactions.spans > 0 ? (
        <p role="note" className="rounded bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {stats.redactions.spans} secret-looking value{stats.redactions.spans === 1 ? "" : "s"} in{" "}
          {stats.redactions.files} file{stats.redactions.files === 1 ? "" : "s"}{" "}
          {stats.redactions.spans === 1 ? "was" : "were"} replaced with{" "}
          <code>[REDACTED:…]</code> before storage. The originals were never saved.
        </p>
      ) : null}
      {stats.syntax_error_files > 0 ? (
        <p role="note" className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-200">
          {stats.syntax_error_files} file{stats.syntax_error_files === 1 ? " has" : "s have"}{" "}
          syntax errors, so their symbols may be incomplete.
        </p>
      ) : null}
    </Card>
  );
}
