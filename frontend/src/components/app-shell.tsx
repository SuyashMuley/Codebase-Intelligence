"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useLogout } from "@/lib/hooks";
import type { Me } from "@/lib/types";
import { Button } from "./ui";

export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  const router = useRouter();
  const logout = useLogout();

  return (
    <>
      <header className="border-b border-zinc-800 bg-zinc-950/80 backdrop-blur">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-4 py-3">
          <Link href="/repos" className="whitespace-nowrap font-semibold tracking-tight text-zinc-100">
            Codebase Intelligence
          </Link>
          <div className="flex shrink-0 items-center gap-2 sm:gap-3">
            {me.avatar_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={me.avatar_url}
                alt=""
                width={28}
                height={28}
                referrerPolicy="no-referrer"
                className="h-7 w-7 rounded-full border border-zinc-700"
              />
            ) : null}
            <Link
              href="/settings/providers"
              className="whitespace-nowrap rounded-md px-2 py-1 text-sm text-zinc-300 hover:bg-zinc-800 focus-visible:outline-2 focus-visible:outline-emerald-400"
            >
              Providers
            </Link>
            <span className="hidden text-sm text-zinc-300 sm:inline">{me.login}</span>
            <Button
              variant="ghost"
              className="whitespace-nowrap px-2 sm:px-4"
              disabled={logout.isPending}
              onClick={() => logout.mutate(undefined, { onSettled: () => router.replace("/login") })}
            >
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">{children}</main>
    </>
  );
}
