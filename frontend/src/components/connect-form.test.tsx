import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";

const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import { ConnectForm } from "./connect-form";

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  push.mockClear();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

async function submit(url: string) {
  renderWithClient(<ConnectForm />);
  await userEvent.type(screen.getByLabelText(/connect a public github repository/i), url);
  await userEvent.click(screen.getByRole("button", { name: /connect/i }));
}

describe("ConnectForm", () => {
  it("rejects an invalid URL locally without calling the API", async () => {
    await submit("https://gitlab.com/o/r");
    expect(screen.getByRole("alert")).toHaveTextContent(/only github\.com/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("connects a repository and opens it", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "repo-1", owner: "o", name: "r", url: "u", default_branch: null, created_at: "2026-01-01T00:00:00Z" }, 201),
    );
    await submit("https://github.com/o/r");
    await waitFor(() => expect(push).toHaveBeenCalledWith("/repos/repo-1"));
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/repositories");
    expect(init.body).toBe(JSON.stringify({ url: "https://github.com/o/r" }));
  });

  it("shows the server's message when it refuses", async () => {
    fetchMock.mockResolvedValue(apiError(400, "invalid_github_url", "Not a valid GitHub repository URL."));
    await submit("https://github.com/o/r");
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a valid github repository url/i);
    expect(push).not.toHaveBeenCalled();
  });

  it("clears the message as the user types again", async () => {
    await submit("nonsense");
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/connect a public github repository/i), "x");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
