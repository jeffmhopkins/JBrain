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
_MPLUS_SEARCH = "https://wsearch.nlm.nih.gov/ws/query"   # MedlinePlus health-topics web service
_RXNORM_OID = "2.16.840.1.113883.6.88"   # RxNorm code system OID, for MedlinePlus Connect

# Only an href on one of these public NLM hosts may ever be stored/cited (a response can't
# redirect us to an arbitrary URL — defense against a spoofed/compromised feed).
_NLM_HOSTS = ("medlineplus.gov", "nlm.nih.gov")

# A single marked line we keep at the foot of a medication article (idempotent find/replace).
_MARK = "<!-- medref -->"
_LINE_RE = re.compile(r"(?m)^<!-- medref -->.*$")


def _now() -> str:
    """Return the current UTC timestamp as a string for cache writes.

    Returns:
        UTC timestamp in '%Y-%m-%d %H:%M:%f' format.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%f")


def _http_get(url: str):
    """Fetch a URL and return parsed JSON. Factored out so tests can stub it without network.

    Args:
        url: Full NLM API URL.

    Returns:
        Parsed JSON object (dict or list).

    Raises:
        urllib.error.URLError: On network or HTTP error.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get_text(url: str) -> str:
    """Fetch a URL and return the response body as text. Stubbable in tests.

    Used for the XML health-topics web service rather than the JSON endpoints.

    Args:
        url: Full NLM API URL.

    Returns:
        Response body decoded as UTF-8.

    Raises:
        urllib.error.URLError: On network or HTTP error.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _host_ok(url: str) -> bool:
    """Return True only if the URL's host is a public NLM host.

    Prevents storing or citing an off-NLM href if a feed is spoofed or compromised.

    Args:
        url: URL string to validate.

    Returns:
        True if the hostname is medlineplus.gov, nlm.nih.gov, or any subdomain thereof.
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return bool(host) and any(host == h or host.endswith("." + h) for h in _NLM_HOSTS)


def _parse_first_topic(xml_text: str) -> dict:
    """Parse the first MedlinePlus health-topic document from an XML response.

    Strips highlight/markup tags from content fields. The caller re-validates the returned
    URL's host before storing or citing it.

    Args:
        xml_text: Raw XML response from the MedlinePlus health-topics web service.

    Returns:
        Dict with keys url, title, snippet; or {} if no document was found or parsing
        failed.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return {}
    doc = root.find(".//document")
    if doc is None:
        return {}
    url = (doc.get("url") or "").strip()
    title = summary = ""
    for c in doc.findall("content"):
        name = c.get("name")
        # Replace the (escaped) HTML highlight/markup with spaces — NOT nothing — so list items
        # and paragraphs don't run together ("platelets:</li><li>If" -> "platelets: If"); collapse ws.
        text = " ".join(re.sub(r"<[^>]+>", " ", "".join(c.itertext())).split())
        if name == "title" and not title:
            title = text
        elif name in ("FullSummary", "snippet") and not summary:
            summary = text
    if not url:
        return {}
    return {"url": url, "title": title or "MedlinePlus", "snippet": (summary or "")[:400]}


def health_topic(conn, query: str) -> dict | None:
    """Resolve a health-topic query to a MedlinePlus reference.

    Uses the MedlinePlus health-topics web service (NLM, public domain). Cached in
    medref_cache; fail-soft to None on network error. The returned URL is host-pinned to
    a public NLM domain.

    Args:
        conn: Database connection (for cache reads/writes).
        query: Health topic search query string.

    Returns:
        Dict with keys url, title, snippet; or None if no result was found.
    """
    key = _norm_key(query)
    if not key:
        return None
    cached = _cache_get(conn, "mplus_topic", key)
    if cached is not None:
        return cached or None
    url = f"{_MPLUS_SEARCH}?db=healthTopics&rettype=brief&retmax=1&term={urllib.parse.quote(query)}"
    try:
        out = _parse_first_topic(_http_get_text(url))
    except Exception:  # noqa: BLE001
        return None
    if out and not _host_ok(out.get("url", "")):
        out = {}                                   # reject an href that isn't on a public NLM host
    _cache_put(conn, "mplus_topic", key, out)      # negative result cached as {} (won't re-fetch)
    return out or None


def _cache_get(conn, kind: str, key: str):
    """Read a cached medref entry.

    Args:
        conn: Database connection.
        kind: Cache entry type (e.g. 'rxcui', 'mplus', 'approx', 'mplus_topic').
        key: Normalized lookup key.

    Returns:
        Parsed JSON payload, or None if the entry is absent or malformed.
    """
    row = conn.execute("SELECT payload_json FROM medref_cache WHERE kind=? AND key=?", (kind, key)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None


def _cache_put(conn, kind: str, key: str, payload) -> None:
    """Write or update a medref cache entry.

    Args:
        conn: Database connection.
        kind: Cache entry type.
        key: Normalized lookup key.
        payload: JSON-serializable payload to store (empty dict {} caches a negative result).
    """
    conn.execute(
        "INSERT INTO medref_cache (kind, key, payload_json, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(kind, key) DO UPDATE SET payload_json=excluded.payload_json, fetched_at=excluded.fetched_at",
        (kind, key, json.dumps(payload), _now()))
    conn.commit()


def _norm_key(name: str) -> str:
    """Normalize a drug or topic name to a stable lowercase cache key.

    Args:
        name: Raw name string.

    Returns:
        Lowercased, whitespace-collapsed string truncated to 200 characters.
    """
    return " ".join((name or "").lower().split())[:200]


def _rxcui_exact(conn, name: str) -> str | None:
    """Look up the exact/normalized RxNorm concept ID (RxCUI) for a drug name.

    Uses search=0 (exact match including known synonyms/spellings). search=1 (normalized)
    is too loose — it maps 'metformin' to a combination product. An empty string '' is
    cached as a negative result so a miss isn't re-fetched.

    Args:
        conn: Database connection (for cache).
        name: Drug name to look up.

    Returns:
        RxCUI string if an exact match was found, or None.
    """
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
    """Return the top approximate RxNorm candidate for a drug name, or None.

    The approximateTerm score is NOT a reliable confidence gate (a good typo can score
    lower than a garbage phrase), so we do not threshold on it — the approximate path only
    ever proposes a match for the owner to confirm, never auto-links.

    Args:
        conn: Database connection (for cache).
        name: Drug name to look up.

    Returns:
        Dict with keys rxcui, score, name; or None if no candidate was found.
    """
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
    """Return the MedlinePlus consumer drug page reference for an RxCUI.

    Uses the MedlinePlus Connect API. Cached in medref_cache; fail-soft to None.

    Args:
        conn: Database connection (for cache).
        rxcui: RxNorm concept ID string.

    Returns:
        Dict with keys url, title, and optionally summary; or None if not found.
    """
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
        summary = e.get("summary")
        if isinstance(summary, dict):
            summary = summary.get("_value")
        if href:
            out = {"url": href.split("?")[0], "title": title or "MedlinePlus"}   # drop utm tracking params
            if summary:
                # The public-domain consumer summary ("what it's for / how it works"). Strip any HTML and
                # collapse whitespace; cap length. NOT dosing — that lives on the page, behind the link.
                out["summary"] = " ".join(re.sub(r"<[^>]+>", " ", str(summary)).split())[:600]
    _cache_put(conn, "mplus", str(rxcui), out)
    return out or None


def resolve(conn, name: str) -> dict | None:
    """Resolve a drug name to a MedlinePlus reference.

    Tries an exact RxNorm match first, then an approximate one. Returns None when no
    confident match is found, MedlinePlus has no page for the RxCUI, or the service is
    offline.

    Args:
        conn: Database connection (for cache).
        name: Drug name to resolve.

    Returns:
        Dict with keys match ('exact' or 'approx'), rxcui, url, title, and optionally
        summary, score, candidate; or None.
    """
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
    if mp.get("summary"):
        out["summary"] = mp["summary"]
    if match == "approx":
        out["score"], out["candidate"] = score, candidate
    return out


def drug_topic(conn, name: str) -> dict | None:
    """Resolve a medication name to a host-pinned MedlinePlus consumer drug page.

    Thin wrapper over resolve() that adds a final host-pin check: the returned URL must be
    on a public NLM host so a feed can't redirect off-NLM. The outbound RxNav / MedlinePlus
    Connect fetch is cached; fails soft to None. This is the single drug-info entry point
    the gated drug_reference tool and its approval endpoint both call.

    Args:
        conn: Database connection (for cache).
        name: Medication name to resolve.

    Returns:
        Dict with keys match, rxcui, url, title, and optionally summary, score, candidate;
        or None if no result is found or the URL is off-NLM.
    """
    res = resolve(conn, name)
    if not res or not _host_ok(res.get("url", "")):
        return None
    return res


def _apply_link(conn, article_title: str, url: str) -> bool:
    """Ensure a medication KB article carries a single marked MedlinePlus 'Further reading' line.

    Idempotent: replaces a stale URL if one already exists. The note is versioned so the
    change is restorable.

    Args:
        conn: Database connection.
        article_title: Exact title of the kb note to update.
        url: MedlinePlus consumer drug page URL.

    Returns:
        True if the article body was changed, False if it already had the correct link or
        the note was not found.
    """
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
    """Add MedlinePlus drug references to medication KB articles.

    Walks medication entities that already have an article. An exact RxNorm match auto-links
    the article; an approximate match is recorded as a talk todo for the owner to confirm.
    Link-only; cached; no LLM.

    Args:
        conn: Database connection.
        limit: Maximum number of medication entities to process.

    Returns:
        Dict with keys: checked, linked, proposed.
    """
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
