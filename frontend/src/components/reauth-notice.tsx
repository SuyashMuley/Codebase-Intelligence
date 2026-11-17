import { ErrorNotice } from "./ui";

/** Shown when GitHub no longer honors the user's authorization (revoked or expired). */
export function ReauthNotice({ returnTo }: { returnTo: string }) {
  return (
    <ErrorNotice title="Your GitHub authorization expired">
      <p>Sign in with GitHub again to keep going. Your repositories and progress are kept.</p>
      {/* Full-page navigation to the API; signing in again replaces the old session. */}
      <a
        href={`/api/auth/github/login?next=${encodeURIComponent(returnTo)}`}
        className="mt-3 inline-flex rounded-md bg-emerald-500 px-3 py-1.5 text-sm font-medium text-zinc-950 hover:bg-emerald-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400"
      >
        Sign in again
      </a>
    </ErrorNotice>
  );
}
