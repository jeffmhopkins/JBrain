// R3-M4 integration: pure `semantic` mode must NOT fire while embeddings aren't ready
// (it would return [] even with the server keyword fallback). The page should fall back
// to hybrid on mount and disable the semantic button.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import SearchPage from "./pages/SearchPage";
import { __reset, ingestVerify } from "./health";

function caps(embeddingsState: string): any {
  return { capabilities: {
    llm: { state: "ready", providers: { anthropic: true, xai: false } },
    embeddings: { state: embeddingsState }, transcription: { state: "ready" },
    push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
  } };
}

const fetchMock = vi.fn(async (url: any) => {
  const u = String(url);
  if (u.includes("/api/search")) return new Response("[]", { status: 200 });
  return new Response("{}", { status: 200 });
});

beforeEach(() => {
  __reset();
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.useFakeTimers();
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); cleanup(); });

function searchUrls(): string[] {
  return fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes("/api/search"));
}

describe("SearchPage semantic gate (R3-M4)", () => {
  it("falls back to hybrid (never fires a semantic request) while embeddings warm", async () => {
    ingestVerify(caps("warming"));
    render(
      <MemoryRouter initialEntries={["/search?mode=semantic&q=hi"]}>
        <SearchPage />
      </MemoryRouter>,
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });   // past the 200ms debounce
    const urls = searchUrls();
    expect(urls.length).toBeGreaterThan(0);
    expect(urls.some((u) => u.includes("mode=semantic"))).toBe(false);   // never doomed-semantic
    expect(urls.some((u) => u.includes("mode=hybrid"))).toBe(true);      // degraded to hybrid
    // The semantic mode button is disabled while embeddings aren't ready.
    const semanticBtn = screen.getByRole("button", { name: "semantic" }) as HTMLButtonElement;
    expect(semanticBtn.disabled).toBe(true);
  });

  it("allows semantic once embeddings are ready", async () => {
    ingestVerify(caps("ready"));
    render(
      <MemoryRouter initialEntries={["/search?mode=semantic&q=hi"]}>
        <SearchPage />
      </MemoryRouter>,
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    expect(searchUrls().some((u) => u.includes("mode=semantic"))).toBe(true);   // semantic fires
    const semanticBtn = screen.getByRole("button", { name: "semantic" }) as HTMLButtonElement;
    expect(semanticBtn.disabled).toBe(false);
  });
});
