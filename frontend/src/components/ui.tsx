"use client";

import { useState } from "react";
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" }) {
  const styles = {
    primary: "bg-emerald-500 text-zinc-950 hover:bg-emerald-400 disabled:bg-emerald-500/40",
    secondary: "border border-zinc-700 text-zinc-100 hover:bg-zinc-800 disabled:opacity-50",
    ghost: "text-zinc-300 hover:bg-zinc-800 disabled:opacity-50",
  }[variant];
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 disabled:cursor-not-allowed ${styles} ${className}`}
    />
  );
}

export function Card({
  children,
  className = "",
  ...rest
}: { children: ReactNode; className?: string } & HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      {...rest}
      className={`rounded-lg border border-zinc-800 bg-zinc-900/60 p-5 ${className}`}
    >
      {children}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-3 text-sm text-zinc-400">
      <span
        aria-hidden
        className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-600 border-t-emerald-400"
      />
      <span>{label ?? "Loading…"}</span>
    </div>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-zinc-700 px-6 py-10 text-center">
      <p className="font-medium text-zinc-200">{title}</p>
      {children ? <div className="mt-2 text-sm text-zinc-400">{children}</div> : null}
    </div>
  );
}

export function ErrorNotice({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div role="alert" className="rounded-lg border border-red-900/70 bg-red-950/40 p-4 text-sm">
      <p className="font-medium text-red-200">{title}</p>
      {children ? <div className="mt-1 text-red-300/90">{children}</div> : null}
    </div>
  );
}

export function Badge({
  tone,
  children,
}: {
  tone: "neutral" | "good" | "warn" | "bad" | "info";
  children: ReactNode;
}) {
  const tones = {
    neutral: "bg-zinc-800 text-zinc-300",
    good: "bg-emerald-500/15 text-emerald-300",
    warn: "bg-amber-500/15 text-amber-300",
    bad: "bg-red-500/15 text-red-300",
    info: "bg-sky-500/15 text-sky-300",
  }[tone];
  return <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${tones}`}>{children}</span>;
}

/** A button that asks "are you sure?" in place before doing something that cannot be undone. */
export function ConfirmButton({
  label,
  confirmLabel,
  warning,
  onConfirm,
  disabled,
  variant = "secondary",
}: {
  label: string;
  confirmLabel: string;
  warning: string;
  onConfirm: () => void;
  disabled?: boolean;
  variant?: "primary" | "secondary" | "ghost";
}) {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <Button variant={variant} disabled={disabled} onClick={() => setAsking(true)}>
        {label}
      </Button>
    );
  }
  return (
    <div
      role="alertdialog"
      aria-label={label}
      className="flex flex-wrap items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
    >
      <span className="text-amber-200">{warning}</span>
      <Button
        onClick={() => {
          setAsking(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </Button>
      <Button variant="ghost" onClick={() => setAsking(false)}>
        Cancel
      </Button>
    </div>
  );
}
