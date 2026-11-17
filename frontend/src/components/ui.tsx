import type { ButtonHTMLAttributes, ReactNode } from "react";

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

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-zinc-800 bg-zinc-900/60 p-5 ${className}`}>
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
