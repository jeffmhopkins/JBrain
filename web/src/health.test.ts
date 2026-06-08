import { beforeEach, describe, expect, it } from "vitest";
import {
  __reset, applyPoll, getSnapshot, ingestVerify, pollEnabledForPath, report, setOnline,
  type Capabilities,
} from "./health";
import { setAccessKey, clearAccessKey } from "./api";

let clock = 1_000_000;
const now = () => clock;

function fullCaps(over: Partial<Capabilities> = {}): any {
  return {
    capabilities: {
      llm: { state: "ready", verified: null, providers: { anthropic: true, xai: false } },
      embeddings: { state: "ready", last_error: null, since: 0 },
      transcription: { state: "ready", last_error: null, since: 0, model: "base", compute_type: "int8" },
      push: { state: "ready" },
      geocoder: { state: "absent" },
      db: { state: "ready" },
      ...over,
    },
  };
}

beforeEach(() => {
  clock = 1_000_000;
  clearAccessKey();
  __reset(now);
});

describe("ingest + poll", () => {
  it("ingestVerify stores caps and marks the server ok", () => {
    ingestVerify(fullCaps());
    const m = getSnapshot();
    expect(m.server).toBe("ok");
    expect(m.caps?.llm.state).toBe("ready");
    expect(m.reachability).toBe("online");
  });

  it("applyPoll with a full doc updates caps", () => {
    applyPoll({ ok: true, data: fullCaps({ embeddings: { state: "warming" } as any }) });
    expect(getSnapshot().caps?.embeddings.state).toBe("warming");
    expect(getSnapshot().server).toBe("ok");
  });

  it("applyPoll unreachable keeps last-known caps but marks server unreachable", () => {
    ingestVerify(fullCaps());
    applyPoll({ ok: false, reason: "unreachable" });
    const m = getSnapshot();
    expect(m.server).toBe("unreachable");
    expect(m.reachability).toBe("server-unreachable");
    expect(m.caps?.llm.state).toBe("ready");   // not wiped
  });
});

describe("reconciliation rule 0 — needs-auth (skeleton + stored key)", () => {
  it("skeleton + stored key → needs-auth, NOT a logout", () => {
    setAccessKey("rotated-but-stored");
    applyPoll({ ok: true, data: { ok: true, brain: "B", ts: "x" } });  // skeleton, no capabilities
    const m = getSnapshot();
    expect(m.server).toBe("needs-auth");
    expect(m.reachability).toBe("online");      // server is reachable, just not authed
  });

  it("skeleton + NO stored key → reachable (ok), never needs-auth", () => {
    clearAccessKey();
    applyPoll({ ok: true, data: { ok: true, brain: "B", ts: "x" } });
    expect(getSnapshot().server).toBe("ok");
    expect(getSnapshot().caps).toBeUndefined();
  });
});

describe("reachability axes", () => {
  it("browser offline dominates", () => {
    ingestVerify(fullCaps());
    setOnline(false);
    expect(getSnapshot().reachability).toBe("browser-offline");
    expect(getSnapshot().server).toBe("unknown");
  });

  it("observed network error with no recent server byte → server-unreachable", () => {
    ingestVerify(fullCaps());   // lastOkAt = clock
    clock += 9000;              // age the last server byte past the window
    report({ kind: "neterr" });
    expect(getSnapshot().reachability).toBe("server-unreachable");
  });

  it("a single transient 5xx does NOT flip to unreachable (a 5xx is a server byte)", () => {
    ingestVerify(fullCaps());
    clock += 100;
    report({ kind: "http", status: 503 });
    const m = getSnapshot();
    expect(m.reachability).toBe("online");        // still reachable
    expect(m.observed.last5xxAt).toBe(clock);
  });
});

describe("LLM observed overlay (declared wins; observed only downgrades + self-heals)", () => {
  it("llm-fail downgrades a ready LLM to degraded, llm-ok heals it", () => {
    ingestVerify(fullCaps());
    expect(getSnapshot().caps?.llm.state).toBe("ready");
    clock += 10;
    report({ kind: "llm-fail" });
    expect(getSnapshot().caps?.llm.state).toBe("degraded");
    clock += 10;
    report({ kind: "llm-ok" });
    expect(getSnapshot().caps?.llm.state).toBe("ready");
  });

  it("observed NEVER upgrades: llm-ok cannot make an absent LLM ready", () => {
    applyPoll({ ok: true, data: fullCaps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } as any }) });
    report({ kind: "llm-ok" });
    expect(getSnapshot().caps?.llm.state).toBe("absent");
  });

  it("the degrade expires after the window", () => {
    ingestVerify(fullCaps());
    report({ kind: "llm-fail" });
    expect(getSnapshot().caps?.llm.state).toBe("degraded");
    clock += 61_000;            // past LLM_DEGRADE_MS
    report({ kind: "http", status: 200 });   // any event re-derives the model
    expect(getSnapshot().caps?.llm.state).toBe("ready");
  });
});

describe("pollEnabledForPath (the /share carve-out)", () => {
  it("disables polling only on the public /share/:token route (basename-stripped)", () => {
    expect(pollEnabledForPath("/share/abc123")).toBe(false);
    expect(pollEnabledForPath("/chat")).toBe(true);
    expect(pollEnabledForPath("/")).toBe(true);
    expect(pollEnabledForPath("/system")).toBe(true);
    // not the shares MANAGEMENT page (owner side, authed) — only the recipient route
    expect(pollEnabledForPath("/shares")).toBe(true);
  });
});

describe("snapshot stability", () => {
  it("getSnapshot returns a stable reference between mutations", () => {
    ingestVerify(fullCaps());
    const a = getSnapshot();
    const b = getSnapshot();
    expect(a).toBe(b);   // no re-render churn for unchanged state
  });
});
