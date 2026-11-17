import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Impact } from "@/lib/types";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";
import { ImpactPanel } from "./impact-panel";

const impact: Impact = {
  snapshot: { id: "s", commit_sha: "abc", state: "ready" },
  target: { type: "file", path: "app/models.py", symbol: null, symbol_id: null },
  max_depth: 3,
  dependents: [
    {
      path: "app/service.py",
      symbol: null,
      depth: 1,
      hops: [{ source: "app/models.py", target: "app/service.py", kind: "imports" }],
    },
    {
      path: "tests/test_api.py",
      symbol: null,
      depth: 3,
      hops: [
        { source: "app/models.py", target: "app/service.py", kind: "imports" },
        { source: "app/service.py", target: "app/api.py", kind: "imports" },
        { source: "app/api.py", target: "tests/test_api.py", kind: "imports" },
      ],
    },
  ],
  direct: 1,
  transitive: 1,
  files_affected: 2,
  truncated: false,
  unverified_callers: [
    { path: "app/api.py", symbol: "other", line: 9, called_as: "client.save", reason: "dynamic_receiver" },
  ],
  suggested_tests: [
    {
      path: "tests/test_api.py",
      basis: "static_dependency",
      confidence: "medium",
      reason: "depends on the changed code through 3 steps",
      heuristic: false,
    },
    {
      path: "tests/test_models.py",
      basis: "naming_convention",
      confidence: "low",
      reason: "test file named after a changed file; no static link was found",
      heuristic: true,
    },
  ],
  limitations: ["Calls on variables are not followed."],
};

afterEach(() => vi.unstubAllGlobals());

function setup(response: Response, target = { file: "app/models.py", symbol: null as number | null }) {
  const fetchMock = vi.fn<typeof fetch>(async () => response.clone());
  vi.stubGlobal("fetch", fetchMock);
  const onOpenFile = vi.fn();
  renderWithClient(<ImpactPanel repoId="r1" target={target} onOpenFile={onOpenFile} />);
  return { fetchMock, onOpenFile };
}

describe("ImpactPanel", () => {
  it("asks for the file's impact and shows the counts", async () => {
    const { fetchMock } = setup(jsonResponse(impact));
    expect(await screen.findByText("app/models.py")).toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/repositories/r1/impact?depth=3&file=app%2Fmodels.py");
    expect(screen.getByText("Direct").nextSibling).toHaveTextContent("1");
    expect(screen.getByText("Indirect").nextSibling).toHaveTextContent("1");
    expect(screen.getByText("Files affected").nextSibling).toHaveTextContent("2");
  });

  it("asks by symbol id when a symbol is selected", async () => {
    const { fetchMock } = setup(jsonResponse(impact), { file: "app/models.py", symbol: 12 });
    await screen.findByText(/If you change this/);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/repositories/r1/impact?depth=3&symbol=12");
  });

  it("groups dependents by distance and shows how each is reached", async () => {
    setup(jsonResponse(impact));
    const deps = await screen.findByRole("region", { name: "Dependents" });
    expect(within(deps).getByText("Directly")).toBeInTheDocument();
    expect(within(deps).getByText("3 steps away")).toBeInTheDocument();
    expect(within(deps).getAllByText("How it is reached")).toHaveLength(2);
    expect(within(deps).getByText("Static facts")).toBeInTheDocument();
  });

  it("labels a heuristic test differently from a static fact", async () => {
    setup(jsonResponse(impact));
    const tests = await screen.findByRole("region", { name: "Suggested tests" });
    const rows = within(tests).getAllByRole("listitem");
    expect(within(rows[0]).getByText("Static fact")).toBeInTheDocument();
    expect(within(rows[0]).getByText("medium confidence")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Heuristic")).toBeInTheDocument();
    expect(within(rows[1]).getByText("low confidence")).toBeInTheDocument();
  });

  it("keeps unverified callers apart and says they are not dependents", async () => {
    setup(jsonResponse(impact));
    const section = await screen.findByRole("region", { name: "Unverified callers" });
    expect(within(section).getByText("Not verified")).toBeInTheDocument();
    expect(within(section).getByText(/leads to check, not dependents/i)).toBeInTheDocument();
    expect(within(section).getByText(/calls client\.save/)).toBeInTheDocument();
    const deps = screen.getByRole("region", { name: "Dependents" });
    expect(within(deps).queryByText(/client\.save/)).not.toBeInTheDocument();
  });

  it("warns that nothing found is not proof of safety", async () => {
    setup(jsonResponse({ ...impact, dependents: [], direct: 0, transitive: 0, files_affected: 0 }));
    expect(await screen.findByText(/before treating it as safe to change/i)).toBeInTheDocument();
  });

  it("lists the limitations", async () => {
    setup(jsonResponse(impact));
    expect(await screen.findByText("Calls on variables are not followed.")).toBeInTheDocument();
  });

  it("re-runs with the chosen number of steps", async () => {
    const { fetchMock } = setup(jsonResponse(impact));
    await screen.findByText(/If you change this/);
    await userEvent.selectOptions(screen.getByLabelText(/how many steps/i), "5");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(String(fetchMock.mock.calls[1][0])).toContain("depth=5");
  });

  it("opens a file when its path is clicked", async () => {
    const { onOpenFile } = setup(jsonResponse(impact));
    const deps = await screen.findByRole("region", { name: "Dependents" });
    await userEvent.click(within(deps).getByRole("button", { name: "app/service.py" }));
    expect(onOpenFile).toHaveBeenCalledWith("app/service.py");
  });

  it("explains when the graph is not ready", async () => {
    setup(apiError(409, "graph_not_ready", "later"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/built when indexing finishes/i);
  });
});

describe("ImpactPanel AI review", () => {
  const review = {
    notice: "AI suggestion. It is not verified, can be wrong, and is never counted as a dependent.",
    model: "some/model:free",
    summary: "Changing models affects the service layer.",
    risks: [
      { description: "Service builds users", confidence: "medium", paths: ["app/service.py"] },
    ],
    tests_to_consider: [{ path: "tests/test_service.py", reason: "covers create" }],
    open_questions: ["Is User pickled?"],
    dropped: 2,
    static_dependents_considered: 2,
  };

  function routed(reviewResponse: Response) {
    const fetchMock = vi.fn<typeof fetch>(async (url) =>
      String(url).includes("/impact/review") ? reviewResponse.clone() : jsonResponse(impact),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onOpenFile = vi.fn();
    renderWithClient(
      <ImpactPanel repoId="r1" target={{ file: "app/models.py", symbol: null }} onOpenFile={onOpenFile} />,
    );
    return { fetchMock, onOpenFile };
  }

  it("is offered as unverified and does not run until asked", async () => {
    const { fetchMock } = routed(jsonResponse(review));
    const section = await screen.findByRole("region", { name: "AI review" });
    expect(within(section).getByText("Unverified")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/impact/review"))).toBe(false);
  });

  it("asks for a review of the same target and depth, then labels the answer", async () => {
    const { fetchMock } = routed(jsonResponse(review));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    const section = screen.getByRole("region", { name: "AI review" });
    expect(await within(section).findByText(/never counted as a dependent/)).toBeInTheDocument();
    expect(within(section).getByText(/some\/model:free/)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([u]) => String(u).includes("/impact/review"))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({ file: "app/models.py", depth: 3 });
    expect(within(section).getByText("Changing models affects the service layer.")).toBeInTheDocument();
    expect(within(section).getByText("Service builds users")).toBeInTheDocument();
    expect(within(section).getByText("Is User pickled?")).toBeInTheDocument();
    expect(within(section).getByText(/2 items removed because they named files/)).toBeInTheDocument();
  });

  it("keeps the AI answer out of the static dependents", async () => {
    routed(jsonResponse(review));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    await screen.findByText(/never counted as a dependent/);
    const deps = screen.getByRole("region", { name: "Dependents" });
    expect(within(deps).queryByText(/Service builds users/)).not.toBeInTheDocument();
  });

  it("lets a cited file be opened", async () => {
    const { onOpenFile } = routed(jsonResponse(review));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    const risks = await screen.findByRole("list", { name: "AI risks" });
    await userEvent.click(within(risks).getByRole("button", { name: "app/service.py" }));
    expect(onOpenFile).toHaveBeenCalledWith("app/service.py");
  });

  it("explains failures in plain words", async () => {
    routed(apiError(503, "llm_rate_limited", "raw message"));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    expect(await screen.findByText(/busy or out of allowance/i)).toBeInTheDocument();
  });

  it("says when AI review is not set up", async () => {
    routed(apiError(503, "llm_not_configured", "raw"));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    expect(await screen.findByText(/not set up on this server/i)).toBeInTheDocument();
  });

  it("can be cleared to ask again", async () => {
    routed(jsonResponse(review));
    await userEvent.click(await screen.findByRole("button", { name: /ask ai to review/i }));
    await screen.findByText(/never counted as a dependent/);
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByRole("button", { name: /ask ai to review/i })).toBeInTheDocument();
  });
});
