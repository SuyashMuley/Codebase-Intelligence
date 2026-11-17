import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";
import { lineLabel, SearchTab } from "./search-tab";

const hit = {
  chunk_id: 1,
  path: "src/auth.py",
  start_line: 10,
  end_line: 24,
  symbol: "create_session",
  text: "def create_session(): ...",
  score: 0.5,
  keyword_rank: null,
  vector_rank: 1,
};

function setup(response: Response, hasSnapshot = true) {
  const fetchMock = vi.fn<typeof fetch>(async () => response.clone());
  vi.stubGlobal("fetch", fetchMock);
  const onOpen = vi.fn();
  renderWithClient(<SearchTab repoId="r1" hasSnapshot={hasSnapshot} onOpen={onOpen} />);
  return { fetchMock, onOpen };
}

async function ask(text: string) {
  await userEvent.type(screen.getByRole("searchbox", { name: /search the code/i }), text);
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
}

afterEach(() => vi.unstubAllGlobals());

describe("SearchTab", () => {
  it("says to index first when there is no snapshot", () => {
    setup(jsonResponse({}), false);
    expect(screen.getByText(/nothing to search yet/i)).toBeInTheDocument();
  });

  it("does not search an empty question", () => {
    setup(jsonResponse({}));
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("sends the question and chosen method, then opens a result at its lines", async () => {
    const { fetchMock, onOpen } = setup(
      jsonResponse({ mode_used: "vector", warnings: [], hits: [hit] }),
    );
    await userEvent.selectOptions(screen.getByLabelText(/search method/i), "keyword");
    await ask("  where are sessions made  ");
    const button = await screen.findByRole("button", { name: "src/auth.py:10-24" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/repositories/r1/search");
    expect(JSON.parse(String(init?.body))).toEqual({
      query: "where are sessions made",
      mode: "keyword",
      limit: 10,
    });
    await userEvent.click(button);
    expect(onOpen).toHaveBeenCalledWith("src/auth.py", "10-24");
  });

  it("explains a fallback to keyword search", async () => {
    setup(jsonResponse({ mode_used: "keyword", warnings: ["embeddings_not_ready"], hits: [hit] }));
    await ask("sessions");
    expect(await screen.findByRole("note")).toHaveTextContent(/used keyword search/i);
  });

  it("explains when the embedding service failed", async () => {
    setup(
      jsonResponse({
        mode_used: "keyword",
        warnings: ["embedding_failed:embedding_unavailable"],
        hits: [],
      }),
    );
    await ask("sessions");
    expect(await screen.findByRole("note")).toHaveTextContent(/could not be reached/i);
    expect(screen.getByText(/no matches/i)).toBeInTheDocument();
  });

  it("shows a friendly message when rate limited", async () => {
    setup(apiError(429, "rate_limited", "too many"));
    await ask("sessions");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/too fast/i));
  });

  it("formats single-line and multi-line ranges", () => {
    expect(lineLabel({ start_line: 5, end_line: 5 })).toBe("5");
    expect(lineLabel({ start_line: 5, end_line: 9 })).toBe("5-9");
  });
});
