// Component-integration (Tier 2) tests for SqlConsole — the read-only SQL console
// (run SELECT/WITH → render result columns/rows) plus the database backup/restore UI
// (export DB, export original notes, import/restore a backup file).
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint a flow touches is stubbed: POST /api/sql for the console, and the
// backup/restore fetches in api.ts (GET /api/system/backup, GET
// /api/system/export/original-notes, POST /api/system/restore). The page never imports
// ../App, so no auth mock is needed; it does use window.confirm, URL.createObjectURL,
// Blob and location.reload for the backup flows, which we stub per-test.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderWithProviders, screen, waitFor } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

import SqlConsole from "./SqlConsole";

beforeEach(() => {
  // jsdom has no URL.createObjectURL/revokeObjectURL; the backup downloaders need both.
  vi.stubGlobal("URL", Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:mock"),
    revokeObjectURL: vi.fn(),
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("SqlConsole — query console", () => {
  it("runs a query: POSTs the edited SQL and renders result columns + rows", async () => {
    let posted: any = null;
    server.use(
      http.post("*/api/sql", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          columns: ["title", "updated_at"],
          rows: [["Groceries", "2024-01-02"], ["Trip", "2024-01-01"]],
        });
      }),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    await user.clear(textarea);
    await user.type(textarea, "SELECT title, updated_at FROM notes");
    await user.click(screen.getByRole("button", { name: "Run query" }));

    // The result table renders the returned column headers and cell values.
    expect(await screen.findByRole("columnheader", { name: "title" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "updated_at" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Groceries" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "2024-01-02" })).toBeInTheDocument();
    expect(screen.getByText("2 row(s)")).toBeInTheDocument();

    // The POST body carried exactly what the user typed.
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual({ sql: "SELECT title, updated_at FROM notes" });
  });

  it("surfaces the server error message and renders no table", async () => {
    server.use(
      http.post("*/api/sql", () =>
        HttpResponse.json({ detail: "Only SELECT/WITH queries are allowed" }, { status: 400 })),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    await user.click(screen.getByRole("button", { name: "Run query" }));

    expect(await screen.findByText("Only SELECT/WITH queries are allowed")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders an empty result (columns but zero rows) as 0 row(s)", async () => {
    server.use(
      http.post("*/api/sql", () =>
        HttpResponse.json({ columns: ["notes"], rows: [] })),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    await user.click(screen.getByRole("button", { name: "Run query" }));

    expect(await screen.findByRole("columnheader", { name: "notes" })).toBeInTheDocument();
    expect(screen.getByText("0 row(s)")).toBeInTheDocument();
    // Header row only — no body rows.
    expect(screen.queryAllByRole("cell")).toHaveLength(0);
  });
});

describe("SqlConsole — backup & restore", () => {
  it("Export database fetches the backup and reports success", async () => {
    let hit = false;
    server.use(
      http.get("*/api/system/backup", () => {
        hit = true;
        return HttpResponse.text("SQLITEBYTES", {
          headers: { "Content-Disposition": 'attachment; filename="jbrain-backup.db"' },
        });
      }),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    await user.click(screen.getByRole("button", { name: "Export database" }));

    expect(await screen.findByText("Backup downloaded.")).toBeInTheDocument();
    expect(hit).toBe(true);
    expect((URL.createObjectURL as any)).toHaveBeenCalled();
  });

  it("Export original notes fetches the JSON export and reports success", async () => {
    let hit = false;
    server.use(
      http.get("*/api/system/export/original-notes", () => {
        hit = true;
        return HttpResponse.json([{ title: "n", content_md: "x" }]);
      }),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    await user.click(screen.getByRole("button", { name: "Export original notes" }));

    expect(await screen.findByText("Original notes exported (JSON).")).toBeInTheDocument();
    expect(hit).toBe(true);
  });

  it("surfaces a failed export", async () => {
    server.use(
      http.get("*/api/system/backup", () => HttpResponse.json({}, { status: 500 })),
    );
    const { user } = renderWithProviders(<SqlConsole />);

    await user.click(screen.getByRole("button", { name: "Export database" }));

    expect(await screen.findByText("Backup failed: Backup failed")).toBeInTheDocument();
  });

  it("Import → confirm → uploads the file to /api/system/restore", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    // The restore POST sends multipart/form-data. MSW-node's request.formData() doesn't
    // resolve for multipart bodies under jsdom, so assert the request fired and carried a
    // multipart Content-Type (the FormData boundary) rather than re-parsing the file.
    let restoreCalled = false;
    let restoreContentType: string | null = null;
    server.use(
      http.post("*/api/system/restore", ({ request }) => {
        restoreCalled = true;
        restoreContentType = request.headers.get("Content-Type");
        return HttpResponse.json({ ok: true });
      }),
    );
    renderWithProviders(<SqlConsole />);

    const file = new File(["SQLITEBYTES"], "my-brain.db", { type: "application/octet-stream" });
    // The "Import database…" button proxies a click to the hidden (display:none) file
    // input. user-event refuses to drive a non-visible element, so set its FileList and
    // fire change directly — exactly what the click-through file picker would produce.
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    fireEvent.change(input);

    // confirm() must be asked (its message names the chosen file) before any upload.
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    expect(confirmSpy.mock.calls[0][0]).toContain("my-brain.db");
    await waitFor(() => expect(restoreCalled).toBe(true));
    expect(restoreContentType).toMatch(/multipart\/form-data/);
    expect(await screen.findByText("Restored. Reloading…")).toBeInTheDocument();
  });

  it("Import is cancelled at the confirm prompt → no request fires", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    let hit = false;
    server.use(
      http.post("*/api/system/restore", () => { hit = true; return HttpResponse.json({ ok: true }); }),
    );
    renderWithProviders(<SqlConsole />);

    const file = new File(["x"], "skip.db", { type: "application/octet-stream" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    fireEvent.change(input);

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(hit).toBe(false);
    expect(screen.queryByText(/Restoring/)).not.toBeInTheDocument();
  });
});
