// MSW node server for component-integration tests. Lifecycle is wired in setup.ts.
import { setupServer } from "msw/node";
import { handlers } from "./handlers";

export const server = setupServer(...handlers);
