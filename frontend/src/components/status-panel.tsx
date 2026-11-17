"use client";

import { useEffect, useState } from "react";
import type { DriverStop } from "@/lib/hooks";
import { formatBytes, shortSha, SKIP_LABELS } from "@/lib/format";
import type { ApiError } from "@/lib/api";
import type { IndexStatus } from "@/lib/types";
import { Badge, Button, Card, EmptyState, ErrorNotice } from "./ui";

const PHASES = [
  { key: "fetch", label: "Fetch source" },
  { key: "parse", label: "Parse code" },
  { key: "embed", label: "Embed" },
  { key: "graph", label: "Build graph" },
  { key: "done", label: "Ready" },
] as const;

function RetryCountdown({ retryAt }: { retryAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(t);
  }, []);
  const seconds = Math.max(0, Math.ceil((new Date(retryAt).getTime() - now) / 1_000));
  return <span>{seconds > 0 ? `Retrying in ${seconds}s…` : "Retrying now…"}</span>;
}

function Stepper({ status }: { status: IndexStatus }) {
  const current = PHASES.findIndex((p) => p.key === status.phase);
  return (
    <ol className="flex flex-wrap gap-2" aria-label="Indexing progress">
      {PHASES.map((phase, i) => {
        const done = status.state === "ready" || i < current;
        const active = status.state !== "ready" && i === current;
        return (
          <li
            key={phase.key}
            aria-current={active ? "step" : undefined}
            className={`rounded-md border px-3 py-1.5 text-xs ${
              done
                ? "border-emerald-700 bg-emerald-500/10 text-emerald-300"
                : active
                  ? "border-sky-600 bg-sky-500/10 text-sky-200"
                  : "border-zinc-800 text-zinc-500"
            }`}
          >
            {done ? "✓ " : ""}
            {phase.label}
          </li>
        );
      })}
    </ol>
  );
}

export function StatusPanel({
  status,
  stopped,
  driverError,
  starting,
  onIndex,
}: {
  status: IndexStatus;
  stopped: DriverStop | null;
  driverError: ApiError | null;
  starting: boolean;
  onIndex: () => void;
}) {
  if (status.state === "not_indexed") {
    return (
      <EmptyState title="Not indexed yet">
        <p>Index the latest commit to fetch its source files.</p>
        <Button className="mt-4" onClick={onIndex} disabled={starting}>
          {starting ? "Starting…" : "Index latest commit"}
        </Button>
      </EmptyState>
    );
  }

  const tone = { pending: "info", running: "info", ready: "good", failed: "bad" } as const;
  const progress = status.progress;
  const skipped = progress ? Object.entries(progress.skipped) : [];

  return (
    <Card className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Badge tone={tone[status.state as keyof typeof tone] ?? "neutral"}>{status.state}</Badge>
          {status.commit_sha ? (
            <span className="font-mono text-xs text-zinc-400">
              commit {shortSha(status.commit_sha)}
            </span>
          ) : null}
        </div>
        <Button variant="secondary" onClick={onIndex} disabled={starting}>
          {status.state === "failed" ? "Try again" : "Check latest commit"}
        </Button>
      </div>

      <Stepper status={status} />

      {progress ? (
        <dl className="grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-xs text-zinc-500">Source files stored</dt>
            <dd className="text-lg font-medium">{progress.files_kept}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Source size</dt>
            <dd className="text-lg font-medium">{formatBytes(progress.bytes_kept)}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Skipped</dt>
            <dd className="text-sm text-zinc-300">
              {skipped.length === 0
                ? "Nothing"
                : skipped.map(([reason, n]) => `${SKIP_LABELS[reason] ?? reason}: ${n}`).join(" · ")}
            </dd>
          </div>
        </dl>
      ) : null}

      {status.parse ? (
        <dl className="grid gap-4 sm:grid-cols-3">
          <div>
            <dt className="text-xs text-zinc-500">Files parsed</dt>
            <dd className="text-lg font-medium">{status.parse.files}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Symbols found</dt>
            <dd className="text-lg font-medium">{status.parse.symbols}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Chunks created</dt>
            <dd className="text-lg font-medium">{status.parse.chunks}</dd>
          </div>
        </dl>
      ) : null}

      {status.embed ? (
        <div>
          <div className="flex items-baseline justify-between text-xs text-zinc-500">
            <span>Chunks embedded</span>
            <span>
              {status.embed.embedded} of {status.embed.total}
            </span>
          </div>
          <progress
            className="mt-1 h-2 w-full"
            aria-label="Embedding progress"
            max={Math.max(status.embed.total, 1)}
            value={status.embed.embedded}
          />
        </div>
      ) : null}

      {progress && progress.secrets_redacted > 0 ? (
        <p role="note" className="rounded bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {progress.secrets_redacted} secret-looking value
          {progress.secrets_redacted === 1 ? " was" : "s were"} replaced with{" "}
          <code>[REDACTED:…]</code> before anything was stored.
        </p>
      ) : null}

      {status.error ? (
        <ErrorNotice
          title={
            status.state === "failed" ? "Indexing failed" : "A step failed and will be retried"
          }
        >
          <p>{status.error.message}</p>
          <p className="mt-1 text-xs text-red-300/70">
            {status.error.code} · attempt {status.attempts} of {status.max_attempts}
            {status.retry_at && status.state !== "failed" ? (
              <>
                {" · "}
                <RetryCountdown retryAt={status.retry_at} />
              </>
            ) : null}
          </p>
        </ErrorNotice>
      ) : null}

      {driverError && !status.error ? (
        <ErrorNotice title="Lost contact with the server">{driverError.message}</ErrorNotice>
      ) : null}

      {stopped === "not_implemented" ? (
        <p className="text-sm text-zinc-400">
          {status.phase === "graph"
            ? "The code is fetched, parsed and embedded, so you can search it. The dependency graph and question answering are not available yet."
            : "Part of the pipeline is not available yet."}
        </p>
      ) : null}
      {stopped === "error" && !driverError ? (
        <p className="text-sm text-zinc-400">Progress updates paused. Reload to continue.</p>
      ) : null}
    </Card>
  );
}
