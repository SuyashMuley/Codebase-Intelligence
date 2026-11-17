"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { checkGithubUrl } from "@/lib/github-url";
import { useConnectRepo } from "@/lib/hooks";
import { Button } from "./ui";

export function ConnectForm() {
  const router = useRouter();
  const connect = useConnectRepo();
  const [url, setUrl] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const check = checkGithubUrl(url);
    if (!check.ok) {
      setProblem(check.reason);
      return;
    }
    setProblem(null);
    connect.mutate(url.trim(), {
      onSuccess: (repo) => router.push(`/repos/${repo.id}`),
    });
  }

  const serverProblem = connect.error instanceof ApiError ? connect.error.message : null;
  const shown = problem ?? serverProblem;

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-2">
      <label htmlFor="repo-url" className="block text-sm font-medium text-zinc-200">
        Connect a public GitHub repository
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id="repo-url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://github.com/pallets/flask"
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setProblem(null);
          }}
          aria-invalid={shown ? true : undefined}
          aria-describedby={shown ? "repo-url-error" : undefined}
          className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus-visible:outline-2 focus-visible:outline-emerald-400"
        />
        <Button type="submit" disabled={connect.isPending}>
          {connect.isPending ? "Connecting…" : "Connect"}
        </Button>
      </div>
      {shown ? (
        <p id="repo-url-error" role="alert" className="text-sm text-red-300">
          {shown}
        </p>
      ) : null}
    </form>
  );
}
