// Component-integration (Tier 2) tests for the chat [[citation]] link + its
// ReactMarkdown `a` renderer (makeChatLinkRenderer).
//
// CitationLink takes `navigate` as a plain prop (a NavigateFunction), so we pass a
// vi.fn() directly rather than stubbing useNavigate — clicking should preventDefault
// and call navigate(href). On hover (after a 250ms window.setTimeout debounce) it opens
// a popover and lazy-fetches GET /api/notes/:slug/preview, caching per-slug in a
// module-level Map. MSW is the only network mock (setup.ts: onUnhandledRequest:"error"),
// so every preview fetch is stubbed here. Each test uses a DISTINCT slug to dodge the
// shared cache. Fake timers drive the hover debounce; user-event is wired to advance them.
import { describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { fireEvent, renderWithProviders, screen, waitFor } from "../test/render";
import { server } from "../test/server";
import { CitationLink, makeChatLinkRenderer } from "./CitationLink";

// The popover opens after a real 250ms window.setTimeout debounce, then lazy-fetches.
// We deliberately use REAL timers: combining fake timers with user-event hover + MSW's
// internal async makes the hover gesture hang. So we drive hover with synchronous fireEvent
// (mouseEnter/mouseLeave map to the component's onMouseEnter/onMouseLeave) and let waitFor
// poll real time past the debounce and the fetch. Each hover test uses a DISTINCT slug so the
// module-level per-slug preview cache in CitationLink can't leak state between tests.

describe("CitationLink", () => {
  it("renders the citation as a wikilink anchor with the given href + label", () => {
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/some-slug" navigate={navigate}>Some Note</CitationLink>);
    const link = screen.getByRole("link", { name: "Some Note" });
    expect(link).toHaveAttribute("href", "/note/some-slug");
    expect(link).toHaveClass("wikilink");
  });

  it("navigates via the router (not a full nav) on click, suppressing the default", () => {
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/click-slug" navigate={navigate}>Click Me</CitationLink>);
    const link = screen.getByRole("link", { name: "Click Me" });
    const evt = new MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(evt);
    expect(navigate).toHaveBeenCalledWith("/note/click-slug");
    expect(evt.defaultPrevented).toBe(true);
  });

  it("opens a preview popover on hover and shows the fetched title + excerpt", async () => {
    server.use(
      http.get("*/api/notes/hover-found/preview", () =>
        HttpResponse.json({ found: true, title: "Hover Found", excerpt: "An excerpt here." }),
      ),
    );
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/hover-found" navigate={navigate}>cite</CitationLink>);

    // No popover before the debounce elapses.
    expect(screen.queryByRole("tooltip")).toBeNull();
    fireEvent.mouseEnter(screen.getByText("cite"));
    // Past the 250ms debounce → popover opens (initially "Loading…", then the fetch lands).
    await waitFor(() => expect(screen.getByRole("tooltip")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Hover Found")).toBeInTheDocument());
    expect(screen.getByText("An excerpt here.")).toBeInTheDocument();
  });

  it("falls back to a 'no longer exists' message when the target preview is not found", async () => {
    server.use(
      http.get("*/api/notes/hover-missing/preview", () =>
        HttpResponse.json({ found: false }),
      ),
    );
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/hover-missing" navigate={navigate}>cite</CitationLink>);
    fireEvent.mouseEnter(screen.getByText("cite"));
    await waitFor(() => expect(screen.getByText("That note no longer exists.")).toBeInTheDocument());
  });

  it("shows '(no preview)' when a found note has an empty excerpt", async () => {
    server.use(
      http.get("*/api/notes/hover-noexcerpt/preview", () =>
        HttpResponse.json({ found: true, title: "Bare Note", excerpt: "" }),
      ),
    );
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/hover-noexcerpt" navigate={navigate}>cite</CitationLink>);
    fireEvent.mouseEnter(screen.getByText("cite"));
    await waitFor(() => expect(screen.getByText("Bare Note")).toBeInTheDocument());
    expect(screen.getByText("(no preview)")).toBeInTheDocument();
  });

  it("does not open a popover if the cursor leaves before the debounce fires (no fetch)", async () => {
    // No handler registered: if a fetch went out, onUnhandledRequest:"error" would fail the test.
    const navigate = vi.fn();
    renderWithProviders(<CitationLink href="/note/quick-pass" navigate={navigate}>cite</CitationLink>);
    const target = screen.getByText("cite");
    fireEvent.mouseEnter(target);
    fireEvent.mouseLeave(target); // bail out well inside the 250ms window → timer is cleared
    // Give real time to comfortably exceed the debounce; the popover must never appear.
    await new Promise((r) => setTimeout(r, 400));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("decodes a URL-encoded slug for the preview fetch path", async () => {
    let requested = "";
    server.use(
      http.get("*/api/notes/:slug/preview", ({ params }) => {
        requested = String(params.slug);
        return HttpResponse.json({ found: true, title: "Decoded", excerpt: "ok" });
      }),
    );
    const navigate = vi.fn();
    // href has an encoded space (%20); component decodes to "my note" before encodeURIComponent re-encodes.
    renderWithProviders(<CitationLink href="/note/my%20note%20uniq" navigate={navigate}>cite</CitationLink>);
    fireEvent.mouseEnter(screen.getByText("cite"));
    await waitFor(() => expect(screen.getByText("Decoded")).toBeInTheDocument());
    // MSW decodes the path param, so we see the decoded slug the client requested.
    expect(requested).toBe("my note uniq");
  });
});

describe("makeChatLinkRenderer", () => {
  it("routes /note/ links through CitationLink (router nav, no full page load)", () => {
    const navigate = vi.fn();
    const Renderer = makeChatLinkRenderer(navigate);
    renderWithProviders(<Renderer href="/note/render-slug">Internal</Renderer>);
    const link = screen.getByRole("link", { name: "Internal" });
    expect(link).toHaveClass("wikilink");
    const evt = new MouseEvent("click", { bubbles: true, cancelable: true });
    link.dispatchEvent(evt);
    expect(navigate).toHaveBeenCalledWith("/note/render-slug");
  });

  it("renders external URLs as a plain new-tab anchor (no router nav)", () => {
    const navigate = vi.fn();
    const Renderer = makeChatLinkRenderer(navigate);
    renderWithProviders(<Renderer href="https://example.com">External</Renderer>);
    const link = screen.getByRole("link", { name: "External" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).not.toHaveClass("wikilink");
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("renders a #map: link as a Google Maps anchor opening in a new tab", () => {
    const navigate = vi.fn();
    const Renderer = makeChatLinkRenderer(navigate);
    renderWithProviders(<Renderer href="#map:Paris%2C%20France">ignored</Renderer>);
    const link = screen.getByRole("link", { name: "Open in Google Maps" });
    expect(link).toHaveClass("map-link");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("title", "Open in Google Maps: Paris, France");
  });

  it("renders a #dyn: live value as a titled span, not a link", () => {
    const navigate = vi.fn();
    const Renderer = makeChatLinkRenderer(navigate);
    renderWithProviders(<Renderer href="#dyn:Today">June 8</Renderer>);
    expect(screen.queryByRole("link")).toBeNull();
    const span = screen.getByText("June 8");
    expect(span).toHaveClass("dyn-date");
    expect(span).toHaveAttribute("title", "Today");
  });
});
