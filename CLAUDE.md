# CLAUDE.md — project notes for Claude Code

Orientation for anyone (human or a local Claude Code session) picking this repo up.
Read this before changing the feed page or the "add topic" flow.

## What this project is

**research-radar** — a single static **feed page** of science/tech breakthroughs.
Two moving parts:

1. **`radar.py`** — the pipeline. Fetch (arXiv + RSS + PubMed) → drop already-seen
   (SQLite `radar_seen.sqlite`) → keyword prefilter → Claude scores each item
   (relevance / maturity stage / significance) → threshold filter → append to
   `docs/feed.json`. Runs on a schedule (and on demand) via GitHub Actions
   (`.github/workflows/digest.yml`). No LLM key needed to try it: `--dry-run`.
2. **`docs/index.html`** — the reader. A **static** page (GitHub Pages) that loads
   `docs/feed.json` and renders follow/unfollow chips, Latest/Top sort, Feed/Columns
   layout, and Refresh. Follow state is per-browser (`localStorage`).

Topics, sources, keywords, and thresholds live in **`config.yaml`**.

Deployed site is served from `docs/` on GitHub Pages at
`https://339999tp.github.io/Leyla-feed-project/`.
`radar-feed-preview.html` (repo root) is a **design-reference snapshot** of the same
page — keep it visually in sync with `docs/index.html` when you change the layout,
but the live site is `docs/index.html`.

## Key architectural constraint

The site is **static** — there is no backend and the browser must never hold a
GitHub token or an LLM key. Anything that needs a secret (triggering the workflow,
calling the LLM) happens either in **GitHub Actions** or in the **Cloudflare Worker**
(see below), never in the page.

## The "Add topic" feature (added on branch `claude/mobile-formatting-issues-l4cteb`)

Goal: a user clicks **+ Add topic** on the page and, seamlessly, Claude generates
keywords + a description, finds and validates sources, adds the topic to
`config.yaml`, and seeds the feed — with **prompt injection made structurally
impossible**.

Flow, end to end:

```
docs/index.html  (+ Add topic button)
  → modal autocompletes over docs/topics.json (the curated allowlist)
  → POSTs ONLY {topic, i}  (i = index into topics.json) to the Worker
worker/add-topic-worker.js  (Cloudflare Worker, holds the GitHub token)
  → re-fetches topics.json, verifies list[i] === topic  (else 422)
  → origin check + per-IP/global rate limit (KV)
  → workflow_dispatch with the SERVER-DERIVED canonical term list[i]
.github/workflows/digest.yml  (workflow_dispatch input `topic`)
  → runs: python radar.py --add-topic "<topic>"   with RADAR_ENFORCE_ALLOWLIST=1
radar.py  add_topic()
  → (defense in depth) refuses any term not exactly in docs/topics.json
  → LLM generates description + keywords + suggested arXiv/PubMed/RSS sources
  → validates every source (arXiv returns entries, RSS parses, PubMed returns ids)
  → writes config.yaml with ruamel.yaml (preserves comments/formatting)
  → backfills the feed for the new topic (bypasses the seen-store)
  → commits config.yaml + docs/feed.json
```

### Why injection has no surface
The page sends an **index**, not free text. The Worker dispatches `list[i]` (a
maintainer-authored string from `topics.json`), never the client string. The Action
re-checks against the same allowlist (`RADAR_ENFORCE_ALLOWLIST=1`). So the only
strings that ever reach the LLM/shell are the short, benign terms **you** curate in
`docs/topics.json`. To offer more topics, append to that file — nothing else.

### The single source of truth
`docs/topics.json` is loaded by (a) the browser picker, (b) the Worker (validation),
and (c) `radar.py` (enforcement). Keep it the one place the vocabulary lives.

### What still needs manual setup (NOT done in-repo, by design)
- **Deploy the Worker** and set its `GH_TOKEN` secret — see `worker/README.md`.
- **Wire the page to the Worker**: set `ADD_TOPIC_ENDPOINT` near the top of the
  `<script>` in `docs/index.html` to the deployed Worker URL. **While it is blank,
  the button falls back to opening the Actions tab** (still works, just not
  one-click).
- The `topic` workflow input + allowlist enforcement must be on the branch Actions
  runs from (i.e. merge this branch to `main`).

### Local / CLI use
`python radar.py --add-topic "Quantum computing"` works locally (needs your Claude
API key, or `--dry-run` for a keyless mock). Without `RADAR_ENFORCE_ALLOWLIST` set,
the CLI accepts any name — the allowlist gate is only enforced in the Action.

## Mobile layout fix (same branch)
`docs/index.html` had a broken mobile header: `.chips` (flex:1) collapsed into a
narrow column while the sort/layout segmented buttons floated over them, and the
sticky header grew tall enough to cover the feed. Fixed in the `@media (max-width:640px)`
block: chips get their own full-width row, chip labels stay on one line, and the
header/column-titles drop `position:sticky` on phones. Mirror any further layout
changes into `radar-feed-preview.html`.

## Gotchas
- `requirements.txt` now includes `ruamel.yaml` (comment-preserving config edits).
- `config.yaml` is committed by the Action when a topic is added — don't be surprised
  by bot commits touching it.
- Feed won't show a new topic until the Action commits `docs/feed.json`; the page
  auto-refreshes a few times after a successful add.
