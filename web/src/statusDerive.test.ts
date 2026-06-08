import { describe, expect, it } from "vitest";
import { deriveStatus, stateLevel } from "./statusDerive";
import type { Capabilities, HealthModel } from "./health";

function caps(over: Partial<Record<keyof Capabilities, any>> = {}): Capabilities {
  return {
    llm: { state: "ready", verified: null, providers: { anthropic: true, xai: false } },
    embeddings: { state: "ready", last_error: null, since: 0 },
    transcription: { state: "ready", last_error: null, since: 0 },
    push: { state: "ready" },
    geocoder: { state: "ready" },
    db: { state: "ready" },
    ...over,
  } as Capabilities;
}

function model(over: Partial<HealthModel> = {}): HealthModel {
  return {
    reachability: "online",
    server: "ok",
    caps: caps(),
    lastOkAt: 1,
    observed: {},
    ...over,
  };
}

describe("stateLevel", () => {
  it("maps subsystem states to severities", () => {
    expect(stateLevel("ready")).toBe("ok");
    expect(stateLevel("failed")).toBe("down");
    expect(stateLevel("unknown")).toBe("unknown");
    expect(stateLevel("warming")).toBe("warn");
    expect(stateLevel("unavailable")).toBe("warn");
    expect(stateLevel("absent")).toBe("warn");
    expect(stateLevel("degraded")).toBe("warn");
  });
});

describe("deriveStatus", () => {
  it("all ready → green ok", () => {
    const d = deriveStatus(model(), "Brain");
    expect(d.level).toBe("ok");
    expect(d.rows).toHaveLength(6);
  });

  it("browser-offline → grey, no rows, regardless of caps", () => {
    const d = deriveStatus(model({ reachability: "browser-offline" }), "Brain");
    expect(d.level).toBe("unknown");
    expect(d.summary).toMatch(/offline/i);
    expect(d.rows).toHaveLength(0);
  });

  it("server-unreachable → red, names the brain", () => {
    const d = deriveStatus(model({ reachability: "server-unreachable", server: "unreachable" }), "MyBrain");
    expect(d.level).toBe("down");
    expect(d.summary).toContain("MyBrain");
  });

  it("needs-auth → amber re-authenticate", () => {
    const d = deriveStatus(model({ server: "needs-auth" }), "Brain");
    expect(d.level).toBe("warn");
    expect(d.summary).toMatch(/re-authenticate/i);
  });

  it("a failed subsystem (db) → red even when reachable", () => {
    const d = deriveStatus(model({ caps: caps({ db: { state: "failed", last_error: "locked" } }) }), "Brain");
    expect(d.level).toBe("down");
  });

  it("llm absent → amber (worst-of), with an explanatory row", () => {
    const d = deriveStatus(model({ caps: caps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } }) }), "Brain");
    expect(d.level).toBe("warn");
    const llmRow = d.rows.find((r) => r.key === "llm");
    expect(llmRow?.level).toBe("warn");
    expect(llmRow?.label).toMatch(/no API key/i);
  });

  it("embeddings warming → amber 'starting up'", () => {
    const d = deriveStatus(model({ caps: caps({ embeddings: { state: "warming" } }) }), "Brain");
    expect(d.level).toBe("warn");
    expect(d.rows.find((r) => r.key === "embeddings")?.label).toMatch(/starting up/i);
  });

  it("pre-first-poll (no caps) → grey checking", () => {
    const d = deriveStatus(model({ caps: undefined, server: "unknown" }), "Brain");
    expect(d.level).toBe("unknown");
    expect(d.rows).toHaveLength(0);
  });

  it("down beats warn beats unknown in the worst-of rollup", () => {
    const d = deriveStatus(model({ caps: caps({
      embeddings: { state: "warming" }, transcription: { state: "unknown" }, db: { state: "failed" },
    }) }), "Brain");
    expect(d.level).toBe("down");
  });
});
