// Component (Tier 2) tests for ModelPicker — the per-tier LLM model selector. On mount
// it GETs /api/prompts and seeds one <select> per tier; selecting writes the matching
// prompt override via PUT /api/prompts/<key>. A chosen model whose provider key isn't
// configured raises an inline warning — the llm capability's `providers` map (seeded
// into the health store via ingestVerify) gates that copy.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so the /api/prompts GET
// and the per-select PUT are stubbed via server.use(...). The health store is reset +
// seeded per test so useCapability("llm").providers is deterministic.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";
import ModelPicker from "./ModelPicker";

const PROMPTS = [
  { key: "agent.model", effective: "" },
  { key: "models.cheap", effective: "claude-sonnet-4-6" },
  { key: "models.synthesis", effective: "" },
  { key: "models.vision", effective: "" },
  { key: "models.default", effective: "some-custom-model" },
];

// Seed health with both provider keys present unless a test overrides it.
function seedCaps(providers: { anthropic: boolean; xai: boolean } = { anthropic: true, xai: true }) {
  ingestVerify({
    capabilities: {
      llm: { state: "ready", providers },
      embeddings: { state: "ready" }, transcription: { state: "ready" },
      push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
    },
  });
}

beforeEach(() => { __reset(); });
afterEach(() => { server.resetHandlers(); });

describe("ModelPicker", () => {
  it("loads /api/prompts and reflects each tier's effective model", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    seedCaps();
    renderWithProviders(<ModelPicker />);

    // The cheap tier's select shows the loaded value once the fetch resolves.
    const cheapRow = (await screen.findByText("Routine jobs")).closest("label")!;
    await waitFor(() =>
      expect((within(cheapRow).getByRole("combobox") as HTMLSelectElement).value).toBe("claude-sonnet-4-6"));
    // One select per tier (5).
    expect(screen.getAllByRole("combobox")).toHaveLength(5);
  });

  it("renders a (custom) option for an effective model not in the known list", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    seedCaps();
    renderWithProviders(<ModelPicker />);

    const row = (await screen.findByText("Fallback")).closest("label")!;
    await waitFor(() =>
      expect((within(row).getByRole("combobox") as HTMLSelectElement).value).toBe("some-custom-model"));
    expect(within(row).getByText("some-custom-model (custom)")).toBeInTheDocument();
  });

  it("selecting a model PUTs /api/prompts/<key> with the chosen value", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    let putKey: string | null = null;
    let putBody: any = null;
    server.use(
      http.put("*/api/prompts/:key", async ({ params, request }) => {
        putKey = params.key as string;
        putBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    seedCaps();
    const { user } = renderWithProviders(<ModelPicker />);

    const row = (await screen.findByText("Chat agent")).closest("label")!;
    await user.selectOptions(within(row).getByRole("combobox"), "claude-opus-4-8");

    await waitFor(() => expect(putKey).toBe("agent.model"));
    expect(putBody).toEqual({ value: "claude-opus-4-8" });
  });

  it("warns when a selected model needs a provider key that isn't configured", async () => {
    // xai key absent + a Grok model selected on a tier → the missing-key warning shows.
    server.use(http.get("*/api/prompts", () =>
      HttpResponse.json(PROMPTS.map((p) => p.key === "models.vision" ? { ...p, effective: "grok-4.3" } : p))));
    seedCaps({ anthropic: true, xai: false });
    renderWithProviders(<ModelPicker />);

    expect(await screen.findByText(/Grok \(xAI\) model is selected but its API key/)).toBeInTheDocument();
    expect(screen.getByText(/set XAI_API_KEY/)).toBeInTheDocument();
  });

  it("shows no missing-key warning when the chosen model's provider key is present", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    seedCaps({ anthropic: true, xai: true });
    renderWithProviders(<ModelPicker />);

    await screen.findByText("Chat agent"); // wait for load
    expect(screen.queryByText(/API key/)).not.toBeInTheDocument();
  });

  const LOCAL = [
    { name: "qwen2.5:7b", size_bytes: 4.7e9, ram_estimate_bytes: 6e9, fits: true, warn: null, state: "ready" as const },
    { name: "llama3.3:70b", size_bytes: 4e10, ram_estimate_bytes: 5e10, fits: false, warn: null, state: "ready" as const },
  ];

  it("lists installed local models in a local optgroup and recommends them for routine jobs", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    seedCaps();
    renderWithProviders(<ModelPicker localModels={LOCAL} />);

    const row = (await screen.findByText("Routine jobs")).closest("label")!;
    expect(within(row).getByText("qwen2.5:7b")).toBeInTheDocument();
    expect(screen.getByText(/Recommended: run routine jobs/)).toBeInTheDocument();
  });

  it("assigning a local model PUTs the bare Ollama id and shows no missing-key warning", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    let putBody: any = null;
    server.use(http.put("*/api/prompts/:key", async ({ request }) => {
      putBody = await request.json();
      return HttpResponse.json({ ok: true });
    }));
    seedCaps({ anthropic: true, xai: false });   // no xai key, but local needs none
    const { user } = renderWithProviders(<ModelPicker localModels={LOCAL} />);

    const row = (await screen.findByText("Routine jobs")).closest("label")!;
    await user.selectOptions(within(row).getByRole("combobox"), "qwen2.5:7b");

    await waitFor(() => expect(putBody).toEqual({ value: "qwen2.5:7b" }));
    expect(screen.queryByText(/API key/)).not.toBeInTheDocument();
  });

  it("disables a local model option that won't fit the hardware", async () => {
    server.use(http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)));
    seedCaps();
    renderWithProviders(<ModelPicker localModels={LOCAL} />);

    const row = (await screen.findByText("Routine jobs")).closest("label")!;
    const opt = within(row).getByRole("option", { name: /llama3\.3:70b — won’t fit/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
  });
});
