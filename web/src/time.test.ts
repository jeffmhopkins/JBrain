import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fmtTs,
  fmtTsShort,
  fmtElapsed,
  parseUtcMs,
  fmtRel,
  expandTimeTokens,
  expandTimeTokensMarked,
} from "./time";

// Node's TZ is UTC in this environment, so toLocaleString with no appTz formats
// in UTC. Where a specific zone matters we pass appTz explicitly.

describe("fmtTs", () => {
  it("returns empty string for empty input", () => {
    expect(fmtTs("")).toBe("");
  });
  it("returns the raw string when unparseable", () => {
    expect(fmtTs("not a date")).toBe("not a date");
  });
  it("localizes a zoneless UTC timestamp into the app timezone", () => {
    // 11:00 UTC == 7:00 AM EDT on Jun 1 2026.
    const out = fmtTs("2026-06-01 11:00:00", "America/New_York");
    expect(out).toContain("Jun 1, 2026");
    expect(out).toContain("7:00");
    expect(out).toContain("AM");
    expect(out).toContain("EDT");
  });
  it("respects an explicit zone suffix on the raw string", () => {
    const out = fmtTs("2026-06-01T11:00:00Z", "America/New_York");
    expect(out).toContain("7:00");
    expect(out).toContain("AM");
  });
  it("omits the timezone label when appTz is not given", () => {
    const out = fmtTs("2026-06-01 11:00:00");
    expect(out).toContain("Jun 1, 2026");
    expect(out).not.toMatch(/UTC|EDT|GMT/);
  });
});

describe("fmtTsShort", () => {
  beforeEach(() => vi.setSystemTime(new Date("2026-06-15T12:00:00Z")));
  afterEach(() => vi.useRealTimers());

  it("returns empty string for empty input", () => {
    expect(fmtTsShort("")).toBe("");
  });
  it("returns the raw string when unparseable", () => {
    expect(fmtTsShort("garbage")).toBe("garbage");
  });
  it("drops the year for a current-year timestamp", () => {
    const out = fmtTsShort("2026-06-01 21:41:00");
    expect(out).toContain("Jun 1");
    expect(out).not.toContain("2026");
    expect(out).toContain("9:41");
    expect(out).toContain("PM");
  });
  it("includes the year for a different year", () => {
    const out = fmtTsShort("2024-06-01 21:41:00");
    expect(out).toContain("2024");
  });
});

describe("fmtElapsed", () => {
  it("clamps negatives to 0s", () => {
    expect(fmtElapsed(-5000)).toBe("0s");
  });
  it("formats sub-minute durations in seconds", () => {
    expect(fmtElapsed(3000)).toBe("3s");
    expect(fmtElapsed(59999)).toBe("59s");
  });
  it("formats minutes with zero-padded seconds", () => {
    expect(fmtElapsed(60000)).toBe("1m 00s");
    expect(fmtElapsed(65000)).toBe("1m 05s");
    expect(fmtElapsed(125000)).toBe("2m 05s");
  });
  it("formats hours with zero-padded minutes", () => {
    expect(fmtElapsed(3600000)).toBe("1h 00m");
    expect(fmtElapsed(2 * 3600000 + 9 * 60000)).toBe("2h 09m");
  });
});

describe("parseUtcMs", () => {
  it("returns NaN for empty / null / undefined", () => {
    expect(parseUtcMs("")).toBeNaN();
    expect(parseUtcMs(null)).toBeNaN();
    expect(parseUtcMs(undefined)).toBeNaN();
  });
  it("parses a zoneless timestamp as UTC", () => {
    expect(parseUtcMs("2026-06-01 11:00:00")).toBe(Date.parse("2026-06-01T11:00:00Z"));
  });
  it("honors an explicit zone suffix", () => {
    expect(parseUtcMs("2026-06-01T11:00:00Z")).toBe(Date.parse("2026-06-01T11:00:00Z"));
  });
  it("returns NaN for an unparseable string", () => {
    expect(parseUtcMs("nope")).toBeNaN();
  });
});

describe("fmtRel", () => {
  beforeEach(() => vi.setSystemTime(new Date("2026-06-15T12:00:00Z")));
  afterEach(() => vi.useRealTimers());

  it("returns empty for empty input", () => {
    expect(fmtRel("")).toBe("");
  });
  it("returns raw for unparseable input", () => {
    expect(fmtRel("nope")).toBe("nope");
  });
  it("reports 'just now' under a minute", () => {
    expect(fmtRel("2026-06-15 11:59:30")).toBe("just now");
  });
  it("reports minutes ago", () => {
    expect(fmtRel("2026-06-15 11:55:00")).toBe("5 minutes ago");
  });
  it("uses singular for exactly one unit", () => {
    expect(fmtRel("2026-06-15 11:00:00")).toBe("1 hour ago");
  });
  it("reports days ago", () => {
    expect(fmtRel("2026-06-13 12:00:00")).toBe("2 days ago");
  });
  it("falls back to a short date beyond ~30 days", () => {
    const out = fmtRel("2026-01-01 12:00:00");
    expect(out).toContain("Jan 1");
    expect(out).not.toContain("ago");
  });
});

describe("expandTimeTokens", () => {
  const NOW = Date.parse("2026-06-15T12:00:00Z");

  it("returns input unchanged when there are no tokens", () => {
    expect(expandTimeTokens("plain text", undefined, NOW)).toBe("plain text");
    expect(expandTimeTokens("", undefined, NOW)).toBe("");
  });

  it("computes whole-year age from a birthdate", () => {
    expect(expandTimeTokens("@t[age:2000-06-01]", "UTC", NOW)).toBe("26");
  });
  it("does not count the year before the birthday has passed", () => {
    expect(expandTimeTokens("@t[age:2000-06-20]", "UTC", NOW)).toBe("25");
  });
  it("leaves a malformed age token verbatim", () => {
    expect(expandTimeTokens("@t[age:not-a-date]", "UTC", NOW)).toBe("@t[age:not-a-date]");
  });
  it("leaves a future-birthdate (negative years) age verbatim", () => {
    const whole = "@t[age:2030-01-01]";
    expect(expandTimeTokens(whole, "UTC", NOW)).toBe(whole);
  });

  it("renders until: in the future", () => {
    expect(expandTimeTokens("@t[until:2026-06-17 12:00:00]", undefined, NOW)).toBe("in 2 days");
  });
  it("renders until: in the past as ago", () => {
    expect(expandTimeTokens("@t[until:2026-06-13 12:00:00]", undefined, NOW)).toBe("2 days ago");
  });
  it("renders until: within a minute as now", () => {
    expect(expandTimeTokens("@t[until:2026-06-15 12:00:30]", undefined, NOW)).toBe("now");
  });

  it("renders since: in the past as ago", () => {
    expect(expandTimeTokens("@t[since:2026-06-14 12:00:00]", undefined, NOW)).toBe("1 day ago");
  });
  it("renders since: in the future as in", () => {
    expect(expandTimeTokens("@t[since:2026-06-16 12:00:00]", undefined, NOW)).toBe("in 1 day");
  });
  it("renders since: within a minute as just now", () => {
    expect(expandTimeTokens("@t[since:2026-06-15 11:59:30]", undefined, NOW)).toBe("just now");
  });

  it("leaves an unparseable until/since anchor verbatim", () => {
    expect(expandTimeTokens("@t[until:bogus]", undefined, NOW)).toBe("@t[until:bogus]");
  });
  it("uses the live clock when nowMs is omitted", () => {
    vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
    expect(expandTimeTokens("@t[age:2000-06-01]", "UTC")).toBe("26");
    vi.useRealTimers();
  });
});

describe("expandTimeTokensMarked", () => {
  const NOW = Date.parse("2026-06-15T12:00:00Z");

  it("returns input unchanged when there are no tokens", () => {
    expect(expandTimeTokensMarked("plain", undefined, NOW)).toBe("plain");
    expect(expandTimeTokensMarked("", undefined, NOW)).toBe("");
  });
  it("wraps a dynamic value in a #dyn: link carrier", () => {
    const out = expandTimeTokensMarked("@t[until:2026-06-17 12:00:00]", undefined, NOW);
    expect(out.startsWith("[in 2 days](#dyn:")).toBe(true);
    expect(decodeURIComponent(out)).toContain("countdown to 2026-06-17 12:00:00");
  });
  it("encodes the age tooltip", () => {
    const out = expandTimeTokensMarked("@t[age:2000-06-01]", "UTC", NOW);
    expect(out.startsWith("[26](#dyn:")).toBe(true);
    expect(decodeURIComponent(out)).toContain("age from 2000-06-01");
  });
  it("encodes the since tooltip", () => {
    const out = expandTimeTokensMarked("@t[since:2026-06-14 12:00:00]", undefined, NOW);
    expect(decodeURIComponent(out)).toContain("elapsed since 2026-06-14 12:00:00");
  });
  it("leaves an unparseable token verbatim (no carrier)", () => {
    expect(expandTimeTokensMarked("@t[until:bogus]", undefined, NOW)).toBe("@t[until:bogus]");
  });
});
