"use client";

import Link from "next/link";
import { ApiError } from "@/lib/api";
import { useIndexDriver, useRepo, useStartIndex, useStatus } from "@/lib/hooks";
import { Button, EmptyState, ErrorNotice, Spinner } from "./ui";
import { ReauthNotice } from "./reauth-notice";
import { StatusPanel } from "./status-panel";

export function RepoDetail({ id }: { id: string }) {
  const repo = useRepo(id);
  const status = useStatus(id);
  const start = useStartIndex(id);

  const active = status.data?.state === "pending" || status.data?.state === "running";
  const driver = useIndexDriver(id, active);

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

      {needsReauth ? <ReauthNotice returnTo={`/repos/${id}`} /> : null}
      {startError && startError.code !== "reauth_required" ? (
        <ErrorNotice title="Could not start indexing">{startError.message}</ErrorNotice>
      ) : null}

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
    </div>
  );
}
