import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ search: "", replace: vi.fn(), signedIn: false }));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(state.search),
  useRouter: () => ({ replace: state.replace }),
}));
vi.mock("@/lib/hooks", () => ({ useMe: () => ({ isSuccess: state.signedIn }) }));

import { LoginCard, safeNext } from "./login-card";

beforeEach(() => {
  state.search = "";
  state.signedIn = false;
  state.replace.mockClear();
});

describe("LoginCard", () => {
  it("links to the backend sign-in route", () => {
    render(<LoginCard />);
    const link = screen.getByRole("link", { name: /continue with github/i });
    expect(link).toHaveAttribute("href", "/api/auth/github/login?next=%2Frepos");
  });

  it("explains what data is read", () => {
    render(<LoginCard />);
    expect(screen.getByText(/only your public profile/i)).toBeInTheDocument();
  });

  it.each([
    ["invalid_state", /expired or was already used/i],
    ["access_denied", /cancelled/i],
    ["github_auth_failed", /rejected the sign-in/i],
    ["auth_not_configured", /not configured/i],
    ["something_new", /something went wrong/i],
  ])("shows a readable message for ?error=%s", (code, text) => {
    state.search = `error=${code}`;
    render(<LoginCard />);
    expect(screen.getByRole("alert")).toHaveTextContent(text);
  });

  it("does not render raw error codes from the URL as markup", () => {
    state.search = `error=${encodeURIComponent("<img src=x onerror=alert(1)>")}`;
    const { container } = render(<LoginCard />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("passes a safe ?next through to the sign-in link", () => {
    state.search = "next=%2Frepos%2Fabc";
    render(<LoginCard />);
    expect(screen.getByRole("link", { name: /continue with github/i })).toHaveAttribute(
      "href",
      "/api/auth/github/login?next=%2Frepos%2Fabc",
    );
  });

  it("redirects away when already signed in", () => {
    state.signedIn = true;
    render(<LoginCard />);
    expect(state.replace).toHaveBeenCalledWith("/repos");
  });
});

describe("safeNext", () => {
  it.each([
    ["/repos", "/repos"],
    ["/repos/abc", "/repos/abc"],
    [null, "/repos"],
    ["", "/repos"],
    ["repos", "/repos"],
    ["//evil.example", "/repos"],
    ["https://evil.example", "/repos"],
    ["/\\evil.example", "/repos"],
    ["javascript:alert(1)", "/repos"],
  ])("%j -> %s", (input, expected) => {
    expect(safeNext(input)).toBe(expected);
  });
});
