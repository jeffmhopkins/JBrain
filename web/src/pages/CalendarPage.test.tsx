// Component-integration (Tier 2) tests for CalendarPage — the temporal-projection
// surface: day/week/month/list view rendering for a pinned "today", view switching,
// quick-add (with reminder presets, timed vs all-day), recurring-event expansion,
// and the event sheet's reminder + remove flows.
//
// Network: MSW only (setup.ts runs onUnhandledRequest: "error"), so every calendar
// endpoint the page touches is stubbed — range/upcoming/history reads, recently-added
// (the ReviewBanner fires on every mount), quick-add, reminders get/set, dismiss.
//
// Determinism (this is date/TZ code):
//   - useAuth() is stubbed to appTz:"UTC" so todayIso()/nowMinutes() are wall-clock
//     stable regardless of the host machine's zone (same trick NotePage.test uses).
//   - The clock is pinned with vi.setSystemTime(2026-06-15T12:00:00Z) (a Monday, noon
//     UTC) so "today", the week/month projections, and the now-line are fixed. We pin
//     ONLY the system clock (not the timer loop): MSW + userEvent rely on the real
//     microtask/timer scheduler to resolve, so faking timers would deadlock the fetch
//     resolution. The page's 60s now-line interval simply never fires in-test.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import type { CalEvent, Reminder } from "../api";

// Stub useAuth so the page reads a fixed display timezone (UTC) without dragging in
// the whole App auth/router tree. UTC keeps the Intl-based "today"/"now" helpers
// independent of the host machine's zone.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import CalendarPage from "./CalendarPage";

// A fixed instant: Monday 2026-06-15, 12:00 UTC. With appTz "UTC" this makes
// today = "2026-06-15" and nowMinutes = 720 (noon).
const NOW = new Date("2026-06-15T12:00:00Z");

// Build a CalEvent with sane defaults; override per case.
function ev(o: Partial<CalEvent> & { id: number; title: string; starts_at: string | null }): CalEvent {
  return {
    kind: "event",
    ends_at: null,
    all_day: 0,
    status: "active",
    location_label: null,
    note_slug: null,
    recurring: false,
    ...o,
  } as CalEvent;
}

// Captured outbound requests so tests can assert routing + bodies without reaching
// into component internals.
let rangeCalls: { start: string | null; end: string | null }[] = [];
let quickAdds: any[] = [];
let setReminderCalls: { id: string; reminders: Reminder[] }[] = [];
let dismissedIds: string[] = [];

// Register every calendar endpoint the page can hit. `events` seeds the view reads;
// list reads reuse the same set (upcoming) / an empty set (history) by default.
function calHandlers(events: CalEvent[], opts: { history?: CalEvent[] } = {}) {
  server.use(
    http.get("*/api/calendar/range", ({ request }) => {
      const url = new URL(request.url);
      rangeCalls.push({ start: url.searchParams.get("start"), end: url.searchParams.get("end") });
      return HttpResponse.json(events);
    }),
    http.get("*/api/calendar/upcoming", () => HttpResponse.json(events)),
    http.get("*/api/calendar/history", () => HttpResponse.json(opts.history ?? [])),
    // The ReviewBanner fires this on every mount; empty → renders nothing.
    http.get("*/api/calendar/recently-added", () =>
      HttpResponse.json({ since: "2026-06-01", events: [] })),
    http.post("*/api/calendar/quick-add", async ({ request }) => {
      quickAdds.push(await request.json());
      return HttpResponse.json({ note_id: 99, note_title: "notes/New", event: null });
    }),
    http.get("*/api/calendar/events/:id/reminders", () =>
      HttpResponse.json({ reminders: [] })),
    http.post("*/api/calendar/events/:id/reminders", async ({ request, params }) => {
      const body = (await request.json()) as { reminders: Reminder[] };
      setReminderCalls.push({ id: String(params.id), reminders: body.reminders });
      return HttpResponse.json({ count: body.reminders.length });
    }),
    http.post("*/api/calendar/events/:id/dismiss", ({ params }) => {
      dismissedIds.push(String(params.id));
      return HttpResponse.json({ identity_key: `ik-${params.id}` });
    }),
  );
}

// Render starting on a given view by pre-seeding the persisted view choice, so we
// land on it without a click (and exercise the LS_VIEW restore path).
function renderOn(view: "list" | "day" | "week" | "month") {
  localStorage.setItem("jbrain_cal_view", view);
  return {
    user: userEvent.setup(),
    ...renderWithProviders(<CalendarPage />),
  };
}

beforeEach(() => {
  vi.setSystemTime(NOW);
  rangeCalls = [];
  quickAdds = [];
  setReminderCalls = [];
  dismissedIds = [];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------- //
// (a) Month view — fixed today, events land in the right day + the day panel
// ---------------------------------------------------------------------------- //
describe("CalendarPage — month view", () => {
  it("requests the month's six-week range and places events on their days", async () => {
    calHandlers([
      ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
      ev({ id: 2, title: "Dentist", starts_at: "2026-06-18T14:30:00" }),
      ev({ id: 3, title: "Holiday", starts_at: "2026-06-15", all_day: 1 }),
    ]);
    renderOn("month");

    // The page fetches the 42-cell grid for June 2026: the week containing June 1
    // (a Monday) starts Sunday May 31; 41 days later is July 11.
    await waitFor(() => expect(rangeCalls).toHaveLength(1));
    expect(rangeCalls[0]).toEqual({ start: "2026-05-31", end: "2026-07-11" });

    // Period label reflects the pinned month.
    expect(screen.getByText("Jun 2026")).toBeInTheDocument();

    // June 15 is today AND the default selected day, so its events show in the panel.
    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("Holiday")).toBeInTheDocument();
    // June 18's event is in the grid (dots) but not in the June-15 panel yet.
    expect(screen.queryByText("Dentist")).not.toBeInTheDocument();

    // Today's cell is labelled as today; June 18 carries its event count.
    expect(
      screen.getByRole("button", { name: /Mon, Jun 15.*today.*selected/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Thu, Jun 18, 1 event/i }),
    ).toBeInTheDocument();
  });

  it("selecting a different day swaps the day panel to that day's events", async () => {
    calHandlers([
      ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
      ev({ id: 2, title: "Dentist", starts_at: "2026-06-18T14:30:00" }),
    ]);
    const { user } = renderOn("month");
    await screen.findByText("Standup");

    await user.click(screen.getByRole("button", { name: /Thu, Jun 18, 1 event/i }));
    expect(await screen.findByText("Dentist")).toBeInTheDocument();
    expect(screen.queryByText("Standup")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Week view — Sunday-anchored 7-day range with per-day sections
// ---------------------------------------------------------------------------- //
describe("CalendarPage — week view", () => {
  it("requests the Sunday-anchored week and sections events under their day", async () => {
    calHandlers([
      ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
      ev({ id: 2, title: "Dentist", starts_at: "2026-06-18T14:30:00" }),
    ]);
    renderOn("week");

    // Cursor June 15 (Monday) → week starts Sunday June 14, ends June 20.
    await waitFor(() => expect(rangeCalls).toHaveLength(1));
    expect(rangeCalls[0]).toEqual({ start: "2026-06-14", end: "2026-06-20" });

    // Both events render (one per day section), with their times.
    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("Dentist")).toBeInTheDocument();
    expect(screen.getByText("09:00")).toBeInTheDocument();
    expect(screen.getByText("14:30")).toBeInTheDocument();
    // The Monday header is flagged as today.
    expect(screen.getByText(/MON Jun 15 · today/i)).toBeInTheDocument();
  });

  it("stepping to the next week refetches the following Sunday-anchored range", async () => {
    calHandlers([]);
    const { user } = renderOn("week");
    await waitFor(() => expect(rangeCalls).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: /Next period/i }));
    await waitFor(() => expect(rangeCalls).toHaveLength(2));
    expect(rangeCalls[1]).toEqual({ start: "2026-06-21", end: "2026-06-27" });
  });
});

// ---------------------------------------------------------------------------- //
// (c) Day view — time-axis grid, all-day lane, timed blocks, the now-line
// ---------------------------------------------------------------------------- //
describe("CalendarPage — day view", () => {
  it("renders the all-day lane and timed blocks for the cursor day", async () => {
    calHandlers([
      ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00", ends_at: "2026-06-15T09:30:00" }),
      ev({ id: 3, title: "Holiday", starts_at: "2026-06-15", all_day: 1 }),
      // An event on another day must NOT appear in this single-day view.
      ev({ id: 2, title: "Dentist", starts_at: "2026-06-18T14:30:00" }),
    ]);
    renderOn("day");

    await waitFor(() => expect(rangeCalls).toHaveLength(1));
    expect(rangeCalls[0]).toEqual({ start: "2026-06-15", end: "2026-06-15" });

    // Header for the pinned day.
    expect(screen.getByText(/Mon Jun 15/i)).toBeInTheDocument();
    // All-day pill in the ALL DAY lane; timed event as a block; nothing from June 18.
    expect(await screen.findByText("Holiday")).toBeInTheDocument();
    expect(screen.getByText("Standup")).toBeInTheDocument();
    expect(screen.queryByText("Dentist")).not.toBeInTheDocument();

    // The now-line renders because today === cursor and 12:00 is within the axis.
    const now = document.querySelector(".cal-now") as HTMLElement | null;
    expect(now).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------- //
// (d) List view — grouped by day with TODAY/TOMORROW headers
// ---------------------------------------------------------------------------- //
describe("CalendarPage — list view", () => {
  it("groups upcoming events under relative-date headers", async () => {
    calHandlers([
      ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
      ev({ id: 2, title: "Dentist", starts_at: "2026-06-16T14:30:00" }),
    ]);
    renderOn("list");

    // List view hits upcoming, not range.
    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(rangeCalls).toHaveLength(0);
    expect(screen.getByText(/TODAY ·/i)).toBeInTheDocument();
    expect(screen.getByText(/TOMORROW ·/i)).toBeInTheDocument();
  });

  it("switches to the Past tab and reads history", async () => {
    let historyHit = 0;
    calHandlers([ev({ id: 1, title: "Standup", starts_at: "2026-06-15T09:00:00" })], {
      history: [ev({ id: 9, title: "Old Meeting", starts_at: "2026-01-02T10:00:00" })],
    });
    server.use(
      http.get("*/api/calendar/history", () => {
        historyHit++;
        return HttpResponse.json([ev({ id: 9, title: "Old Meeting", starts_at: "2026-01-02T10:00:00" })]);
      }),
    );
    const { user } = renderOn("list");
    await screen.findByText("Standup");

    await user.click(screen.getByRole("button", { name: /^Past$/i }));
    expect(await screen.findByText("Old Meeting")).toBeInTheDocument();
    expect(historyHit).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------- //
// (e) View switching persists to localStorage
// ---------------------------------------------------------------------------- //
describe("CalendarPage — view switching", () => {
  it("switching views refetches and persists the choice", async () => {
    calHandlers([]);
    const { user } = renderOn("list");
    await waitFor(() => expect(screen.queryByText(/Loading…/i)).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /^Week$/i }));
    await waitFor(() => expect(localStorage.getItem("jbrain_cal_view")).toBe("week"));
    await waitFor(() => expect(rangeCalls.length).toBeGreaterThanOrEqual(1));

    await user.click(screen.getByRole("button", { name: /^Month$/i }));
    expect(localStorage.getItem("jbrain_cal_view")).toBe("month");
  });
});

// ---------------------------------------------------------------------------- //
// (f) Quick-add — timed event with a timed reminder preset
// ---------------------------------------------------------------------------- //
describe("CalendarPage — quick-add", () => {
  it("creates a timed event from the day grid with a timed reminder preset", async () => {
    calHandlers([]);
    const { user } = renderOn("day");
    await waitFor(() => expect(rangeCalls).toHaveLength(1));

    // The Day header has a "+ Add event" button → opens the quick-add sheet pre-set
    // to the cursor date.
    await user.click(screen.getByRole("button", { name: /\+ Add event/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByPlaceholderText(/What…/i), "Lunch");
    // Give it a time → all-day is off, so TIMED presets are shown. The TIME <label>
    // isn't associated with its input (sibling in a .cal-field div), so target the
    // type="time" input directly. type="time" inputs take a HH:MM value.
    const timeInput = dialog.querySelector('input[type="time"]') as HTMLInputElement;
    await user.clear(timeInput);
    await user.type(timeInput, "13:00");
    // Pick the "30m" timed reminder preset.
    await user.click(within(dialog).getByRole("button", { name: /^30m$/i }));

    await user.click(within(dialog).getByRole("button", { name: /^Add$/i }));

    await waitFor(() => expect(quickAdds).toHaveLength(1));
    expect(quickAdds[0]).toMatchObject({
      title: "Lunch",
      date: "2026-06-15",
      time: "13:00",
      kind: "event",
      reminders: [{ offset_minutes: 30, anchor: "start" }],
    });
  });

  it("creates an all-day event (no time) with an all-day reminder preset", async () => {
    calHandlers([]);
    const { user } = renderOn("month");
    await screen.findByText("Jun 2026");

    // The selected-day group offers a "+ Add" that opens quick-add for that day with
    // no time → all-day, so ALLDAY presets show.
    await user.click(screen.getByRole("button", { name: /^\+ Add$/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByPlaceholderText(/What…/i), "Birthday");
    // No time entered → all-day. Pick the "Evening before" all-day preset (off 900).
    await user.click(within(dialog).getByRole("button", { name: /Evening before/i }));

    await user.click(within(dialog).getByRole("button", { name: /^Add$/i }));

    await waitFor(() => expect(quickAdds).toHaveLength(1));
    expect(quickAdds[0]).toMatchObject({
      title: "Birthday",
      date: "2026-06-15",
      kind: "event",
      reminders: [{ offset_minutes: 900, anchor: "day_of" }],
    });
    // All-day → no `time` field sent.
    expect(quickAdds[0].time).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------- //
// (g) Recurring event expansion across a view
// ---------------------------------------------------------------------------- //
describe("CalendarPage — recurring events", () => {
  it("renders a recurring marker for an event expanded into multiple week days", async () => {
    // The server expands a recurrence server-side into one row per occurrence; the
    // page just buckets them by day. Same id, distinct starts_at across the week.
    calHandlers([
      ev({ id: 7, title: "Daily Walk", starts_at: "2026-06-15T08:00:00", recurring: true }),
      ev({ id: 7, title: "Daily Walk", starts_at: "2026-06-16T08:00:00", recurring: true }),
      ev({ id: 7, title: "Daily Walk", starts_at: "2026-06-17T08:00:00", recurring: true }),
    ]);
    renderOn("week");

    // All three occurrences render (one per day section).
    const occurrences = await screen.findAllByText("Daily Walk");
    expect(occurrences).toHaveLength(3);
    // Each is flagged recurring (the ↻ marker, labelled "recurring").
    expect(screen.getAllByLabelText("recurring").length).toBeGreaterThanOrEqual(3);
  });
});

// ---------------------------------------------------------------------------- //
// (h) Event sheet — reminders load + save, and Remove-from-calendar dismiss
// ---------------------------------------------------------------------------- //
describe("CalendarPage — event sheet", () => {
  it("toggling a reminder preset POSTs the full set for the event", async () => {
    calHandlers([
      ev({ id: 5, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
    ]);
    const { user } = renderOn("week");
    await screen.findByText("Standup");

    await user.click(screen.getByRole("button", { name: /Standup/i }));
    const dialog = await screen.findByRole("dialog");
    // It's a timed event → timed presets. Toggle "1h" (off 60).
    await user.click(within(dialog).getByRole("button", { name: /^1h$/i }));

    await waitFor(() => expect(setReminderCalls).toHaveLength(1));
    expect(setReminderCalls[0].id).toBe("5");
    expect(setReminderCalls[0].reminders).toEqual([{ offset_minutes: 60, anchor: "start" }]);
    expect(await within(dialog).findByText(/Reminders saved/i)).toBeInTheDocument();
  });

  it("Remove from calendar dismisses the event and shows an undo snackbar", async () => {
    calHandlers([
      ev({ id: 5, title: "Standup", starts_at: "2026-06-15T09:00:00" }),
    ]);
    const { user } = renderOn("week");
    await screen.findByText("Standup");

    await user.click(screen.getByRole("button", { name: /Standup/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /Remove from calendar/i }));

    await waitFor(() => expect(dismissedIds).toEqual(["5"]));
    // The reload after dismiss + the undo snackbar.
    expect(await screen.findByText(/Removed .Standup. from calendar/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Undo$/i })).toBeInTheDocument();
  });
});
