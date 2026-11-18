import { browserKeyHeaders } from "./browser-keys";
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }

  /** The user is not signed in, or their GitHub authorization lapsed. */
  get needsSignIn(): boolean {
    return this.status === 401;
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== "object" || value === null) return false;
  const err = (value as { error?: unknown }).error;
  return (
    typeof err === "object" &&
    err !== null &&
    typeof (err as { code?: unknown }).code === "string" &&
    typeof (err as { message?: unknown }).message === "string"
  );
}

/** Calls the backend through the same-origin /api proxy. Cookies travel automatically. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...browserKeyHeaders(path),
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
  } catch {
    // Offline, or the free-tier backend is still waking up.
    throw new ApiError(0, "network_error", "Could not reach the server. Please try again.");
  }

  if (response.status === 204) return undefined as T;

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON body (e.g. a proxy error page); handled below.
  }

  if (!response.ok) {
    if (isErrorBody(body)) {
      const { code, message, details, request_id } = body.error;
      throw new ApiError(response.status, code, message, details ?? {}, request_id);
    }
    throw new ApiError(
      response.status,
      response.status >= 500 ? "server_error" : "http_error",
      response.status >= 500
        ? "The server had a problem. Please try again in a moment."
        : "The request could not be completed.",
    );
  }
  return body as T;
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
