import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, jsonResponse, renderWithClient } from "@/test/utils";
import { getBrowserKey, setBrowserKey } from "@/lib/browser-keys";

import { ProviderSettings } from "./provider-settings";

const KEY = "sk-live-SECRET-0123456789abcd";
const ACCOUNT = "0123456789abcdef0123456789abcdef";

const CATALOG = {
  chat_providers: [
    { id: "openrouter", label: "OpenRouter", default_model: "openai/gpt-4o-mini" },
    { id: "openai", label: "OpenAI", default_model: "gpt-4o-mini" },
  ],
  embedding_providers: [
    { id: "cloudflare", label: "Cloudflare Workers AI", models: ["@cf/baai/bge-small-en-v1.5"], needs_account_id: true },
    { id: "openai", label: "OpenAI", models: ["text-embedding-3-small", "text-embedding-3-large"], needs_account_id: false },
  ],
  embedding: null,
  chat: null,
};

let state: typeof CATALOG & Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;
let saved: { slot: string; body: Record<string, unknown> }[];

beforeEach(() => {
  window.sessionStorage.clear();
  state = structuredClone(CATALOG);
  saved = [];
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/providers" && method === "GET") return jsonResponse(state);
    const m = /^\/api\/providers\/(embedding|chat)$/.exec(url);
    if (m && method === "PUT") {
      const body = JSON.parse(String(init?.body));
      saved.push({ slot: m[1], body });
      const row = {
        slot: m[1],
        kind: body.kind,
        mode: body.mode,
        model: body.model ?? null,
        account_id: body.account_id ?? null,
        key_last4: body.api_key ? String(body.api_key).slice(-4) : null,
        updated_at: "2026-11-18T00:00:00Z",
      };
      (state as Record<string, unknown>)[m[1]] = row;
      return jsonResponse(row);
    }
    if (m && method === "DELETE") {
      (state as Record<string, unknown>)[m[1]] = null;
      return new Response(null, { status: 204 });
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const form = (name: RegExp) => screen.findByRole("form", { name });

describe("ProviderSettings", () => {
  it("explains what is sent where and starts with nothing set up", async () => {
    renderWithClient(<ProviderSettings />);
    expect(await screen.findByText(/never sent to them/i)).toBeInTheDocument();
    expect(screen.getAllByText("not set up")).toHaveLength(2);
    expect(screen.getByText(/every chunk of code in a private repository is sent/i)).toBeInTheDocument();
  });

  it("saves a stored chat key, shows only its last four characters and clears the field", async () => {
    renderWithClient(<ProviderSettings />);
    const chat = within(await form(/chat model provider/i));
    await userEvent.selectOptions(chat.getByLabelText("Provider"), "openai");
    await userEvent.type(chat.getByLabelText("API key"), KEY);
    await userEvent.click(chat.getByRole("button", { name: "Save" }));

    expect(saved[0]).toEqual({ slot: "chat", body: { kind: "openai", mode: "stored", api_key: KEY } });
    expect(await screen.findByTestId("current-chat")).toHaveTextContent("stored key ending abcd");
    expect(screen.getByTestId("current-chat")).not.toHaveTextContent(KEY);
    expect(document.body.textContent).not.toContain(KEY);
    expect(chat.getByLabelText("API key")).toHaveValue(""); // not left sitting in the form
    expect(getBrowserKey("chat")).toBeNull();
    expect(chat.getByLabelText("API key")).toHaveAttribute("type", "password");
  });

  it("browser-only mode keeps the key in this tab and never sends it to the server", async () => {
    renderWithClient(<ProviderSettings />);
    const chat = within(await form(/chat model provider/i));
    await userEvent.click(chat.getByLabelText(/browser only/i));
    await userEvent.type(chat.getByLabelText("API key"), KEY);
    await userEvent.click(chat.getByRole("button", { name: "Save" }));

    await screen.findByTestId("current-chat");
    expect(saved[0].body).toEqual({ kind: "openrouter", mode: "browser" }); // no api_key at all
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain(KEY);
    expect(getBrowserKey("chat")).toBe(KEY);
    expect(screen.getByTestId("current-chat")).toHaveTextContent("key held in this tab");
  });

  it("says when a browser-only key is missing from this tab and asks for it again", async () => {
    (state as Record<string, unknown>).chat = {
      slot: "chat", kind: "openai", mode: "browser", model: null, account_id: null, key_last4: null, updated_at: "x",
    };
    renderWithClient(<ProviderSettings />);
    expect(await screen.findByTestId("current-chat")).toHaveTextContent("key not in this tab");
    const chat = within(await form(/chat model provider/i));
    await userEvent.click(chat.getByRole("button", { name: "Update" }));
    expect(await chat.findByRole("alert")).toHaveTextContent(/enter the key again/i);
    expect(saved).toHaveLength(0);
  });

  it("asks for a Cloudflare account id and offers only 384-dimension models", async () => {
    renderWithClient(<ProviderSettings />);
    const emb = within(await form(/embeddings provider/i));
    expect(emb.getByLabelText("Cloudflare account id")).toBeInTheDocument();
    await userEvent.type(emb.getByLabelText("Cloudflare account id"), ACCOUNT);
    await userEvent.type(emb.getByLabelText("API key"), KEY);
    await userEvent.click(emb.getByRole("button", { name: "Save" }));
    await screen.findByTestId("current-embedding");
    expect(saved[0].body).toMatchObject({ kind: "cloudflare", account_id: ACCOUNT, mode: "stored" });

    await userEvent.selectOptions(emb.getByLabelText("Provider"), "openai");
    expect(emb.queryByLabelText("Cloudflare account id")).not.toBeInTheDocument();
    expect(emb.getAllByRole("option").map((o) => o.textContent)).toEqual(
      expect.arrayContaining(["text-embedding-3-small", "text-embedding-3-large"]),
    );
  });

  it("refuses an empty key without calling the server", async () => {
    renderWithClient(<ProviderSettings />);
    const chat = within(await form(/chat model provider/i));
    await userEvent.click(chat.getByRole("button", { name: "Save" }));
    expect(await chat.findByRole("alert")).toHaveTextContent(/enter the api key/i);
    expect(saved).toHaveLength(0);
  });

  it("shows the server's reason when saving fails", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) =>
      (init?.method ?? "GET") === "PUT"
        ? apiError(422, "invalid_provider", "The Cloudflare account id is 32 lowercase hex characters.")
        : jsonResponse(state),
    );
    renderWithClient(<ProviderSettings />);
    const emb = within(await form(/embeddings provider/i));
    await userEvent.type(emb.getByLabelText("Cloudflare account id"), "nope");
    await userEvent.type(emb.getByLabelText("API key"), KEY);
    await userEvent.click(emb.getByRole("button", { name: "Save" }));
    expect(await emb.findByRole("alert")).toHaveTextContent(/32 lowercase hex/i);
  });

  it("removes a provider after confirmation and forgets the browser-held key", async () => {
    (state as Record<string, unknown>).chat = {
      slot: "chat", kind: "openai", mode: "browser", model: null, account_id: null, key_last4: null, updated_at: "x",
    };
    setBrowserKey("chat", KEY);
    renderWithClient(<ProviderSettings />);
    const chat = within(await form(/chat model provider/i));
    await userEvent.click(chat.getByRole("button", { name: "Remove" }));
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === "DELETE")).toBe(false);
    await userEvent.click(chat.getByRole("button", { name: /remove it/i }));
    await vi.waitFor(() => expect(getBrowserKey("chat")).toBeNull());
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === "DELETE")).toBe(true);
  });

  it("asks to sign in again when GitHub authorization is gone", async () => {
    fetchMock.mockImplementation(async () => apiError(401, "reauth_required", "Please sign in."));
    renderWithClient(<ProviderSettings />);
    expect(await screen.findByRole("link", { name: /sign in again/i })).toBeInTheDocument();
  });
});
