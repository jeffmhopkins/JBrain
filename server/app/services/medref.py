"""Medical-reference linking from reputable, public-domain NLM sources.

Medications only (the deterministic case): resolve a drug name -> an RxNorm RxCUI via the
RxNav API, then turn that code into a consumer drug-information page via MedlinePlus
Connect. LINK-ONLY — we add a "Further reading" link to the medication's KB article, never
import prose (so no accuracy / medical-advice / licensing exposure; every source is
US-government public domain). EXACT RxNorm matches auto-link; APPROXIMATE matches are
recorded as a talk todo for the owner to confirm.

Every lookup is cached in `medref_cache` so a name/code is fetched at most once. `_http_get`
is factored out so tests stub it (no network); a network hiccup degrades to "no link",
never an error.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_UA = "JBrain/0.1 (+https://github.com/jeffmhopkins/JBrain; personal brain)"
_TIMEOUT = 8.0
_RXNORM = "https://rxnav.nlm.nih.gov/REST"
_MEDLINEPLUS = "https://connect.medlineplus.gov/service"
_RXNORM_OID = "2.16.840.1.113883.6.88"   # RxNorm code system OID, for MedlinePlus Connect

# A single marked line we keep at the foot of a medication article (idempotent find/replace).
_MARK = "<!-- medref -->"
_LINE_RE = re.compile(r"(?m)^<!-- medref -->.*$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%f")


def _http_get(url: str):
    """One NLM GET -> parsed JSON. Factored out so tests stub it (no network)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _cache_get(conn, kind: str, key: str):
    row = conn.execute("SELECT payload_json FROM medref_cache WHERE kind=? AND key=?", (kind, key)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None


def _cache_put(conn, kind: str, key: str, payload) -> None:
    conn.execute(
        "INSERT INTO medref_cache (kind, key, payload_json, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(kind, key) DO UPDATE SET payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
        (kind, key, json.dumps(payload), _now()))
    conn.commit()


def _norm_key(name: str) -> str:
    return " ".join((name or "").lower().split())[:200]


def _rxcui_exact(conn, name: str) -> str | None:
    """Exact/normalized RxNorm concept id (RxCUI) for a drug name, or None. '' is cached as
    a negative result so a miss isn't re-fetched."""
    key = _norm_key(name)
    cached = _cache_get(conn, "rxcui", key)
    if cached is not None:
        return cached or None
    # search=0 = EXACT (incl. known synonyms/spellings). search=1 (normalized) is too loose —
    # it maps "metformin" to a combination product, so it must NOT be used for auto-linking.
    url = f"{_RXNORM}/rxcui.json?name={urllib.parse.quote(name)}&search=0"
    try:
        data = _http_get(url)
    except Exception:  # noqa: BLE001
        return None
    ids = (((data or {}).get("idGroup") or {}).get("rxnormId")) or []
    rxcui = str(ids[0]) if ids else ""
    _cache_put(conn, "rxcui", key, rxcui)
    return rxcui or None


def _rxcui_approx(conn, name: str) -> dict | None:
    """Top APPROXIMATE RxNorm candidate {rxcui, score, name}, or None. NB: the approximateTerm
    `score` is NOT a reliable confidence gate (a good typo can score lower than a garbage
    phrase), so we do NOT threshold on it — the approximate path only ever *proposes* a match
    for the owner to confirm, never auto-links."""
    key = _norm_key(name)
    cached = _cache_get(conn, "approx", key)
    if cached is not None:
        return cached or None
    url = f"{_RXNORM}/approximateTerm.json?term={urllib.parse.quote(name)}&maxEntries=1"
    try:
        data = _http_get(url)
    except Exception:  # noqa: BLE001
        return None
    cands = (((data or {}).get("approximateGroup") or {}).get("candidate")) or []
    out: dict = {}
    if cands and cands[0].get("rxcui"):
        c = cands[0]
        try:
            score = round(float(c.get("score") or 0), 1)
        except (TypeError, ValueError):
            score = 0
        out = {"rxcui": str(c["rxcui"]), "score": score, "name": c.get("name") or ""}
    _cache_put(conn, "approx", key, out)
    return out or None


def medlineplus_url(conn, rxcui: str) -> dict | None:
    """{url, title} of the MedlinePlus consumer drug page for an RxCUI (via MedlinePlus
    Connect), or None."""
    cached = _cache_get(conn, "mplus", str(rxcui))
    if cached is not None:
        return cached or None
    url = (f"{_MEDLINEPLUS}?mainSearchCriteria.v.cs={_RXNORM_OID}"
           f"&mainSearchCriteria.v.c={urllib.parse.quote(str(rxcui))}&knowledgeResponseType=application/json")
    try:
        data = _http_get(url)
    except Exception:  # noqa: BLE001
        return None
    out: dict = {}
    entries = (((data or {}).get("feed") or {}).get("entry")) or []
    if entries:
        e = entries[0]
        href = next((ln.get("href") for ln in (e.get("link") or []) if ln.get("href")), "")
        title = e.get("title")
        if isinstance(title, dict):
            title = title.get("_value")
        if href:
            out = {"url": href, "title": title or "MedlinePlus"}
    _cache_put(conn, "mplus", str(rxcui), out)
    return out or None


def resolve(conn, name: str) -> dict | None:
    """Resolve a drug NAME to a MedlinePlus reference. Tries an exact RxNorm match first,
    then an approximate one. Returns {match:'exact'|'approx', rxcui, url, title[, candidate,
    score]} or None (no confident match / nothing on MedlinePlus / offline)."""
    name = (name or "").strip()
    if not name:
        return None
    rxcui = _rxcui_exact(conn, name)
    match, score, candidate = "exact", None, None
    if not rxcui:
        ap = _rxcui_approx(conn, name)
        if not ap:
            return None
        rxcui, match, score, candidate = ap["rxcui"], "approx", ap["score"], ap["name"]
    mp = medlineplus_url(conn, rxcui)
    if not mp:
        return None
    out = {"match": match, "rxcui": rxcui, "url": mp["url"], "title": mp["title"]}
    if match == "approx":
        out["score"], out["candidate"] = score, candidate
    return out


def _apply_link(conn, article_title: str, url: str) -> bool:
    """Ensure the medication article carries a single marked MedlinePlus 'Further reading'
    line (idempotent; replaces a stale URL). Versioned; returns True if it changed the body."""
    from . import notes as notes_svc
    row = conn.execute(
        "SELECT id, content_md FROM notes WHERE lower(title)=lower(?) AND deleted_at IS NULL AND kind='kb'",
        (article_title,)).fetchone()
    if not row:
        return False
    line = f"{_MARK} **Further reading:** [MedlinePlus drug information]({url})"
    body = row["content_md"] or ""
    new = _LINE_RE.sub(line, body) if _LINE_RE.search(body) else (body.rstrip() + "\n\n" + line + "\n")
    if new == body:
        return False
    notes_svc.upsert_note(conn, article_title, new, note_id=row["id"], kind="kb",
                          source="medref", version_note="medref: MedlinePlus drug reference")
    return True


def link_medications(conn, limit: int = 200) -> dict:
    """Add MedlinePlus drug references to medication KB articles. Walks medication entities
    that already have an article; an EXACT RxNorm match auto-links the article, an
    APPROXIMATE match is recorded as a talk todo for the owner to confirm. Link-only; cached;
    no LLM. Returns {checked, linked, proposed}."""
    from . import article_talk
    rows = conn.execute(
        "SELECT canonical_name, article_title FROM entities "
        "WHERE type='medication' AND article_title IS NOT NULL ORDER BY note_count DESC LIMIT ?",
        (max(1, int(limit)),)).fetchall()
    checked = linked = proposed = 0
    for r in rows:
        checked += 1
        res = resolve(conn, r["canonical_name"])
        if not res:
            continue
        if res["match"] == "exact":
            if _apply_link(conn, r["article_title"], res["url"]):
                linked += 1
        else:
            body = (f"External medication reference: \"{r['canonical_name']}\" approximately matches "
                    f"MedlinePlus via RxNorm \"{res.get('candidate') or ''}\" (rxcui {res['rxcui']}, "
                    f"score {res.get('score')}). Confirm to link: {res['url']}")
            article_talk.record(conn, r["article_title"], [{"kind": "todo", "body": body}], author="ai")
            proposed += 1
    conn.commit()
    return {"checked": checked, "linked": linked, "proposed": proposed}
