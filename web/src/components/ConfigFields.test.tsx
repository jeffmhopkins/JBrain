// Component (Tier 2) tests for ConfigFields — the schema-driven editor for a
// workflow action_config. It edits the SAME JSON string the raw editor shows
// (single source of truth), mutating only the keys it manages and preserving
// unmodeled keys. These tests are props-only: no network is touched (no api.ts
// call), so MSW's onUnhandledRequest:"error" never fires.
//
// Covered: render of each field type from a value JSON string; emitted onChange
// payloads for text / number / select / boolean / review edits (incl. preserving
// unmodeled keys and deleting a key on blank); validation/edge — empty schema and
// invalid (non-object) JSON hide the fields.
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, fireEvent } from "../test/render";
import ConfigFields, { type FieldSpec } from "./ConfigFields";

const SCHEMA: FieldSpec[] = [
  { key: "title", label: "Note title", type: "text", required: true, default: "Untitled" },
  { key: "body", label: "Body", type: "textarea" },
  { key: "limit", label: "Limit", type: "number", default: 500 },
  { key: "mode", label: "Mode", type: "select", options: ["fast", "slow"] },
  { key: "enabled", label: "Enabled", type: "boolean", default: true },
  { key: "review", label: "Review card", type: "review" },
];

// Parse the JSON string the component last emitted.
const lastJson = (onChange: ReturnType<typeof vi.fn>) =>
  JSON.parse(onChange.mock.calls.at(-1)![0] as string);

describe("ConfigFields", () => {
  it("renders inputs for every field type with current values populated", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <ConfigFields schema={SCHEMA} onChange={onChange}
        value={JSON.stringify({ title: "Hi", body: "B", limit: 12, mode: "slow", enabled: false })} />,
    );
    expect((screen.getByDisplayValue("Hi") as HTMLInputElement).value).toBe("Hi");
    expect(screen.getByDisplayValue("B").tagName).toBe("TEXTAREA");
    expect((screen.getByDisplayValue("12") as HTMLInputElement).type).toBe("number");
    // Mode select reflects "slow"; the boolean checkbox reflects the explicit false.
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("slow");
    const checkboxes = screen.getAllByRole("checkbox");
    expect((checkboxes[0] as HTMLInputElement).checked).toBe(false); // enabled
  });

  it("editing a text field emits the merged JSON and preserves unmodeled keys", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <ConfigFields schema={SCHEMA} onChange={onChange}
        value={JSON.stringify({ title: "Old", keepMe: 7 })} />,
    );
    fireEvent.change(screen.getByDisplayValue("Old"), { target: { value: "New" } });
    expect(lastJson(onChange)).toEqual({ title: "New", keepMe: 7 });
  });

  it("clearing a text field deletes the key (falls back to the action default)", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <ConfigFields schema={SCHEMA} onChange={onChange} value={JSON.stringify({ title: "Old" })} />,
    );
    fireEvent.change(screen.getByDisplayValue("Old"), { target: { value: "" } });
    expect(lastJson(onChange)).toEqual({});
  });

  it("number field coerces to a Number and select/checkbox/review emit typed values", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <ConfigFields schema={SCHEMA} onChange={onChange} value="{}" />,
    );
    // number → Number, blank-aware
    fireEvent.change(screen.getByPlaceholderText("500 (default)"), { target: { value: "42" } });
    expect(lastJson(onChange)).toEqual({ limit: 42 });

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fast" } });
    expect(lastJson(onChange)).toEqual({ mode: "fast" });

    // boolean default is true, so unchecking emits false.
    const [enabled] = screen.getAllByRole("checkbox");
    fireEvent.click(enabled);
    expect(lastJson(onChange)).toEqual({ enabled: false });
  });

  it("review checkbox writes {} when turned on and false when turned off", () => {
    const onChange = vi.fn();
    const { rerender } = renderWithProviders(
      <ConfigFields schema={SCHEMA} onChange={onChange} value="{}" />,
    );
    const reviewBox = screen.getAllByRole("checkbox")[1]; // [0]=enabled, [1]=review
    fireEvent.click(reviewBox);
    expect(lastJson(onChange)).toEqual({ review: {} });

    // With review on, a title input appears and unchecking writes false.
    rerender(<ConfigFields schema={SCHEMA} onChange={onChange} value={JSON.stringify({ review: { title: "T" } })} />);
    expect(screen.getByPlaceholderText("Card title (optional)")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(lastJson(onChange)).toEqual({ review: false });
  });

  it("renders nothing for an empty schema and hides fields for non-object JSON", () => {
    const onChange = vi.fn();
    const { container, rerender } = renderWithProviders(
      <ConfigFields schema={[]} onChange={onChange} value="{}" />,
    );
    expect(container.firstChild).toBeNull();

    rerender(<ConfigFields schema={SCHEMA} onChange={onChange} value="[1,2,3]" />);
    expect(screen.getByText(/isn.t a valid object/)).toBeInTheDocument();
    expect(screen.queryByText("Note title")).not.toBeInTheDocument();
  });
});
