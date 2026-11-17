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

export const SKIP_LABELS: Record<string, string> = {
  unsupported: "Other file types",
  vendored: "Vendored or build output",
  generated: "Generated code",
  binary: "Binary files",
  too_large: "Too large",
  secret_file: "Secret-looking files",
  unsafe_path: "Unsafe paths",
};
