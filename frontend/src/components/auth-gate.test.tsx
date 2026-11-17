import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";

const state = vi.hoisted(() => ({
  me: {} as Record<string, unknown>,
  replace: vi.fn(),
  refetch: vi.fn(),
  slow: false,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: state.replace }),
  usePathname: () => "/repos/abc",
}));
vi.mock("@/lib/hooks", () => ({
  useMe: () => state.me,
  useSlowFlag: () => state.slow,
  useLogout: () => ({ isPending: false, mutate: vi.fn() }),
}));

import { AuthGate } from "./auth-gate";

beforeEach(() => {
  state.slow = false;
  state.replace.mockClear();
  state.refetch.mockClear();
});

describe("AuthGate", () => {
  it("shows a loading state while the session is checked", () => {
    state.me = { isPending: true, isError: false };
    render(<AuthGate>secret</AuthGate>);
    expect(screen.getByRole("status")).toHaveTextContent(/checking your session/i);
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("explains a cold start when the check is slow", () => {
    state.me = { isPending: true, isError: false };
    state.slow = true;
    render(<AuthGate>secret</AuthGate>);
    expect(screen.getByRole("status")).toHaveTextContent(/waking up the server/i);
    expect(screen.getByText(/sleeps when idle/i)).toBeInTheDocument();
  });

  it("sends signed-out users to sign in and keeps where they were going", () => {
    state.me = {
      isPending: false,
      isError: true,
      error: new ApiError(401, "unauthenticated", "Sign in to continue."),
    };
    render(<AuthGate>secret</AuthGate>);
    expect(state.replace).toHaveBeenCalledWith("/login?next=%2Frepos%2Fabc");
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });

  it("shows other errors with a retry instead of redirecting", async () => {
    state.me = {
      isPending: false,
      isError: true,
      error: new ApiError(0, "network_error", "Could not reach the server."),
      refetch: state.refetch,
    };
    render(<AuthGate>secret</AuthGate>);
    expect(screen.getByRole("alert")).toHaveTextContent(/could not reach the server/i);
    expect(state.replace).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(state.refetch).toHaveBeenCalled();
  });

  it("renders the app for a signed-in user", () => {
    state.me = {
      isPending: false,
      isError: false,
      data: { id: "u1", login: "alice", avatar_url: null },
    };
    render(<AuthGate>secret</AuthGate>);
    expect(screen.getByText("secret")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(state.replace).not.toHaveBeenCalled();
  });
});
