"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useConnectRepo, useGithubRepos, useRepos } from "@/lib/hooks";
import type { GithubRepo } from "@/lib/types";
import { ReauthNotice } from "./reauth-notice";
import { Badge, Button, EmptyState, ErrorNotice, Spinner } from "./ui";

const PAGE = 30;

function key(owner: string, name: string): string {
  return `${owner}/${name}`.toLowerCase();
}

function matches(repo: GithubRepo, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [repo.name, repo.description, repo.language].some((v) => v?.toLowerCase().includes(q));
}

function updated(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short" });
}

export function GithubRepoPicker() {
  const router = useRouter();
  const github = useGithubRepos();
  const mine = useRepos();
  const connect = useConnectRepo();
  const [query, setQuery] = useState("");
  const [hideForks, setHideForks] = useState(true);
  const [shown, setShown] = useState(PAGE);

  if (github.isPending) return <Spinner label="Loading your GitHub repositories…" />;
  if (github.isError) {
    if (github.error instanceof ApiError && github.error.code === "reauth_required") {
      return <ReauthNotice returnTo="/repos" />;
    }
    return (
      <div className="space-y-3">
        <ErrorNotice title="Could not load your GitHub repositories">
          {github.error.message}
        </ErrorNotice>
        <Button variant="secondary" onClick={() => github.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const all = github.data.items;
  if (all.length === 0) {
    return (
      <EmptyState title="No public repositories on your GitHub account">
        You can still connect any public repository by pasting its URL above.
      </EmptyState>
    );
  }

  const connected = new Set((mine.data?.items ?? []).map((r) => key(r.owner, r.name)));
  const visible = all.filter((r) => matches(r, query) && !(hideForks && r.fork && !query.trim()));
  const page = visible.slice(0, shown);
  const connectError = connect.error instanceof ApiError ? connect.error.message : null;
  const connecting = connect.isPending ? connect.variables : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <label className="sr-only" htmlFor="gh-filter">
          Filter your GitHub repositories
        </label>
        <input
          id="gh-filter"
          type="search"
          placeholder="Filter by name, description or language"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setShown(PAGE);
          }}
          className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus-visible:outline-2 focus-visible:outline-emerald-400"
        />
        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={hideForks}
            onChange={(e) => setHideForks(e.target.checked)}
            className="h-4 w-4 accent-emerald-500"
          />
          Hide forks
        </label>
      </div>

      {connectError ? <ErrorNotice title="Could not connect that repository">{connectError}</ErrorNotice> : null}

      {page.length === 0 ? (
        <EmptyState title="Nothing matches">
          {hideForks ? "Try clearing the filter, or untick “Hide forks”." : "Try a different filter."}
        </EmptyState>
      ) : (
        <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800" aria-label="Your GitHub repositories">
          {page.map((repo) => {
            const url = `https://github.com/${repo.owner}/${repo.name}`;
            const isConnected = connected.has(key(repo.owner, repo.name));
            return (
              <li key={url} className="flex items-start justify-between gap-4 p-4">
                <div className="min-w-0">
                  <p className="flex flex-wrap items-center gap-2 font-medium text-zinc-100">
                    <span className="truncate">
                      {repo.owner}/{repo.name}
                    </span>
                    {repo.fork ? <Badge tone="neutral">fork</Badge> : null}
                    {repo.archived ? <Badge tone="warn">archived</Badge> : null}
                  </p>
                  {repo.description ? (
                    <p className="mt-1 text-sm text-zinc-400">{repo.description}</p>
                  ) : null}
                  <p className="mt-1 text-xs text-zinc-500">
                    {[repo.language, repo.stars > 0 ? `★ ${repo.stars}` : null, updated(repo.pushed_at) && `Updated ${updated(repo.pushed_at)}`]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </div>
                <Button
                  variant="secondary"
                  className="shrink-0"
                  disabled={isConnected || connect.isPending}
                  aria-label={isConnected ? `${repo.name} is already connected` : `Connect ${repo.name}`}
                  onClick={() =>
                    connect.mutate(url, { onSuccess: (r) => router.push(`/repos/${r.id}`) })
                  }
                >
                  {isConnected ? "Connected" : connecting === url ? "Connecting…" : "Connect"}
                </Button>
              </li>
            );
          })}
        </ul>
      )}

      {visible.length > page.length ? (
        <Button variant="ghost" onClick={() => setShown((n) => n + PAGE)}>
          Show more ({visible.length - page.length} left)
        </Button>
      ) : null}
    </div>
  );
}
