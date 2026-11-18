"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { useConnectRepo, useGithubPrivateRepos, useRepos } from "@/lib/hooks";
import { PrivateDataNotice } from "./private-data-notice";
import { ReauthNotice } from "./reauth-notice";
import { Badge, Button, EmptyState, ErrorNotice, Spinner } from "./ui";

function key(owner: string, name: string): string {
  return `${owner}/${name}`.toLowerCase();
}

/**
 * Private repositories the GitHub App can see. A private repository is yours alone: it is never
 * shared with anyone, and (once provider settings exist) its code is only ever sent to
 * providers you supply.
 */
export function PrivateRepoPicker() {
  const router = useRouter();
  const privates = useGithubPrivateRepos();
  const mine = useRepos();
  const connect = useConnectRepo();
  const [acknowledged, setAcknowledged] = useState(false);

  if (privates.isPending) return <Spinner label="Looking for private repositories…" />;
  if (privates.isError) {
    if (privates.error instanceof ApiError && privates.error.code === "reauth_required") {
      return <ReauthNotice returnTo="/repos" />;
    }
    return (
      <div className="space-y-3">
        <ErrorNotice title="Could not load your private repositories">
          {privates.error.message}
        </ErrorNotice>
        <Button variant="secondary" onClick={() => privates.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const { items, install_url: installUrl, retention_days: retentionDays } = privates.data;
  const connected = new Set((mine.data?.items ?? []).map((r) => key(r.owner, r.name)));
  const connectError = connect.error instanceof ApiError ? connect.error.message : null;
  const connecting = connect.isPending ? connect.variables : null;
  const installLink = installUrl ? (
    <a
      href={installUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="text-emerald-400 underline"
    >
      Install the GitHub App on a repository
    </a>
  ) : (
    "Install the GitHub App on a repository from its page on GitHub"
  );

  return (
    <div className="space-y-4">
      <PrivateDataNotice
        retentionDays={retentionDays}
        acknowledged={acknowledged}
        onAcknowledge={setAcknowledged}
      />
      {connectError ? (
        <ErrorNotice title="Could not connect that repository">{connectError}</ErrorNotice>
      ) : null}

      {items.length === 0 ? (
        <EmptyState title="No private repositories available">
          {installLink} to see it here. Only repositories you can access and the app is installed on
          are listed.
        </EmptyState>
      ) : (
        <>
          <ul
            className="divide-y divide-zinc-800 rounded-lg border border-zinc-800"
            aria-label="Your private repositories"
          >
            {items.map((repo) => {
              const url = `https://github.com/${repo.owner}/${repo.name}`;
              const isConnected = connected.has(key(repo.owner, repo.name));
              return (
                <li key={url} className="flex items-start justify-between gap-4 p-4">
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2 font-medium text-zinc-100">
                      <span className="truncate">
                        {repo.owner}/{repo.name}
                      </span>
                      <Badge tone="warn">private</Badge>
                    </p>
                    {repo.description ? (
                      <p className="mt-1 text-sm text-zinc-400">{repo.description}</p>
                    ) : null}
                  </div>
                  <Button
                    variant="secondary"
                    className="shrink-0"
                    disabled={isConnected || connect.isPending || !acknowledged}
                    title={acknowledged || isConnected ? undefined : "Read the notice above and tick the box first"}
                    aria-label={
                      isConnected ? `${repo.name} is already connected` : `Connect ${repo.name}`
                    }
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
          <p className="text-sm text-zinc-500">{installLink} to add more.</p>
        </>
      )}
    </div>
  );
}
