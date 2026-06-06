"""One-time migration: split a person's personal medical history out of their kb/People/<Name>
article into a dedicated kb/Health/<Name> PHI page — deterministically, faithfully, undoably.

Why deterministic (no LLM rewriting): we MOVE existing prose verbatim, so the migration can
never hallucinate or drop a medical fact. The only judgement is WHICH sections are medical, and
that is conservative: a top-level "## <medical heading>" section is moved only if it also carries
a medical SIGNAL (a kb/Reference/Medicine link or a medical term). Anything ambiguous (medical
words in the lead / Key facts, a medical-looking heading with no signal) is reported for manual
review and left untouched — never auto-cut.

Idempotent: a re-run finds no medical section left in an already-split People article and no-ops.
Every write is versioned (source='workflow') so the whole migration is restorable note-by-note.
"""
from __future__ import annotations

import re

from ..db import get_meta, set_meta
from . import notes as notes_svc
from . import wiki_guides

# Per-person completion marker: meta["health_split:done:<people_title>"] = "<health_title>".
LEDGER_PREFIX = "health_split:done:"

# Top-level "## <name>" headings whose content is a person's personal medical record.
_MED_HEADINGS = {
    "health", "health history", "medical", "medical history", "medical record",
    "conditions", "diagnoses", "diagnosis", "medications", "prescriptions", "procedures",
    "surgeries", "labs", "lab results", "labs & vitals", "vitals", "encounters",
    "admissions", "hospitalizations", "care notes",
}

# A medical SIGNAL gates a cut so a non-medical "## Conditions" (of employment) / "## Procedures"
# (of a job) is never moved. A Reference/Medicine link, or a clinical term, is enough.
_MED_SIGNAL = re.compile(
    r"\[\[kb/Reference/Medicine/"
    r"|\b(?:diagnos|prescri|dosage|dose|\d+\s*m(?:g|cg|l)\b|lab|blood|platelet|cholesterol|"
    r"glucose|mmhg|vital|surgery|surgical|admitt|admission|discharge|symptom|treatment|"
    r"medication|vaccin|biopsy|mg/dl|hospital|clinic|physician|prognosis)", re.I)

_HEAD_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_FN_MARK_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")           # a footnote USE (not a definition)
_FN_DEF_RE = re.compile(r"(?m)^(\[\^([^\]\s]+)\]:\s.*)$")    # a footnote DEFINITION line


def _truthy(v) -> bool:
    return v not in (False, None, 0, "", "0", "false", "False", "no", "off")


def _sections(content: str):
    """Split markdown into (lead, [{name, text}]) on top-level ## headings; `text` includes
    the heading line through the byte before the next ## (or EOF), right-stripped."""
    heads = list(_HEAD_RE.finditer(content))
    if not heads:
        return content, []
    lead = content[:heads[0].start()]
    secs = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        secs.append({"name": m.group(1).strip(), "text": content[m.start():end].rstrip()})
    return lead, secs


def _plan_person(conn, people_title: str, content: str) -> dict | None:
    """Plan the move for one People article. Returns None when there's nothing medical, a
    {borderline: reason} dict when medical content exists but can't be safely auto-cut, else a
    full plan {leaf, people_title, moved_names, health_body, new_people_body}."""
    lead, secs = _sections(content)
    med = [s for s in secs if s["name"].lower() in _MED_HEADINGS
           and s["name"].lower() != "references" and _MED_SIGNAL.search(s["text"])]
    if not med:
        # Conservative: if the article carries a medical signal but it isn't in a cleanly-cuttable
        # "## <medical heading>" section (e.g. it's in the lead or Key facts), surface it for manual
        # review rather than guessing a cut. No signal at all → nothing medical here, leave it.
        return ({"borderline": "possible medical content but no clean medical ## section to cut"}
                if _MED_SIGNAL.search(content) else None)

    leaf = people_title.split("/")[-1]
    remaining_secs = [s for s in secs if s not in med]

    moved_text = "\n\n".join(s["text"] for s in med)
    remaining_text = lead + "\n" + "\n".join(s["text"] for s in remaining_secs)
    markers_moved = set(_FN_MARK_RE.findall(moved_text))
    markers_remaining = set(_FN_MARK_RE.findall(remaining_text))
    defs = {m.group(2): m.group(1) for m in _FN_DEF_RE.finditer(content)}
    # MOVE a def only if its marker is used exclusively in the moved sections; COPY (keep in
    # both) one still used by surviving People text — so neither page is left with a dangling marker.
    to_move = {i for i in markers_moved if i in defs and i not in markers_remaining}
    to_copy = {i for i in markers_moved if i in defs and i in markers_remaining}

    # --- The new Health page: H1 + a back-link lead + the moved sections + the carried defs.
    hp = [f"# {leaf}", f"Personal health record for [[{people_title}]]."]
    hp += [s["text"] for s in med]
    health_defs = [defs[i] for i in sorted(to_move | to_copy)]
    if health_defs:
        hp.append("## References\n" + "\n".join(health_defs))
    health_body = "\n\n".join(hp).rstrip() + "\n"

    # --- The stripped People page: lead + non-medical sections, with moved defs removed and an
    #     emptied ## References dropped. No link to the Health page (the PII firewall forbids it).
    pp = [lead.rstrip()]
    for s in remaining_secs:
        if s["name"].lower() == "references":
            lines = s["text"].splitlines()
            kept = [ln for ln in lines if not any(ln.strip().startswith(f"[^{i}]:") for i in to_move)]
            if not [ln for ln in kept[1:] if ln.strip()]:       # only the heading left → drop it
                continue
            pp.append("\n".join(kept).rstrip())
        else:
            pp.append(s["text"])
    new_people_body = "\n\n".join(p for p in pp if p.strip()).rstrip() + "\n"

    return {"leaf": leaf, "people_title": people_title, "moved_names": [s["name"] for s in med],
            "health_body": health_body, "new_people_body": new_people_body, "borderline": None}


def _disambiguate(conn, people_title: str, leaf: str) -> str:
    """A kb/Health/<leaf> already belongs to a DIFFERENT person (two same-named People under
    different parents). Pick a stable, collision-free title that mirrors the People parent."""
    parts = people_title.split("/")
    parent = parts[-2] if len(parts) >= 4 else None             # kb/People/<parent>/<leaf>
    cand = f"kb/Health/{parent} {leaf}" if parent else f"kb/Health/{leaf} (2)"
    n = 1
    while True:
        ex = notes_svc.get_by_title(conn, cand)
        if not ex or f"[[{people_title}]]" in (ex["content_md"] or ""):
            return cand
        n += 1
        cand = f"kb/Health/{leaf} ({n})"


def _apply_person(conn, plan: dict, on_conflict: str) -> dict:
    """Write one person's split (validated, versioned, committed). Never writes a structurally
    broken page — a lint error aborts that person without touching their People article."""
    people_title = plan["people_title"]
    target = f"kb/Health/{plan['leaf']}"
    existing = notes_svc.get_by_title(conn, target)
    if existing:
        owns = (f"[[{people_title}]]" in (existing["content_md"] or "")
                or get_meta(LEDGER_PREFIX + people_title) == target)
        if not owns:
            target = _disambiguate(conn, people_title, plan["leaf"])
            existing = notes_svc.get_by_title(conn, target)
        if existing and owns:
            # The owner may have edited their health page since a prior run — don't clobber it.
            if on_conflict == "skip":
                return {"status": "conflict", "title": target}
            if on_conflict == "append":
                tail = plan["health_body"].split("\n\n", 2)[-1]   # drop the H1 + lead, keep sections
                plan = {**plan, "health_body": existing["content_md"].rstrip() + "\n\n" + tail}

    # Validate BOTH resulting pages (catches a dangling footnote marker on either side) before
    # any write — so an abort leaves the source untouched.
    for ttl, body in ((target, plan["health_body"]), (people_title, plan["new_people_body"])):
        v = wiki_guides.validate_structure(ttl, body)
        if v["errors"]:
            return {"status": "error", "title": ttl, "errors": v["errors"]}

    notes_svc.upsert_note(conn, target, plan["health_body"], source="workflow",
                          version_note=f"health split: from {people_title}", kind="kb")
    pid = notes_svc.get_by_title(conn, people_title)["id"]
    notes_svc.upsert_note(conn, people_title, plan["new_people_body"], note_id=pid, source="workflow",
                          version_note=f"health split: moved {', '.join(plan['moved_names'])} → {target}",
                          kind="kb")
    set_meta(conn, LEDGER_PREFIX + people_title, target)
    conn.commit()
    return {"status": "extracted", "title": target, "moved": plan["moved_names"]}


def _scan(conn, *, dry_run: bool, limit: int, on_conflict: str) -> dict:
    rep = {"ok": True, "dry_run": dry_run, "scanned": 0, "extracted": 0,
           "conflicts": 0, "borderline": 0, "errors": 0, "people": [], "flagged": []}
    rows = conn.execute(
        "SELECT id, title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
        "AND redirect_to IS NULL AND title LIKE 'kb/People/%' ORDER BY title").fetchall()
    for r in rows:
        if not dry_run and rep["extracted"] >= limit:
            break
        if wiki_guides.is_protected(r["title"]):                # never touch kb/People/_Guide
            continue
        rep["scanned"] += 1
        plan = _plan_person(conn, r["title"], r["content_md"] or "")
        if plan is None:
            continue
        if plan.get("borderline"):
            rep["borderline"] += 1
            rep["flagged"].append({"person": r["title"], "reason": plan["borderline"]})
            continue
        if dry_run:
            rep["extracted"] += 1
            rep["people"].append({"person": r["title"], "target": f"kb/Health/{plan['leaf']}",
                                  "sections": plan["moved_names"]})
            continue
        res = _apply_person(conn, plan, on_conflict)
        if res["status"] == "extracted":
            rep["extracted"] += 1
            rep["people"].append({"person": r["title"], "target": res["title"], "sections": res["moved"]})
        elif res["status"] == "conflict":
            rep["conflicts"] += 1
            rep["flagged"].append({"person": r["title"], "reason": f"health page already exists → {res['title']} (skipped)"})
        else:
            rep["errors"] += 1
            rep["flagged"].append({"person": r["title"], "reason": "; ".join(res.get("errors") or ["lint error"])})
    return rep


def extract_health(conn, *, dry_run: bool = True, limit: int = 200, on_conflict: str = "skip") -> dict:
    """Scan kb/People/* for personal medical sections and (unless dry_run) move each into a
    kb/Health/<Person> page. Apply runs under the KB write lock so it can't interleave with the
    nightly incremental update / maintenance. dry_run reports what WOULD move and writes nothing."""
    dry_run = _truthy(dry_run)
    if dry_run:
        return _scan(conn, dry_run=True, limit=int(limit), on_conflict=str(on_conflict))
    from . import wiki_build
    if not wiki_build.kb_lock_acquire(conn):
        return {"ok": False, "skipped": "kb busy (another KB write is in progress) — try again shortly"}
    try:
        return _scan(conn, dry_run=False, limit=int(limit), on_conflict=str(on_conflict))
    finally:
        wiki_build.kb_lock_release(conn)


def health_page_for(conn, people_title: str) -> str | None:
    """The kb/Health/<leaf> page that holds this person's medical record, if one exists — used
    by the incremental builder to route a person's medical captures to their health page instead
    of their (now medical-free) People article. Existence-gated: no health page → no rerouting."""
    if not (people_title or "").lower().startswith("kb/people/"):
        return None
    target = "kb/Health/" + people_title.split("/")[-1]
    # redirect_to IS NULL: a merged-away health page is a redirect, never the live record —
    # returning it would route medical captures into a redirect (and resurrect it on write).
    row = conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL "
                       "AND redirect_to IS NULL AND lower(title)=lower(?)", (target,)).fetchone()
    if row:
        return row["title"]
    # Disambiguated page (collision case): find a kb/Health/* that links back to this person.
    for r in conn.execute("SELECT title, content_md FROM notes WHERE kind='kb' AND deleted_at IS NULL "
                          "AND redirect_to IS NULL AND lower(title) LIKE 'kb/health/%'"):
        if f"[[{people_title}]]" in (r["content_md"] or ""):
            return r["title"]
    return None
