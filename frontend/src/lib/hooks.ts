"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, ApiError, post } from "./api";
import { decideNext, type StopReason } from "./tick-policy";
import type {
  IndexStartResult,
  IndexStatus,
  Me,
  Repository,
  RepositoryList,
  TickResult,
} from "./types";

export const keys = {
  me: ["me"] as const,
  repos: ["repos"] as const,
  repo: (id: string) => ["repo", id] as const,
  status: (id: string) => ["status", id] as const,
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

    const step = async () => {
      try {
        const result = await post<TickResult>(`/api/repositories/${repoId}/index/tick`);
        if (cancelled) return;
        consecutiveErrors = 0;
        setLastError(null);
        qc.setQueryData(keys.status(repoId), result.status);
        const next = decideNext(result);
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
