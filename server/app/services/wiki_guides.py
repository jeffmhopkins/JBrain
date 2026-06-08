"""Knowledge-base GUIDES — the backbone of the wiki build/maintenance.

Each guide is human-readable prose (read by the LLM when writing an article) PLUS a
fenced ```spec block that the structure lint parses — so the guide is the single
source of truth for both writing AND checking, and the two can't drift. Guides are
seeded as read-only `kb/_*` pages (viewable, owner-editable), and are the canonical
text the article-writer references.

`validate_structure` enforces the spec: a required lead, citation integrity (reusing
pipeline.citation_issues), the Reference PII firewall (no links to People/Groups), and
required sections — with recommended sections/links surfaced as advisory warnings.
"""
from __future__ import annotations

import re

import yaml

from . import prompts

# Taxonomy roots (the seeded domains). Order is display order. "Health" and "Finance" sit
# together after People as the firewalled PII vaults (kb/Health/<Person>, kb/Finance/<Sub>/<Name>)
# split out of the shareable wiki, so a subject can be shared without leaking medical or financial data.
DOMAINS = ["Reference", "People", "Health", "Finance", "Groups", "Places", "Things", "Activities"]

# Spec defaults — overlaid by the general guide's spec, then the domain guide's spec.
_DEFAULTS = {
    "require_lead": True,
    "require_references_when_cited": True,
    "required_sections": [],
    "recommended_sections": [],
    "forbid_link_prefixes": [],
    "recommend_link_prefixes": [],
    "stub_max_chars": 350,
}

_SPEC_RE = re.compile(r"```spec\s*\n(.*?)```", re.DOTALL)
_H1_RE = re.compile(r"(?m)^#\s.*$")
_SECTION_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_FIRST_SECTION_RE = re.compile(r"(?m)^##\s")
_FN_MARK_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_FN_DEF_RE = re.compile(r"(?m)^[ \t]*\[\^([^\]\s]+)\]:")     # [^id]: definition line
_REFS_HEAD_RE = re.compile(r"(?mi)^##\s+References\s*$")
# Relative-time values that should be live @t[...] tokens, not frozen numbers. A
# correctly-encoded age ("@t[age:1986-03-15] years old") has no digit before "years",
# so it won't match — only literals like "40 years old" / "3 months ago" / "aged 40" do.
_REL_TIME_RE = re.compile(
    r"\b(\d{1,3}\s+years?\s+old|aged\s+\d{1,3}|\d+\s+(?:years?|months?|weeks?|days?)\s+ago)\b", re.I)


def guide_key(domain: str | None) -> str:
    """Return the prompts.yaml key for a domain's guide.

    Args:
        domain: Taxonomy domain name, or None for the general style guide.

    Returns:
        Dotted key string suitable for prompts.get().
    """
    return "actions.wiki_guide.general" if not domain else f"actions.wiki_guide.{domain.lower()}"


def guide_title(domain: str | None) -> str:
    """Return the wiki page title for a domain's guide.

    Args:
        domain: Taxonomy domain name, or None for the general style guide.

    Returns:
        Page title string (e.g. 'kb/_Style Guide' or 'kb/People/_Guide').
    """
    return "kb/_Style Guide" if not domain else f"kb/{domain}/_Guide"


def guide_text(domain: str | None) -> str:
    """Return the guide prose for a domain from the prompt store.

    Args:
        domain: Taxonomy domain name, or None for the general style guide.

    Returns:
        Guide text string, or '' if not found.
    """
    return prompts.get(guide_key(domain), "")


def is_protected(title: str) -> bool:
    """Return True if a wiki page is a protected system page.

    A page is protected when any path segment starts with '_' (e.g. kb/_index,
    kb/_Style Guide, kb/People/_Guide). Protected pages are never deleted by a rebuild,
    never fed back as a synthesis source, and never overwritten by the article writer.

    Args:
        title: Wiki page title (slash-separated path).

    Returns:
        True if the title contains a '_'-prefixed segment.
    """
    return any(seg.startswith("_") for seg in (title or "").split("/"))


def domain_for_title(title: str) -> str | None:
    """Return the taxonomy domain for a kb article from its path (kb/<Domain>/...).

    Args:
        title: Wiki page title (slash-separated path).

    Returns:
        Matched domain string (e.g. 'People'), or None if unrecognised.
    """
    parts = (title or "").split("/")
    if len(parts) >= 2 and parts[0].lower() == "kb":
        for d in DOMAINS:
            if parts[1].lower() == d.lower():
                return d
    return None


# The PHI firewall predicate — THE single place that recognises a personal-health page.
# Reused by the structure lint (via the People/Reference forbid lists), the share layer
# (PHI-hardened minting), and research scope (default-deny). A kb/Health/<Person> page is a
# per-person medical record; nothing outside Health may link it, and recipients never reach it.
HEALTH_PREFIX = "kb/health/"


def is_health_title(title: str) -> bool:
    """Return True for a personal-health page (kb/Health/<Person>) via case-insensitive prefix match.

    Args:
        title: Wiki page title to test.

    Returns:
        True if the title is under the kb/Health/ prefix.
    """
    return (title or "").lower().startswith(HEALTH_PREFIX)


# The PRIVATE/sensitive domains: PII vaults that are firewalled out of the shareable wiki
# (no other domain may link them), share-hardened (any share is browser-bound + finite-TTL),
# and hidden from research recipients. "Health" is live; "Finance" is registered here so the
# firewall protects any kb/Finance/* note from the moment one exists — it is added to DOMAINS
# (with its guide) separately, so this registry can lead the taxonomy without churn.
PRIVATE_DOMAINS = ("Health", "Finance")
_PRIVATE_PREFIXES = tuple(f"kb/{d.lower()}/" for d in PRIVATE_DOMAINS)


def is_private_title(title: str) -> bool:
    """Return True for any sensitive-domain page (kb/Health/... or kb/Finance/...).

    This is the single firewall predicate reused by the share layer, research scope,
    the structure lint, and the entity index.

    Args:
        title: Wiki page title to test.

    Returns:
        True if the title falls under any private-domain prefix.
    """
    t = (title or "").lower()
    return any(t.startswith(p) for p in _PRIVATE_PREFIXES)


def private_domain_for(title: str) -> str | None:
    """Return the private domain a title belongs to, or None.

    Returns a domain name only once it is also in DOMAINS (i.e. fully wired with a
    guide); before that, domain_for_title will not recognise it.

    Args:
        title: Wiki page title to inspect.

    Returns:
        Private domain string (e.g. 'Health'), or None.
    """
    d = domain_for_title(title)
    return d if d in PRIVATE_DOMAINS else None


def parse_spec(text: str) -> dict:
    """Extract and parse the fenced ```spec YAML block from a guide.

    Args:
        text: Raw guide text that may contain a fenced spec block.

    Returns:
        Parsed spec dict, or {} if absent or unparseable.
    """
    m = _SPEC_RE.search(text or "")
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def spec_for(domain: str | None) -> dict:
    """Return the effective spec for a domain by layering defaults, general guide, and domain guide.

    Merge order: _DEFAULTS <- general guide spec <- domain guide spec.

    Args:
        domain: Taxonomy domain name, or None for the general spec only.

    Returns:
        Merged spec dict.
    """
    spec = dict(_DEFAULTS)
    spec.update(parse_spec(guide_text(None)))
    if domain:
        spec.update(parse_spec(guide_text(domain)))
    return spec


def _links(text: str) -> list[str]:
    """Return all wikilink targets found in text.

    Args:
        text: Markdown text to scan.

    Returns:
        List of link target strings.
    """
    from . import wikilinks
    return wikilinks.extract_links(text or "")


def _references_slice(body: str) -> str:
    """Return the text under the '## References' heading, up to the next section or EOF.

    Args:
        body: Full article markdown text.

    Returns:
        Text content of the References section, or '' if the article has none.
    """
    m = _REFS_HEAD_RE.search(body or "")
    if not m:
        return ""
    rest = body[m.end():]
    nxt = _FIRST_SECTION_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def validate_structure(title: str, content_md: str) -> dict:
    """Lint one article against its domain guide's spec.

    Checks for a lead paragraph, required/recommended sections, citation integrity,
    the Reference PII firewall, and frozen relative-time values. A short, section-less
    article is classified as a 'stub' and is exempt from lead/section requirements.

    Args:
        title: Wiki page title, used to determine the domain and its spec.
        content_md: Article markdown content to validate.

    Returns:
        Dict with keys: ok (bool), errors (list), warnings (list), stub (bool),
        domain (str | None). ``errors`` are blocking; ``warnings`` are advisory.
    """
    domain = domain_for_title(title)
    spec = spec_for(domain)
    body = content_md or ""

    m = _FIRST_SECTION_RE.search(body)
    head = body[:m.start()] if m else body
    lead = _H1_RE.sub("", head).strip()            # prose before the first ## (minus the H1)
    sections = [s.strip() for s in _SECTION_RE.findall(body)]
    sec_lower = {s.lower() for s in sections}
    compact = re.sub(r"\s+", " ", body).strip()
    is_stub = len(compact) < int(spec.get("stub_max_chars", 350)) and not sections

    errors: list[str] = []
    warnings: list[str] = []

    if spec.get("require_lead") and not is_stub and len(lead) < 30:
        errors.append("missing a lead paragraph")

    for s in spec.get("required_sections") or []:
        if not is_stub and s.lower() not in sec_lower:
            errors.append(f'missing required section "## {s}"')
    for s in spec.get("recommended_sections") or []:
        if s.lower() not in sec_lower:
            warnings.append(f'consider a "## {s}" section')

    has_markers = bool(_FN_MARK_RE.search(body))
    has_defs = bool(_FN_DEF_RE.search(body))
    # Validate citation integrity whenever the article carries markers OR definitions (a
    # definition that lost its link / a duplicate id is a graph hazard either way) — but never
    # on a stub, which is exempt from structure requirements.
    if not is_stub and (has_markers or has_defs):
        from .pipeline import citation_issues
        errors.extend(citation_issues(body))
    if has_markers and spec.get("require_references_when_cited") and "references" not in sec_lower:
        errors.append('has footnote markers but no "## References" section')
    # An EMPTY "## References" heading (no footnote definitions under it) — the signature of a
    # truncated draft or an orphaned scaffold. Advisory (a warning): the bounded revise loop
    # either supplies the citations or drops the heading; never a hard quarantine.
    if "references" in sec_lower and not _FN_DEF_RE.search(_references_slice(body)):
        warnings.append('"## References" section has no citations — add the footnote definitions or drop the heading')

    forbid = [p.lower() for p in (spec.get("forbid_link_prefixes") or [])]
    if forbid:
        for tgt in _links(body):
            tl = tgt.strip().lower()
            if any(tl.startswith(p) for p in forbid):
                pretty = ", ".join(spec["forbid_link_prefixes"])
                errors.append(f"PII firewall: [[{tgt}]] links {pretty}, which this domain must not reference")
                break

    rec = [p.lower() for p in (spec.get("recommend_link_prefixes") or [])]
    if rec:
        present = any(any(t.strip().lower().startswith(p) for p in rec) for t in _links(body))
        if not present:
            warnings.append(f"consider linking {', '.join(spec['recommend_link_prefixes'])}")

    frozen = _REL_TIME_RE.search(body)
    if frozen:
        warnings.append(f'"{frozen.group(0)}" looks frozen — use a live @t[...] token so it stays current')

    # Foldered domains must live in a subcategory (kb/<Domain>/<Sub>/<Name>), not flat:
    # Reference (the general-knowledge tree) and Finance (the vault: Accounts/, Investments/, …).
    if domain in ("Reference", "Finance") and len([p for p in (title or "").split("/") if p]) < 4:
        warnings.append(f"{domain} article should sit in a subcategory (kb/{domain}/<Subcategory>/<Name>), not flat")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "stub": is_stub, "domain": domain}


def seed_guides(conn) -> int:
    """Seed or update the read-only guide pages from the prompt blocks.

    Writes kb/_Style Guide and kb/<Domain>/_Guide for every domain. Idempotent —
    only writes when a guide is missing or its text has changed, so it does not churn
    a new version on every restart.

    Args:
        conn: SQLite connection (commits internally when anything is written).

    Returns:
        Number of guide pages written or updated.
    """
    from . import notes as notes_svc
    written = 0
    for domain in [None, *DOMAINS]:
        text = guide_text(domain).strip()
        if not text:
            continue
        title = guide_title(domain)
        existing = notes_svc.get_by_title(conn, title)
        if existing and (existing["content_md"] or "").strip() == text:
            continue
        notes_svc.upsert_note(conn, title, text, kind="kb", source="import",
                              version_note="guide seed", fire_events=False)
        written += 1
    if written:
        conn.commit()
    return written
