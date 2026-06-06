// Rendered-markdown diffing. Instead of stripping a note to bare +/- lines, we
// build ONE merged markdown document that still renders with full formatting
// (headings, lists, **bold**, [[wiki-links]] …) and carry the red/green diff as
// invisible sentinel markers. A small rehype pass then turns those markers into
// <ins>/<del> spans and per-block add/remove classes — so the preview reads as
// the note will actually look, with the change highlighted in place.
//
// Used by the staging approvals, the Shares proposal review, and the version
// history diff so all three look + behave identically.

// Sentinels — Unicode Private Use Area chars. They never occur in real notes,
// survive markdown parsing as ordinary text, and are stripped again at render.
const DEL_O = "\uE000", DEL_C = "\uE001";   // inline deletion  (open/close)
const INS_O = "\uE002", INS_C = "\uE003";   // inline insertion (open/close)
const B_DEL = "\uE004";                      // whole-block deletion marker
const B_INS = "\uE005";                      // whole-block insertion marker
const INLINE_SENTINEL = /[\uE000-\uE003]/;  // any inline open/close marker
const ANY_SENTINEL = /[\uE000-\uE005]/g;    // all sentinels (for cleaning input)

interface Op { tag: "equal" | "delete" | "insert" | "replace"; a0: number; a1: number; b0: number; b1: number; }

// LCS-based opcodes over any string sequence (lines, then words). difflib-style:
// equal / delete / insert / replace runs. O(n·m) — guarded for pathological size.
function opcodes(a: string[], b: string[]): Op[] {
  const n = a.length, m = b.length;
  if (n === 0 && m === 0) return [];
  if (n === 0) return [{ tag: "insert", a0: 0, a1: 0, b0: 0, b1: m }];
  if (m === 0) return [{ tag: "delete", a0: 0, a1: n, b0: 0, b1: 0 }];
  // Too large to diff cell-by-cell → fall back to a wholesale replace.
  if (n > 5000 || m > 5000 || n * m > 6_000_000) {
    return [{ tag: "replace", a0: 0, a1: n, b0: 0, b1: m }];
  }

  // dp[i][j] = length of the LCS of a[i:] and b[j:].
  const dp: Uint32Array[] = [];
  for (let i = 0; i <= n; i++) dp.push(new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    const di = dp[i], di1 = dp[i + 1], ai = a[i];
    for (let j = m - 1; j >= 0; j--) {
      di[j] = ai === b[j] ? di1[j + 1] + 1 : Math.max(di1[j], di[j + 1]);
    }
  }
  // Walk the table into a per-element tag stream.
  const raw: ("equal" | "delete" | "insert")[] = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { raw.push("equal"); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { raw.push("delete"); i++; }
    else { raw.push("insert"); j++; }
  }
  while (i < n) { raw.push("delete"); i++; }
  while (j < m) { raw.push("insert"); j++; }

  // Coalesce runs; a delete-run adjacent to an insert-run becomes a replace.
  const ops: Op[] = [];
  let k = 0, ai = 0, bi = 0;
  while (k < raw.length) {
    if (raw[k] === "equal") {
      const s = k; while (k < raw.length && raw[k] === "equal") k++;
      const c = k - s;
      ops.push({ tag: "equal", a0: ai, a1: ai + c, b0: bi, b1: bi + c });
      ai += c; bi += c;
    } else {
      let da = 0, db = 0;
      while (k < raw.length && raw[k] !== "equal") { raw[k] === "delete" ? da++ : db++; k++; }
      if (da && db) ops.push({ tag: "replace", a0: ai, a1: ai + da, b0: bi, b1: bi + db });
      else if (da) ops.push({ tag: "delete", a0: ai, a1: ai + da, b0: bi, b1: bi });
      else ops.push({ tag: "insert", a0: ai, a1: ai, b0: bi, b1: bi + db });
      ai += da; bi += db;
    }
  }
  return ops;
}

// Tokenise a line for word-level diffing: whitespace runs, alphanumeric runs, and
// every other char on its own. Splitting punctuation out (so `**`, `[[`, `]]`,
// `_`… are their own tokens) keeps equal markdown markers aligned and lets the
// change sentinels land INSIDE emphasis/links — e.g. **decent**→**great** marks
// just the word, with the `**` staying put so the bold still renders.
const tokenize = (s: string): string[] => s.match(/\s+|[\p{L}\p{N}]+|[^\s\p{L}\p{N}]/gu) || [];

// Word-level diff of two single lines → a merged line carrying inline sentinels,
// plus a similarity ratio (0–1) used to decide inline-vs-block treatment.
function inlineMerge(aLine: string, bLine: string): { merged: string; ratio: number } {
  const at = tokenize(aLine), bt = tokenize(bLine);
  let out = "", eq = 0;
  for (const op of opcodes(at, bt)) {
    if (op.tag === "equal") { const s = at.slice(op.a0, op.a1).join(""); out += s; eq += s.length; }
    else if (op.tag === "delete") out += DEL_O + at.slice(op.a0, op.a1).join("") + DEL_C;
    else if (op.tag === "insert") out += INS_O + bt.slice(op.b0, op.b1).join("") + INS_C;
    else out += DEL_O + at.slice(op.a0, op.a1).join("") + DEL_C + INS_O + bt.slice(op.b0, op.b1).join("") + INS_C;
  }
  const denom = aLine.length + bLine.length;
  return { merged: out, ratio: denom ? (2 * eq) / denom : 1 };
}

// Insert a block sentinel into a line's rendered content — i.e. AFTER any leading
// markdown marker (list bullet, ordered number, heading #s, blockquote >, task
// checkbox) so the marker still parses and the whole block gets tagged.
const MARK_RE = /^(\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s+)?(?:\[[ xX]\]\s+)?)([\s\S]*)$/;
function markBlock(line: string, sentinel: string): string {
  if (line.trim() === "") return line;              // blank line: nothing to tag
  const m = line.match(MARK_RE);
  return m ? m[1] + sentinel + m[2] : sentinel + line;
}

export interface MarkdownDiffResult { merged: string; changed: boolean; }

/** Build the merged, sentinel-carrying markdown for `before` → `after`. */
export function buildMarkdownDiff(before: string, after: string): MarkdownDiffResult {
  before = before || ""; after = after || "";
  if (before === after) return { merged: after, changed: false };
  // Defend against a note that happens to contain our PUA sentinels already.
  const clean = (s: string) => s.replace(ANY_SENTINEL, "");
  const al = clean(before).split("\n"), bl = clean(after).split("\n");

  const out: string[] = [];
  let changed = false;
  for (const op of opcodes(al, bl)) {
    if (op.tag === "equal") {
      for (let i = op.a0; i < op.a1; i++) out.push(al[i]);
    } else if (op.tag === "delete") {
      changed = true;
      for (let i = op.a0; i < op.a1; i++) out.push(markBlock(al[i], B_DEL));
    } else if (op.tag === "insert") {
      changed = true;
      for (let j = op.b0; j < op.b1; j++) out.push(markBlock(bl[j], B_INS));
    } else {
      // Replace: pair lines positionally; a pair similar enough gets an inline
      // word-diff (edit in place), otherwise it reads as a clean remove + add.
      changed = true;
      const da = al.slice(op.a0, op.a1), bd = bl.slice(op.b0, op.b1);
      const pairs = Math.min(da.length, bd.length);
      for (let k = 0; k < pairs; k++) {
        const { merged, ratio } = inlineMerge(da[k], bd[k]);
        if (ratio >= 0.4 && da[k].trim() && bd[k].trim()) out.push(merged);
        else { out.push(markBlock(da[k], B_DEL)); out.push(markBlock(bd[k], B_INS)); }
      }
      for (let k = pairs; k < da.length; k++) out.push(markBlock(da[k], B_DEL));
      for (let k = pairs; k < bd.length; k++) out.push(markBlock(bd[k], B_INS));
    }
  }
  return { merged: out.join("\n"), changed };
}

// ---- rehype plugin: turn sentinels into <ins>/<del> + block add/remove classes ----

function addClass(node: any, cls: string) {
  node.properties = node.properties || {};
  const c = node.properties.className;
  if (Array.isArray(c)) { if (!c.includes(cls)) c.push(cls); }
  else node.properties.className = c ? String(c).split(/\s+/).concat(cls) : [cls];
}

// Split a text value on inline sentinels into a mix of text + <ins>/<del> nodes.
function splitInline(value: string): any[] {
  const res: any[] = [];
  let i = 0, buf = "";
  const flush = () => { if (buf) { res.push({ type: "text", value: buf }); buf = ""; } };
  while (i < value.length) {
    const c = value[i];
    if (c === DEL_O || c === INS_O) {
      flush();
      const close = c === DEL_O ? DEL_C : INS_C;
      const end = value.indexOf(close, i + 1);
      const inner = end < 0 ? value.slice(i + 1) : value.slice(i + 1, end);
      // Skip empties — a sentinel that lost its partner (e.g. it ended up either
      // side of a markdown construct) must not leave a blank highlighted sliver.
      if (inner) res.push({
        type: "element",
        tagName: c === DEL_O ? "del" : "ins",
        properties: { className: [c === DEL_O ? "jb-del" : "jb-ins"] },
        children: [{ type: "text", value: inner }],
      });
      i = end < 0 ? value.length : end + 1;
    } else if (c === DEL_C || c === INS_C) {
      i++;                       // stray close marker → drop
    } else { buf += c; i++; }
  }
  flush();
  return res;
}

function walk(node: any) {
  if (!node || !Array.isArray(node.children)) return;
  const out: any[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      let v: string = child.value;
      if (v.indexOf(B_DEL) >= 0) { addClass(node, "jb-block-del"); v = v.split(B_DEL).join(""); }
      if (v.indexOf(B_INS) >= 0) { addClass(node, "jb-block-ins"); v = v.split(B_INS).join(""); }
      if (INLINE_SENTINEL.test(v)) out.push(...splitInline(v));
      else { child.value = v; out.push(child); }
    } else {
      walk(child);
      out.push(child);
    }
  }
  node.children = out;
}

/** rehype plugin (for react-markdown's `rehypePlugins`). */
export function rehypeDiff() {
  return (tree: any) => walk(tree);
}
