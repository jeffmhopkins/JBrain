// Demo mode: lets you explore the full interface with canned data and no server.
// Opt-in only (a button on the login screen, or ?demo=1). Never affects real use.

const DEMO_KEY = "jbrain_demo";

export function isDemo(): boolean {
  return localStorage.getItem(DEMO_KEY) === "1" ||
    new URLSearchParams(location.search).has("demo");
}
export function setDemo(on: boolean) {
  if (on) localStorage.setItem(DEMO_KEY, "1");
  else localStorage.removeItem(DEMO_KEY);
}

const NOTE_BODIES: Record<string, any> = {
  "project-kickoff": {
    id: 1, title: "Project Kickoff", slug: "project-kickoff", kind: "entry",
    content_md: "Kicked off the launch. Owner is [[Health & Habits]] adjacent work.\n\n- scope the MVP\n- pick the stack\n- draft the timeline",
    created_at: "2026-05-30 09:12", updated_at: "2026-05-30 09:30",
    lat: 37.7749, lon: -122.4194, location_label: null,
    tags: ["work", "planning"],
    backlinks: [{ id: 3, title: "Health & Habits", slug: "health-habits" }],
  },
  "shopping-list": {
    id: 2, title: "Shopping List", slug: "shopping-list", kind: "entry",
    content_md: "# Shopping List\n\n- [ ] milk\n- [ ] eggs\n- [x] coffee\n- [ ] sourdough",
    created_at: "2026-05-31 18:40", updated_at: "2026-05-31 18:42",
    lat: null, lon: null, location_label: null, tags: ["errands"], backlinks: [],
  },
  "health-habits": {
    id: 3, title: "Health & Habits", slug: "health-habits", kind: "kb",
    content_md: "Synthesised knowledge.\n\nConsistent sleep and small habits compound. See [[Project Kickoff]] and [[Daily Log]].",
    created_at: "2026-05-25 08:00", updated_at: "2026-05-31 07:00",
    lat: null, lon: null, location_label: null, tags: ["health"],
    backlinks: [{ id: 4, title: "Daily Log", slug: "daily-log" }],
  },
  "daily-log": {
    id: 4, title: "Daily Log", slug: "daily-log", kind: "entry",
    content_md: "# Daily Log\n\n- **2026-05-30** ran 5k, felt good\n- **2026-05-31** deep work morning; read on habits",
    created_at: "2026-05-30 21:00", updated_at: "2026-05-31 21:15",
    lat: null, lon: null, location_label: null, tags: [], backlinks: [],
  },
};

const NOTES_LIST = Object.values(NOTE_BODIES).map((n: any) =>
  ({ id: n.id, title: n.title, slug: n.slug, kind: n.kind, updated_at: n.updated_at }));

const WORKFLOWS = [
  { id: 1, key: "daily-log-summary", name: "Daily log summary", trigger_type: "event",
    trigger_config: { event: "log_appended", match: { note_title: "Daily Log" } },
    action_type: "summarize_day_log", action_config: { log_title: "Daily Log" },
    enabled: true, locked: false, source: "repo", last_status: "ok", last_run_at: "2026-05-31 07:00" },
  { id: 2, key: "wiki-synthesis", name: "Knowledge-base synthesis", trigger_type: "schedule",
    trigger_config: { interval_seconds: 86400 }, action_type: "synthesize_wiki",
    action_config: { batch_limit: 50 }, enabled: true, locked: false, source: "repo",
    last_status: "ok", last_run_at: "2026-05-31 03:00" },
  { id: 3, key: "entry-autotag", name: "Auto-tag new entries", trigger_type: "event",
    trigger_config: { event: "entry_created" }, action_type: "generate_tags",
    action_config: {}, enabled: false, locked: false, source: "repo", last_status: null, last_run_at: null },
];

const PROMPTS = [
  { key: "architect.assisted", default: "You are the Chief Knowledge Architect…", override: null, effective: "You are the Chief Knowledge Architect…" },
  { key: "architect.research", default: "You are the read-only Researcher…", override: null, effective: "You are the read-only Researcher…" },
  { key: "actions.generate_tags", default: "Suggest 3-6 short lowercase tags…", override: null, effective: "Suggest 3-6 short lowercase tags…" },
];

const REVIEWS = [
  { id: 1, title: "Daily review — 2026-05-31", message: "Summarised 1 day of 'Daily Log'.", link_slug: "daily-log", created_at: "2026-05-31 07:00" },
];

// --- Action recipes (declarative pipelines) for the Actions card -----------
const ACTION_DEFS: Record<string, any> = {
  synthesize: {
    source: "repo", locked: false,
    recipe: { type: "synthesize", steps: [
      { id: "ctx", do: "gather_context", with: { source_title: "{{ config.source_title | default(none) }}", context_query: "{{ config.context_query | default(none) }}" } },
      { id: "out", do: "llm", with: { prompt: "{{ config.prompt | default(prompts.actions.synthesize) }}", content: "{{ ctx }}", max_tokens: 1024, on_no_key: "raise" } },
      { do: "write_note", with: { title: "{{ config.target_title }}", content_md: "{{ out }}", mode: "{{ config.mode | default('replace') }}" } },
      { do: "create_review", when: "{{ config.review }}", with: { title: "{{ config.review.title | default('Review: ' ~ config.target_title) }}", link_title: "{{ config.target_title }}" } },
    ] },
  },
  synthesize_wiki: {
    source: "repo", locked: false,
    recipe: { type: "synthesize_wiki", steps: [
      { id: "wm", do: "get_meta", with: { key: "wiki_synth:last_note_id", default: "0" } },
      { id: "entries", do: "query_notes", with: { kind: "entry", since_id: "{{ wm }}", limit: "{{ config.batch_limit | default(50) }}" }, stop_when_empty: "no new entries since last run" },
      { id: "kb", do: "query_notes", with: { kind: "kb" } },
      { id: "plan", do: "wiki_plan", with: { entries: "{{ entries }}", existing_kb: "{{ kb }}", instructions: "{{ config.instructions | default(none) }}" } },
      { id: "write", for_each: "{{ plan }}", steps: [
        { do: "write_note", with: { title: "{{ item.title }}", content_md: "{{ item.content_md }}", kind: "kb" } },
      ] },
      { do: "set_meta", when: "{{ plan }}", with: { key: "wiki_synth:last_note_id", value: "{{ entries | map(attribute='id') | max }}" } },
      { do: "create_review", when: "{{ config.review != false and plan }}", with: { title: "{{ config.review.title | default('Knowledge base updated') }}", link_title: "{{ plan[0].title }}" } },
    ] },
  },
  generate_tags: {
    source: "repo", locked: false,
    recipe: { type: "generate_tags", steps: [
      { id: "entry", do: "read_note", when: "{{ trigger.note_id }}", with: { id: "{{ trigger.note_id }}" } },
      { id: "tags", do: "suggest_tags", when: "{{ entry }}", with: { title: "{{ entry.title }}", content: "{{ entry.content_md }}", prompt: "{{ config.prompt | default(none) }}" } },
      { do: "set_tags", when: "{{ tags }}", with: { note_id: "{{ entry.id }}", tags: "{{ tags }}" } },
    ] },
  },
  append_to_note: {
    source: "repo", locked: false,
    recipe: { type: "append_to_note", steps: [
      { do: "write_note", with: { title: "{{ config.title }}", text: "{{ config.text | default('') }}", mode: "append" } },
      { do: "create_review", when: "{{ config.review }}", with: { title: "{{ config.review.title | default('Review: ' ~ config.title) }}", link_title: "{{ config.title }}" } },
    ] },
  },
  weekly_digest: {
    source: "user", locked: true,
    recipe: { type: "weekly_digest", steps: [
      { id: "kb", do: "call_action", with: { action: "synthesize_wiki", config: { batch_limit: 100 } } },
      { do: "create_review", with: { title: "Weekly digest ready", message: "Folded {{ kb.steps }} steps of new entries into the KB." } },
    ] },
  },
};

const PRIMITIVES_CATALOG = [
  { name: "call_action", summary: "Run another action recipe as a sub-pipeline (chaining).", output: "object" },
  { name: "create_review", summary: "Post a card to the Review inbox.", output: "object" },
  { name: "gather_context", summary: "Build context text from a note or a semantic search.", output: "scalar" },
  { name: "llm", summary: "Run an LLM prompt over optional context.", output: "scalar" },
  { name: "query_notes", summary: "List notes by kind / since id.", output: "list" },
  { name: "read_note", summary: "Read a note by title or id.", output: "object" },
  { name: "set_meta", summary: "Write a stored key.", output: "none" },
  { name: "set_tags", summary: "Set a note's tags.", output: "list" },
  { name: "suggest_tags", summary: "Ask the LLM for tags for a note.", output: "list" },
  { name: "wiki_plan", summary: "Fold entries into KB notes.", output: "list" },
  { name: "write_note", summary: "Create or update a note (versioned).", output: "object" },
];

function _flatDos(steps: any[]): string[] {
  const out: string[] = [];
  for (const s of steps || []) {
    if (s.for_each !== undefined) { out.push("for_each"); out.push(..._flatDos(s.steps)); }
    else if (s.do) out.push(s.do);
  }
  return out;
}

function _yamlScalar(v: any): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  const s = String(v);
  return /[:#{}[\],'"]|^\s|\s$|^$/.test(s) ? JSON.stringify(s) : s;
}
function _yaml(v: any, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(v)) {
    if (!v.length) return pad + "[]";
    return v.map((it) => {
      if (it && typeof it === "object") {
        const lines = _yaml(it, 0).split("\n");
        return lines.map((ln, i) => (i === 0 ? pad + "- " : pad + "  ") + ln).join("\n");
      }
      return pad + "- " + _yamlScalar(it);
    }).join("\n");
  }
  if (v && typeof v === "object") {
    return Object.entries(v).map(([k, val]) =>
      val && typeof val === "object" && (Array.isArray(val) ? val.length : Object.keys(val).length)
        ? pad + k + ":\n" + _yaml(val, indent + 1)
        : pad + k + ": " + _yamlScalar(val)).join("\n");
  }
  return pad + _yamlScalar(v);
}

function _actionList() {
  return Object.entries(ACTION_DEFS).map(([type, d]) => {
    const seq = _flatDos(d.recipe.steps);
    return { type, source: d.source, locked: d.locked, num_steps: seq.length, summary: seq.join(" → ") };
  });
}
function _actionDetail(type: string) {
  const d = ACTION_DEFS[type];
  return { type, source: d.source, locked: d.locked, recipe: d.recipe,
           recipe_yaml: _yaml(d.recipe), warnings: [], ref_count: 0 };
}

function match(path: string): any {
  const p = path.split("?")[0];
  if (p === "/api/auth/info") return { brain_name: "Demo Brain", version: __PWA_VERSION__ };
  if (p === "/api/auth/verify") return { ok: true };
  if (p === "/api/system/version") return { current: "demo", latest: null, update_available: false, release_url: null };
  if (p === "/api/reviews/count") return { pending: REVIEWS.length };
  if (p === "/api/reviews") return REVIEWS;
  if (p === "/api/workflows") return WORKFLOWS;
  if (/^\/api\/workflows\/\d+\/runs/.test(p)) return [{ id: 1, started_at: "2026-05-31 07:00", status: "ok", detail: "summarised 2026-05-30" }];
  if (p === "/api/prompts") return PROMPTS;
  if (p === "/api/action-defs") return _actionList();
  if (p === "/api/action-defs/primitives") return PRIMITIVES_CATALOG;
  const adef = p.match(/^\/api\/action-defs\/([^/]+)$/);
  if (adef && ACTION_DEFS[adef[1]]) return _actionDetail(adef[1]);
  if (p === "/api/graph") return {
    nodes: NOTES_LIST.map((n) => ({ id: n.id, title: n.title, slug: n.slug, val: 2 })),
    links: [{ source: 1, target: 3 }, { source: 4, target: 3 }],
  };
  if (p === "/api/search") return [
    { kind: "note", title: "Health & Habits", slug: "health-habits", score: 1.7 },
    { kind: "note", title: "Daily Log", slug: "daily-log", score: 1.2 },
  ];
  if (p === "/api/notes" || p === "/api/notes/") {
    const kind = new URLSearchParams(path.split("?")[1] || "").get("kind");
    return kind ? NOTES_LIST.filter((n) => n.kind === kind) : NOTES_LIST;
  }
  const ver = p.match(/^\/api\/notes\/([^/]+)\/versions$/);
  if (ver) return [
    { version_id: 2, is_current: true, title: "v", source: "workflow", conversation_id: null, note: null, created_at: "2026-05-31 07:00", size: 220 },
    { version_id: 1, is_current: false, title: "v", source: "user", conversation_id: null, note: null, created_at: "2026-05-30 09:30", size: 180 },
  ];
  const note = p.match(/^\/api\/notes\/([^/]+)$/);
  if (note) return NOTE_BODIES[note[1]] || { id: 0, title: note[1], slug: note[1], kind: "entry", content_md: "(demo note)", created_at: "", updated_at: "", lat: null, lon: null, location_label: null, tags: [], backlinks: [] };
  if (p === "/api/staging") return [];
  if (p === "/api/capture") return [];
  return null;
}

export function demoResponse(path: string, method = "GET"): any {
  if (method !== "GET") {
    if (/\/conversations$/.test(path)) return { id: 1 };
    if (/\/notes\/entry$/.test(path)) return { id: Date.now(), title: "Demo entry", slug: "health-habits" };
    if (/\/action-defs\/validate$/.test(path)) return { warnings: ["Demo mode — changes aren’t saved."], recipe: null };
    if (/\/action-defs\/sync$/.test(path)) return { synced: 0 };
    return { ok: true, id: 1 };
  }
  return match(path) ?? [];
}

export async function demoStream(
  text: string,
  onEvent: (e: any) => void,
  mode: string,
): Promise<void> {
  const reply = mode === "research"
    ? "Demo mode — I'd normally search your notes to answer that. For example, your [[Daily Log]] shows a 5k run on 2026-05-30."
    : `Demo mode — I'd help you shape that into a note. Here's a sample proposal for "${text.slice(0, 40)}".`;
  for (const word of reply.split(" ")) {
    onEvent({ type: "token", text: word + " " });
    await new Promise((r) => setTimeout(r, 35));
  }
  if (mode === "assisted") {
    onEvent({ type: "staging", actions: [{ type: "CREATE", title: "Sample Note", summary: "Proposed from your message" }] });
  }
  onEvent({ type: "done" });
}
