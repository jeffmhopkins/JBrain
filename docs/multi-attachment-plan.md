# Multiple attachments per note — full review & gauntlet synthesis

Output of a full-stack review of the question: *what does it actually take to let a note
carry many attachments — and to give JBrain a real system for **what it does** with each
kind (text, PDF, image, audio, video, …) — without regressing labs, image analysis,
search, sharing, or the carefully-tuned optimistic compose box?*

Three explorations were run independently (labs path, image-analysis + AI-read path,
capture/compose + share + native path) and reconciled against the actual code. This is the
result.

---

## 0. The headline finding

**The data layer is already multi-attachment. The gap is one input box and one missing
abstraction.**

Almost everything people assume needs building for "multiple attachments per note" is
already there and already loops correctly:

- `attachments.note_id` is a nullable FK with a plain `idx_attachments_note` index — **N:1,
  no UNIQUE on `(note_id, …)`**. Many rows per note are legal today (`schema.sql:181`).
- The note-page **`Attachments.tsx` already has `multiple` on its file input** and uploads
  in a `for` loop with an `(i/N)` counter (`web/src/components/Attachments.tsx:152,100`).
- **Lab ingestion already iterates every attachment** on a note —
  `stage_note()` does `SELECT id FROM attachments WHERE note_id = ?` then loops
  `stage_attachment(a["id"])` (`lab_ingest.py`), and `lab_status / lab_json /
  lab_extracted_at` are **per-attachment columns** (`schema.sql:195`). `lab_results`
  rows carry both `note_id` **and** `attachment_id`, deduped by an `identity_key` that
  folds in the attachment's `sha256`.
- **Image analysis is per-`att_id`**, atomic per row, with summary blocks fenced by
  `<!-- jbrain:image-summary att={id} -->` and an integer-boundary stripper, so N images
  on one note each analyze, render, re-analyze and delete independently
  (`image_analysis.py`). `block_for_note()` already concatenates *all* of a note's image
  summaries into one bounded block for the AI.
- **Search/embeddings are per-attachment**: each attachment is chunked into
  `attachment_chunks`, embedded into `vec_chunks`, and `semantic_search_attachments`
  collapses to the best chunk **per attachment** (`embeddings.py`). The AI reads via
  `search_attachments(query)` → `read_attachment(att_id)` — never "the attachment for this
  note" (`architect.py`).
- **Share payloads already emit an array**: `_note_payload` returns
  `attachments: [{id, filename, mime, byte_size}, …]` (`routers/share.py`). Lab-share and
  research-share intentionally expose **no** attachments (scoped to analyte rows / approved
  note bodies) — that boundary is correct and unchanged.
- The Android app has **no attach/share-intent UI at all** — capture is PWA-only — so there
  is nothing native to change.

So this is **not** a schema or pipeline project. It is:

1. a **small, delicate front-end change** to the optimistic compose box (`Chat.tsx`), and
2. an optional but valuable **handler-registry refactor** that turns today's scattered
   `if mime.startswith(...)` logic into one typed place to answer *"what do we DO with this
   kind of file?"* — the seam where audio/video/office handlers (transcription, etc.) plug
   in later.

---

## 1. Current state — single vs. already-plural

| Surface | Single today? | Reality |
|---|---|---|
| `attachments` table | **plural** | N:1, no UNIQUE; per-attachment lab/analysis columns |
| Note-page upload (`Attachments.tsx`) | **plural** | `multiple` input + sequential loop + `(i/N)` |
| Upload endpoint (`routers/attachments.py`) | per-request | one file/request, no per-note cap; call it N times |
| Lab extraction (`lab_ingest.stage_note`) | **plural** | loops every attachment; per-attachment status |
| Image analysis (`image_analysis`) | **plural** | per-`att_id`, fenced summary blocks |
| Search / embeddings | **plural** | per-attachment chunks; best-per-attachment collapse |
| AI read (`architect`/`research`) | **plural** | `search_attachments`→`read_attachment(id)` |
| Share read payload | **plural** | `attachments: [...]` array |
| **Compose box (`Chat.tsx`)** | **SINGLE** ❌ | `pendingFile: File \| null` — the only real gap |
| Handler dispatch (`extract_text`) | flat if/elif | works, but no seam for audio/video/office |

The entire user-visible limitation reduces to: **you can drag many files onto an existing
note, but you can only attach one file while capturing a new one.**

---

## 2. The real gap — the compose box (`web/src/pages/Chat.tsx`)

This is a deliberately delicate file: optimistic-first send, a synchronous re-entrancy
latch (`sendingRef`), id-keyed bubble reconciliation, GPS-stamp racing. The change must
preserve all of that. Concretely, single→plural:

| Item | Line(s) | Now | Change |
|---|---|---|---|
| state | 132 | `pendingFile: File \| null` | `pendingFiles: File[]` |
| send-gate | 379 | `!pendingFile` | `pendingFiles.length === 0` |
| optimistic bubble | 389 | `📎 ${file.name}` | `📎 name` (1) / `📎 N files` (N) |
| entry/medical upload | 406–415 | one `uploadAttachment` | loop all; lab-extract once if **any** PDF |
| assisted carrier | 453–463 | one carrier note/file | **one carrier note, N attachments** (§5.7) |
| rollback | 367–371 | `file: File \| null` | `files: File[]` |
| attach chip | 623–627 | one chip | map chips, remove-by-index |
| file input | 670–675 | no `multiple` | add `multiple`; `Array.from(files)`; size-check each |
| send-disabled | 680 | `!pendingFile` | `pendingFiles.length === 0` |

Notes that make this safe:

- **Lab extraction stays note-level.** `extractLabs(slug)` already stages *all* PDFs on the
  note server-side, so the medical path calls it **once** after the upload loop, gated on
  "any pending file is a PDF" — not once per file.
- **Per-file progress.** Reuse the existing `uploadPct` chip but prefix an `(i/N)` label
  (mirrors `Attachments.tsx`) so a 3-file capture shows progress, not a frozen bar.
- **Partial failure.** Upload files in sequence; if file *k* fails, keep the *k−1* that
  landed, surface "couldn't attach X", and **don't** drop the saved note. The note is the
  durable artifact; attachments are additive (matches `Attachments.tsx` semantics).
- **Backend, API, schema: unchanged.** `uploadAttachment(slug, file, …)` is already
  one-file-per-call and is simply invoked N times.

---

## 3. The attachment-handler system — *"what do we DO with each kind?"*

Today the answer to "what do we do with a file?" is split across three places that each
re-sniff the mime/extension:

- `attachments.extract_text()` — flat `if text / elif pdf / elif image` for **search text**.
- `image_analysis` — separate vision path, triggered in the router by
  `mime.startswith("image/")`.
- `lab_ingest` — separately decides PDF-vs-image rendering for the lab path.

That works for three kinds. It does **not** give us a place to answer the question the user
actually asked — *audio, video, office docs* — without sprinkling more `if mime ==` across
routers. The proposal is a single **handler registry** keyed by a normalized **kind**.

### 3.1 Kind classification (one function, one source of truth)

`classify(mime, filename) -> Kind` where `Kind ∈ {text, pdf, image, audio, video,
office, archive, binary}`. Everything else dispatches off `Kind`, never re-sniffs mime.

### 3.2 A handler declares its capabilities

```python
@dataclass
class Handler:
    kind: str
    # search text for FTS + embeddings (sync, cheap, best-effort)
    extract_text: Callable[[bytes, str, str], str]
    # optional rich, possibly-async/LLM enrichment (vision summary, transcript, …)
    enrich: Callable[[Conn, int], EnrichResult] | None = None
    enrich_label: str = ""          # UI verb: "Analyze", "Transcribe", "Summarize"
    auto_enrich: bool = False       # run on upload without asking?
    preview: str = "download"       # inline | image | text | audio | video | download
```

| Kind | `extract_text` (search corpus) | `enrich` (rich, opt-in/auto) | Preview |
|---|---|---|---|
| text/code | decode utf-8 (today) | — | text |
| pdf | pypdf text; OCR-render fallback | (labs handled by lab path) | download/text |
| image | EXIF/metadata (today) | **vision summary** (today, auto) | image |
| **audio** | filename/container tags | **transcription** (whisper/LLM) → text | audio player |
| **video** | container metadata | **transcription** + keyframe captions | video player |
| office | unzip→text (docx/xlsx/pptx) | optional LLM summary | download |
| archive | manifest (file list) only | — (do **not** auto-expand) | download |
| binary | "" | — | download |

The critical insight: **`extract_text` and `enrich` are the same two hooks image analysis
already implements** — metadata→FTS now, vision summary→`analysis_md` sidecar. Audio/video
are *exactly the same shape*: cheap metadata into FTS immediately, an async transcript
written to a sidecar column and indexed when it lands. We are generalizing a pattern that
already works, not inventing one.

### 3.3 Storage for enrichment results

Image analysis already owns `analysis_status / analysis_detail / analyzed_at /
analysis_md`. Rather than add `transcript_status / transcript_md / …` per modality,
**generalize to one enrichment sidecar** the registry writes to (kind decides what
"analysis" means):

- Reuse `analysis_*` columns as the generic enrichment slot **or** add a neutral
  `enrich_kind` discriminator. The transcript text *also* flows into `content_text` +
  `attachment_chunks` so it's searchable and AI-readable through the **existing**
  `search_attachments` path — zero new read plumbing.

This keeps the gauntlet's promise: a transcript is just searchable attachment text plus a
human-readable sidecar, identical to how an image summary already behaves.

### 3.4 What stays out of scope (deliberately)

- **No auto-transcription by default** for audio/video — it is LLM/compute cost on possibly
  large media. `auto_enrich=False`; expose a per-attachment **"Transcribe"** button
  (mirrors today's **"Analyze with AI"**). Auto-on can be a later setting.
- **No archive auto-expansion** — list the manifest, never recursively ingest (zip-bomb /
  surprise-fan-out safety).
- **Provider choice for audio/video transcription is an open decision** (§7) — the registry
  seam means it can land later without touching routers, search, or the AI.

---

## 4. Labs & image analysis — verification under multiplicity

Both were specifically re-audited because the user called them out.

**Labs.** Already per-attachment end-to-end. Extract loops every attachment; approval writes
`lab_results` keyed by `attachment_id` and **deletes only that attachment's prior rows**
before re-inserting; revoke/re-analyze are attachment-scoped; the `identity_key`
(includes the attachment `sha256`) dedupes across re-uploads. Two lab PDFs + one portal
screenshot on one note already stage, preview, and approve **independently**. The
`LabImportPanel` already renders **one panel per attachment**. *No change required; this is
a tested invariant we must not break — the compose change must keep calling `extractLabs`
at the note level so the loop still sees every PDF.*

**Image analysis.** Already per-`att_id`, atomic per row, fenced summary blocks with an
integer-boundary stripper (`att=1` never matches inside `att=10`), background worker, and
`for_note()`/`block_for_note()` that fold **all** of a note's image summaries into the AI
context. Uploading 5 images in one capture will fan out into 5 independent background
analyses — already supported. The only watch-item is **fan-out cost/concurrency** (§5.5).

---

## 5. The gauntlet (adversarial pass)

1. **Dedup within one capture.** `add_attachment` dedupes by `(note_id, sha256)` and returns
   `duplicate: true`. Attaching the same file twice in one batch is a no-op on the second —
   correct, but the UI should *say* "already attached" rather than imply a second copy.
2. **Partial-batch failure.** File 3 of 5 exceeds 10 MB or 500s. Keep 1–2, skip 3, continue
   4–5; never roll back the note. Surface a per-file error list. (Matches `Attachments.tsx`.)
3. **Ordering.** `list_for_note` is `ORDER BY created_at DESC`. Sequential uploads in one
   second can tie on `created_at` (second granularity) → unstable order. Low-stakes, but
   tie-break by `id` if order ever matters for display.
4. **Lab multi-PDF.** Covered in §4 — already independent; just keep extraction note-level.
5. **Image fan-out cost.** N images = N vision calls on upload. Bound it: the existing
   per-row `BEGIN IMMEDIATE` already serializes DB writes; cap *auto*-analysis fan-out
   (e.g. only auto-analyze the first K images in a batch, offer "Analyze" on the rest) to
   avoid a 20-image dump spending 20 vision calls unprompted. Decision flag in §7.
6. **Carrier-note semantics (assisted).** N notes for N files spams the chat with N
   wikilinks and N near-empty notes. **One carrier note holding all N attachments** is the
   right default (one `[[wikilink]]`, one place to find the batch) — §5.7.
7. **Carrier-note title.** With one file, today's "filename" title is good. With many, use
   "Attached N files (2026-06-05)" or the first filename + "(+N more)"; keep it a real,
   findable title.
8. **Size/quota.** Per-file 10 MB cap is enforced client- and server-side. Multi-upload
   makes it *easy* to push tens of MB of blobs into SQLite per note. Not a blocker (backups
   already carry blobs), but worth a soft per-note total warning later.
9. **FTS/embedding load.** Each attachment embeds independently (already true). A 10-file
   batch = 10 embedding passes; fine, they're local. No new index pressure.
10. **Share exposure.** Share already emits the full array, and download is per-id with the
    hardened headers (`Content-Disposition: attachment`, `nosniff`, neutralized script
    mimes). More attachments = more rows in the same safe path. Lab/research share still
    expose none. No change.
11. **Re-entrancy / optimistic UI.** The compose change must not weaken `sendingRef`, the
    id-keyed reconciliation, or the GPS race. Plural state is a drop-in for the single
    `File`; the loops live *inside* the existing try/catch arms.
12. **Demo mode.** `uploadAttachment` is demo-guarded; the loop inherits that — N demo
    uploads resolve to N fake ids. Fine.

---

## 6. Implementation plan (phased, each shippable alone)

- **Phase 1 — compose multi-upload (the actual feature).** `Chat.tsx` single→plural per
  §2. One carrier note for assisted mode. Note-level lab extraction preserved. **No backend
  change.** This closes the only real gap and is the heart of the branch.
- **Phase 2 — handler registry refactor.** Introduce `classify()` + `Handler` registry;
  port today's text/pdf/image logic onto it behind the same behavior (pure refactor, fully
  covered by existing tests). Generalize the `analysis_*` sidecar into a kind-agnostic
  enrichment slot. No user-visible change; this is the seam.
- **Phase 3 — office + richer previews.** docx/xlsx/pptx text extraction; audio/video inline
  players in the viewer (no transcription yet). Pure additive handlers.
- **Phase 4 — audio/video transcription.** Plug a transcription `enrich` into the registry
  behind a **"Transcribe"** button (opt-in), writing transcript → `content_text` + chunks +
  sidecar. Provider decision (§7) resolved here. The rest of the stack already reads it.

Phases 2–4 are independent of Phase 1 and of each other; Phase 1 is the user's headline ask.

---

## 7. Open decisions

1. **Assisted-mode carrier (Phase 1):** one note with N attachments *(recommended)* vs. N
   notes. Default chosen: **one note**.
2. **Image auto-analysis fan-out cap (Phase 1):** auto-analyze all images in a batch, or
   only the first K and offer "Analyze" on the rest? Default proposed: **auto all** (matches
   today), revisit if cost bites.
3. **Audio/video transcription provider (Phase 4):** local `faster-whisper` (no API key,
   heavier image, matches the "no extra key" ethos of local embeddings) vs. an LLM/cloud
   transcription call (lighter image, needs egress + cost). This is the one genuinely new
   dependency and the only blocker for Phase 4.
4. **Enrichment storage (Phase 2):** reuse `analysis_*` columns as a generic slot vs. add an
   `enrich_kind` discriminator. Default proposed: **add `enrich_kind`**, keep `analysis_md`
   as the rendered text, so images and transcripts coexist cleanly.
