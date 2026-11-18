import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatAnswer } from "@/lib/types";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";
import { ChatTab, lineRange, toHistory } from "./chat-tab";

const snapshot = { id: "s", commit_sha: "abc", state: "ready" };
const base: ChatAnswer = {
  insufficient_evidence: false,
  reason: null,
  confidence: "high",
  claims: [
    { text: "It signs a cookie for the user.", cites: [1] },
    { text: "Revoking deletes the token from the store.", cites: [2, 1] },
  ],
  citations: [
    { n: 1, chunk_id: 11, path: "auth/sessions.py", start_line: 12, end_line: 12 },
    { n: 2, chunk_id: 12, path: "auth/sessions.py", start_line: 15, end_line: 17 },
  ],
  missing: "",
  notes: [],
  dropped_claims: 0,
  model: "some/model:free",
  snapshot,
  retrieval: { mode: "vector", chunks_considered: 6, expanded_from_graph: 0, top_similarity: 0.81 },
  attempts: 1,
  trace: [],
};

afterEach(() => vi.unstubAllGlobals());

function setup(responses: Response[] | Response, ready = true) {
  const queue = Array.isArray(responses) ? [...responses] : [responses];
  const fetchMock = vi.fn<typeof fetch>(async () => (queue.length > 1 ? queue.shift()! : queue[0].clone()));
  vi.stubGlobal("fetch", fetchMock);
  const onOpen = vi.fn();
  renderWithClient(<ChatTab repoId="r1" ready={ready} onOpen={onOpen} />);
  return { fetchMock, onOpen };
}

async function ask(text: string) {
  await userEvent.type(screen.getByRole("textbox", { name: "Your question" }), text);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

describe("ChatTab", () => {
  it("waits for indexing to finish", () => {
    setup(jsonResponse(base), false);
    expect(screen.getByText(/open when indexing finishes/i)).toBeInTheDocument();
  });

  it("sends the question with no history first, and shows the answer with its sources", async () => {
    const { fetchMock } = setup(jsonResponse(base));
    await ask("  how are sessions made?  ");
    const answer = await screen.findByRole("list", { name: "Answer" });
    expect(within(answer).getByText(/signs a cookie/)).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/repositories/r1/chat");
    expect(JSON.parse(String(init?.body))).toEqual({ question: "how are sessions made?", history: [] });
    const sources = screen.getByRole("list", { name: "Sources" });
    expect(within(sources).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("high confidence")).toBeInTheDocument();
  });

  it("a citation number jumps to the cited lines", async () => {
    const { onOpen } = setup(jsonResponse(base));
    await ask("how are sessions made?");
    await userEvent.click(await screen.findByRole("button", { name: "Open auth/sessions.py lines 15-17" }));
    expect(onOpen).toHaveBeenCalledWith("auth/sessions.py", "15-17");
    await userEvent.click(screen.getAllByRole("button", { name: "Open auth/sessions.py lines 12" })[0]);
    expect(onOpen).toHaveBeenLastCalledWith("auth/sessions.py", "12");
  });

  it("a source line opens the same lines", async () => {
    const { onOpen } = setup(jsonResponse(base));
    await ask("q");
    const sources = await screen.findByRole("list", { name: "Sources" });
    await userEvent.click(within(sources).getByRole("button", { name: "auth/sessions.py:15-17" }));
    expect(onOpen).toHaveBeenCalledWith("auth/sessions.py", "15-17");
  });

  it("says plainly when the code cannot answer, with no citations", async () => {
    setup(
      jsonResponse({
        ...base,
        insufficient_evidence: true,
        reason: "no_relevant_code",
        confidence: "none",
        claims: [],
        citations: [],
        missing: "No code in this repository was close enough to the question.",
      }),
    );
    await ask("what is the capital of France?");
    expect(await screen.findByText("I cannot determine this from the code.")).toBeInTheDocument();
    expect(screen.getByText(/close enough to the question/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Sources" })).not.toBeInTheDocument();
    expect(screen.queryByText(/confidence/)).not.toBeInTheDocument();
  });

  it("says when statements were removed", async () => {
    setup(
      jsonResponse({
        ...base,
        confidence: "low",
        dropped_claims: 1,
        notes: ["1 statement the model wrote could not be tied to the code and was removed."],
      }),
    );
    await ask("q");
    expect(await screen.findByRole("note")).toHaveTextContent(/could not be tied to the code/);
    expect(screen.getByText("low confidence")).toBeInTheDocument();
  });

  it("is honest about what a citation proves", async () => {
    setup(jsonResponse(base));
    await ask("q");
    expect(await screen.findByText(/does not prove the statement reads the code correctly/)).toBeInTheDocument();
  });

  it("sends earlier turns as plain text on the next question", async () => {
    const { fetchMock } = setup([jsonResponse(base), jsonResponse(base)]);
    await ask("how are sessions made?");
    await screen.findByRole("list", { name: "Answer" });
    await ask("and revoked?");
    await screen.findAllByRole("list", { name: "Answer" });
    const second = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(second.history).toEqual([
      { role: "user", content: "how are sessions made?" },
      { role: "assistant", content: "It signs a cookie for the user. Revoking deletes the token from the store." },
    ]);
  });

  it("can start a new conversation", async () => {
    setup(jsonResponse(base));
    await ask("q");
    await screen.findByRole("list", { name: "Answer" });
    await userEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByRole("list", { name: "Conversation" })).not.toBeInTheDocument();
    expect(screen.getByText(/Ask about this code/)).toBeInTheDocument();
  });

  it.each([
    ["chat_quota_exceeded", 429, /used today's questions/i],
    ["llm_rate_limited", 503, /busy or out of allowance/i],
    ["llm_unavailable", 502, /too slow or could not be reached/i],
    ["rate_limited", 429, /too fast/i],
  ])("explains %s in plain words", async (code, status, text) => {
    setup(apiError(status, code, "raw"));
    await ask("q");
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
  });

  it("does not send an empty question", () => {
    setup(jsonResponse(base));
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });
});

describe("helpers", () => {
  it("formats single-line and multi-line ranges", () => {
    expect(lineRange({ start_line: 4, end_line: 4 })).toBe("4");
    expect(lineRange({ start_line: 4, end_line: 9 })).toBe("4-9");
  });

  it("keeps only the last turns and never forwards citations", () => {
    const messages = Array.from({ length: 10 }, (_, i) => ({ id: i, role: "user" as const, text: `q${i}` }));
    const history = toHistory(messages);
    expect(history).toHaveLength(6);
    expect(history[0].content).toBe("q4");
    const declined = toHistory([
      { id: 1, role: "assistant", answer: { ...base, insufficient_evidence: true, claims: [] } },
    ]);
    expect(declined[0].content).toBe("I could not determine this from the code.");
  });
});
