import { describe, expect, it } from "vitest";
import {
  slugify,
  reviewHref,
  leaf,
  truncate,
  wikiLabel,
  renderWikiLinks,
  stripSummarySentinels,
  stripLeadingTitleH1,
  mapsUrl,
  linkifyAddresses,
} from "./util";

describe("slugify", () => {
  it("lowercases and dashes non-alphanumerics", () => {
    expect(slugify("Hello World")).toBe("hello-world");
    expect(slugify("Jeffrey Mark Hopkins")).toBe("jeffrey-mark-hopkins");
  });
  it("collapses runs of separators into one dash", () => {
    expect(slugify("a -- b__c!!d")).toBe("a-b-c-d");
  });
  it("trims leading and trailing dashes", () => {
    expect(slugify("  !!Hello!!  ")).toBe("hello");
    expect(slugify("/notes/Foo")).toBe("notes-foo");
  });
  it("falls back to 'note' when nothing remains", () => {
    expect(slugify("")).toBe("note");
    expect(slugify("!!!")).toBe("note");
    expect(slugify("---")).toBe("note");
  });
});

describe("reviewHref", () => {
  it("passes through absolute paths unchanged", () => {
    expect(reviewHref("/shares")).toBe("/shares");
    expect(reviewHref("/note/foo")).toBe("/note/foo");
  });
  it("maps the __shares__ sentinel to /shares", () => {
    expect(reviewHref("__shares__")).toBe("/shares");
  });
  it("treats a bare slug as a note route", () => {
    expect(reviewHref("my-note")).toBe("/note/my-note");
  });
});

describe("leaf", () => {
  it("strips known root prefixes case-insensitively", () => {
    expect(leaf("notes/Foo")).toBe("Foo");
    expect(leaf("KB/People/Jane")).toBe("People/Jane");
    expect(leaf("lists/Groceries")).toBe("Groceries");
    expect(leaf("logs/2026")).toBe("2026");
  });
  it("leaves unknown prefixes alone", () => {
    expect(leaf("misc/Foo")).toBe("misc/Foo");
  });
  it("handles empty/undefined input", () => {
    expect(leaf("")).toBe("");
    expect(leaf(undefined as unknown as string)).toBe("");
  });
});

describe("truncate", () => {
  it("returns the string unchanged when short enough", () => {
    expect(truncate("hello", 10)).toBe("hello");
    expect(truncate("hello", 5)).toBe("hello");
  });
  it("clips and appends an ellipsis when too long", () => {
    expect(truncate("hello world", 5)).toBe("hell…");
  });
});

describe("wikiLabel", () => {
  it("returns the last segment for kb/ titles", () => {
    expect(wikiLabel("kb/People/Jeffrey Mark Hopkins")).toBe("Jeffrey Mark Hopkins");
    expect(wikiLabel("KB/Foo")).toBe("Foo");
  });
  it("returns the sole 'kb' segment when there is no article path", () => {
    expect(wikiLabel("kb/")).toBe("kb");
  });
  it("returns the 'kb' bucket when the article path is empty after trim", () => {
    // "kb/  " trims to "kb/", split→["kb",""], filter(Boolean)→["kb"]; the last
    // (and only) segment is the bucket itself, so that is returned.
    expect(wikiLabel("kb/  ")).toBe("kb");
  });
  it("strips the root leaf for non-kb titles", () => {
    expect(wikiLabel("notes/Foo")).toBe("Foo");
    expect(wikiLabel("Plain Title")).toBe("Plain Title");
  });
  it("trims and tolerates empty/undefined input", () => {
    expect(wikiLabel("  notes/Foo  ")).toBe("Foo");
    expect(wikiLabel("")).toBe("");
    expect(wikiLabel(undefined as unknown as string)).toBe("");
  });
});

describe("renderWikiLinks", () => {
  it("rewrites a bare [[Title]] into a markdown link", () => {
    expect(renderWikiLinks("see [[Hello World]] here")).toBe(
      "see [Hello World](/note/hello-world) here",
    );
  });
  it("uses the explicit alias when provided and adds no tooltip", () => {
    expect(renderWikiLinks("[[notes/Foo|My Foo]]")).toBe("[My Foo](/note/notes-foo)");
  });
  it("adds a hover tooltip when a bare link was shortened", () => {
    expect(renderWikiLinks("[[kb/People/Jane]]")).toBe(
      '[Jane](/note/kb-people-jane "kb/People/Jane")',
    );
  });
  it("does not add a tooltip when label equals the title", () => {
    expect(renderWikiLinks("[[Foo]]")).toBe("[Foo](/note/foo)");
  });
  it("strips quotes from the tooltip title", () => {
    const out = renderWikiLinks('[[kb/X/Say "hi"]]');
    expect(out).toContain('"kb/X/Say hi"');
  });
  it("returns empty / passes through text with no links", () => {
    expect(renderWikiLinks("")).toBe("");
    expect(renderWikiLinks("no links here")).toBe("no links here");
  });
  it("tolerates null/undefined input via the || '' guard", () => {
    expect(renderWikiLinks(null as unknown as string)).toBe("");
  });
});

describe("stripSummarySentinels", () => {
  it("removes opening and closing image-summary comments", () => {
    const md = "<!-- jbrain:image-summary att=2 -->\nbody\n<!-- /jbrain:image-summary att=2 -->\n";
    expect(stripSummarySentinels(md)).toBe("body\n");
  });
  it("leaves unrelated HTML comments intact", () => {
    expect(stripSummarySentinels("<!-- other -->")).toBe("<!-- other -->");
  });
  it("handles empty input", () => {
    expect(stripSummarySentinels("")).toBe("");
  });
});

describe("stripLeadingTitleH1", () => {
  it("strips a leading H1 that matches the full title", () => {
    expect(stripLeadingTitleH1("# My Note\n\nbody", "My Note")).toBe("body");
  });
  it("strips a leading H1 that matches the title's leaf", () => {
    expect(stripLeadingTitleH1("# Jane\nbody", "kb/People/Jane")).toBe("body");
  });
  it("leaves a non-matching H1 untouched", () => {
    expect(stripLeadingTitleH1("# Other\nbody", "My Note")).toBe("# Other\nbody");
  });
  it("leaves the body when there is no leading H1", () => {
    expect(stripLeadingTitleH1("body\n# My Note", "My Note")).toBe("body\n# My Note");
  });
  it("returns body unchanged when title has no leaf", () => {
    expect(stripLeadingTitleH1("# X\nbody", "")).toBe("# X\nbody");
  });
  it("is case-insensitive on the heading match", () => {
    expect(stripLeadingTitleH1("# MY NOTE\nbody", "My Note")).toBe("body");
  });
  it("returns '' for empty body input", () => {
    expect(stripLeadingTitleH1("", "My Note")).toBe("");
  });
  it("strips a matching H1 that is the only line (no trailing newline)", () => {
    expect(stripLeadingTitleH1("# My Note", "My Note")).toBe("");
  });
});

describe("mapsUrl", () => {
  it("builds an encoded Google Maps search URL", () => {
    expect(mapsUrl("123 Main St, Town, FL 32952")).toBe(
      "https://www.google.com/maps/search/?api=1&query=123%20Main%20St%2C%20Town%2C%20FL%2032952",
    );
  });
});

describe("linkifyAddresses", () => {
  it("returns falsy input unchanged", () => {
    expect(linkifyAddresses("")).toBe("");
  });
  it("appends a map pin after a US street address", () => {
    const out = linkifyAddresses("Meet at 234 E Merritt Island Cswy, Merritt Island, FL 32952 ok");
    expect(out).toContain("[map](#map:");
    expect(out).toContain("234%20E%20Merritt%20Island%20Cswy");
  });
  it("appends a map pin after a valid coordinate pair", () => {
    const out = linkifyAddresses("here 28.46388, -80.82609 done");
    expect(out).toContain("[map](#map:28.46388%2C-80.82609)");
  });
  it("ignores out-of-range coordinates", () => {
    expect(linkifyAddresses("99.12345, -200.54321")).toBe("99.12345, -200.54321");
  });
  it("ignores numbers without enough fractional digits (prices)", () => {
    expect(linkifyAddresses("costs 1,234.50 today")).toBe("costs 1,234.50 today");
  });
  it("does not linkify inside code spans", () => {
    const md = "`28.46388, -80.82609`";
    expect(linkifyAddresses(md)).toBe(md);
  });
  it("does not linkify inside existing wiki links", () => {
    const md = "[[28.46388, -80.82609]]";
    expect(linkifyAddresses(md)).toBe(md);
  });
  it("does not linkify inside an existing markdown link target", () => {
    const md = "[here](https://x/28.46388, -80.82609)";
    expect(linkifyAddresses(md)).toBe(md);
  });
  it("linkifies prose around a skipped code span (multi-segment walk)", () => {
    const out = linkifyAddresses("`code` then 28.46388, -80.82609 end");
    expect(out).toContain("`code`");
    expect(out).toContain("[map](#map:28.46388%2C-80.82609)");
  });
});
