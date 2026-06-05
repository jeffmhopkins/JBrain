# Robust Lab Intake — Hardened Design (gauntlet synthesis)

Output of a multi-author + adversarial gauntlet on the question: *how do we make lab
intake robust across every input modality (clean PDF → scanned PDF → phone photo →
portal screenshot), without ever storing a number the source doesn't faithfully assert?*

Two architects (deterministic-core vs. vision-first) and an adversarial red-teamer were
run independently; their findings were then verified against the actual code. This is the
reconciled result.

---

## 0. The headline finding

The biggest risks are **not** in the (unbuilt) vision path we were debating — they are
**live in today's code**, independent of screenshots. The verbatim faithfulness filter
only proves *a string exists somewhere in the document*. It does nothing against
**misattribution** (right number, wrong analyte/date), **source-read errors**,
**coincidental substring matches**, or **adversarial/hidden text**. And the one per-row
trust signal the human reviewer could lean on — `confidence` — is hard-coded to `1.0`,
so the designed last line of defense (human approval) operates blind.

So robustness is *first* a hardening problem on the existing pipeline, and *second* a
coverage problem (photos/screenshots). We do them in that order.

---

## 1. The faithfulness contract, restated

Today's contract (one line): **a value is stored only if its exact text appears in the
document.** Necessary, not sufficient. The hardened contract is a **per-row trust score**
built from independent signals, surfaced to the reviewer, and used to decide auto-stage
vs. flag-for-attention vs. reject:

| Signal | Catches | Source |
|---|---|---|
| **Verbatim, word-boundary** match against the *specific extracted word* (not raw substring) | coincidental substrings ("12" in "120"), partial OCR matches | tighten lab_ingest.py:63 |
| **Range plausibility** — value within (or near) the analyte's own parsed reference range, by order of magnitude | 100×/1000× decimal & unit errors, gross misattribution | new, per-row |
| **Bind margin** — how much closer the chosen analyte-name / date-column was than the runner-up | geometry mis-bind (wrong analyte / adjacent date column) | from `_parse_page` geometry |
| **Column reconciliation** — header date-count vs. per-row value-count | dropped/extra cells, cropped columns, orphan pages | new, per-page |
| **OCR cross-read** (image inputs only) — value must appear in an *independent* OCR token set | model fabrication | new, vision path |
| **Cross-run agreement** (image inputs only) — value+date stable across two extractions | non-determinism, one-off model errors | new, vision path |

Per-row `confidence ∈ {high, medium, low}` is computed from these and **replaces the
hard-coded `1.0`**. The review UI pins low/medium rows to the top with the reason.

This is the core intellectual output: faithfulness goes from a boolean "is it in the
text" to a *defense-in-depth score the human can act on*.

---

## 2. Architecture (where the two authors converged)

A layered pipeline; every input ends in the **same** staging payload and the **same**
human-approve gate that exists today (`lab_ingest.stage_attachment` →
`approve_attachment`). Nothing downstream of staging changes.

```
            ┌─ clean-text PDF ─────────────► lab_parse (deterministic geometry)  ◄─ trusted, tested
 input ─►  detect/route                                  │
            ├─ scanned/image PDF ─┐                       ▼
            ├─ phone photo ───────┼─► render→image ─► OCR (local) ─► vision-extract ─► verify
            └─ portal screenshot ─┘                       │                    │
                                                          └──── OCR cross-read ─┘
                                                                       │
                              all paths ─► per-row trust score ─► STAGE ─► human approve ─► lab_results
```

- **Deterministic parser stays the trusted core** for the formats it fits (it is more
  transposition-proof than a model on its home turf, and it is unit-testable). It is
  *also* kept as a **verifier**: when its `confidence==1.0` for a PDF, diff its rows
  against any vision output and require agreement.
- **Vision/OCR is purely additive coverage**, plugging in exactly where
  `parse_lab_pdf` returns `doc_type=='unknown'` or the input is an image. It never writes
  to `lab_results`; only `approve_attachment` does.
- **OCR is the faithfulness substrate for images.** Tesseract (local, no PHI egress) is
  run *independently* of the vision extractor; a value reaches `lab_json` only if it is in
  the OCR token set — the same contract as the text path, with two non-colluding readers.
- **PHI: local-first.** Reuse `image_analysis._prepare_image` + the daemon-thread
  pattern. Local quantized VLM via the existing OpenAI-shaped `llm.py` adapter
  (`base_url` is config, not code). Frontier cloud only behind an explicit per-upload
  toggle, never default, reusing the `has_credentials()` + opt-in gate.

---

## 3. Hard rules (non-negotiable invariants)

1. **The LLM never writes `lab_results`.** It proposes; OCR gates; the human approves.
2. **Vision prose is non-authoritative.** `image_analysis` markdown must never answer a
   numeric lab question. `lab_stat`/`lab_value_at` already read only structured
   `lab_results` (safe); the rule is: no numeric lab answer may come from FTS/KB prose.
3. **Nothing is silent.** Image/scanned inputs the system can't read get a *visible*
   status (`image_unparsed`), never a NULL no-op. Dropped values get an *itemized,
   reasoned* skip log, not just an integer count.
4. **No silent magnitude jumps.** `_backfill` must not stamp a unit onto a row whose
   value is orders-of-magnitude off the analyte's median.
5. **Dates are validated.** `month≤12`, `day≤31`, not in the future, not absurdly old —
   or the row is flagged loudly, never stored as a malformed string.

---

## 4. Phased plan

### Phase 0 — Harden what exists (no new modalities; highest ROI)
Confirmed live bugs, each with a regression test:
- **F1** Word-boundary faithfulness match against the extracted *word*, not raw substring (lab_ingest.py:63).
- **F2** Uniform `(date,value)` dedup across `list_analytes` and `abnormal_analytes` (lab_series.py:78, 143) — or drop `sha256` from `identity_key` so re-exports dedup at write time. Kills re-upload double-counting.
- **F3** Per-row `confidence` from geometry bind-margins + range-plausibility; replace the hard-coded `1.0`; surface in the staging payload + review UI.
- **F4** Date validation in `_COLLECTED_RE` / `parse_date` (month/day bounds, future/stale flags); detect DD/MM by any first-field token >12.
- **F5** `_backfill` guard: never stamp a modal unit onto a >~50× magnitude-outlier row; flag intra-analyte order-of-magnitude jumps.
- **F6** Observability: itemized skip/drop log (analyte, date, raw token, reason) spanning *parser-level* drops too (locale commas, non-digit OCR, orphan pages), shown at review. Orphan-page warning when a page has value tokens but no anchors.
- **F7** Dateless approved rows: warn at staging; never store a value that will be invisible to every read path.

### Phase 1 — Image/screenshot coverage (the requested capability)
- **C1** New `lab_vision.py`: render PDF pages to images (pdfium) + accept image MIMEs; reuse `image_analysis._prepare_image`. Route here when `doc_type=='unknown'` or input is an image.
- **C2** Local OCR pass (Tesseract) → token set with boxes + per-token confidence = the faithfulness corpus for images.
- **C3** Vision extractor → strict JSON row schema **identical** to the `lab_parse` dict shape, via the existing tool-use/JSON-schema plumbing; per-cell bbox + confidence.
- **C4** Verification layer: OCR cross-read + bbox co-location (anti-transposition) + cross-run agreement → per-row trust score → same staging payload.
- **C5** Review UX: show the OCR-cropped source thumbnail beside each vision value (eyeball digit-vs-pixel); low-confidence rows pinned + flagged.

### Phase 2 — Provenance & identity
- **P1 [SHIPPED]** Parse + display the document's patient name/DOB at approval; a configured
  owner DOB flags a mismatch (red) / unverified (amber) before merge. DOBs are normalized to
  ISO on both sides so format drift never fabricates a mismatch. Owner is set in Medical
  settings (Advanced → Medical).
- **P2 [SHIPPED]** Faithfulness matched against the *visible* words the parser laid out
  (extract_words), not extract_text → defeats hidden / white-on-white injected text.
- **P3 [SHIPPED]** Row provenance: a `lab_results.source` column (migration v41) records how a
  row was extracted (`lab_trend_export | lab_report | lab_image | NULL`=legacy auto-applied),
  written on approve and surfaced on the chart point detail ("from a photo — verify" for
  vision-derived points).
- **P4 [SHIPPED]** Collection *time*: a `lab_results.collected_time` column (migration v41,
  HH:MM) captured from reports that show it (e.g. Quest "Collected Date: 05/12/2026 11:48"),
  used to order genuine twice-daily draws (`ORDER BY collected_at, collected_time, id`). Kept
  separate from the date-only `collected_at` the chart x-axis and dedup rely on; date-only
  sources (trend exports) leave it NULL.

## Implementation status & build notes

Phases 0–2 are SHIPPED and tested (unit + integration: 302 passing).

**Optional vision dependencies.** The image/screenshot/scanned-PDF path
(`lab_vision.py`) needs `pytesseract` + `pypdfium2` and the system `tesseract-ocr`
binary — see `server/requirements-vision.txt`. They are intentionally NOT in the base
image: the deterministic text-PDF path needs none of them, and without them image inputs
degrade to a *visible* `image_unparsed` status (never a silent no-op). Tesseract is the
independent OCR reader that *gates* the vision model, so it is what makes that path
trustworthy — not an optional nicety.

## Gauntlet round 2 — review-panel findings (all addressed)

A second panel (security/faithfulness, correctness/regression, integration/quality) audited
the implementation. It confirmed the core guarantees hold (word-boundary faithfulness,
visible-text corpus, OCR-refusal-when-absent, uniform re-export dedup, ReDoS-clear) and
surfaced fixes that were folded back in: ISO-normalized DOB compare (no false "different
patient"), `datetime`-validated dates (reject impossible day-for-month, not just day≤31),
scanned-PDF-without-renderer → visible `image_unparsed` (not silent), flag-aware abnormal
dedup (a duplicate can't suppress a real abnormal), finite-only ref values (don't break the
browser parser), three-state identity (verified / mismatch / unverified), per-row confidence
*reasons* surfaced in review, the owner-identity settings UI, and the optional-deps file +
docs above.

---

## Appendix — failure catalog (condensed)

Severity-ordered, verified against code where checkable. **[L]** = confirmed live today.

- **Misattribution** — value bound to wrong analyte (nearest-name near-tie) or wrong date column (60px band overlap); passes verbatim filter. **[L]** High. → bind-margin confidence + range plausibility.
- **Source-read error** — OCR misreads 1.7→7.7; verbatim filter confirms it (same bad source). High (image path). → cross-engine OCR agreement + range plausibility.
- **Re-export double count** — `list_analytes`/`abnormal_analytes` count un-deduped rows. **[L]** High. → F2.
- **Decimal/unit 100×–1000×** — 0.84→84, thou/cumm vs /uL, backfill stamps consistent-looking wrong unit. **[L]** High. → F5 + range plausibility.
- **Coincidental substring** — short value matches inside a longer number/date. **[L]** Med-High. → F1.
- **Blind review** — `confidence` always 1.0; reviewer can't tell ambiguous binds from clean ones. **[L]** High. → F3.
- **DD/MM date** — silent mis-date or invalid ISO string excluded by `date()`. **[L]** High. → F4.
- **Silent loss** — locale commas, non-digit OCR, orphan continuation pages, dateless rows: dropped/invisible, not in `skipped` count. **[L]** High. → F6/F7.
- **Modality gap** — photos/screenshots/scanned PDFs yield nothing structured today; user reads silence as "no labs found." **[L]** High. → Phase 1.
- **Vision prose as truth** — `image_analysis` markdown in FTS could answer a numeric lab question with no faithfulness gate. Med-High. → hard rule #2.
- **Prompt injection / hidden text** — crafted document steers a downstream LLM or satisfies the filter with injected text. Med. → P2 + non-authoritative vision.
- **Wrong patient** — no identity check on `lab_results`. **[L]** High. → P1.
