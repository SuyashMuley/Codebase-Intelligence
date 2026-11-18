"use client";

import { useRef, useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { useAsk } from "@/lib/hooks";
import type { ChatAnswer, ChatTurn } from "@/lib/types";
import { Badge, Button, Card, EmptyState, ErrorNotice } from "./ui";

const MAX_HISTORY_TURNS = 6;

const CONFIDENCE: Record<ChatAnswer["confidence"], { tone: "good" | "info" | "warn" | "neutral"; text: string }> = {
  high: { tone: "good", text: "Strong match, every statement checked" },
  medium: { tone: "info", text: "Checked, but the match was moderate or needed a retry" },
  low: { tone: "warn", text: "Some statements were removed or the match was weak" },
  none: { tone: "neutral", text: "No answer" },
};

const REASONS: Record<string, string> = {
  no_relevant_code:
    "Nothing in this repository was close enough to the question to answer from.",
  embeddings_not_ready: "Search is not ready for this repository yet.",
  model_declined: "The retrieved code does not contain the answer.",
  no_supported_claims: "No statement could be tied to a quotation from the code.",
};

const ERRORS: Record<string, string> = {
  chat_quota_exceeded: "You have used today's questions for this repository. Try again tomorrow.",
  rate_limited: "You are asking too fast. Wait a moment and try again.",
  llm_rate_limited: "The free AI model is busy or out of allowance. Try again in a minute.",
  llm_unavailable: "The AI model was too slow or could not be reached. Try again.",
  llm_bad_response: "The AI model gave an answer that could not be used. Try again.",
  llm_not_configured: "Asking questions is not set up on this server.",
  llm_auth_failed: "The server's AI credentials were rejected.",
  snapshot_not_ready: "This repository is still being indexed.",
};

type Message =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; answer: ChatAnswer };

export function lineRange(c: { start_line: number; end_line: number }): string {
  return c.start_line === c.end_line ? `${c.start_line}` : `${c.start_line}-${c.end_line}`;
}

/** What the model is told about the earlier conversation: plain text, never citations. */
export function toHistory(messages: Message[]): ChatTurn[] {
  return messages.slice(-MAX_HISTORY_TURNS).map((m) =>
    m.role === "user"
      ? { role: "user", content: m.text }
      : {
          role: "assistant",
          content: m.answer.insufficient_evidence
            ? "I could not determine this from the code."
            : m.answer.claims.map((c) => c.text).join(" "),
        },
  );
}

function AnswerView({
  answer,
  onOpen,
}: {
  answer: ChatAnswer;
  onOpen: (path: string, line: string) => void;
}) {
  if (answer.insufficient_evidence) {
    return (
      <div className="space-y-2">
        <p className="text-sm font-medium text-zinc-200">I cannot determine this from the code.</p>
        <p className="text-sm text-zinc-400">
          {answer.missing || REASONS[answer.reason ?? ""] || REASONS.no_relevant_code}
        </p>
        {answer.dropped_claims > 0 ? (
          <p className="text-xs text-zinc-500">
            {answer.dropped_claims} statement{answer.dropped_claims === 1 ? "" : "s"} the model
            wrote could not be tied to the code and {answer.dropped_claims === 1 ? "was" : "were"}{" "}
            removed.
          </p>
        ) : null}
      </div>
    );
  }
  const conf = CONFIDENCE[answer.confidence];
  const byNumber = new Map(answer.citations.map((c) => [c.n, c]));
  return (
    <div className="space-y-3">
      <ul aria-label="Answer" className="space-y-2">
        {answer.claims.map((claim) => (
          <li key={claim.text} className="text-sm text-zinc-200">
            {claim.text}{" "}
            {claim.cites.map((n) => {
              const c = byNumber.get(n);
              return c ? (
                <button
                  key={n}
                  type="button"
                  onClick={() => onOpen(c.path, lineRange(c))}
                  aria-label={`Open ${c.path} lines ${lineRange(c)}`}
                  className="mx-0.5 rounded bg-emerald-500/15 px-1.5 text-xs font-medium text-emerald-300 hover:bg-emerald-500/30 focus-visible:outline-2 focus-visible:outline-emerald-400"
                >
                  {n}
                </button>
              ) : null;
            })}
          </li>
        ))}
      </ul>
      <ol aria-label="Sources" className="space-y-0.5 text-xs">
        {answer.citations.map((c) => (
          <li key={c.n} className="font-mono">
            <span className="text-zinc-500">[{c.n}] </span>
            <button
              type="button"
              onClick={() => onOpen(c.path, lineRange(c))}
              className="max-w-full break-all text-left text-emerald-400 hover:underline focus-visible:outline-2 focus-visible:outline-emerald-400"
            >
              {c.path}:{lineRange(c)}
            </button>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={conf.tone}>{answer.confidence} confidence</Badge>
        <span className="text-xs text-zinc-500">{conf.text}</span>
      </div>
      {answer.notes.map((n) => (
        <p key={n} role="note" className="rounded bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          {n}
        </p>
      ))}
      {answer.missing ? (
        <p className="text-xs text-zinc-500">Not covered by the code shown: {answer.missing}</p>
      ) : null}
      <p className="text-xs text-zinc-600">
        Each statement&apos;s quotation was found in the cited lines. That ties it to real code;
        it does not prove the statement reads the code correctly.
        {answer.model ? ` Model: ${answer.model}.` : ""}
      </p>
    </div>
  );
}

export function ChatTab({
  repoId,
  ready,
  onOpen,
}: {
  repoId: string;
  ready: boolean;
  onOpen: (path: string, line: string) => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const nextId = useRef(1);
  const ask = useAsk(repoId);

  if (!ready) {
    return (
      <EmptyState title="Questions open when indexing finishes">
        Answers are written from the indexed code. Check the Overview tab for progress.
      </EmptyState>
    );
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const question = text.trim();
    if (!question || ask.isPending) return;
    const history = toHistory(messages);
    setMessages((m) => [...m, { id: nextId.current++, role: "user", text: question }]);
    setText("");
    ask.mutate(
      { question, history },
      {
        onSuccess: (answer) =>
          setMessages((m) => [...m, { id: nextId.current++, role: "assistant", answer }]),
      },
    );
  }

  const error = ask.error instanceof ApiError ? ask.error : null;

  return (
    <div className="space-y-4">
      {messages.length === 0 ? (
        <Card>
          <p className="text-sm text-zinc-400">
            Ask about this code, for example &quot;where is `get_netrc_auth` defined?&quot; or
            &quot;how are cookies kept between requests?&quot;. Answers cite the lines they come
            from, and when the code does not say, the answer says so.
          </p>
        </Card>
      ) : (
        <ol aria-label="Conversation" className="space-y-3">
          {messages.map((m) => (
            <li key={m.id}>
              {m.role === "user" ? (
                <p className="rounded-lg bg-zinc-800/60 px-4 py-2 text-sm text-zinc-100">{m.text}</p>
              ) : (
                <Card className="p-4">
                  <AnswerView answer={m.answer} onOpen={onOpen} />
                </Card>
              )}
            </li>
          ))}
        </ol>
      )}

      {ask.isPending ? (
        <p role="status" className="text-sm text-zinc-400">
          Reading the code… this can take up to half a minute.
        </p>
      ) : null}
      {error ? (
        <ErrorNotice title="Could not answer">{ERRORS[error.code] ?? error.message}</ErrorNotice>
      ) : null}

      <form onSubmit={submit} className="flex flex-wrap gap-2" aria-label="Ask a question">
        <input
          type="text"
          value={text}
          maxLength={1000}
          onChange={(e) => setText(e.target.value)}
          aria-label="Your question"
          placeholder="Ask about this code"
          className="w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-emerald-400 sm:w-auto sm:flex-1"
        />
        <Button type="submit" disabled={ask.isPending || !text.trim()}>
          {ask.isPending ? "Asking…" : "Ask"}
        </Button>
        {messages.length > 0 ? (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setMessages([]);
              ask.reset();
            }}
          >
            New conversation
          </Button>
        ) : null}
      </form>
    </div>
  );
}
