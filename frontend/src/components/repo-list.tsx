"use client";

import Link from "next/link";
import { useRepos } from "@/lib/hooks";
import { formatDate } from "@/lib/format";
import { Button, Card, EmptyState, ErrorNotice, Spinner } from "./ui";

export function RepoList() {
  const repos = useRepos();

  if (repos.isPending) return <Spinner label="Loading your repositories…" />;
  if (repos.isError) {
    return (
      <div className="space-y-3">
        <ErrorNotice title="Could not load your repositories">{repos.error.message}</ErrorNotice>
        <Button variant="secondary" onClick={() => repos.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  if (repos.data.items.length === 0) {
    return (
      <EmptyState title="No repositories yet">
        Paste a public GitHub URL above, or pick one of your own repositories below.
      </EmptyState>
    );
  }
  return (
    <ul className="grid gap-3 sm:grid-cols-2" aria-label="Your repositories">
      {repos.data.items.map((repo) => (
        <li key={repo.id}>
          <Link href={`/repos/${repo.id}`} className="block rounded-lg focus-visible:outline-2 focus-visible:outline-emerald-400">
            <Card className="transition-colors hover:border-zinc-600">
              <p className="truncate font-medium text-zinc-100">
                {repo.owner}/{repo.name}
              </p>
              <p className="mt-1 text-xs text-zinc-500">
                Connected {formatDate(repo.created_at)}
                {repo.default_branch ? ` · ${repo.default_branch}` : ""}
              </p>
            </Card>
          </Link>
        </li>
      ))}
    </ul>
  );
}
