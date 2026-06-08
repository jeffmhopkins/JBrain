# docs/archive — historical design & planning

These documents shaped features that have since **shipped**. They are kept for
historical context on the design intent and the adversarial review that produced
it. They are **not** maintained: numbers, file paths, API shapes, and
cross-references are frozen at the time of writing and may no longer match the
code. For current behaviour, see the top-level [`README.md`](../../README.md).

## What's here

| Doc | Feature it designed (now shipped) |
|-----|-----------------------------------|
| `calendar-events-plan.md`, `calendar-ui/` | Calendar + recurring events, calendar UI/mockups |
| `health-domain-plan.md` | Medical capture, labs, private-domain handling |
| `finance-domain-plan.md` | Financial filing destination |
| `lab` intake | *(the live spec stayed at `docs/lab-intake-plan.md`)* |
| `multi-attachment-plan.md` | Multiple attachments per note (≤100 MB each) |
| `location-features-plan.md`, `map-performance-plan.md` | Location trail, Map view, heatmap, trips |
| `kb-maintenance-redesign.md` | KB update/maintenance/rebuild passes + Talk pages |
| `source-of-truth-corrections-plan.md` | Corrections / source-of-truth flow |
| `research-link-plan.md` | Read-only research share links |
| `talk-box-redesign.md`, `talk-box-mockup.html`, `modes-redesign-mockup.html` | Compose box + 3-mode segmented control |
| `diff-markup-mockup.png` | Revision-history line diffs |
| `health-status/` | Server/API health model + capability gating (full red-team set, final plan `70-hybrid-v2.md`) |

Throwaway mockup *generator* scripts (e.g. the old `_mockup_diff.py`,
`calendar-ui/mockup.py`/`render.py`) were removed rather than archived — they only
produced the rendered mockups kept above and are recoverable from git history.
