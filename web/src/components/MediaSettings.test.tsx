// Component (Tier 2) tests for MediaSettings — the "Media & transcription" card. On
// mount it GETs /api/system/settings/media and renders the Whisper model + compute-type
// selects, the frame-interval text input, and the max-frames number input from the
// returned options. Save PUTs only the four editable fields (max coerced to a Number)
// via /api/system/settings/media and shows the saved confirmation; a failed save shows
// the server's error message. Until the GET resolves the card renders nothing.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so the media GET and the
// save PUT are stubbed via server.use(...). Controlled inputs go through fireEvent.change
// to avoid user-event's focus-steal on re-render.
import { afterEach, describe, expect, it } from "vitest";
import { renderWithProviders, screen, waitFor, within, fireEvent } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import MediaSettings from "./MediaSettings";

const MEDIA = {
  audio_model: "tiny", audio_compute_type: "int8",
  video_frame_interval: "30s", video_frame_max: 8,
  audio_model_options: ["tiny", "base", "small"],
  compute_type_options: ["int8", "float16"],
};

afterEach(() => { server.resetHandlers(); });

describe("MediaSettings", () => {
  it("renders nothing until the settings GET resolves", async () => {
    server.use(http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)));
    const { container } = renderWithProviders(<MediaSettings />);
    // First paint (pre-fetch) is empty.
    expect(container.querySelector(".card")).toBeNull();
    // After the GET resolves the card appears.
    expect(await screen.findByText("Media & transcription")).toBeInTheDocument();
  });

  it("populates the model/compute selects + frame inputs from the loaded settings", async () => {
    server.use(http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)));
    renderWithProviders(<MediaSettings />);

    await screen.findByText("Media & transcription");
    const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
    expect(selects[0].value).toBe("tiny");           // audio model
    expect(selects[1].value).toBe("int8");           // compute type
    // Both option lists came from the payload.
    expect(within(selects[0]).getByText("small")).toBeInTheDocument();
    expect(within(selects[1]).getByText("float16")).toBeInTheDocument();
    expect((screen.getByPlaceholderText("30s or 25%") as HTMLInputElement).value).toBe("30s");
    expect((screen.getByRole("spinbutton") as HTMLInputElement).value).toBe("8");
  });

  it("saving PUTs only the four editable fields (max coerced to Number) and confirms", async () => {
    server.use(http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)));
    let putBody: any = null;
    server.use(
      http.put("*/api/system/settings/media", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ ...MEDIA, ...putBody });
      }),
    );
    renderWithProviders(<MediaSettings />);

    await screen.findByText("Media & transcription");
    const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
    fireEvent.change(selects[0], { target: { value: "small" } });
    fireEvent.change(selects[1], { target: { value: "float16" } });
    fireEvent.change(screen.getByPlaceholderText("30s or 25%"), { target: { value: "25%" } });
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).toEqual({
      audio_model: "small", audio_compute_type: "float16",
      video_frame_interval: "25%", video_frame_max: 12,
    }));
    // The option-list keys are NOT sent.
    expect(putBody).not.toHaveProperty("audio_model_options");
    expect(await screen.findByText(/new transcriptions use these right away/)).toBeInTheDocument();
  });

  it("treats a blank max-frames as 0 in the saved body", async () => {
    server.use(http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)));
    let putBody: any = null;
    server.use(
      http.put("*/api/system/settings/media", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ ...MEDIA, ...putBody });
      }),
    );
    renderWithProviders(<MediaSettings />);

    await screen.findByText("Media & transcription");
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody.video_frame_max).toBe(0);
  });

  it("surfaces the server error message when the save fails", async () => {
    server.use(http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)));
    server.use(
      http.put("*/api/system/settings/media", () =>
        HttpResponse.json({ detail: "Disk full" }, { status: 500 })),
    );
    renderWithProviders(<MediaSettings />);

    await screen.findByText("Media & transcription");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Disk full")).toBeInTheDocument();
  });
});
