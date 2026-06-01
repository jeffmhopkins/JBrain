// Shared model for list (checkbox) notes — used by the Lists card and the public
// share editor so both manipulate lists the same way.
export interface PItem { checked: boolean; text: string; priority: number | null; }
export interface Parsed { header: string; items: PItem[]; queue: boolean; }

const ITEM_RE = /^(\s*)- \[( |x|X)\] (.*)$/;
const PRIO_RE = /^\(P(\d+)\)\s+(.*)$/;            // leading "(P1)" priority token
export const QUEUE_MARK = "<!-- jbrain:queue -->";  // marks a priority-queue list

// Parse a list note into a canonical model: header text + items (display order:
// priority ascending, stable) + whether it's a priority queue.
export function parseList(md: string): Parsed {
  const queue = (md || "").includes("jbrain:queue");
  const header: string[] = [];
  const items: PItem[] = [];
  let seen = false;
  for (const ln of (md || "").split("\n")) {
    const m = ln.match(ITEM_RE);
    if (m) {
      seen = true;
      let text = m[3];
      let priority: number | null = null;
      const p = text.match(PRIO_RE);
      if (p) { priority = Number(p[1]); text = p[2]; }
      items.push({ checked: m[2].toLowerCase() === "x", text, priority });
    } else if (!seen && !ln.includes("jbrain:queue")) {
      header.push(ln);
    }
  }
  items.sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity));   // stable
  return { header: header.join("\n").replace(/\n+$/, ""), items, queue };
}

// Serialize back. Queue mode numbers every item (P1..Pn) by position.
export function serialize(p: Parsed): string {
  const out: string[] = [];
  if (p.header.trim()) out.push(p.header.replace(/\s+$/, ""));
  if (p.queue) out.push(QUEUE_MARK);
  p.items.forEach((it, i) => {
    const box = it.checked ? "[x]" : "[ ]";
    const prio = p.queue ? `(P${i + 1}) ` : (it.priority ? `(P${it.priority}) ` : "");
    out.push(`- ${box} ${prio}${it.text}`);
  });
  return out.join("\n") + "\n";
}
