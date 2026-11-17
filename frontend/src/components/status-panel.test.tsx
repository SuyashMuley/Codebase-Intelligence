import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { makeStatus } from "@/test/utils";
import { StatusPanel } from "./status-panel";

function setup(status: ReturnType<typeof makeStatus>, extra: Partial<Parameters<typeof StatusPanel>[0]> = {}) {
  const onIndex = vi.fn();
  render(
    <StatusPanel
      status={status}
      stopped={null}
      driverError={null}
      starting={false}
      onIndex={onIndex}
      {...extra}
    />,
  );
  return onIndex;
}

describe("StatusPanel", () => {
  it("offers to index when nothing has been indexed", async () => {
    const onIndex = setup(makeStatus({ state: "not_indexed", snapshot_id: null, commit_sha: null, phase: null }));
    expect(screen.getByText(/not indexed yet/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /index latest commit/i }));
    expect(onIndex).toHaveBeenCalled();
  });

  it("shows progress numbers and what was skipped", () => {
    setup(
      makeStatus({
        phase: "parse",
        progress: {
          files_kept: 12,
          bytes_kept: 29182,
          skipped: { unsupported: 35, vendored: 2 },
          secrets_redacted: 0,
          files_redacted: 0,
        },
      }),
    );
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("28.5 KB")).toBeInTheDocument();
    expect(screen.getByText(/other file types: 35/i)).toBeInTheDocument();
    expect(screen.getByText(/vendored or build output: 2/i)).toBeInTheDocument();
    expect(screen.getByText(/commit 0123456/)).toBeInTheDocument();
  });

  it("marks finished phases and the current one in the stepper", () => {
    setup(makeStatus({ phase: "parse" }));
    const steps = screen.getAllByRole("listitem");
    expect(steps[0]).toHaveTextContent("✓ Fetch source");
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(steps[2]).not.toHaveAttribute("aria-current");
  });

  it("explains a retryable error with its attempt count and countdown", () => {
    setup(
      makeStatus({
        attempts: 1,
        error: { code: "github_unavailable", message: "Could not reach GitHub." },
        retry_at: new Date(Date.now() + 30_000).toISOString(),
      }),
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/will be retried/i);
    expect(alert).toHaveTextContent("Could not reach GitHub.");
    expect(alert).toHaveTextContent("github_unavailable · attempt 1 of 3");
    expect(alert).toHaveTextContent(/retrying in \d+s/i);
  });

  it("shows a failure and lets the user try again", async () => {
    const onIndex = setup(
      makeStatus({
        state: "failed",
        phase: "failed",
        attempts: 3,
        can_resume: true,
        error: { code: "repo_too_large", message: "Repository has more than 2000 source files." },
      }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/indexing failed/i);
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onIndex).toHaveBeenCalled();
  });

  it("is honest that the steps after parsing are not built yet", () => {
    setup(makeStatus({ phase: "embed" }), { stopped: "not_implemented" });
    expect(screen.getByText(/fetched, parsed and split into chunks/i)).toBeInTheDocument();
    expect(screen.getByText(/not available yet/i)).toBeInTheDocument();
  });

  it("shows parse progress and says when secrets were redacted", () => {
    setup(
      makeStatus({
        phase: "embed",
        parse: {
          files: 12,
          symbols: 140,
          chunks: 96,
          refs: 800,
          syntax_error_files: 0,
          truncated_files: 0,
          done: true,
        },
        progress: {
          files_kept: 12,
          bytes_kept: 100,
          skipped: {},
          secrets_redacted: 3,
          files_redacted: 2,
        },
      }),
    );
    expect(screen.getByText("Files parsed").nextSibling).toHaveTextContent("12");
    expect(screen.getByText("Symbols found").nextSibling).toHaveTextContent("140");
    expect(screen.getByText("Chunks created").nextSibling).toHaveTextContent("96");
    expect(screen.getByRole("note")).toHaveTextContent(/3 secret-looking values were replaced/i);
  });

  it("reports lost contact with the server", () => {
    setup(makeStatus(), { driverError: new ApiError(0, "network_error", "Could not reach the server.") });
    expect(screen.getByRole("alert")).toHaveTextContent(/lost contact/i);
  });

  it("disables the action while a start request is in flight", () => {
    setup(makeStatus({ state: "ready", phase: "done" }), { starting: true });
    expect(screen.getByRole("button", { name: /check latest commit/i })).toBeDisabled();
  });
});
