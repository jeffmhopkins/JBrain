# Finance Domain — Design Plan

Add a dedicated, firewalled **`kb/Finance/`** domain for personal financial information, modeled
on the Health domain but **without a migration** (the database currently holds essentially no
financial data, so there is nothing to relocate — this is purely the forward-looking machinery).

Same security driver as Health: a person, household, business, or asset can be shared via a link
without leaking their financial details. General/de-identified financial *knowledge* stays in
`kb/Reference/Finance`.

## Locked decisions

1. **Shape: foldered vault** — `kb/Finance/<Subcategory>/<Name>`, always foldered (like Reference),
   never a flat `kb/Finance/<Name>`. Expected subtrees: `kb/Finance/Accounts/<Name>`,
   `kb/Finance/Income`, `kb/Finance/Budget`, `kb/Finance/Investments/<Broker>`,
   `kb/Finance/Taxes/<Year>`, `kb/Finance/People/<Person>` (per-person financial summary),
   `kb/Finance/Debts/<Name>`.
2. **Money → Finance.** Personal account balances, numbers, statements, loan/mortgage terms, and
   net worth live in `kb/Finance`. A physical asset stays a **Thing** (`kb/Things/Vehicles/<Car>`)
   and its *financial* record (the auto loan, the home's mortgage/value) lives in Finance, which
   links back to the Thing. General financial knowledge ("what a Roth IRA is") stays in
   `kb/Reference/Finance`.
3. **Generalize first.** Refactor the Health-specific code into a shared *private-domain*
   abstraction, then add Finance on top — so the two share one firewall, and future sensitive
   domains (Legal, Credentials, Estate…) become near-declarative.
4. **Deliverable now:** this plan doc, for review before any code.

## What we are NOT building (vs the Health project)

- **No migration.** No `extract_*` deterministic section-cut, no one-time workflow. (If financial
  prose later accumulates in the wrong place, a generic "extract private domain" tool can be added,
  but it's out of scope now.)
- **No structured-data layer / charts** (Health had `lab_results` + lab charts). A future
  net-worth/balance trend chart would be the natural parallel, but not now.
- **No dedicated capture mode** (Health had "Medical mode" → `notes/medical/`). v1 relies on the
  outline/writer routing; a "Finance mode" + `notes/finance/` folder can come later.

---

## Part 1 — The generalization refactor (do this first, no behavior change)

Today the Health firewall is hardcoded at each site. Replace those literals with one registry so
adding Finance is declarative and the load-bearing **leaf-collision fix can never drift**.

### 1a. `server/app/services/wiki_guides.py` — the registry
- `DOMAINS` gains `"Finance"` (placed after `Health`, grouping the two private domains).
- Add:
  ```python
  PRIVATE_DOMAINS = ("Health", "Finance")            # sensitive PII domains, firewalled + share-hardened
  _PRIVATE_PREFIXES = tuple(f"kb/{d.lower()}/" for d in PRIVATE_DOMAINS)

  def is_private_title(title: str) -> bool:
      t = (title or "").lower()
      return any(t.startswith(p) for p in _PRIVATE_PREFIXES)

  def private_domain_for(title: str) -> str | None:
      d = domain_for_title(title)
      return d if d in PRIVATE_DOMAINS else None
  ```
- Keep `is_health_title()` as a thin alias (or delete it once call sites move) — `HEALTH_PREFIX`
  stays for the health migration code that already shipped.

### 1b. `share.py` — generic PHI/PII hardening
- `_phi_harden(conn, note_id, …)` swaps `wiki_guides.is_health_title` → `is_private_title`.
- Rename `assert_health_share_policy` → `assert_private_share_policy` (same logic; the clamp is
  domain-agnostic). Update the call in `main.py`.
- Net: a share of **any** `kb/Health/*` or `kb/Finance/*` note is force bound + finite-TTL,
  through the same single chokepoint (`create_link`) every caller — the `/api/shares` route and
  the architect `create_share_link` tool — already passes through.

### 1c. `research_scope.py` — exclude all private prefixes
- `filter_match_ids`: replace the literal `AND lower(n.title) NOT LIKE 'kb/health/%'` with a clause
  generated from `_PRIVATE_PREFIXES` (one `NOT LIKE` per private prefix).
- `scoped_search`: the per-row drop checks `is_private_title(...)` instead of the `kb/health/` literal.

### 1d. `entity_index.py` + `wiki_build.py` — generalize the leaf-collision exclusions
`kb/Finance/People/<Person>` collides with `kb/People/<Person>` on the leaf exactly as Health did,
so all three sites must use the set, not the Health literal:
- `entity_index._link_articles` leaf-map build → skip `is_private_title(title)`.
- `wiki_build.create_article` dedup loop → skip `is_private_title`.
- `wiki_build.check_needed_links` leafmap → skip `is_private_title`.

### 1e. Tests
Re-point the existing Health assertions at the generic predicate and add Finance equivalents
(share hardening, research exclusion, collision) so both domains are covered by the same suite.

> After Part 1 alone, behavior is unchanged (Finance has no pages yet) — it's a pure refactor with
> the registry holding only what Health already enforced, plus `"Finance"` registered.

---

## Part 2 — The Finance domain

### 2a. Guide — `actions.wiki_guide.finance` (+ seeded `kb/Finance/_Guide`)
Prose: Finance is the firewalled home for personal/household/business financial detail — account
balances & numbers, statements, income, budgets, investments & holdings, debts/loans/mortgages,
taxes, net worth. Always foldered. It **links out** to the people, groups, things, and places its
money concerns (`[[kb/People/…]]`, `[[kb/Groups/…]]`, `[[kb/Things/…]]`) and to general
`[[kb/Reference/Finance/…]]` background — but nothing outside Finance links back in. Spec:
```spec
require_lead: true
recommend_link_prefixes: [kb/People, kb/Groups, kb/Things, kb/Reference/Finance]
```
(No `forbid_link_prefixes` — Finance must be free to link its subjects.) Add a "must be foldered"
lint warning like Reference's (generalize the existing `domain == "Reference"` foldering check to
also cover `"Finance"`).

### 2b. Firewall — broader than Health
`kb/Finance` is added to the `forbid_link_prefixes` of every shareable domain it touches:
- **People** spec: `[kb/Health, kb/Finance]`
- **Groups** spec: `[kb/Finance]`
- **Things** spec: `[kb/Finance]`
- **Reference** spec: `[kb/People, kb/Groups, kb/Health, kb/Finance]`

So a People/Groups/Things/Reference article that links a Finance page is **quarantined by the
lint**. Discovery for the owner is via backlinks/graph (the Finance page links the subject; the
subject never links Finance), exactly like Health → People. (Same firewall direction: the private
domain links out; public domains never link in.)

### 2c. Go-forward writing
- **`wiki_outline`**: add a `Finance` line to the domain enumeration and a routing rule —
  *a person's/household's/business's personal financial details (account balances & numbers,
  income, budgets, investments, debts, taxes, net worth) go in `kb/Finance/<Subcategory>/<Name>`,
  always foldered, NEVER in their People/Groups/Things article and NEVER in Reference. General
  financial knowledge → `kb/Reference/Finance`.* Add a `kb/Finance/...` example to the JSON sample.
- **People guide**: add a non-destructive line — financial details live in `kb/Finance`, don't put
  them here, don't link `kb/Finance`.
- **Things guide**: the **accounts shift** — an asset's *financial* record (balance, account
  number, statement, loan/mortgage terms, value) lives in `kb/Finance` (e.g.
  `kb/Finance/Accounts/<Bank>`), not here; a Thing names the asset and is linked *from* its Finance
  record. (Keep `kb/Things/Accounts` only for a non-financial "this account exists" stub if useful,
  else route accounts to Finance outright.)
- **Groups guide**: a group's financials (a business's revenue, a household budget) → `kb/Finance`.
- **Reference guide**: the existing `kb/Reference/Finance/…` subcategory note stays; add the
  general-vs-personal split sentence mirroring the medical one.

### 2d. Entity routing nuance (accounts)
Finance is **foldered/topic-based**, not 1:1-per-person, so there is **no `_route_medical_to_health`
analog** to build. Financial routing is driven by the outline/writer + the firewall. One open
detail: the entity type system (`_TYPE_DOMAIN` in `wiki_build.py`) has no "account" type — accounts
surface as `thing`. v1 routes financial content via the **prompts + foldering** (the writer files
account/balance content under `kb/Finance/Accounts/<Name>`); a dedicated `account` entity type that
routes to `kb/Finance/Accounts` is a possible v2 refinement, not required now.

### 2e. PWA
Generalize the `NotePage.tsx` share-dialog hardening: compute `isPrivate` from
`kb/health/` **or** `kb/finance/` (mirror the existing `isHealth` block — bind-on default, finite
TTL, view-only, a "private record" notice). The research scope picker already hides private pages
because the server (1c) doesn't surface them.

---

## Tests
Mirror the Health suite for Finance: `domain_for_title("kb/Finance/Accounts/Chase") == "Finance"`;
`is_private_title` true for both domains; a People/Things/Groups/Reference article linking
`[[kb/Finance/…]]` is quarantined; a Finance page linking its subjects + Reference/Finance passes;
`create_link` force-hardens a `kb/Finance/*` share; `research_scope` excludes `kb/Finance`; the
entity for "Chase" (if both `kb/Things/...Chase` and `kb/Finance/Accounts/Chase` exist) binds
correctly and the two don't fold; foldering warning fires on a flat `kb/Finance/<Name>`.

## Rollout / risk
- Ship Part 1 (refactor) + Part 2 as one PR; with zero Finance pages it's inert until the writer
  starts producing them (next rebuild / new notes). No migration, no data-loss window.
- **Riskiest item:** the leaf-collision generalization (1d) — `kb/Finance/People/<Person>` collides
  with `kb/People/<Person>`; if the exclusion isn't set-driven it silently mis-binds the entity.
  Covered by tests.
- The broader firewall (People/Groups/Things/Reference forbid `kb/Finance`) won't quarantine any
  existing article, since none link `kb/Finance` today.

## Effort
Roughly an afternoon: the refactor is mechanical (swap a literal for a predicate at ~6 sites), the
domain is one guide + four forbid-list edits + outline/prose updates, and there's no migration to
build or operate.
