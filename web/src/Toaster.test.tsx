import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import Toaster from "./components/Toaster";
import { __resetToasts, showToast } from "./toast";

beforeEach(() => __resetToasts());
afterEach(() => cleanup());

describe("Toaster", () => {
  it("renders a toast and dismisses it on the × button", () => {
    render(<Toaster />);
    act(() => { showToast("upload failed"); });
    expect(screen.getByText("upload failed")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(screen.queryByText("upload failed")).not.toBeInTheDocument();
  });

  it("uses an assertive alert region when an error is present", () => {
    const { container } = render(<Toaster />);
    act(() => { showToast("boom", "error"); });
    const region = container.querySelector(".toaster")!;
    expect(region.getAttribute("role")).toBe("alert");
    expect(region.getAttribute("aria-live")).toBe("assertive");
  });

  it("is a polite status region for non-error toasts", () => {
    const { container } = render(<Toaster />);
    act(() => { showToast("saved", "success"); });
    const region = container.querySelector(".toaster")!;
    expect(region.getAttribute("role")).toBe("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
  });
});
