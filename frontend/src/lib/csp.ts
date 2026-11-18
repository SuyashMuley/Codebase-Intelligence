/**
 * The Content-Security-Policy for the app, built per request.
 *
 * What it is for: if a script were ever injected into a page, the browser would refuse to run it,
 * because only scripts carrying this request's random nonce (and what they load) may execute. That
 * also protects a provider key held in sessionStorage. The app has no known injection sink; this is
 * the second line of defence.
 *
 * Choices that are not obvious:
 * - `script-src` has no `'unsafe-inline'` and no host list. A nonce plus `'strict-dynamic'` lets
 *   Next's own scripts (and the chunks they load) run and nothing else.
 * - `style-src` does allow `'unsafe-inline'`: Monaco and React Flow create `<style>` elements and
 *   style attributes at run time, which a nonce cannot cover. Injected CSS is far less dangerous
 *   than injected script, and this is the one deliberate hole.
 * - `connect-src 'self'`: the browser talks only to this origin (the API is proxied through it).
 * - `worker-src 'self' blob:`: Monaco's editor worker.
 * - `img-src` allows GitHub's avatar host and nothing else remote.
 * - Development needs `'unsafe-eval'` (React's debugging) and the hot-reload websocket; production
 *   gets neither, and upgrades insecure requests when the request itself was https.
 */

const GITHUB_AVATARS = "https://avatars.githubusercontent.com";

export type CspOptions = {
  nonce: string;
  isDev: boolean;
  /** The request arrived over https: only then is "upgrade insecure requests" meaningful (over
   * plain http, as in a local production run, it would turn every asset request into https and
   * break the page). */
  secure: boolean;
};

export function buildCsp({ nonce, isDev, secure }: CspOptions): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(isDev ? ["'unsafe-eval'"] : [])],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:", GITHUB_AVATARS],
    "font-src": ["'self'", "data:"],
    "connect-src": ["'self'", ...(isDev ? ["ws://localhost:*", "ws://127.0.0.1:*"] : [])],
    "worker-src": ["'self'", "blob:"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
  };
  const parts = Object.entries(directives).map(([name, values]) => `${name} ${values.join(" ")}`);
  if (secure && !isDev) parts.push("upgrade-insecure-requests");
  return parts.join("; ");
}

/** "enforce" (default), "report-only" (the browser reports violations but blocks nothing, for
 * rolling out a change safely) or "off". Set with CSP_MODE. */
export type CspMode = "enforce" | "report-only" | "off";

export function cspMode(value: string | undefined): CspMode {
  return value === "report-only" || value === "off" ? value : "enforce";
}

/** A fresh, unpredictable value for one request. */
export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}
