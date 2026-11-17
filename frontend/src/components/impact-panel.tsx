"use client";

import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useAiReview, useImpact, type ImpactTargetQuery } from "@/lib/hooks";
import type { Impact, ImpactDependent, SuggestedTest } from "@/lib/types";
import { Badge, Button, Card, ErrorNotice, Spinner } from "./ui";

const CONFIDENCE_TONE = { high: "good", medium: "info", low: "warn" } as const;

function targetLabel(t: Impact["target"]): string {
  return t.symbol ? `${t.path} › ${t.symbol}` : t.path;
}

function DependentRow({
  d,
  onOpenFile,
}: {
  d: ImpactDependent;
  onOpenFile: (path: string) => void;
}) {
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-2">
        <button
          type="button"
          onClick={() => onOpenFile(d.path)}
          className="text-left font-mono text-xs text-emerald-400 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
        >
          {d.path}
          {d.symbol ? ` › ${d.symbol}` : ""}
        </button>
      </div>
      <details className="text-xs text-zinc-500">
        <summary className="cursor-pointer">How it is reached</summary>
        <ol className="mt-1 space-y-0.5 pl-4">
          {d.hops.map((h) => (
            <li key={`${h.source}>${h.target}`} className="font-mono">
              {h.source} → {h.target} <span className="text-zinc-600">({h.kind})</span>
            </li>
          ))}
        </ol>
      </details>
    </li>
  );
}

function TestRow({ t, onOpenFile }: { t: SuggestedTest; onOpenFile: (path: string) => void }) {
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => onOpenFile(t.path)}
          className="text-left font-mono text-xs text-emerald-400 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
        >
          {t.path}
        </button>
        <Badge tone={t.heuristic ? "warn" : "good"}>{t.heuristic ? "Heuristic" : "Static fact"}</Badge>
        <Badge tone={CONFIDENCE_TONE[t.confidence]}>{t.confidence} confidence</Badge>
      </div>
      <p className="text-xs text-zinc-500">{t.reason}</p>
    </li>
  );
}

const AI_ERRORS: Record<string, string> = {
  llm_not_configured: "AI review is not set up on this server.",
  llm_rate_limited: "The free AI model is busy or out of allowance. Try again in a minute.",
  llm_unavailable: "The AI model could not be reached. Try again shortly.",
  llm_bad_response: "The AI model gave an answer that could not be used. Try again.",
  llm_auth_failed: "The server's AI credentials were rejected.",
  rate_limited: "You are asking too fast. Wait a moment and try again.",
};

function AiReviewSection({
  repoId,
  target,
  depth,
  onOpenFile,
}: {
  repoId: string;
  target: ImpactTargetQuery;
  depth: number;
  onOpenFile: (path: string) => void;
}) {
  const review = useAiReview(repoId);
  const error = review.error instanceof ApiError ? review.error : null;
  const data = review.data;
  return (
    <section aria-label="AI review" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">AI review</h3>
        <Badge tone="warn">Unverified</Badge>
      </div>
      {!data ? (
        <>
          <p className="text-xs text-zinc-500">
            Asks a free language model to read the results above and point out risks. It can be
            wrong, so treat it as a second opinion, not a fact. It can take up to half a minute.
          </p>
          <Button
            variant="secondary"
            disabled={review.isPending}
            onClick={() => review.mutate({ file: target.file, symbol: target.symbol, depth })}
          >
            {review.isPending ? "Asking…" : "Ask AI to review this change"}
          </Button>
        </>
      ) : (
        <div className="space-y-3 rounded-md border border-amber-900/60 bg-amber-500/5 p-3">
          <p role="note" className="text-xs text-amber-200">
            {data.notice}
            {data.model ? ` Model: ${data.model}.` : ""}
          </p>
          <p className="text-sm text-zinc-200">{data.summary}</p>
          {data.risks.length > 0 ? (
            <ul aria-label="AI risks" className="space-y-2">
              {data.risks.map((r) => (
                <li key={r.description} className="space-y-1">
                  <div className="flex items-start gap-2">
                    <Badge tone={CONFIDENCE_TONE[r.confidence]}>{r.confidence}</Badge>
                    <p className="text-sm text-zinc-300">{r.description}</p>
                  </div>
                  {r.paths.length > 0 ? (
                    <p className="space-x-2 pl-1 font-mono text-xs">
                      {r.paths.map((p) => (
                        <button
                          key={p}
                          type="button"
                          onClick={() => onOpenFile(p)}
                          className="text-emerald-400 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
                        >
                          {p}
                        </button>
                      ))}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          {data.tests_to_consider.length > 0 ? (
            <div>
              <p className="text-xs text-zinc-500">Tests it suggests</p>
              <ul className="space-y-1">
                {data.tests_to_consider.map((t) => (
                  <li key={t.path} className="text-xs text-zinc-400">
                    <span className="font-mono text-zinc-300">{t.path}</span> {t.reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {data.open_questions.length > 0 ? (
            <div>
              <p className="text-xs text-zinc-500">Worth checking by hand</p>
              <ul className="list-disc space-y-1 pl-5 text-xs text-zinc-400">
                {data.open_questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {data.dropped > 0 ? (
            <p className="text-xs text-zinc-500">
              {data.dropped} item{data.dropped === 1 ? "" : "s"} removed because{" "}
              {data.dropped === 1 ? "it" : "they"} named files that do not exist or are not
              relevant.
            </p>
          ) : null}
          <Button variant="ghost" onClick={() => review.reset()}>
            Clear
          </Button>
        </div>
      )}
      {error ? (
        <ErrorNotice title="AI review failed">{AI_ERRORS[error.code] ?? error.message}</ErrorNotice>
      ) : null}
    </section>
  );
}

export function ImpactPanel({
  repoId,
  target,
  onOpenFile,
}: {
  repoId: string;
  target: ImpactTargetQuery;
  onOpenFile: (path: string) => void;
}) {
  const [depth, setDepth] = useState(3);
  const impact = useImpact(repoId, target, depth, true);

  if (impact.isPending) return <Spinner label="Working out what depends on this…" />;
  if (impact.isError) {
    const err = impact.error instanceof ApiError ? impact.error : null;
    return (
      <ErrorNotice title="Could not analyse this">
        {err?.code === "graph_not_ready"
          ? "The graph is built when indexing finishes."
          : (err?.message ?? "Please try again.")}
      </ErrorNotice>
    );
  }
  const data = impact.data;
  const byDepth = new Map<number, ImpactDependent[]>();
  for (const d of data.dependents) byDepth.set(d.depth, [...(byDepth.get(d.depth) ?? []), d]);

  return (
    <Card className="space-y-5" role="region" aria-label="Change impact">
      <div className="space-y-1">
        <h2 className="text-sm font-medium text-zinc-200">If you change this</h2>
        <p className="break-all font-mono text-xs text-zinc-400">{targetLabel(data.target)}</p>
      </div>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <dl className="flex gap-6">
          <div>
            <dt className="text-xs text-zinc-500">Direct</dt>
            <dd className="text-lg font-medium">{data.direct}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Indirect</dt>
            <dd className="text-lg font-medium">{data.transitive}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Files affected</dt>
            <dd className="text-lg font-medium">{data.files_affected}</dd>
          </div>
        </dl>
        <label className="text-xs text-zinc-400">
          Follow
          <select
            aria-label="How many steps to follow"
            value={depth}
            onChange={(e) => setDepth(Number(e.target.value))}
            className="ml-2 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs"
          >
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <option key={n} value={n}>
                {n} {n === 1 ? "step" : "steps"}
              </option>
            ))}
          </select>
        </label>
      </div>

      <section aria-label="Dependents" className="space-y-3">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">Dependents</h3>
          <Badge tone="good">Static facts</Badge>
        </div>
        {data.dependents.length === 0 ? (
          <p className="text-sm text-zinc-400">
            Nothing that could be linked statically depends on this. See the unverified callers
            below before treating it as safe to change.
          </p>
        ) : (
          [...byDepth.entries()]
            .sort((a, b) => a[0] - b[0])
            .map(([depthKey, items]) => (
              <div key={depthKey}>
                <p className="mb-1 text-xs text-zinc-500">
                  {depthKey === 1 ? "Directly" : `${depthKey} steps away`}
                </p>
                <ul className="space-y-2">
                  {items.map((d) => (
                    <DependentRow key={`${d.path}::${d.symbol ?? ""}`} d={d} onOpenFile={onOpenFile} />
                  ))}
                </ul>
              </div>
            ))
        )}
        {data.truncated ? (
          <p role="note" className="text-xs text-amber-200">
            The list was cut off at its size limit.
          </p>
        ) : null}
      </section>

      <section aria-label="Suggested tests" className="space-y-3">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">Tests to run</h3>
        {data.suggested_tests.length === 0 ? (
          <p className="text-sm text-zinc-400">No test files were found for this change.</p>
        ) : (
          <ul className="space-y-2">
            {data.suggested_tests.map((t) => (
              <TestRow key={t.path} t={t} onOpenFile={onOpenFile} />
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Unverified callers" className="space-y-3">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">
            Possible callers
          </h3>
          <Badge tone="warn">Not verified</Badge>
        </div>
        {data.unverified_callers.length === 0 ? (
          <p className="text-sm text-zinc-400">No unresolved calls use this name.</p>
        ) : (
          <>
            <p className="text-xs text-zinc-500">
              These calls use the same name but could not be tied to this code (for example a
              method called on a variable). They are leads to check, not dependents.
            </p>
            <ul className="space-y-1">
              {data.unverified_callers.map((u) => (
                <li key={`${u.path}:${u.line}:${u.called_as}`} className="font-mono text-xs">
                  <button
                    type="button"
                    onClick={() => onOpenFile(u.path)}
                    className="text-amber-300 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
                  >
                    {u.path}
                    {u.line ? `:${u.line}` : ""}
                  </button>{" "}
                  <span className="text-zinc-500">
                    {u.symbol ? `in ${u.symbol} ` : ""}calls {u.called_as}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <AiReviewSection repoId={repoId} target={target} depth={depth} onOpenFile={onOpenFile} />

      <details className="text-xs text-zinc-500">
        <summary className="cursor-pointer">What this analysis can and cannot see</summary>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          {data.limitations.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      </details>
    </Card>
  );
}
