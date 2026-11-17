"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { useMe, useSlowFlag } from "@/lib/hooks";
import { Button, ErrorNotice, Spinner } from "./ui";
import { AppShell } from "./app-shell";

/** Renders children only for a signed-in user; otherwise sends them to sign in. */
export function AuthGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const me = useMe();
  const slow = useSlowFlag(me.isPending);

  const needsSignIn = me.isError && "needsSignIn" in me.error && me.error.needsSignIn === true;
  useEffect(() => {
    if (needsSignIn) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [needsSignIn, router, pathname]);

  if (me.isPending || needsSignIn) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
        <Spinner label={slow ? "Waking up the server…" : "Checking your session…"} />
        {slow ? (
          <p className="max-w-sm text-center text-sm text-zinc-500">
            The free-tier server sleeps when idle, so the first request can take up to a minute.
          </p>
        ) : null}
      </div>
    );
  }
  if (me.isError) {
    return (
      <div className="mx-auto w-full max-w-md p-8">
        <ErrorNotice title="Could not load your account">{me.error.message}</ErrorNotice>
        <Button className="mt-4" variant="secondary" onClick={() => me.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  return <AppShell me={me.data}>{children}</AppShell>;
}
