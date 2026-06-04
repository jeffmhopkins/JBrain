# Grokipedia external-reference fill — feasibility + plan (PARKED)

Status: **PARKED / not implementing yet** — waiting on the KB maintenance redesign
(`docs/kb-maintenance-redesign.md`) to land first, so this slots into the scrub/per-article
model rather than the soon-to-be-demoted full rebuild. Decisions so far: **link-only MVP
(Concept 1)**, provider **Grok Live Search (Route A)** for the heavier path if/when we do
content fill. This doc captures the feasibility spike so it isn't lost.

## What it's for
Phase 1 already exists: `flag_ungrounded_reference` (`server/app/services/wiki_build.py:557-576`)
tags thin `kb/Reference/*` stubs with a talk-todo *"External reference needed (Grokipedia):
<topic>"*, and the build posts a "Reference needs grounding" card. This feature is the
**consumer of those flags** — it adds an approved external reference to the stub.

## Feasibility findings (spike, 2026-06)
- **No official Grokipedia API.** Read-only website. The "Grokipedia API 2026",
  `grokexpedia.us`, `grokxpedia.us` results are **typosquat/scam**, not xAI.
- **Content reliability is questionable** (PolitiFact/Wired: fabricated sources, conspiracy
  content, biased framing). Critical because most flagged stubs are **Medical/Reference** —
  importing prose is hazardous. (en.wikipedia.org/wiki/Grokipedia)
- **License:** CC BY-SA 4.0 (Wikipedia-derived) + "xAI Community License" (non-commercial /
  research). Fine for a single-owner private brain, but **attribution is mandatory** and
  ShareAlike bites if articles are ever shared out.
- **The server CAN reach grokipedia.com** (the WebFetch 403 was that service's IP/UA being
  blocklisted, not blanket protection). Verified by direct `urllib` from a server-like client:
  - Real page → **HTTP 200**, with `<meta og:title>` = "<Topic> — Grokipedia" + canonical URL.
  - Bogus slug → **HTTP 404** (it is NOT an SPA that 200s everything).
  - Confirmed on `Elon_Musk`, `Aspirin` (200) and a nonsense slug (404).

## Three access routes
- **A. xAI Grok API + Live Search** (`docs.x.ai`, `web_search` tool, citation-bearing).
  Official, paid-per-token, and JBrain is **already plumbed** for xAI
  (`config.xai_api_key` / `xai_base_url`). Use for *content fill* if we ever do it.
- **B. Unofficial scraper** (`github.com/jasonniebauer/grokipedia-api`, v0.3 Dec 2025):
  BeautifulSoup scrape, `GET /page/{slug}`, no search, self-warns "structure may change."
  Fragile/third-party. Not recommended.
- **C. Direct scrape of grokipedia.com.** Possible (we get 200s) but ToS/AUP exposure for
  reproducing content. Only the **link-only** use below avoids the content-reuse problem.

## Link-only MVP (the chosen first step)
No API, no Grok, no content scraping — just resolve + verify a URL and add a "Further
reading" pointer. De-risked by the spike.

- `grokipedia.find_page(topic) -> {url, title} | None`:
  - Build slug variants from the topic (`Topic_With_Underscores`, Wikipedia title-case).
  - `HEAD`/tiny `GET` each; **200 = exists, 404 = not**; accept the first hit.
  - Parse `og:title`; confirm it matches the topic (guards slug collisions).
  - Cache results in a small table (a `geocode_cache`-style `(kind,key,payload,fetched_at)`).
- Consumer of the `flag_ungrounded_reference` todos: for each flagged Reference stub with a
  resolved page, **stage** a proposal adding
  `> Further reading: [<Topic> on Grokipedia](url) — CC BY-SA 4.0` (attribution required).
  - **Owner-approved via Review card, never auto-written, off the scrub hot path.**
  - Honors §1.11's "a link or nothing" grounding firewall: adds a citation, no prose/facts.

## Content fill (Concept 2 — deferred, heavier)
If link-only proves valuable (measure owner accept-rate first, per §1.11):
- Provider = **Route A (Grok Live Search)**, not scraping — official + cited.
- Store in a fenced `## External reference (Grok/xAI, fetched <date>) — unverified` block,
  **never blended** into owner-grounded facts; owner notes always win.
- **Drop any claim without a citation.** Domain-gate: OK for general concepts; **off (or
  extra explicit confirmation) for Medical conditions/medications/dosages.**
- Owner-approved, off the hot path, cached by topic.

## Open decisions when we resume
- (a) provider for any content fill: Grok Live Search (lean) vs unofficial scraper.
- (b) confirm link attribution wording + whether to domain-gate which Reference subtrees get
  a Grokipedia link at all.
- (c) wire point: a per-article step in the redesigned scrub vs an owner-triggered batch over
  open grounding flags.

Sources: en.wikipedia.org/wiki/Grokipedia · docs.x.ai/developers/tools/web-search ·
github.com/jasonniebauer/grokipedia-api
