// Component-integration (Tier 2) tests for ShareOptions — the standardized share-link
// options widget reused by every share kind. Props-only and pure (no fetch, no router):
// it renders bind / single-use / expiry / usage-cap controls gated by `show`, and emits
// a partial patch through onChange for each interaction. `ttlMin > 0` removes the
// "(0 = never)" hint and floors the expiry; `expiry:false` hides the expiry control.
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "../test/render";
import userEvent from "@testing-library/user-event";

import ShareOptions, { DEFAULT_SHARE_OPTIONS, type ShareCommonOptions } from "./ShareOptions";

afterEach(cleanup);

const base: ShareCommonOptions = { ...DEFAULT_SHARE_OPTIONS };

describe("ShareOptions", () => {
  it("renders the bind, single-use and expiry controls by default", () => {
    render(<ShareOptions value={base} onChange={() => {}} />);
    expect(screen.getByText(/Lock to first browser/)).toBeInTheDocument();
    expect(screen.getByText(/Single use/)).toBeInTheDocument();
    expect(screen.getByText(/Expires in/)).toBeInTheDocument();
    expect(screen.getByText(/0 = never/)).toBeInTheDocument();
    // No usage caps unless show.caps.
    expect(screen.queryByText(/Usage caps/)).not.toBeInTheDocument();
  });

  it("emits { bind } when the lock checkbox is toggled", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ShareOptions value={base} onChange={onChange} />);
    await user.click(screen.getByRole("checkbox", { name: /Lock to first browser/ }));
    expect(onChange).toHaveBeenCalledWith({ bind: true });
  });

  it("emits { single_use } when the single-use checkbox is toggled", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ShareOptions value={base} onChange={onChange} />);
    await user.click(screen.getByRole("checkbox", { name: /Single use/ }));
    expect(onChange).toHaveBeenCalledWith({ single_use: true });
  });

  it("emits { ttl_days } from the expiry number input, floored at ttlMin", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ShareOptions value={base} onChange={onChange} show={{ ttlMin: 7 }} />);
    // ttlMin > 0 removes the "(0 = never)" hint.
    expect(screen.queryByText(/0 = never/)).not.toBeInTheDocument();
    const input = screen.getByRole("spinbutton");
    // Typing a value below the minimum is floored to ttlMin.
    await user.type(input, "3");
    expect(onChange).toHaveBeenLastCalledWith({ ttl_days: 7 });
  });

  it("hides the single-use control when show.singleUse === false", () => {
    render(<ShareOptions value={base} onChange={() => {}} show={{ singleUse: false }} />);
    expect(screen.queryByText(/Single use/)).not.toBeInTheDocument();
    expect(screen.getByText(/Lock to first browser/)).toBeInTheDocument();
  });

  it("hides the expiry control when show.expiry === false", () => {
    render(<ShareOptions value={base} onChange={() => {}} show={{ expiry: false }} />);
    expect(screen.queryByText(/Expires in/)).not.toBeInTheDocument();
  });

  it("renders usage caps and emits clamped max_turns / max_total_replies", () => {
    const onChange = vi.fn();
    render(<ShareOptions value={base} onChange={onChange} show={{ caps: true }} />);
    expect(screen.getByText(/Replies per session/)).toBeInTheDocument();
    expect(screen.getByText(/Total replies/)).toBeInTheDocument();
    // Three spinbuttons render: [0] is the expiry ttl, then the two caps. The
    // component is controlled (value prop is fixed here), so drive a single
    // deterministic change per field via fireEvent.change.
    const [, perSession, total] = screen.getAllByRole("spinbutton");
    fireEvent.change(perSession, { target: { value: "5" } });
    expect(onChange).toHaveBeenLastCalledWith({ max_turns: 5 });
    fireEvent.change(total, { target: { value: "50" } });
    expect(onChange).toHaveBeenLastCalledWith({ max_total_replies: 50 });
    // Below-minimum input is clamped to 1.
    fireEvent.change(perSession, { target: { value: "0" } });
    expect(onChange).toHaveBeenLastCalledWith({ max_turns: 1 });
  });

  it("disables every control when disabled", () => {
    render(<ShareOptions value={base} onChange={() => {}} show={{ caps: true }} disabled />);
    for (const el of screen.getAllByRole("checkbox")) expect(el).toBeDisabled();
    for (const el of screen.getAllByRole("spinbutton")) expect(el).toBeDisabled();
  });
});
