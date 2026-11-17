"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { useMe } from "@/lib/hooks";
import { Card, ErrorNotice } from "./ui";

export const LOGIN_ERRORS: Record<string, string> = {
  invalid_state: "That sign-in link expired or was already used. Please try again.",
  access_denied: "You cancelled the GitHub sign-in.",
  github_auth_failed: "GitHub rejected the sign-in. Please try again.",
  github_unavailable: "GitHub could not be reached. Please try again in a moment.",
  github_error: "GitHub reported a problem. Please try again.",
  auth_not_configured: "Sign-in is not configured on this server yet.",
};

/** Same-site relative paths only, so ?next= can never send someone to another site. */
export function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/repos";
  }
  return value;
}

export function LoginCard() {
  const params = useSearchParams();
  const router = useRouter();
  const me = useMe();
  const error = params.get("error");
  const next = safeNext(params.get("next"));

  useEffect(() => {
    if (me.isSuccess) router.replace(next);
  }, [me.isSuccess, router, next]);

  return (
    <Card className="w-full max-w-md">
      <h1 className="text-xl font-semibold tracking-tight">Codebase Intelligence</h1>
      <p className="mt-2 text-sm text-zinc-400">
        Connect a public GitHub repository, index its source, and explore how it fits together.
        Sign in with GitHub to get your own workspace and your own GitHub rate limit.
      </p>
      {error ? (
        <div className="mt-4">
          <ErrorNotice title="Could not sign you in">
            {LOGIN_ERRORS[error] ?? "Something went wrong. Please try again."}
          </ErrorNotice>
        </div>
      ) : null}
      {/* A plain link, not <Link>: this is a full-page navigation to the API, not a route. */}
      <a
        href={`/api/auth/github/login?next=${encodeURIComponent(next)}`}
        className="mt-6 inline-flex w-full items-center justify-center rounded-md bg-emerald-500 px-4 py-2.5 text-sm font-medium text-zinc-950 hover:bg-emerald-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400"
      >
        Continue with GitHub
      </a>
      <p className="mt-4 text-xs text-zinc-500">
        We read only your public profile (name and avatar). No email, no access to your private
        code. The free-tier server sleeps when idle, so signing in after a quiet spell can take up
        to a minute.
      </p>
    </Card>
  );
}
