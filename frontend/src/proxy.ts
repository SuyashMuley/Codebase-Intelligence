import { NextResponse, type NextRequest } from "next/server";
import { buildCsp, cspMode, newNonce } from "@/lib/csp";

/**
 * Gives every page request its own nonce and a Content-Security-Policy that names it. Next reads
 * the nonce from the request's policy header and puts it on its own scripts, so nothing else is
 * needed in the pages. Every page therefore renders per request (see the root layout).
 */
export function proxy(request: NextRequest) {
  const mode = cspMode(process.env.CSP_MODE);
  if (mode === "off") return NextResponse.next();

  const nonce = newNonce();
  // Behind a TLS-terminating host (Vercel) the original scheme arrives in x-forwarded-proto.
  const secure =
    request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  const policy = buildCsp({ nonce, isDev: process.env.NODE_ENV === "development", secure });

  // Next looks for the nonce in the *request's* Content-Security-Policy, whatever the mode.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", policy);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set(
    mode === "report-only" ? "Content-Security-Policy-Report-Only" : "Content-Security-Policy",
    policy,
  );
  return response;
}

export const config = {
  matcher: [
    {
      // Pages only: not the API (proxied JSON), build assets or the favicon, and not prefetches.
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
