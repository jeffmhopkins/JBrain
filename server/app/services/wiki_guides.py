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
    return "actions.wiki_guide.general" if not domain else f"actions.wiki_guide.{domain.lower()}"


def guide_title(domain: str | None) -> str:
    return "kb/_Style Guide" if not domain else f"kb/{domain}/_Guide"


def guide_text(domain: str | None) -> str:
    return prompts.get(guide_key(domain), "")


def is_protected(title: str) -> bool:
    """A protected system page — any path segment starts with '_' (e.g. kb/_index,
    kb/_Style Guide, kb/People/_Guide). Never deleted by a rebuild, never fed back as a
    synthesis source, never overwritten by the article writer."""
    return any(seg.startswith("_") for seg in (title or "").split("/"))


def domain_for_title(title: str) -> str | None:
    """The taxonomy domain a kb article belongs to, from its path (kb/<Domain>/…)."""
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
    """True for a personal-health page (kb/Health/<Person>) — case-insensitive prefix match."""
    return (title or "").lower().startswith(HEALTH_PREFIX)


# The PRIVATE/sensitive domains: PII vaults that are firewalled out of the shareable wiki
# (no other domain may link them), share-hardened (any share is browser-bound + finite-TTL),
# and hidden from research recipients. "Health" is live; "Finance" is registered here so the
# firewall protects any kb/Finance/* note from the moment one exists — it is added to DOMAINS
# (with its guide) separately, so this registry can lead the taxonomy without churn.
PRIVATE_DOMAINS = ("Health", "Finance")
_PRIVATE_PREFIXES = tuple(f"kb/{d.lower()}/" for d in PRIVATE_DOMAINS)


def is_private_title(title: str) -> bool:
    """True for any sensitive-domain page (kb/Health/… or kb/Finance/…) — the single firewall
    predicate reused by the share layer, research scope, the structure lint, and the entity index."""
    t = (title or "").lower()
    return any(t.startswith(p) for p in _PRIVATE_PREFIXES)


def private_domain_for(title: str) -> str | None:
    """The private domain a title belongs to, or None. (Returns a domain only once it's also in
    DOMAINS — i.e. fully wired with a guide; before that domain_for_title won't recognise it.)"""
    d = domain_for_title(title)
    return d if d in PRIVATE_DOMAINS else None


def parse_spec(text: str) -> dict:
    """Extract and parse the fenced ```spec YAML block from a guide. {} if absent."""
    m = _SPEC_RE.search(text or "")
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def spec_for(domain: str | None) -> dict:
    """Effective spec for a domain: defaults ← general guide spec ← domain guide spec."""
    spec = dict(_DEFAULTS)
    spec.update(parse_spec(guide_text(None)))
    if domain:
        spec.update(parse_spec(guide_text(domain)))
    return spec


def _links(text: str) -> list[str]:
    from . import wikilinks
    return wikilinks.extract_links(text or "")


def _references_slice(body: str) -> str:
    """The text under the '## References' heading up to the next '## ' (or EOF). '' if the
    article has no References section."""
    m = _REFS_HEAD_RE.search(body or "")
    if not m:
        return ""
    rest = body[m.end():]
    nxt = _FIRST_SECTION_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def validate_structure(title: str, content_md: str) -> dict:
    """Lint one article against its domain guide's spec. Returns
    {ok, errors, warnings, stub, domain}. `errors` are blocking (an article with any
    is quarantined, not saved); `warnings` are advisory (the revise pass can act on
    them). A short, section-less article is a 'stub' — allowed, and exempt from the
    lead/section requirements so a thin entity page isn't rejected."""
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
    """Seed/update the read-only guide pages (kb/_Style Guide + kb/<Domain>/_Guide)
    from the prompt blocks. Idempotent — only writes when missing or changed, so it
    won't churn a new version on every restart. Returns how many were written."""
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
