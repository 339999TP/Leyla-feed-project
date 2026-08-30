# research-radar

A single, living **feed page** of breakthroughs and events in the sci/tech fields
you pick — pulled from a trusted source allowlist, filtered by Claude on three axes:
**relevance**, **maturity stage**, and **significance**. You browse it like a
timeline: follow/unfollow topics, sort Latest or Top, hit Refresh.

Tracks: Nuclear fusion · Superconductors · Dark matter · Exoplanet imaging
telescopes · Early life and archaea. Tuned so only **landmark results and concrete
events** get through (probe landings, first light, first detections, records) — not
incremental papers or reviews.

## Two parts

1. **`radar.py`** — the pipeline: fetch → drop already-seen → keyword prefilter →
   Claude scores each item → threshold filter → append to `site/feed.json`.
2. **`site/index.html`** — the reader: a static page that loads `feed.json` and
   renders the feed with follow/unfollow chips, sort, and refresh. Follow state is
   saved in your browser.

---

# Setup, step by step

## 1. Get the code running locally

You need Python 3.10+. In a terminal:

```bash
cd research-radar
pip install -r requirements.txt
cp config.example.yaml config.yaml
python radar.py --dry-run          # keyless test run; fills site/feed.json
```

Open `site/index.html` in a browser to see the feed. `--dry-run` uses a dumb
keyword-only scorer (it misclassifies things) — it just proves the plumbing before
you spend a cent. The real scoring comes from Claude in step 3.

## 2. Get a Claude API key

The API is a **separate product from your Claude Pro subscription**. Pro is the
chat app; it does not include API access, and — importantly — **API usage does not
touch your Pro limits at all**. You pay the API separately, per token, from prepaid
credit. It's cheap (see costs below).

1. Go to **console.anthropic.com** and sign in (you can use the same email as your
   Pro account, but it's a distinct Developer Platform login).
2. **Billing → add a payment method → buy credit.** The minimum is $5. There's no
   monthly fee; you only spend what you use.
3. **API keys → Create key.** Copy it immediately — it's shown once. It looks like
   `sk-ant-api03-...`.

## 3. Point the tool at Claude

`config.yaml` already has:

```yaml
llm:
  provider: anthropic
  model: claude-haiku-4-5     # cheapest Claude, fine for this triage job
```

Put your key in an environment variable (never in the file) and run:

```bash
export LLM_API_KEY="sk-ant-api03-...your key..."
python radar.py --config config.yaml
```

That's a real run: Claude scores the week's items, and only breakthroughs/events
land in `site/feed.json`. Refresh `index.html` to see them.

## 4. Put it on a schedule + host the feed (free)

This makes it update itself and gives you a URL to open anywhere.

1. Push this folder to a GitHub repo. Commit `config.yaml` — it holds no secrets
   (the key lives in an environment variable, not the file).
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret.** Name it `LLM_API_KEY`, paste your key.
3. **Settings → Pages → Build and deployment → Deploy from a branch → `main`,
   folder `/site`.** Your feed goes live at `https://<you>.github.io/<repo>/`.
4. The workflow in `.github/workflows/digest.yml` runs every Monday 07:00 UTC (edit
   the `cron:` line to change that), scores new items, and commits `feed.json`. To
   run it now, go to the **Actions** tab → research-radar → **Run workflow**.

## 4b. (Optional) Make the backend private, keep the page public

GitHub Pages on a **private** repo needs a paid plan. To keep the code/config/
secrets private on a free plan, use **two repos**: this one stays private
(backend), and a small **public** repo hosts only the built site. The workflow
already has a `Publish site to public Pages repo` step that does the copy — it
stays dormant until you configure these:

1. **Create a new public repo**, e.g. `leyla-feed-public` (empty is fine).
2. **Make a fine-grained token** (github.com/settings/personal-access-tokens):
   *Repository access* → only the new public repo; *Permissions* → **Contents:
   Read and write**. In THIS repo, add it as a secret named `PAGES_DEPLOY_TOKEN`
   (**Settings → Secrets and variables → Actions → Secrets**).
3. In THIS repo, add a **variable** (same page → **Variables** tab) named
   `PAGES_REPO` with value `<owner>/leyla-feed-public`.
4. Run the workflow once (Actions → **Run workflow**). It publishes `docs/` to a
   `gh-pages` branch on the public repo.
5. In the **public** repo: **Settings → Pages → Deploy from a branch →
   `gh-pages` / root**. The live URL becomes
   `https://<owner>.github.io/leyla-feed-public/`.
6. Now flip THIS repo to **private** (Settings → General → Danger Zone). The page
   keeps updating on every run; the backend is hidden.

Note: if you use the Cloudflare "Add topic" worker, point its `ALLOWED_ORIGIN`
and the page's fetch at the **public** URL, and its `TOPICS_URL` at the public
`topics.json`.

## 5. (Optional) Customize topics and sources

**Add a new topic the easy way (Claude does the setup):**
Just give it a name — Claude writes the keywords and description, finds and
validates relevant sources (arXiv categories, PubMed queries, RSS feeds), adds it
all to `config.yaml`, and seeds the feed so the topic shows up right away.

- **On the site (seamless):** click **+ Add topic** in the chip row and pick a topic
  from the list. That's it — the feed rebuilds itself in a minute or two. This is
  powered by a small Cloudflare Worker that safely triggers the pipeline; deploy it
  once with the steps in [`worker/README.md`](worker/README.md), then set
  `ADD_TOPIC_ENDPOINT` in `docs/index.html`. Until that's wired up, the button falls
  back to opening the Actions tab.
  - You can only pick **recognised topics** from the curated allowlist in
    [`docs/topics.json`](docs/topics.json). This is a deliberate safety measure — only
    short, vetted terms ever reach the pipeline, so there's no prompt-injection
    surface. To offer more topics, append to that file.
- **From the Actions tab:** research-radar → **Run workflow** → type a topic name
  in the *topic* field. (Must be a term in `docs/topics.json`.)
- **Locally:** `python radar.py --add-topic "Quantum computing"` (uses your Claude
  API key), then commit and push the updated `config.yaml` + `feed.json`.

Architecture notes for maintainers live in [`CLAUDE.md`](CLAUDE.md).

Or edit `config.yaml` by hand for full control:

**Add a new topic:**
```yaml
topics:
  - name: Quantum Computing
    description: Quantum computers, algorithms, error correction, quantum advantage
    keywords: [quantum computing, quantum computer, quantum algorithm, qubit, quantum gate, quantum error correction]
    min_stage: discovery
    min_significance: 3
```

**Add custom RSS feeds:**
Add any mainstream science news or academic journal RSS feed to the `sources.rss` list:
```yaml
rss:
  - https://your-science-news-site.com/rss/physics.xml
  - https://research-org.org/feed/
```

**Adjust sensitivity:**
- `min_significance: 3` = more items (include notable advances)
- `min_significance: 4` = fewer items (only major breakthroughs)
- Supported values: 1 (incremental) through 5 (landmark breakthrough)

**On GitHub:** Edit `config.yaml` in the web editor, commit, and the next scheduled run will use your new settings. Or trigger an immediate run from the **Actions** tab.

**Locally:** Edit `config.yaml`, commit, and push. GitHub Actions will pick up your changes automatically.

## 6. (Optional) Email each digest

Set `delivery.email.enabled: true` in the config, fill in your SMTP details, and add
an `EMAIL_PASSWORD` secret (for Gmail, use an App Password, not your login).

---

# What it costs

Measured on the shipped config (5 topics, 14 feeds + arXiv + PubMed), one weekly run
scores ~110 items ≈ **27k input + 5.5k output tokens**.

Claude Haiku 4.5 is **$1 per million input tokens and $5 per million output**
([Anthropic pricing](https://docs.claude.com/en/docs/about-claude/pricing)), so:

- **Per weekly run: ~$0.05.**
- **Per month: ~$0.20.** Per year: a few dollars.
- $5 of credit lasts well over a year.

The token cost is set by how many items reach Claude, which the keyword prefilter
controls — **not** by your significance threshold (everything matched still gets
scored; the threshold just decides what's shown). Levers if you ever want it lower:
tighten `keywords`, drop the noisiest arXiv categories, or switch the Batch API on
(50% off). Switching to Sonnet 5 (`claude-sonnet-5`, $2/$10) roughly doubles cost
and is overkill for triage — Haiku is the right call here.

Again: none of this comes out of your Pro plan. It's separate prepaid API credit.

---

# How it maps to what you asked for

| You wanted | How it's done |
|---|---|
| Topics you choose | `topics:` in the config |
| Update frequency | Cron schedule + `lookback_days` |
| Threshold (breakthroughs/events only) | `min_significance: 4` filters to landmark results + events; `min_stage` kept at `discovery` so big discoveries still pass |
| Reputable sources, incl. news | arXiv, PubMed, ScienceDaily topic feeds, Physics World, Symmetry, Space.com, Quanta, Scientific American, Science (AAAS), Nature, Universe Today |
| Claude checks relevance / what's new | Claude scores each item; strict relevance kills passing-mention false positives; a seen-store makes "new" mean unseen since last run |
| Feed you can refresh, follow, unfollow | `site/index.html` |

# Honest limitations

- **Titles + abstracts only** — true significance sometimes only shows in full text.
- **PubMed summaries lack abstracts** (titles only), so scoring there is weaker.
- **Maturity/significance is a judgment call** Claude will occasionally get wrong.
- **New Scientist** has no working public RSS (paywalled); comparable outlets are
  used instead. Add a feed URL if your subscription provides one.
- Follow/unfollow is per-browser, not synced; the feed content itself is shared.
