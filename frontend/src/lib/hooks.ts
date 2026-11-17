"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, ApiError, post } from "./api";
import { decideNext, type StopReason } from "./tick-policy";
import type {
  FileDetail,
  FileList,
  GithubRepoList,
  IndexStartResult,
  IndexStatus,
  Me,
  Repository,
  RepositoryList,
  SearchMode,
  SearchResponse,
  Stats,
  TickResult,
} from "./types";

export const keys = {
  me: ["me"] as const,
  repos: ["repos"] as const,
  repo: (id: string) => ["repo", id] as const,
  status: (id: string) => ["status", id] as const,
  githubRepos: ["github-repos"] as const,
  files: (id: string) => ["files", id] as const,
  file: (id: string, path: string) => ["file", id, path] as const,
  stats: (id: string) => ["stats", id] as const,
};

export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => api<Me>("/api/me"),
    retry: false,
    staleTime: 60_000,
  });
}

export function useRepos() {
  return useQuery({
    queryKey: keys.repos,
    queryFn: () => api<RepositoryList>("/api/repositories?limit=100"),
  });
}

/** The signed-in user's own public GitHub repositories, for the "pick one" list. */
export function useGithubRepos() {
  return useQuery({
    queryKey: keys.githubRepos,
    queryFn: () => api<GithubRepoList>("/api/github/repos"),
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useRepo(id: string) {
  return useQuery({
    queryKey: keys.repo(id),
    queryFn: () => api<Repository>(`/api/repositories/${id}`),
    retry: false,
  });
}

export function useStatus(id: string) {
  return useQuery({
    queryKey: keys.status(id),
    queryFn: () => api<IndexStatus>(`/api/repositories/${id}/status`),
    retry: false,
  });
}

/** Files of the newest snapshot. Refreshes while indexing is still running. */
export function useFiles(id: string, enabled: boolean, refreshing: boolean) {
  return useQuery({
    queryKey: keys.files(id),
    queryFn: () => api<FileList>(`/api/repositories/${id}/files`),
    enabled,
    retry: false,
    refetchInterval: refreshing ? 5_000 : false,
  });
}

export function useFile(id: string, path: string | null) {
  return useQuery({
    queryKey: keys.file(id, path ?? ""),
    queryFn: () => api<FileDetail>(`/api/repositories/${id}/files/${encodeFilePath(path ?? "")}`),
    enabled: path !== null,
    retry: false,
    staleTime: 60_000,
  });
}

export function useStats(id: string, enabled: boolean, refreshing: boolean) {
  return useQuery({
    queryKey: keys.stats(id),
    queryFn: () => api<Stats>(`/api/repositories/${id}/stats`),
    enabled,
    retry: false,
    refetchInterval: refreshing ? 5_000 : false,
  });
}

/** A repository path as a URL path: each segment encoded, the slashes kept. */
export function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function useConnectRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => post<Repository>("/api/repositories", { url }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.repos }),
  });
}

export function useStartIndex(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => post<IndexStartResult>(`/api/repositories/${id}/index`),
    onSuccess: (result) => qc.setQueryData(keys.status(id), result.status),
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => post<void>("/api/auth/logout"),
    onSettled: () => qc.clear(),
  });
}

export type DriverStop = StopReason | "error";

/**
 * Drives an indexing job forward while the page is open. The backend runs one bounded step per
 * tick call (there is no background worker on the free tier), so this loop is what keeps a job
 * moving. It stops when the job finishes, fails, needs a phase that is not built yet, or the
 * API keeps erroring.
 */
export function useIndexDriver(repoId: string, active: boolean) {
  const qc = useQueryClient();
  const [stopped, setStopped] = useState<DriverStop | null>(null);
  const [lastError, setLastError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let consecutiveErrors = 0;
    let lastPhase: string | null = null;

    const step = async () => {
      try {
        const result = await post<TickResult>(`/api/repositories/${repoId}/index/tick`);
        if (cancelled) return;
        consecutiveErrors = 0;
        setLastError(null);
        qc.setQueryData(keys.status(repoId), result.status);
        const next = decideNext(result);
        // What was indexed changes when a phase finishes, so refresh the explorer and statistics
        // at those moments (and when the driver stops) rather than waiting for a timer.
        if (next.action === "stop" || result.status.phase !== lastPhase) {
          qc.invalidateQueries({ queryKey: keys.stats(repoId) });
          qc.invalidateQueries({ queryKey: keys.files(repoId) });
        }
        lastPhase = result.status.phase;
        if (next.action === "stop") {
          setStopped(next.reason);
          return;
        }
        timer = setTimeout(step, next.delayMs);
      } catch (e) {
        if (cancelled) return;
        const err = e instanceof ApiError ? e : new ApiError(0, "network_error", "Network error.");
        setLastError(err);
        if (err.needsSignIn) {
          qc.invalidateQueries({ queryKey: keys.me });
          setStopped("error");
          return;
        }
        if (err.code === "no_active_job") {
          qc.invalidateQueries({ queryKey: keys.status(repoId) });
          setStopped("no_job");
          return;
        }
        consecutiveErrors += 1;
        if (consecutiveErrors >= 5) {
          setStopped("error");
          return;
        }
        timer = setTimeout(step, Math.min(30_000, 2_000 * 2 ** consecutiveErrors));
      }
    };
    void step();

    return () => {
      cancelled = true;
      clearTimeout(timer);
      // Reset for the next run. Done here, not at the top of the effect, so the effect body
      // never sets state synchronously.
      setStopped(null);
      setLastError(null);
    };
  }, [repoId, active, qc]);

  return { stopped, lastError };
}

/** True once `pending` has lasted longer than `afterMs`: used to explain free-tier cold starts. */
export function useSlowFlag(pending: boolean, afterMs = 2_500): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => setSlow(true), afterMs);
    return () => {
      clearTimeout(t);
      setSlow(false);
    };
  }, [pending, afterMs]);
  return pending && slow;
}

/** One search request. A mutation, not a query: each call embeds the question (rate limited). */
export function useSearch(repoId: string) {
  return useMutation({
    mutationFn: (input: { query: string; mode: SearchMode }) =>
      post<SearchResponse>(`/api/repositories/${repoId}/search`, { ...input, limit: 10 }),
  });
}
