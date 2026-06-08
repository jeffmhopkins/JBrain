// Component-integration (Tier 2) tests for ListsPage — the Lists carousel that
// loads every kind:"list" note, renders each as a checklist card, and persists
// every edit (add / check / remove an item) immediately as a PUT. Create issues a
// POST /api/lists; deleting a card issues a DELETE.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so every endpoint
// the page touches on mount + per flow is stubbed — the index list, each list's
// full body, plus the per-flow PUT/POST/DELETE. The default handlers cover
// status/auth. We assert both the outgoing request (body/method) and the DOM.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

// ListsPage doesn't read useAuth itself, but mirror the established standard and
// stub ../App so no test ever drags in the real auth/router App tree.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import ListsPage from "./ListsPage";

interface ListNote { slug: string; title: string; content_md: string; }

function list(slug: string, title: string, content_md: string): ListNote {
  return { slug, title, content_md };
}

// Wire the two GETs every mount performs — the kind:"list" index, then each list's
// full note — backed by a mutable store so a PUT/DELETE is reflected on reload().
// Returns the store + captured requests for assertions.
function mountHandlers(initial: ListNote[]) {
  const store = new Map(initial.map((l) => [l.slug, { ...l }]));
  const puts: { slug: string; body: any }[] = [];
  const deletes: string[] = [];
  const posts: any[] = [];

  server.use(
    http.get("*/api/notes", ({ request }) => {
      const url = new URL(request.url);
      if (url.searchParams.get("kind") !== "list") return HttpResponse.json([]);
      return HttpResponse.json([...store.values()].map((l) => ({ slug: l.slug })));
    }),
    http.get("*/api/notes/:slug", ({ params }) => {
      const l = store.get(params.slug as string);
      return l ? HttpResponse.json(l) : HttpResponse.json({ detail: "not found" }, { status: 404 });
    }),
    http.put("*/api/notes/:slug", async ({ params, request }) => {
      const slug = params.slug as string;
      const body: any = await request.json();
      puts.push({ slug, body });
      store.set(slug, { slug, title: body.title, content_md: body.content_md });
      return HttpResponse.json({ slug });
    }),
    http.delete("*/api/notes/:slug", ({ params }) => {
      const slug = params.slug as string;
      deletes.push(slug);
      store.delete(slug);
      return HttpResponse.json({ ok: true });
    }),
    http.post("*/api/lists", async ({ request }) => {
      const body: any = await request.json();
      posts.push(body);
      const slug = body.title.toLowerCase().replace(/\s+/g, "-");
      store.set(slug, { slug, title: `lists/${body.title}`, content_md: "- [ ] seed item\n" });
      return HttpResponse.json({ slug });
    }),
  );

  return { store, puts, deletes, posts };
}

const SHOPPING = list("shopping", "lists/Shopping", "- [ ] Milk\n- [ ] Eggs\n");
const CHORES = list("chores", "lists/Chores", "- [x] Dishes\n- [ ] Laundry\n");

beforeEach(() => {
  // jsdom has no matchMedia; harmless here but keep parity with the page suite.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
  // jsdom doesn't implement scrollIntoView; the post-create effect snaps the
  // carousel to the new card via it. Stub so that effect is a no-op in-test.
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("ListsPage", () => {
  it("loads every list note and renders each as a card with items + progress", async () => {
    mountHandlers([SHOPPING, CHORES]);
    renderWithProviders(<ListsPage />, { route: "/lists" });

    // Both cards render with their leaf titles (the "lists/" prefix is stripped).
    expect(await screen.findByText("Shopping")).toBeInTheDocument();
    expect(await screen.findByText("Chores")).toBeInTheDocument();

    // Items come through the ListEditor checklist.
    expect(screen.getByText("Milk")).toBeInTheDocument();
    expect(screen.getByText("Eggs")).toBeInTheDocument();
    expect(screen.getByText("Laundry")).toBeInTheDocument();

    // Progress badge: Shopping 0/2, Chores 1/2 (one box already checked).
    expect(screen.getByText("0/2")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();

    // Three checkboxes total (2 + 2), one of them checked.
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes).toHaveLength(4);
    expect(boxes.filter((b) => b.checked)).toHaveLength(1);
  });

  it("shows the empty state when there are no lists", async () => {
    mountHandlers([]);
    renderWithProviders(<ListsPage />, { route: "/lists" });

    expect(await screen.findByText(/No lists yet/)).toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  });

  it("tolerates a failed load and renders the empty state", async () => {
    server.use(
      http.get("*/api/notes", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<ListsPage />, { route: "/lists" });

    // load() swallows the error, leaves lists empty, and stops loading.
    expect(await screen.findByText(/No lists yet/)).toBeInTheDocument();
  });

  it("adding an item PUTs the serialized list with the new item appended", async () => {
    const h = mountHandlers([SHOPPING]);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Shopping")).closest(".card") as HTMLElement;
    const input = within(card).getByPlaceholderText("Add an item…");
    await user.type(input, "Bread");
    await user.click(within(card).getByRole("button", { name: "Add" }));

    await waitFor(() => expect(h.puts).toHaveLength(1));
    expect(h.puts[0].slug).toBe("shopping");
    expect(h.puts[0].body.title).toBe("lists/Shopping");
    // serialize() emits one "- [ ] <text>" line per item; the new item is appended.
    expect(h.puts[0].body.content_md).toBe("- [ ] Milk\n- [ ] Eggs\n- [ ] Bread\n");

    // Optimistic re-render shows the added item and bumps the count.
    expect(await screen.findByText("Bread")).toBeInTheDocument();
    expect(screen.getByText("0/3")).toBeInTheDocument();
  });

  it("checking an item PUTs the list with that box marked [x]", async () => {
    const h = mountHandlers([SHOPPING]);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Shopping")).closest(".card") as HTMLElement;
    const boxes = within(card).getAllByRole("checkbox") as HTMLInputElement[];
    await user.click(boxes[0]);   // check "Milk"

    await waitFor(() => expect(h.puts).toHaveLength(1));
    expect(h.puts[0].body.content_md).toBe("- [x] Milk\n- [ ] Eggs\n");
    // Progress reflects the now-checked item.
    expect(await screen.findByText("1/2")).toBeInTheDocument();
  });

  it("unchecking an already-checked item PUTs it back to [ ]", async () => {
    const h = mountHandlers([CHORES]);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Chores")).closest(".card") as HTMLElement;
    const checked = (within(card).getAllByRole("checkbox") as HTMLInputElement[])
      .find((b) => b.checked)!;
    await user.click(checked);   // uncheck "Dishes"

    await waitFor(() => expect(h.puts).toHaveLength(1));
    expect(h.puts[0].body.content_md).toBe("- [ ] Dishes\n- [ ] Laundry\n");
    expect(await screen.findByText("0/2")).toBeInTheDocument();
  });

  it("removing an item PUTs the list without that item", async () => {
    const h = mountHandlers([SHOPPING]);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Shopping")).closest(".card") as HTMLElement;
    // The per-item remove buttons render the glyph "✕" (their accessible name);
    // click the first to drop "Milk".
    const removeBtns = within(card).getAllByRole("button", { name: "✕" });
    await user.click(removeBtns[0]);

    await waitFor(() => expect(h.puts).toHaveLength(1));
    expect(h.puts[0].body.content_md).toBe("- [ ] Eggs\n");
    await waitFor(() => expect(within(card).queryByText("Milk")).not.toBeInTheDocument());
    expect(screen.getByText("0/1")).toBeInTheDocument();
  });

  it("deleting a list confirms, DELETEs it, and drops the card", async () => {
    const h = mountHandlers([SHOPPING, CHORES]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Shopping")).closest(".card") as HTMLElement;
    await user.click(within(card).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(h.deletes).toEqual(["shopping"]));
    // Card is removed optimistically; Chores remains.
    await waitFor(() => expect(screen.queryByText("Shopping")).not.toBeInTheDocument());
    expect(screen.getByText("Chores")).toBeInTheDocument();
  });

  it("cancelling the delete confirm issues no DELETE", async () => {
    const h = mountHandlers([SHOPPING]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    const card = (await screen.findByText("Shopping")).closest(".card") as HTMLElement;
    await user.click(within(card).getByRole("button", { name: "Delete" }));

    expect(h.deletes).toEqual([]);
    expect(screen.getByText("Shopping")).toBeInTheDocument();
  });

  it("creating a list POSTs {title} and the new list appears after reload", async () => {
    const h = mountHandlers([SHOPPING]);
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    await screen.findByText("Shopping");
    await user.click(screen.getByRole("button", { name: /New list/ }));

    // The modal re-runs its focus effect on each render (its onClose prop is a fresh
    // closure), which steals focus from the input and drops per-keystroke typing — so
    // set the controlled value atomically via a change event.
    const nameInput = await screen.findByPlaceholderText("List name…") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Packing" } });
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(h.posts).toEqual([{ title: "Packing" }]));
    // load() refetches and the carousel now shows the created list.
    expect(await screen.findByText("Packing")).toBeInTheDocument();
  });

  it("surfaces a create failure via alert and keeps the modal open", async () => {
    mountHandlers([SHOPPING]);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.post("*/api/lists", () =>
        HttpResponse.json({ detail: "Couldn't create" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<ListsPage />, { route: "/lists" });

    await screen.findByText("Shopping");
    await user.click(screen.getByRole("button", { name: /New list/ }));
    fireEvent.change(await screen.findByPlaceholderText("List name…"), { target: { value: "Nope" } });
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Couldn't create"));
    // Modal stays open (Create button still present) so the name isn't lost.
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });
});
