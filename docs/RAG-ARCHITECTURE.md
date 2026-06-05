# JBrain — Data Retrieval Architecture (in RAG terms)

A map of JBrain's data-retrieval stack onto standard RAG / AI-industry
vocabulary, for explaining the system to people fluent in that jargon.
Produced from a read-only investigation of `server/app`, `prompts.yaml`,
`workflows/`, and `actions/`.

---

## Elevator pitch (one paragraph)

> JBrain is a **self-hosted, single-node agentic RAG system** over a personal
> knowledge base. Documents (notes + attachments) live in **SQLite as the
> system of record**, with two retrieval indexes layered on top: a **lexical
> index** (FTS5 / BM25) and a **dense vector index** (`sqlite-vec`, 384-dim
> embeddings from a local `bge-small-en-v1.5` **bi-encoder**). Retrieval is
> **hybrid** — lexical and dense candidates fused with **Reciprocal Rank
> Fusion (RRF)**. Two consumption paths sit on top: an **agentic RAG** chat
> loop (tool-calling Claude that decides what to retrieve), and a scheduled
> **hierarchical synthesis pipeline** that distills raw notes into a
> continually-maintained, entity-first "wiki" knowledge layer. It runs
> entirely local — no embedding API, no external vector DB.

---

## 1. The database → "system of record + co-located indexes"

A single **SQLite** file (`/data/brain.db`, WAL mode) is *both* the
transactional store and the retrieval substrate. No separate vector DB
(Pinecone/Weaviate/Qdrant), no separate search cluster (Elasticsearch).
Everything — documents, vectors, lexical index, version history, knowledge
graph — lives in one file, indexed synchronously on write.

| Layer | Jargon | Implementation |
|---|---|---|
| Document store | "System of record" | `notes` (markdown body, slug, geo, `kind`) |
| Version history | "Append-only lineage / provenance log" | `note_versions`, attributed by `source` (`user` / `architect` / `restore`); every write reversible |
| Lexical index | "Sparse / keyword retrieval" | `notes_fts`, `attachments_fts` — FTS5 virtual tables, **BM25**-ranked |
| Vector store | "Embedded dense index" | `vec_notes`, `vec_note_chunks`, `vec_chunks`, `vec_entities` — `sqlite-vec` `vec0`, `float[384]` |
| Knowledge graph | "Entity resolution + link graph" | `links` (`[[wiki-link]]` edges + backlinks); `entities` / `entity_aliases` / `entity_mentions` (canonicalization + alias merging, e.g. "TTP" ↔ "Thrombotic Thrombocytopenic Purpura") |

**Expert framing:** co-located store — vectors live in the same SQLite file as
the documents, indexed synchronously on write. **Flat (exact) KNN, not ANN.**
Optimized for privacy and operational simplicity at personal scale; not how
you'd build for many tenants.

---

## 2. Embeddings → "local bi-encoder, dense retrieval"

- **Model:** `BAAI/bge-small-en-v1.5`, run **locally via `fastembed`** (ONNX,
  CPU). A **bi-encoder** (query and document embedded independently, compared
  by cosine) — not a cross-encoder reranker.
- **Dimensionality:** 384, **L2-normalized**, compared by **cosine
  similarity** (`bge` scale: ~0 identical, ~0.6–0.9 related, ≳1.0 unrelated).
- **Indexing strategy:** **eager / synchronous** — recomputed inline on every
  note write; **lazy backfill** at startup for pre-chunking notes; entities use
  a **content-hash cache** (`embed_hash`) to skip unchanged items.
- **Storage:** `float32` serialized into `sqlite-vec` virtual tables.

**Expert framing:** "Dense retrieval with a local bi-encoder, L2-normalized
cosine, eager indexing on write." Notable: **fully local, zero-API** —
privacy-preserving by construction.

---

## 3. Chunking → "newline-aware sliding window, dual-granularity"

Real chunking, not whole-document-only:

- **Splitter:** **sliding window**, **1500 chars / 200-char overlap**, max 200
  chunks/doc, breaking on newlines when possible. **Character-based, not
  token-based**, and **not markdown-structure-aware** (a chunk can straddle
  headings).
- **Dual-granularity embedding:** each note gets *both* a **whole-note vector**
  (`vec_notes`) *and* **per-chunk vectors** (`vec_note_chunks`). The embedder
  truncates at ~512 tokens, so a long note's tail would vanish from the
  whole-note vector; chunk vectors recover it.
- **Query-time collapse:** semantic search queries the **chunks** and
  **collapses to the best-matching chunk per note**, over-fetching k×10 so one
  long note doesn't crowd out others.

**Expert framing:** "Passage-level retrieval with a sliding-window splitter,
plus a document-level vector as fallback — collapse-to-best-chunk at query
time." Caveat: **fixed-size char windows, no semantic/markdown-aware
splitting**, so boundaries can mix unrelated sections.

---

## 4. Hybrid retrieval → "FTS + dense, fused with RRF"

- **Lexical half:** FTS5, BM25, prefix-matched and quoted to neutralize FTS
  operators.
- **Dense half:** `sqlite-vec` KNN over chunk vectors.
- **Fusion:** **Reciprocal Rank Fusion** — `score += 1/(rank+1)` per source,
  summed and re-sorted. Unsupervised, no learned weights, **no cross-encoder
  reranker**.
- **Scoped variant** (shared "research links"): swaps global KNN for an
  **in-process NumPy brute-force exact cosine** over an allow-listed subset
  (global ANN/KNN could return zero in-scope hits). A **metadata-filtered
  retrieval with a hard allowlist boundary**, keeping a **retrieval audit
  trail** (`retrieved_ids_json`) of which notes informed each answer.

---

## 5. Wiki synthesis → "agentic RAG + hierarchical, watermark-driven summarization"

Two distinct pipelines.

### Path A — Research/Assisted chat = Agentic RAG

Not "retrieve-once-then-stuff." A **tool-calling loop** (Claude via the
Anthropic SDK, streaming, bounded by max-iterations / token budget). The model
chooses among tools — `search_notes` (hybrid + RRF, top-k 8 with relevance
snippets), `read_note(s)`, `query_sql` (a **SELECT-only** tool over a
whitelisted schema), plus geo/medical tools — and assembles context on demand.
Retrieved content is wrapped in **nonce-delimited "untrusted data" fences** to
resist prompt injection; the model is told to **ground answers and cite with
`[[wiki-links]]`**. Research mode is **read-only**; Assisted mode can stage
additive edits.

### Path B — Knowledge-base synthesis = hierarchical / map-reduce summarization

Three scheduled workflows:

1. **Full rebuild** (manual): *survey → outline → write → lint → self-critique*.
   Builds an **entity-first taxonomy** (one article per canonical entity),
   assigns source notes, then writes each article under a **strict grounding
   rule** ("write only what the sources contain"), a **structure linter**, a
   **dead-link guard** (can only link to known titles), and a **bounded
   self-critique/revise loop** (≤2 non-regressing passes).
2. **Daily incremental update** (cron): **watermark-driven** — only notes
   changed since the last run flow into affected articles (found via semantic
   similarity *and* citation backlinks).
3. **Nightly maintenance** (cron): works open **"talk items"**
   (conflicts/questions/todos recorded Wikipedia-style on each article) —
   making **uncertainty explicit and reconcilable** rather than silently
   dropped.

**Expert framing:** "Path A is **agentic RAG** (tool-use retrieval loop).
Path B is **hierarchical extractive-then-abstractive summarization** into a
curated KB layer, with **incremental, watermark-gated re-synthesis** and a
**self-critique/lint** quality gate — closer to a continuously-maintained
encyclopedia than a one-shot summarizer."

---

## How it diverges from "textbook RAG"

| Textbook RAG | JBrain |
|---|---|
| Separate vector DB + ANN (HNSW/IVF) | **Co-located `sqlite-vec`, exact/flat KNN**, brute-force fallback |
| Remote embedding API | **Local bi-encoder, zero-API** |
| Retrieve-once → stuff → generate | **Agentic tool-calling loop** (model drives retrieval) |
| RAG answers a query | Also **synthesizes a durable, versioned KB layer** (map-reduce + maintenance) |
| Chunks are the whole story | **Dual-granularity** (chunk + whole-doc vectors), collapse-to-best-chunk |
| Cross-encoder reranking | **RRF only**, no learned reranker |

---

## Red-team findings (skeptical senior-engineer view)

Relayed **with a confidence caveat**: some items are *suspected*, and one
*contradicts* another investigation — verify before treating as fact.

### Highest-value, well-evidenced

- **Flat KNN doesn't scale (CRITICAL, confirmed).** Brute-force cosine is O(n)
  — fine at personal scale, but at ~100K+ vectors you hit latency/memory
  cliffs and the query timeout. *Mitigation: an ANN index (HNSW) in
  sqlite-vec.* The single most defensible critique.
- **SQLite single-writer bottleneck (HIGH, confirmed).** Nightly synthesis +
  indexing + user edits contend on one writer with a 5s busy-timeout; heavy
  synthesis can starve user writes. *Mitigation: write queue / single writer
  thread, or Postgres if multi-user.*
- **Prompt injection is the real attack surface (HIGH).** Notes / attachments /
  PDF text *are* untrusted input that later reaches an LLM. The chat path
  **does** fence content in nonce-delimited untrusted tags (good). Whether the
  **synthesis path** fences equally well is **disputed**: the synthesis-tracing
  pass reported all stored content is wrapped, while the red-team pass claims
  `wiki_build` does *not* wrap source text. **Resolve this directly** — it's the
  difference between "defended" and "a poisoned note can inject false 'durable
  knowledge' into the KB."
- **Fixed-size char chunking, no rerank (MEDIUM).** Recall/precision leave
  performance on the table vs. semantic chunking + a cross-encoder.

### Worth a look, lower confidence

- `auto_apply` on KB-mutating tools possibly bypassing staging (red team rated
  CRITICAL, but the system claims everything is versioned/reversible — verify
  whether "auto-applied" still means "versioned + undoable," which downgrades
  it).
- `query_sql` error messages leaking schema names; CORS `*` (by-design,
  bearer-not-cookie, so lower risk); stale embeddings on model swap (no
  model-version stamp in `meta`).
- SQL-injection in vector/FTS queries — investigated and largely **cleared**
  (params bound, FTS tokens quoted); a *style* risk, not a confirmed hole.

---

## Quick reference — key files

| Concern | Files |
|---|---|
| DB / schema / migrations | `server/app/db.py`, `server/app/schema.sql`, `server/app/config.py` |
| Embeddings + chunking | `server/app/services/embeddings.py`, `server/app/services/attachments.py` |
| Hybrid search (RRF) | `server/app/services/search.py`, `server/app/routers/search.py` |
| Scoped research retrieval | `server/app/services/research_scope.py` |
| Agentic chat loop | `server/app/routers/chat.py`, `server/app/services/architect.py`, `server/app/services/llm.py` |
| KB synthesis | `server/app/services/wiki_build.py`, `actions/*.yaml`, `workflows/*.yaml`, `prompts.yaml` |
| Entity index | `server/app/services/entity_index.py` |
