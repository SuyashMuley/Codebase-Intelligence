"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ApiError } from "@/lib/api";
import {
  useIndexDriver,
  useRepo,
  useStartIndex,
  useStats,
  useStatus,
} from "@/lib/hooks";
import { formatLineRange, parseLineRange } from "@/lib/tree";
import { FilesTab } from "./files-tab";
import { SearchTab } from "./search-tab";
import { ReauthNotice } from "./reauth-notice";
import { StatsPanel } from "./stats-panel";
import { StatusPanel } from "./status-panel";
import { Button, EmptyState, ErrorNotice, Spinner } from "./ui";
import type { LineRange } from "./code-viewer";

type Tab = "overview" | "files" | "search";
const MAX_PATH = 500;

export function RepoDetail({ id }: { id: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const repo = useRepo(id);
  const status = useStatus(id);
  const start = useStartIndex(id);

  const hasSnapshot = !!status.data && status.data.state !== "not_indexed";
  const active = status.data?.state === "pending" || status.data?.state === "running";
  const driver = useIndexDriver(id, active);
  const stats = useStats(id, hasSnapshot, active);

  // The open tab, file and highlighted lines live in the URL, so a view can be shared.
  const rawFile = params.get("file");
  const file = rawFile && rawFile.length <= MAX_PATH ? rawFile : null;
  const range = parseLineRange(params.get("line"));
  const tabParam = params.get("tab");
  const tab: Tab =
    tabParam === "search"
      ? "search"
      : tabParam === "files" || (file !== null && !tabParam)
        ? "files"
        : "overview";

  function update(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  if (repo.isPending) return <Spinner label="Loading repository…" />;
  if (repo.isError) {
    const notFound = repo.error instanceof ApiError && repo.error.status === 404;
    return notFound ? (
      <EmptyState title="Repository not found">
        It may not exist, or it is not in your account.{" "}
        <Link href="/repos" className="text-emerald-400 underline">
          Back to your repositories
        </Link>
      </EmptyState>
    ) : (
      <ErrorNotice title="Could not load this repository">{repo.error.message}</ErrorNotice>
    );
  }

  const startError = start.error instanceof ApiError ? start.error : null;
  // GitHub revoked or expired the user's authorization while their session is still valid.
  const needsReauth = [startError, driver.lastError, status.error].some(
    (e) => e instanceof ApiError && e.code === "reauth_required",
  );

  const tabs: { key: Tab; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "files", label: "Files" },
    { key: "search", label: "Search" },
  ];

  return (
    <div className="space-y-6">
      <div>
        <Link href="/repos" className="text-sm text-zinc-400 hover:text-zinc-200">
          ← All repositories
        </Link>
        <h1 className="mt-2 text-xl font-semibold tracking-tight">
          {repo.data.owner}/{repo.data.name}
        </h1>
        <p className="mt-1 text-sm text-zinc-400">
          <a
            href={repo.data.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-emerald-400 hover:underline"
          >
            View on GitHub
          </a>
          {repo.data.default_branch ? ` · default branch ${repo.data.default_branch}` : ""}
        </p>
      </div>

      <div role="tablist" aria-label="Repository views" className="flex gap-1 border-b border-zinc-800">
        {tabs.map((t) => (
          <button
            key={t.key}
            role="tab"
            type="button"
            id={`tab-${t.key}`}
            aria-selected={tab === t.key}
            aria-controls={`panel-${t.key}`}
            onClick={() => update({ tab: t.key === "overview" ? null : t.key })}
            className={`-mb-px border-b-2 px-4 py-2 text-sm focus-visible:outline-2 focus-visible:outline-emerald-400 ${
              tab === t.key
                ? "border-emerald-400 text-zinc-100"
                : "border-transparent text-zinc-400 hover:text-zinc-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {needsReauth ? <ReauthNotice returnTo={`/repos/${id}`} /> : null}
      {startError && startError.code !== "reauth_required" ? (
        <ErrorNotice title="Could not start indexing">{startError.message}</ErrorNotice>
      ) : null}

      {tab === "overview" ? (
        <div role="tabpanel" id="panel-overview" aria-labelledby="tab-overview" className="space-y-6">
          {status.isPending ? <Spinner label="Loading status…" /> : null}
          {status.isError && !needsReauth ? (
            <div className="space-y-3">
              <ErrorNotice title="Could not load indexing status">{status.error.message}</ErrorNotice>
              <Button variant="secondary" onClick={() => status.refetch()}>
                Try again
              </Button>
            </div>
          ) : null}
          {status.data ? (
            <StatusPanel
              status={status.data}
              stopped={driver.stopped}
              driverError={driver.lastError}
              starting={start.isPending}
              onIndex={() => start.mutate()}
            />
          ) : null}
          {stats.data ? <StatsPanel stats={stats.data} progress={status.data?.parse ?? null} /> : null}
        </div>
      ) : tab === "search" ? (
        <div role="tabpanel" id="panel-search" aria-labelledby="tab-search">
          <SearchTab
            repoId={id}
            hasSnapshot={hasSnapshot}
            onOpen={(path, line) => update({ tab: "files", file: path, line })}
          />
        </div>
      ) : (
        <div role="tabpanel" id="panel-files" aria-labelledby="tab-files">
          <FilesTab
            repoId={id}
            hasSnapshot={hasSnapshot}
            refreshing={active}
            selected={file}
            range={range}
            onSelect={(path) => update({ tab: "files", file: path, line: null })}
            onSelectRange={(r: LineRange | null) => update({ line: formatLineRange(r) })}
          />
        </div>
      )}
    </div>
  );
}
