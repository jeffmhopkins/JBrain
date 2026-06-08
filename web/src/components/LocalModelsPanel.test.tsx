// Component (Tier 2) tests for LocalModelsPanel — the "Local models (Ollama)" card.
// It receives the local-models payload from the parent and renders: the calm
// "not running" state, the installed list with remove, the curated pull section
// (excluding installed + disabling won't-fit), and a streamed pull with progress.
// Network: MSW only. The pull is an SSE stream faked via a ReadableStream body.
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within, fireEvent } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import LocalModelsPanel from "./LocalModelsPanel";
import type { LocalModelsResponse } from "../api";

const HW = { usable_ram_bytes: 24e9, total_ram_bytes: 32e9, cpu_only: true, note: "CPU-only —" };

function payload(over: Partial<LocalModelsResponse> = {}): LocalModelsResponse {
  return { running: true, models: [], hardware: HW, ...over };
}

afterEach(() => { server.resetHandlers(); vi.restoreAllMocks(); });

describe("LocalModelsPanel", () => {
  it("renders nothing until data is provided", () => {
    const { container } = renderWithProviders(<LocalModelsPanel data={null} onRefresh={() => {}} />);
    expect(container.querySelector(".card")).toBeNull();
  });

  it("shows the calm 'not running' state when Ollama is down", () => {
    renderWithProviders(<LocalModelsPanel data={payload({ running: false })} onRefresh={() => {}} />);
    expect(screen.getByText(/Ollama isn’t running/)).toBeInTheDocument();
    expect(screen.queryByText("Pull a model")).not.toBeInTheDocument();
  });

  it("lists installed models with a remove button", () => {
    const data = payload({ models: [
      { name: "qwen2.5:7b", size_bytes: 4.7e9, ram_estimate_bytes: 6e9, fits: true, warn: null, state: "ready" },
    ] });
    renderWithProviders(<LocalModelsPanel data={data} onRefresh={() => {}} />);
    expect(screen.getByText("qwen2.5:7b")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("offers curated models, excluding already-installed ones", () => {
    const data = payload({ models: [
      { name: "qwen2.5:7b", size_bytes: 4.7e9, ram_estimate_bytes: 6e9, fits: true, warn: null, state: "ready" },
    ] });
    renderWithProviders(<LocalModelsPanel data={data} onRefresh={() => {}} />);
    // qwen2.5 is installed → not offered; llama3.1 + llava still offered.
    const pull = screen.getByText("Pull a model").parentElement!;
    expect(within(pull).queryByText("Qwen2.5 7B")).not.toBeInTheDocument();
    expect(within(pull).getByText("Llama 3.1 8B")).toBeInTheDocument();
  });

  it("disables the Pull button for a model that won't fit the hardware", () => {
    renderWithProviders(<LocalModelsPanel data={payload({ hardware: { ...HW, usable_ram_bytes: 1e9 } })} onRefresh={() => {}} />);
    const llamaRow = screen.getByText("Llama 3.1 8B").closest(".row")!;
    expect((within(llamaRow).getByRole("button", { name: "Pull" }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(llamaRow).getByText(/won’t fit/)).toBeInTheDocument();
  });

  it("streams a pull to completion and refreshes the parent", async () => {
    const frames = [
      'data: {"type":"status","status":"pulling manifest"}\n\n',
      'data: {"type":"progress","completed":50,"total":100,"status":"downloading"}\n\n',
      'data: {"type":"done"}\n\n',
    ];
    server.use(http.post("*/api/system/local-models/pull", () => {
      const stream = new ReadableStream({
        start(c) { const enc = new TextEncoder(); frames.forEach((f) => c.enqueue(enc.encode(f))); c.close(); },
      });
      return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
    }));
    const onRefresh = vi.fn();
    renderWithProviders(<LocalModelsPanel data={payload()} onRefresh={onRefresh} />);

    const llamaRow = screen.getByText("Llama 3.1 8B").closest(".row")!;
    fireEvent.click(within(llamaRow).getByRole("button", { name: "Pull" }));

    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
  });

  it("surfaces a pull error frame", async () => {
    server.use(http.post("*/api/system/local-models/pull", () => {
      const stream = new ReadableStream({
        start(c) { c.enqueue(new TextEncoder().encode('data: {"type":"error","message":"no such model"}\n\n')); c.close(); },
      });
      return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
    }));
    renderWithProviders(<LocalModelsPanel data={payload()} onRefresh={() => {}} />);

    const llamaRow = screen.getByText("Llama 3.1 8B").closest(".row")!;
    fireEvent.click(within(llamaRow).getByRole("button", { name: "Pull" }));
    expect(await screen.findByText("no such model")).toBeInTheDocument();
  });

  it("deletes a model via the API and refreshes", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted = "";
    server.use(http.delete("*/api/system/local-models/:name", ({ params }) => {
      deleted = params.name as string;
      return HttpResponse.json({ removed: true });
    }));
    const data = payload({ models: [
      { name: "qwen2.5:7b", size_bytes: 4.7e9, ram_estimate_bytes: 6e9, fits: true, warn: null, state: "ready" },
    ] });
    const onRefresh = vi.fn();
    renderWithProviders(<LocalModelsPanel data={data} onRefresh={onRefresh} />);

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(deleted).toContain("qwen2.5");
  });
});
