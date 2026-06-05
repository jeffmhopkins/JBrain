# wiki_lab — local pipeline harness for iterating on the KB

`wiki_lab.py` runs JBrain's **real** action pipeline (consolidate / build / update /
maintain) on your laptop, with the `claude` CLI standing in for the LLM — no API key
and no running server. It's the fast loop for changing a wiki prompt or recipe and
seeing what the KB actually looks like afterwards.

## Requirements
- The `claude` CLI on your PATH and logged in.
- The server deps importable: `pip install -r server/requirements.txt`
  (first run downloads the local embedding model).

## Data stays out of git
- Point `--notes` at a JBrain export (Settings → **Export original notes**) kept
  **outside the repo**.
- The throwaway DB defaults to `data/wiki_lab.db` — `data/` and `*.db` are already
  `.gitignored`. Never commit either.

A full DB export also contains JBrain's own output (the synthesized `kb/` layer and
back-dated analysis cards); `wiki_lab` ingests only genuine user captures. `days` shows
the split.

## Typical loop
```bash
export WIKI_LAB_NOTES=~/jbrain-export.json

python server/tools/wiki_lab.py days                       # per-day capture counts
python server/tools/wiki_lab.py ingest 2026-06-01          # backdated to the originals
python server/tools/wiki_lab.py analyze                    # pre-analyze CONCURRENTLY (big speedup)
python server/tools/wiki_lab.py run consolidate_daily '{"review": false}'
python server/tools/wiki_lab.py run wiki_build '{}'        # bootstrap the KB
python server/tools/wiki_lab.py dump --full                # read the articles

python server/tools/wiki_lab.py ingest 2026-06-02          # next day…
python server/tools/wiki_lab.py analyze
python server/tools/wiki_lab.py run wiki_update '{}'       # incremental maintenance
python server/tools/wiki_lab.py run wiki_maintain '{}'

python server/tools/wiki_lab.py entities                   # entity index
python server/tools/wiki_lab.py reset                      # wipe and start over
```

`run ACTION '<json>'` takes any action type with its config, so you can exercise a
single recipe in isolation while editing `actions/*.yaml` or `prompts.yaml`.

`analyze` is the main speed lever. The pipeline runs its per-note analysis serially
(~10–20s per `claude -p` call), so a cold build spends most of its time there.
`analyze` fans those independent calls out across `--workers` threads (default 6, each
with its own SQLite connection); the build/update then finds the analysis already
cached and skips it. Synthesis (outline + article writing) still runs serially, so
prose quality is unaffected.

Tune the models with `--model-default` / `--model-cheap` (or `$WIKI_LAB_MODEL[_CHEAP]`);
cheap-tier work (analysis, tagging, summaries) uses the cheap model, synthesis the default.
