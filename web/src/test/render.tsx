// Shared render helper for component/page tests (see docs/testing-plan/PLAN.md §4.2).
// Wraps the subject in the providers the app needs (router today; auth context later)
// so tests stop re-stamping <MemoryRouter> boilerplate, and returns a configured
// user-event instance alongside the usual RTL queries.
import { render, type RenderOptions } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement, ReactNode } from "react";

interface Opts extends Omit<RenderOptions, "wrapper"> {
  /** initial URL, e.g. "/search?q=hi" */
  route?: string;
  // future: auth?: Partial<AuthState> to override the AuthCtx provider value
}

export function renderWithProviders(ui: ReactElement, { route = "/", ...rest }: Opts = {}) {
  function Wrapper({ children }: { children: ReactNode }) {
    return <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>;
  }
  return {
    // NB: when pairing with vi.useFakeTimers(), create user-event with
    // { advanceTimers: vi.advanceTimersByTime } at the call site instead.
    user: userEvent.setup(),
    ...render(ui, { wrapper: Wrapper, ...rest }),
  };
}

// Re-export RTL so tests can import everything from one place.
export * from "@testing-library/react";
export { userEvent };
