// Component-integration (Tier 2) tests for MapPage — the Leaflet location surface:
// multi-person trail + note pins fetched into the people/legend chips, the history
// range presets refetching with the right `since`, the heatmap toggle, and the saved
// places panel (load / add via map-tap / inline edit-PATCH / delete).
//
// Leaflet & leaflet.heat are pure canvas/DOM-geometry libraries that don't run under
// jsdom, so we replace them with minimal fakes (see the vi.mock blocks): the map and
// every layer are chainable no-op stubs that record nothing about pixels. The ONE
// behaviour we keep is map event registration — `map.on("click", fn)` stashes the
// handler so a test can fire a synthetic map tap and drive the "drop a place" flow.
// Everything asserted here is DATA flow (requests fired + their bodies/params, and the
// React-rendered DOM: chips, the places list, control state), never Leaflet output.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so EVERY endpoint the
// page touches on mount + interaction is stubbed: /api/locations, /api/notes/located,
// /api/places, /api/people, plus POST/PATCH/DELETE on places.
//
// Determinism: the page computes `since` from Date.now(), so the clock is pinned with
// vi.setSystemTime (NOT fake timers — those deadlock MSW/userEvent's real scheduler;
// the 15s live-poll interval simply never fires within a test). Restored in afterEach.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within, cleanup, fireEvent } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import type { LocPoint, LocatedNote, Place, Person } from "../api";

// --------------------------------------------------------------------------- //
// Leaflet fake. Each constructor returns a chainable object whose methods are
// no-ops returning `this`, so the component's fluent `.bindTooltip().on().addTo()`
// chains work. The map records click/zoom handlers into `mapHandlers` so a test can
// invoke them (the only Leaflet behaviour we actually exercise).
// --------------------------------------------------------------------------- //
let mapHandlers: Record<string, (e: any) => void> = {};

vi.mock("leaflet", () => {
  const layer = () => {
    const o: any = {};
    const ret = () => o;
    o.addTo = ret; o.bindTooltip = ret; o.bindPopup = ret; o.on = ret;
    o.setLatLng = ret; o.setContent = ret; o.openOn = ret; o.openPopup = ret;
    o.addLayer = ret; o.removeLayer = ret;
    return o;
  };
  const map = () => {
    const m: any = {};
    m.setView = () => m;
    m.on = (ev: string, fn: (e: any) => void) => { mapHandlers[ev] = fn; return m; };
    m.invalidateSize = () => m;
    m.remove = () => m;
    m.removeLayer = () => m;
    m.addLayer = () => m;
    m.fitBounds = () => m;
    m.getZoom = () => 2;
    return m;
  };
  const L: any = {
    map,
    tileLayer: layer,
    layerGroup: layer,
    circle: layer,
    circleMarker: layer,
    polyline: layer,
    popup: layer,
    latLngBounds: (x: any) => x,
    // heatLayer present so the page takes the real-heat branch (a no-op layer).
    heatLayer: layer,
  };
  return { default: L, ...L };
});
// Side-effect import in the page; nothing to do under test.
vi.mock("leaflet.heat", () => ({}));

import MapPage from "./MapPage";

// A fixed instant so `since = now - hours` is deterministic.
const NOW = new Date("2026-06-15T12:00:00Z");

// Captured outbound mutations.
let locationCalls: { since: string | null }[] = [];
let notesCalls: { since: string | null }[] = [];
let placePosts: any[] = [];
let placePatches: { id: string; body: any }[] = [];
let placeDeletes: string[] = [];

function person(o: Partial<Person> & { id: number; name: string }): Person {
  return {
    color: "#4ea1ff",
    is_default: false,
    aliases: "",
    note_slug: null,
    location_key: null,
    ...o,
  };
}
function loc(o: Partial<LocPoint> & { id: number; lat: number; lon: number; recorded_at: string }): LocPoint {
  return { accuracy_m: null, source: null, ...o };
}
function placeOf(o: Partial<Place> & { id: number; name: string; lat: number; lon: number; radius_m: number }): Place {
  return { note_slug: null, ...o };
}

// Register every endpoint the page can hit. Pass the seed data per case.
function mapHandlersSetup(opts: {
  locations?: LocPoint[];
  notes?: LocatedNote[];
  places?: Place[];
  people?: Person[];
} = {}) {
  let places = opts.places ?? [];
  server.use(
    http.get("*/api/locations", ({ request }) => {
      locationCalls.push({ since: new URL(request.url).searchParams.get("since") });
      return HttpResponse.json(opts.locations ?? []);
    }),
    http.get("*/api/notes/located", ({ request }) => {
      notesCalls.push({ since: new URL(request.url).searchParams.get("since") });
      return HttpResponse.json(opts.notes ?? []);
    }),
    http.get("*/api/people", () => HttpResponse.json(opts.people ?? [])),
    http.get("*/api/places", () => HttpResponse.json(places)),
    http.post("*/api/places", async ({ request }) => {
      const body = await request.json();
      placePosts.push(body);
      return HttpResponse.json({ id: 999, name: (body as any).name });
    }),
    http.patch("*/api/places/:id", async ({ request, params }) => {
      placePatches.push({ id: String(params.id), body: await request.json() });
      return HttpResponse.json({ ok: true });
    }),
    http.delete("*/api/places/:id", ({ params }) => {
      placeDeletes.push(String(params.id));
      places = places.filter((p) => String(p.id) !== String(params.id));
      return HttpResponse.json({ ok: true });
    }),
  );
}

beforeEach(() => {
  vi.setSystemTime(NOW);
  mapHandlers = {};
  locationCalls = [];
  notesCalls = [];
  placePosts = [];
  placePatches = [];
  placeDeletes = [];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// --------------------------------------------------------------------------- //
// (a) Mount: trail + notes fetched for the default 24h window
// --------------------------------------------------------------------------- //
describe("MapPage — initial load", () => {
  it("fetches locations and located-notes for the default 24h window with a since param", async () => {
    mapHandlersSetup();
    renderWithProviders(<MapPage />);

    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));
    await waitFor(() => expect(notesCalls.length).toBeGreaterThanOrEqual(1));

    // Default range is 24h → since = NOW - 24h.
    const expected = new Date(NOW.getTime() - 24 * 3600000).toISOString();
    expect(locationCalls[0].since).toBe(expected);
    expect(notesCalls[0].since).toBe(expected);

    // The trail/heat segmented control renders with Trail active by default.
    expect(screen.getByRole("button", { name: "Trail" })).toHaveClass("on");
    expect(screen.getByRole("button", { name: "Heatmap" })).not.toHaveClass("on");
  });

  it("shows 'No location data yet' when there are no fixes or notes", async () => {
    mapHandlersSetup();
    renderWithProviders(<MapPage />);
    expect(await screen.findByText(/No location data yet/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// (b) People legend chips — one per person present in the loaded trail
// --------------------------------------------------------------------------- //
describe("MapPage — people filter chips", () => {
  it("renders a chip per person present in the trail and toggles one off on click", async () => {
    mapHandlersSetup({
      people: [
        person({ id: 1, name: "Alice", is_default: true, color: "#f00" }),
        person({ id: 2, name: "Bob", color: "#0f0", aliases: "bobby" }),
        // Carol has a person record but no fixes → no chip.
        person({ id: 3, name: "Carol", color: "#00f" }),
      ],
      locations: [
        loc({ id: 1, lat: 40, lon: -70, recorded_at: "2026-06-15 11:00:00", source: "Alice" }),
        loc({ id: 2, lat: 41, lon: -71, recorded_at: "2026-06-15 11:30:00", source: "bobby" }),
      ],
    });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };

    // Chips appear only when >1 person is present in the trail.
    const alice = await screen.findByRole("button", { name: /Alice/i });
    const bob = await screen.findByRole("button", { name: /Bob/i });
    expect(alice).toBeInTheDocument();
    expect(bob).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Carol/i })).not.toBeInTheDocument();

    // Toggling a chip off marks it (the trail then omits that person).
    expect(bob).not.toHaveClass("off");
    await user.click(bob);
    await waitFor(() => expect(screen.getByRole("button", { name: /Bob/i })).toHaveClass("off"));
  });
});

// --------------------------------------------------------------------------- //
// (c) Range select — switching the time window refetches with a new since
// --------------------------------------------------------------------------- //
describe("MapPage — range window", () => {
  it("changing the range select refetches locations + notes with the matching since", async () => {
    mapHandlersSetup();
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));

    const select = screen.getByRole("combobox");
    // Pick "1 week" (168h).
    await user.selectOptions(select, "168");
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(2));

    const expected = new Date(NOW.getTime() - 168 * 3600000).toISOString();
    expect(locationCalls[locationCalls.length - 1].since).toBe(expected);
    expect(notesCalls[notesCalls.length - 1].since).toBe(expected);
  });

  it("the 'All' range sends no since param", async () => {
    mapHandlersSetup();
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));

    await user.selectOptions(screen.getByRole("combobox"), "0");
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(2));
    expect(locationCalls[locationCalls.length - 1].since).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// (d) Heatmap toggle — control state flips and the slider appears
// --------------------------------------------------------------------------- //
describe("MapPage — heatmap toggle", () => {
  it("switching to Heatmap activates that mode and reveals the intensity slider", async () => {
    mapHandlersSetup({
      locations: [loc({ id: 1, lat: 40, lon: -70, recorded_at: "2026-06-15 11:00:00" })],
    });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));

    // No intensity slider in trail mode.
    expect(screen.queryByTitle(/Heat intensity/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Heatmap" }));
    expect(screen.getByRole("button", { name: "Heatmap" })).toHaveClass("on");
    expect(screen.getByRole("button", { name: "Trail" })).not.toHaveClass("on");
    // The intensity slider is now present.
    expect(screen.getByTitle(/Heat intensity/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// (e) Places panel — load list, delete, inline edit (PATCH body)
// --------------------------------------------------------------------------- //
describe("MapPage — places panel", () => {
  it("opening Places shows the loaded places with their radii", async () => {
    mapHandlersSetup({
      places: [
        placeOf({ id: 1, name: "Home", lat: 40, lon: -70, radius_m: 150 }),
        placeOf({ id: 2, name: "Work", lat: 41, lon: -71, radius_m: 300 }),
      ],
    });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));

    await user.click(screen.getByRole("button", { name: "Places" }));
    expect(await screen.findByRole("button", { name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Work" })).toBeInTheDocument();
    expect(screen.getByText("150 m")).toBeInTheDocument();
    expect(screen.getByText("300 m")).toBeInTheDocument();
  });

  it("shows the empty state when there are no places", async () => {
    mapHandlersSetup({ places: [] });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));

    await user.click(screen.getByRole("button", { name: "Places" }));
    expect(await screen.findByText(/No places yet/i)).toBeInTheDocument();
  });

  it("deleting a place DELETEs it and refetches the now-shorter list", async () => {
    mapHandlersSetup({
      places: [
        placeOf({ id: 1, name: "Home", lat: 40, lon: -70, radius_m: 150 }),
        placeOf({ id: 2, name: "Work", lat: 41, lon: -71, radius_m: 300 }),
      ],
    });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));
    await user.click(screen.getByRole("button", { name: "Places" }));
    await screen.findByRole("button", { name: "Home" });

    // The ✕ delete button for Home (first list item). Its accessible name is the
    // emoji text content ("✕"), so target it by its `title` tooltip instead.
    const homeItem = screen.getByRole("button", { name: "Home" }).closest("li")!;
    await user.click(within(homeItem).getByTitle("Delete"));

    await waitFor(() => expect(placeDeletes).toEqual(["1"]));
    // After the reload the handler returns the list without Home.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Home" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Work" })).toBeInTheDocument();
  });

  it("editing a place PATCHes only the changed fields (name + radius)", async () => {
    mapHandlersSetup({
      places: [placeOf({ id: 1, name: "Home", lat: 40, lon: -70, radius_m: 150 })],
    });
    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));
    await user.click(screen.getByRole("button", { name: "Places" }));
    await screen.findByRole("button", { name: "Home" });

    // Open the inline editor (✎). Its accessible name is the emoji text content,
    // so target it by its `title` tooltip instead.
    await user.click(screen.getByTitle("Edit name & fence size"));
    const nameInput = await screen.findByPlaceholderText("Place name");
    await user.clear(nameInput);
    await user.type(nameInput, "Casa");
    // Bump the numeric fence size. The number input clamps per-keystroke
    // (Math.max(20, Math.min(value, 20000))), so typing digit-by-digit lands on
    // intermediate clamped values; set the final value in one change instead.
    const numInput = screen.getByRole("spinbutton");
    fireEvent.change(numInput, { target: { value: "500" } });

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(placePatches).toHaveLength(1));
    expect(placePatches[0].id).toBe("1");
    expect(placePatches[0].body).toEqual({ name: "Casa", radius_m: 500 });
  });
});

// --------------------------------------------------------------------------- //
// (f) Add place via map tap — "+ Add" arms add-mode; a map click POSTs the place
// --------------------------------------------------------------------------- //
describe("MapPage — add a place", () => {
  it("arming '+ Add' then tapping the map POSTs a new place at those coords", async () => {
    mapHandlersSetup({ places: [] });
    // The component reads the new name from window.prompt.
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("Cafe");

    const { user } = { user: userEvent.setup(), ...renderWithProviders(<MapPage />) };
    await waitFor(() => expect(locationCalls.length).toBeGreaterThanOrEqual(1));
    await user.click(screen.getByRole("button", { name: "Places" }));

    // Arm add-mode; the button label switches to the tap prompt.
    await user.click(await screen.findByRole("button", { name: /\+ Add/i }));
    expect(await screen.findByRole("button", { name: /Tap the map/i })).toBeInTheDocument();

    // Fire a synthetic Leaflet map click — the page's click handler drops a place.
    expect(mapHandlers.click).toBeDefined();
    mapHandlers.click({ latlng: { lat: 12.5, lng: -8.25 } });

    await waitFor(() => expect(placePosts).toHaveLength(1));
    expect(placePosts[0]).toEqual({ name: "Cafe", lat: 12.5, lon: -8.25, radius_m: 150 });

    promptSpy.mockRestore();
  });
});
