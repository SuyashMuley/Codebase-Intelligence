import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";

import { PrivateDataNotice } from "./private-data-notice";

const CATALOG = {
  chat_providers: [{ id: "openai", label: "OpenAI", default_model: "gpt-4o-mini" }],
  embedding_providers: [
    { id: "cloudflare", label: "Cloudflare Workers AI", models: ["@cf/baai/bge-small-en-v1.5"], needs_account_id: true },
  ],
};
const setting = (slot: string, kind: string, mode: string, model: string | null, last4: string | null) => ({
  slot, kind, mode, model, account_id: null, key_last4: last4, updated_at: "x",
});

let fetchMock: ReturnType<typeof vi.fn>;
function providers(body: unknown, status = 200) {
  fetchMock.mockImplementation(async () => (status === 200 ? jsonResponse(body) : apiError(status, "server_error", "boom")));
}
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("PrivateDataNotice", () => {
  it("names the user's own providers and says what goes to each", async () => {
    providers({
      ...CATALOG,
      embedding: setting("embedding", "cloudflare", "stored", "@cf/baai/bge-small-en-v1.5", "abcd"),
      chat: setting("chat", "openai", "browser", "gpt-4o-mini", null),
    });
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    expect(await screen.findByText("(Cloudflare Workers AI, @cf/baai/bge-small-en-v1.5)")).toBeInTheDocument();
    expect(screen.getByText("(OpenAI, gpt-4o-mini)")).toBeInTheDocument();
    expect(screen.getByText(/every chunk of the code is sent there/i)).toBeInTheDocument();
    expect(screen.getByText(/your question and the code excerpts found for it are sent there/i)).toBeInTheDocument();
    expect(screen.getByText(/never used for it/i)).toBeInTheDocument();
    // both key modes are explained when both are in use
    expect(screen.getByText(/stored keys are encrypted on this server/i)).toBeInTheDocument();
    expect(screen.getByText(/browser-only keys are never saved on the server/i)).toBeInTheDocument();
    expect(screen.getByText(/their own terms/i)).toBeInTheDocument();
  });

  it("warns, and says indexing stays blocked, for a provider that is not set up", async () => {
    providers({ ...CATALOG, embedding: null, chat: setting("chat", "openai", "stored", "gpt-4o-mini", "wxyz") });
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    expect(await screen.findByText(/not set up: indexing stays blocked until you add your embedding provider/i)).toBeInTheDocument();
    expect(screen.getByText("(OpenAI, gpt-4o-mini)")).toBeInTheDocument();
    expect(screen.queryByText(/browser-only keys/i)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /provider settings/i })).toHaveAttribute("href", "/settings/providers");
  });

  it("omits the keys line when nothing is set up", async () => {
    providers({ ...CATALOG, embedding: null, chat: null });
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    await screen.findAllByText(/not set up/i);
    expect(screen.queryByText(/your keys/i)).not.toBeInTheDocument();
  });

  it("states the retention period, or that data is kept until disconnect", async () => {
    providers({ ...CATALOG, embedding: null, chat: null });
    const { unmount } = renderWithClient(<PrivateDataNotice retentionDays={14} />);
    expect(await screen.findByText(/deleted automatically 14 days after indexing/i)).toBeInTheDocument();
    unmount();
    renderWithClient(<PrivateDataNotice retentionDays={null} />);
    expect(await screen.findByText(/kept until you disconnect/i)).toBeInTheDocument();
    expect(screen.queryByText(/deleted automatically/i)).not.toBeInTheDocument();
  });

  it("says plainly that backups are not covered by deletion", async () => {
    providers({ ...CATALOG, embedding: null, chat: null });
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    expect(await screen.findByText(/backups, if the operator keeps any, are not erased/i)).toBeInTheDocument();
  });

  it("still shows the fixed facts while loading and when settings cannot be read", async () => {
    providers({}, 500);
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    expect(screen.getByText(/only you/i)).toBeInTheDocument();
    expect(await screen.findAllByText(/could not read your settings/i)).toHaveLength(2);
  });

  it("offers an acknowledgement only when asked to", async () => {
    providers({ ...CATALOG, embedding: null, chat: null });
    const onAck = vi.fn();
    const first = renderWithClient(
      <PrivateDataNotice retentionDays={30} acknowledged={false} onAcknowledge={onAck} />,
    );
    await userEvent.click(await screen.findByLabelText(/i understand where my private code goes/i));
    expect(onAck).toHaveBeenCalledWith(true);
    first.unmount();
    renderWithClient(<PrivateDataNotice retentionDays={30} />);
    await screen.findAllByText(/not set up/i);
    expect(screen.queryByLabelText(/i understand/i)).not.toBeInTheDocument();
  });

  it("folds into a details element when compact", async () => {
    providers({ ...CATALOG, embedding: null, chat: null });
    const { container } = renderWithClient(<PrivateDataNotice compact />);
    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText(/where this repository.s data goes/i)).toBeInTheDocument();
  });
});
