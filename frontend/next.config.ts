import type { NextConfig } from "next";

// The browser only ever talks to this origin. /api is proxied to the backend, which keeps the
// session cookie first-party (SameSite=Lax is enough) and avoids CORS in the browser.
const BACKEND_URL = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          // The app needs none of these browser powers; denying them limits what an injected
          // script could reach.
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
          },
          // Keeps a window opened by another site from keeping a handle on this one. Sign-in is a
          // full-page redirect, not a popup, so it is unaffected.
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
