// Client-side mirror of backend/app/services/github/url.py. This only gives instant feedback;
// the backend is the authority and validates again.

const OWNER = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/;
const NAME = /^[A-Za-z0-9._-]{1,100}$/;

export type UrlCheck = { ok: true; owner: string; name: string } | { ok: false; reason: string };

export function checkGithubUrl(raw: string): UrlCheck {
  const value = raw.trim();
  if (!value) return { ok: false, reason: "Enter a repository URL." };
  if (value.length > 200) return { ok: false, reason: "That URL is too long." };

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, reason: "Use a full URL like https://github.com/owner/repo." };
  }
  if (url.protocol !== "https:") return { ok: false, reason: "The URL must start with https://." };
  if (url.hostname !== "github.com" && url.hostname !== "www.github.com") {
    return { ok: false, reason: "Only github.com repositories are supported." };
  }
  if (url.username || url.password || url.port || url.search || url.hash) {
    return { ok: false, reason: "Use the plain repository URL, without extras." };
  }
  const parts = url.pathname.replace(/^\/+|\/+$/g, "").split("/");
  if (parts.length !== 2 || parts.some((p) => !p)) {
    return { ok: false, reason: "The path must be exactly owner/repo." };
  }
  const owner = parts[0];
  const name = parts[1].replace(/\.git$/, "");
  if (!OWNER.test(owner)) return { ok: false, reason: "That owner name is not valid." };
  if (!NAME.test(name) || name === "." || name === "..") {
    return { ok: false, reason: "That repository name is not valid." };
  }
  return { ok: true, owner, name };
}
