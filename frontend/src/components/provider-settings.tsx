"use client";

import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { clearBrowserKey, getBrowserKey, setBrowserKey } from "@/lib/browser-keys";
import { useProviders, useRemoveProvider, useSaveProvider } from "@/lib/hooks";
import type { ProviderCatalog, ProviderSetting, ProviderSlot } from "@/lib/types";
import { ReauthNotice } from "./reauth-notice";
import { Badge, Button, Card, ConfirmButton, ErrorNotice, Spinner } from "./ui";

const FIELD =
  "w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus-visible:outline-2 focus-visible:outline-emerald-400";

const COPY: Record<ProviderSlot, { title: string; sends: string }> = {
  embedding: {
    title: "Embeddings",
    sends:
      "Every chunk of code in a private repository is sent to this provider when it is indexed, and every search question is sent to it when you search. Only models that give 384-dimension vectors are offered.",
  },
  chat: {
    title: "Chat model",
    sends:
      "Your question and the excerpts of code retrieved for it are sent to this provider when you ask a question or request an AI review.",
  },
};

export function ProviderSettings() {
  const providers = useProviders();

  if (providers.isPending) return <Spinner label="Loading your provider settings…" />;
  if (providers.isError) {
    if (providers.error instanceof ApiError && providers.error.code === "reauth_required") {
      return <ReauthNotice returnTo="/settings/providers" />;
    }
    return (
      <div className="space-y-3">
        <ErrorNotice title="Could not load your provider settings">
          {providers.error.message}
        </ErrorNotice>
        <Button variant="secondary" onClick={() => providers.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  const catalog = providers.data;
  return (
    <div className="space-y-6">
      <Card className="space-y-2 text-sm text-zinc-300">
        <p>
          Public repositories use this app&apos;s own providers. A <strong>private</strong>{" "}
          repository is never sent to them: its code goes only to the providers you set up here,
          with your own keys. A private repository cannot be indexed until both are set.
        </p>
        <p className="text-zinc-400">
          <strong className="text-zinc-300">Stored</strong> keys are saved encrypted on the server so
          indexing works without this tab open; only the last four characters are ever shown.{" "}
          <strong className="text-zinc-300">Browser only</strong> keys are never saved on the server:
          this tab sends the key with each request and forgets it when the tab closes, so indexing
          only moves while the tab is open.
        </p>
      </Card>
      <SlotCard slot="embedding" catalog={catalog} current={catalog.embedding} />
      <SlotCard slot="chat" catalog={catalog} current={catalog.chat} />
    </div>
  );
}

function SlotCard({
  slot,
  catalog,
  current,
}: {
  slot: ProviderSlot;
  catalog: ProviderCatalog;
  current: ProviderSetting | null;
}) {
  const options = slot === "chat" ? catalog.chat_providers : catalog.embedding_providers;
  const first = options[0];
  const [kind, setKind] = useState(current?.kind ?? first.id);
  const [model, setModel] = useState(current?.model ?? "");
  const [accountId, setAccountId] = useState(current?.account_id ?? "");
  const [mode, setMode] = useState<"stored" | "browser">(current?.mode ?? "stored");
  const [key, setKey] = useState("");
  const [, bump] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const save = useSaveProvider(slot);
  const remove = useRemoveProvider(slot);

  const embedding = catalog.embedding_providers.find((p) => p.id === kind);
  const chat = catalog.chat_providers.find((p) => p.id === kind);
  const models = slot === "embedding" ? (embedding?.models ?? []) : [];
  const needsAccount = slot === "embedding" && embedding?.needs_account_id === true;
  const heldHere = getBrowserKey(slot) !== null;
  const saveError = save.error instanceof ApiError ? save.error.message : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    setProblem(null);
    const trimmed = key.trim();
    const needKey = mode === "stored" || !heldHere;
    if (needKey && trimmed.length < 8) {
      setProblem(
        mode === "stored"
          ? "Enter the API key."
          : "Enter the key again: this tab does not have it.",
      );
      return;
    }
    save.mutate(
      {
        kind,
        mode,
        ...(model.trim() ? { model: model.trim() } : {}),
        ...(needsAccount ? { account_id: accountId.trim() } : {}),
        ...(mode === "stored" ? { api_key: trimmed } : {}),
      },
      {
        onSuccess: () => {
          if (mode === "browser") {
            if (trimmed) setBrowserKey(slot, trimmed);
          } else {
            clearBrowserKey(slot); // it is stored on the server now
          }
          setKey("");
          bump((n) => n + 1);
        },
      },
    );
  }

  const copy = COPY[slot];
  const label = options.find((p) => p.id === current?.kind)?.label ?? current?.kind;

  return (
    <Card className="space-y-4" aria-labelledby={`slot-${slot}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`slot-${slot}`} className="font-medium text-zinc-100">
          {copy.title}
        </h2>
        {current ? (
          <Badge tone="good">set up</Badge>
        ) : (
          <Badge tone="warn">not set up</Badge>
        )}
      </div>
      <p className="text-sm text-zinc-400">{copy.sends}</p>

      {current ? (
        <p className="text-sm text-zinc-300" data-testid={`current-${slot}`}>
          {label}
          {current.model ? ` · ${current.model}` : ""} ·{" "}
          {current.mode === "stored"
            ? `stored key ending ${current.key_last4}`
            : heldHere
              ? "key held in this tab"
              : "key not in this tab: enter it below"}
        </p>
      ) : null}

      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" aria-label={`${copy.title} provider`}>
        <label className="space-y-1 text-sm text-zinc-300">
          Provider
          <select
            className={FIELD}
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setModel("");
            }}
          >
            {options.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        {slot === "embedding" ? (
          <label className="space-y-1 text-sm text-zinc-300">
            Model
            <select className={FIELD} value={model || models[0]} onChange={(e) => setModel(e.target.value)}>
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label className="space-y-1 text-sm text-zinc-300">
            Model
            <input
              className={FIELD}
              value={model}
              placeholder={chat?.default_model ?? ""}
              onChange={(e) => setModel(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        )}

        {needsAccount ? (
          <label className="space-y-1 text-sm text-zinc-300 sm:col-span-2">
            Cloudflare account id
            <input
              className={FIELD}
              value={accountId}
              placeholder="32 characters"
              onChange={(e) => setAccountId(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : null}

        <fieldset className="space-y-1 text-sm text-zinc-300 sm:col-span-2">
          <legend className="mb-1">Where the key lives</legend>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={`mode-${slot}`}
              checked={mode === "stored"}
              onChange={() => setMode("stored")}
              className="accent-emerald-500"
            />
            Stored, encrypted on the server (indexing works with the tab closed)
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={`mode-${slot}`}
              checked={mode === "browser"}
              onChange={() => setMode("browser")}
              className="accent-emerald-500"
            />
            Browser only (never saved on the server)
          </label>
        </fieldset>

        <label className="space-y-1 text-sm text-zinc-300 sm:col-span-2">
          API key
          <input
            className={FIELD}
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={
              current
                ? mode === "stored"
                  ? "Enter a key to replace the saved one"
                  : "Enter the key for this tab"
                : "Paste your API key"
            }
            autoComplete="off"
            spellCheck={false}
            data-1p-ignore
          />
        </label>

        {problem || saveError ? (
          <p role="alert" className="text-sm text-red-300 sm:col-span-2">
            {problem ?? saveError}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : current ? "Update" : "Save"}
          </Button>
          {current ? (
            <ConfirmButton
              label="Remove"
              confirmLabel="Remove it"
              warning="Remove this provider and its stored key? Private repositories that need it stop working."
              disabled={remove.isPending}
              onConfirm={() =>
                remove.mutate(undefined, {
                  onSuccess: () => {
                    clearBrowserKey(slot);
                    bump((n) => n + 1);
                  },
                })
              }
            />
          ) : null}
        </div>
      </form>
    </Card>
  );
}
