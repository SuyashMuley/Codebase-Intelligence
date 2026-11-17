import { describe, expect, it } from "vitest";
import { checkGithubUrl } from "./github-url";

describe("checkGithubUrl", () => {
  it.each([
    ["https://github.com/pallets/flask", "pallets", "flask"],
    ["https://github.com/pallets/flask/", "pallets", "flask"],
    ["https://github.com/pallets/flask.git", "pallets", "flask"],
    ["https://www.github.com/pallets/flask", "pallets", "flask"],
    ["  https://github.com/pallets/flask  ", "pallets", "flask"],
    ["https://github.com/my-org/my.repo_name-2", "my-org", "my.repo_name-2"],
  ])("accepts %s", (url, owner, name) => {
    expect(checkGithubUrl(url)).toEqual({ ok: true, owner, name });
  });

  it.each([
    "",
    "   ",
    "github.com/pallets/flask",
    "http://github.com/pallets/flask",
    "https://gitlab.com/pallets/flask",
    "https://github.com.evil.com/pallets/flask",
    "https://github.com@evil.com/pallets/flask",
    "https://user:pw@github.com/pallets/flask",
    "https://github.com:8443/pallets/flask",
    "https://github.com/pallets",
    "https://github.com/pallets/flask/tree/main",
    "https://github.com/pallets/flask?tab=readme",
    "https://github.com/pallets/flask#readme",
    "https://github.com/-bad/flask",
    "https://github.com/pallets/..",
    "https://github.com/" + "a".repeat(40) + "/x",
    "https://github.com/pallets/" + "x".repeat(101),
    "https://github.com/" + "a/".repeat(120),
  ])("rejects %j", (url) => {
    const result = checkGithubUrl(url);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason.length).toBeGreaterThan(0);
  });
});
