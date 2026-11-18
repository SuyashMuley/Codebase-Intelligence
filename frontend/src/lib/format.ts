export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "";
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export const SKIP_EXPLANATIONS: Record<string, string> = {
  too_large:
    "This file is larger than the 256 KB limit, so it was not stored or parsed.",
  binary: "This file looks binary (or is not valid UTF-8), so it is not shown.",
  generated:
    "This file looks generated (minified, protobuf output, or marked as generated).",
  secret_file:
    "This file name looks like it holds credentials, so it was never read.",
  vendored: "This file is in a vendored or build-output folder.",
  unsupported:
    "Only Python, JavaScript, TypeScript and Go code, and README files, are indexed.",
};

export function formatLanguage(language: string | null): string {
  return (
    {
      python: "Python",
      javascript: "JavaScript",
      typescript: "TypeScript",
      go: "Go",
      markdown: "Markdown",
    }[language ?? ""] ??
    language ??
    "Unknown"
  );
}

export const LANGUAGE_BADGES: Record<string, { short: string; color: string }> =
  {
    python: { short: "PY", color: "text-sky-300 bg-sky-500/10" },
    javascript: { short: "JS", color: "text-yellow-300 bg-yellow-500/10" },
    typescript: { short: "TS", color: "text-blue-300 bg-blue-500/10" },
    go: { short: "GO", color: "text-cyan-300 bg-cyan-500/10" },
    markdown: { short: "MD", color: "text-zinc-300 bg-zinc-500/10" },
  };

export const SKIP_LABELS: Record<string, string> = {
  unsupported: "Other file types",
  vendored: "Vendored or build output",
  generated: "Generated code",
  binary: "Binary files",
  too_large: "Too large",
  secret_file: "Secret-looking files",
  unsafe_path: "Unsafe paths",
};
