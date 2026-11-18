"use client";

import Link from "next/link";
import { useProviders } from "@/lib/hooks";
import type { ProviderCatalog, ProviderSetting } from "@/lib/types";

function describe(setting: ProviderSetting | null, catalog: ProviderCatalog | undefined): string | null {
  if (!setting || !catalog) return null;
  const list = setting.slot === "chat" ? catalog.chat_providers : catalog.embedding_providers;
  const label = list.find((p) => p.id === setting.kind)?.label ?? setting.kind;
  return setting.model ? `${label}, ${setting.model}` : label;
}

function Provider({
  what,
  setting,
  catalog,
  loading,
  failed,
}: {
  what: string;
  setting: ProviderSetting | null;
  catalog: ProviderCatalog | undefined;
  loading: boolean;
  failed: boolean;
}) {
  const named = describe(setting, catalog);
  if (loading) return <span className="text-zinc-500">(checking your settings…)</span>;
  if (failed) return <span className="text-zinc-500">(could not read your settings)</span>;
  if (named) return <span className="font-medium text-zinc-100">({named})</span>;
  return (
    <span className="text-amber-300">
      (not set up: indexing stays blocked until you add your {what} provider)
    </span>
  );
}

/**
 * What happens to a private repository, in plain language, with the user's own providers named.
 * Shown before connecting one (with an acknowledgement) and, folded, on the repository's page.
 */
export function PrivateDataNotice({
  retentionDays,
  acknowledged,
  onAcknowledge,
  compact = false,
}: {
  retentionDays?: number | null;
  acknowledged?: boolean;
  onAcknowledge?: (value: boolean) => void;
  compact?: boolean;
}) {
  const providers = useProviders();
  const loading = providers.isPending;
  const failed = providers.isError;
  const catalog = providers.data;
  const modes = [catalog?.embedding?.mode, catalog?.chat?.mode];
  const anyStored = modes.includes("stored");
  const anyBrowser = modes.includes("browser");

  const body = (
    <>
      <ul className="space-y-2">
        <li>
          <strong className="text-zinc-100">Who can see it.</strong> Only you. It is never shared with
          other users and never used to answer anyone else&apos;s questions. This app&apos;s own
          embedding and chat providers are never used for it.
        </li>
        <li>
          <strong className="text-zinc-100">GitHub.</strong> The GitHub App reads the repository
          (read-only) to download one snapshot of its source. GitHub is asked again whenever you use
          it, so removing the app or your access stops it.
        </li>
        <li>
          <strong className="text-zinc-100">This app&apos;s database.</strong> The source text
          (values that look like secrets are replaced before saving), its symbols, a dependency graph
          and search vectors are stored.{" "}
          {retentionDays
            ? `They are deleted automatically ${retentionDays} days after indexing, or at once when you Disconnect.`
            : "They are kept until you Disconnect, which deletes them at once."}{" "}
          Database backups, if the operator keeps any, are not erased by that.
        </li>
        <li>
          <strong className="text-zinc-100">Your embedding provider</strong>{" "}
          <Provider what="embedding" setting={catalog?.embedding ?? null} catalog={catalog} loading={loading} failed={failed} />
          . Every chunk of the code is sent there when it is indexed, and every search question when
          you search.
        </li>
        <li>
          <strong className="text-zinc-100">Your chat provider</strong>{" "}
          <Provider what="chat" setting={catalog?.chat ?? null} catalog={catalog} loading={loading} failed={failed} />
          . Your question and the code excerpts found for it are sent there when you ask a question
          or request an AI review.
        </li>
        {anyStored || anyBrowser ? (
          <li>
            <strong className="text-zinc-100">Your keys.</strong>{" "}
            {anyStored
              ? "Stored keys are encrypted on this server and decrypted only for the request that needs them. "
              : ""}
            {anyBrowser
              ? "Browser-only keys are never saved on the server: this tab sends them with each request and forgets them when it closes."
              : ""}
          </li>
        ) : null}
        <li>
          <strong className="text-zinc-100">Their rules.</strong> What your providers do with what
          you send them is set by their own terms, not by this app. Check those terms before sending
          private code.
        </li>
      </ul>
      <p>
        Choose or change providers in{" "}
        <Link href="/settings/providers" className="text-emerald-400 underline">
          provider settings
        </Link>
        .
      </p>
      {onAcknowledge ? (
        <label className="flex items-start gap-2 text-zinc-100">
          <input
            type="checkbox"
            checked={acknowledged ?? false}
            onChange={(e) => onAcknowledge(e.target.checked)}
            className="mt-0.5 h-4 w-4 accent-emerald-500"
          />
          I understand where my private code goes.
        </label>
      ) : null}
    </>
  );

  if (compact) {
    return (
      <details className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-4 text-sm text-zinc-300">
        <summary className="cursor-pointer text-zinc-100">Where this repository&apos;s data goes</summary>
        <div className="mt-3 space-y-3">{body}</div>
      </details>
    );
  }
  return (
    <section
      aria-labelledby="private-notice-heading"
      className="space-y-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-zinc-300"
    >
      <h3 id="private-notice-heading" className="font-medium text-zinc-100">
        Before you connect a private repository
      </h3>
      {body}
    </section>
  );
}
