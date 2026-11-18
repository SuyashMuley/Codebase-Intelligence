/**
 * Provider keys the user chose to keep in the browser only ("browser-held" mode).
 *
 * They live in sessionStorage: gone when the tab closes, never written to the server, and only
 * ever attached to requests to this app's own repository API (where the server uses them for that
 * one request and keeps nothing). The trade-off against localStorage is that they must be entered
 * again in a new tab or session; sessionStorage is not a defence against a script injected into
 * the page, so this app also serves no third-party scripts.
 */

export type KeySlot = "embedding" | "chat";

const STORAGE = { embedding: "cip.embedding-key", chat: "cip.chat-key" } as const;
const HEADER = { embedding: "X-Embedding-Key", chat: "X-Chat-Key" } as const;

function store(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null; // blocked storage (some private modes): behave as if there is no key
  }
}

export function getBrowserKey(slot: KeySlot): string | null {
  try {
    return store()?.getItem(STORAGE[slot]) ?? null;
  } catch {
    return null;
  }
}

export function setBrowserKey(slot: KeySlot, key: string): boolean {
  try {
    const s = store();
    if (!s) return false;
    s.setItem(STORAGE[slot], key);
    return true;
  } catch {
    return false;
  }
}

export function clearBrowserKey(slot: KeySlot): void {
  try {
    store()?.removeItem(STORAGE[slot]);
  } catch {
    // nothing to clear
  }
}

/**
 * Headers carrying the browser-held keys, for requests that may need them. Only repository
 * routes get them (indexing, search, questions, review); nothing else, and never another origin.
 */
export function browserKeyHeaders(path: string): Record<string, string> {
  if (!path.startsWith("/api/repositories/")) return {};
  const headers: Record<string, string> = {};
  for (const slot of ["embedding", "chat"] as const) {
    const key = getBrowserKey(slot);
    if (key) headers[HEADER[slot]] = key;
  }
  return headers;
}
