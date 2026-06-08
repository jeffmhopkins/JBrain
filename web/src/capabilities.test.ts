import { beforeEach, describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { act } from "react";
import { CAP_COPY, useCapability, type CapId } from "./capabilities";
import { __reset, applyPoll, ingestVerify, setOnline, type CapState } from "./health";

function caps(over: Record<string, any> = {}): any {
  return { capabilities: {
    llm: { state: "ready", verified: null, providers: { anthropic: true, xai: false } },
    embeddings: { state: "ready" }, transcription: { state: "ready" },
    push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
    ...over,
  } };
}

beforeEach(() => { __reset(); });

describe("useCapability", () => {
  it("ready when the subsystem is ready", () => {
    const { result } = renderHook(() => useCapability("llm"));
    act(() => { ingestVerify(caps()); });
    expect(result.current.ready).toBe(true);
    expect(result.current.reason).toBe("");
    expect(result.current.providers).toEqual({ anthropic: true, xai: false });
  });

  it("not ready + reason when absent", () => {
    const { result } = renderHook(() => useCapability("llm"));
    act(() => { ingestVerify(caps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } })); });
    expect(result.current.ready).toBe(false);
    expect(result.current.reason).toMatch(/API key/i);
  });

  it("embeddings warming → not ready, 'starting up' copy", () => {
    const { result } = renderHook(() => useCapability("embeddings"));
    act(() => { ingestVerify(caps({ embeddings: { state: "warming" } })); });
    expect(result.current.ready).toBe(false);
    expect(result.current.reason).toMatch(/loading|starting/i);
  });

  it("server-unreachable disables every capability with a retrying message", () => {
    const { result } = renderHook(() => useCapability("transcription"));
    act(() => { ingestVerify(caps()); applyPoll({ ok: false, reason: "unreachable" }); });
    expect(result.current.ready).toBe(false);
    expect(result.current.reason).toMatch(/unreachable/i);
  });

  it("browser-offline disables with an offline message", () => {
    const { result } = renderHook(() => useCapability("llm"));
    act(() => { ingestVerify(caps()); setOnline(false); });
    expect(result.current.ready).toBe(false);
    expect(result.current.reason).toMatch(/offline/i);
  });

  it("llm degraded (observed) is not ready and explains", () => {
    const { result } = renderHook(() => useCapability("llm"));
    act(() => { ingestVerify(caps({ llm: { state: "degraded", providers: { anthropic: true, xai: false } } })); });
    expect(result.current.ready).toBe(false);
    expect(result.current.reason).toMatch(/revoked|over quota|failed/i);
  });
});

describe("CAP_COPY exhaustiveness", () => {
  // Every non-ready state a gated capability can present must have user copy, so a
  // disabled control never shows an empty reason (the generic fallback aside).
  const expected: Record<CapId, CapState[]> = {
    llm: ["absent", "degraded"],
    embeddings: ["warming", "failed", "unavailable"],
    transcription: ["warming", "unavailable", "failed"],
    geocoder: ["absent"],
    push: ["absent"],
  };
  for (const id of Object.keys(expected) as CapId[]) {
    for (const state of expected[id]) {
      it(`${id}/${state} has copy`, () => {
        expect(CAP_COPY[id]?.[state]).toBeTruthy();
      });
    }
  }
});
