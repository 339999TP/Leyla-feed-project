# radar-add-topic worker

A tiny Cloudflare Worker that lets the **+ Add topic** button on the feed page
trigger the GitHub Actions pipeline **seamlessly** — without ever putting a
GitHub token in the browser.

## How it stays safe

- The page only ever sends an **index into the curated allowlist**
  (`docs/topics.json`) plus the term at that index.
- The worker re-fetches the same allowlist and dispatches the **server-derived
  canonical term** (`list[i]`) — never the raw client string. Anything not in
  the list is rejected (`422`). So no free-form text can reach the workflow,
  `radar.py`, or the LLM: **prompt injection has no surface.**
- The GitHub token lives only in the worker's secret store, scoped to Actions on
  this one repo.
- Requests are origin-checked and rate-limited (per-IP + global).

Defense in depth: the workflow itself runs with `RADAR_ENFORCE_ALLOWLIST=1`, so
even a manual `workflow_dispatch` with arbitrary text is refused unless the term
is in `topics.json`.

## Deploy (one time)

You need a Cloudflare account and [`wrangler`](https://developers.cloudflare.com/workers/wrangler/install-and-update/)
(`npm i -g wrangler`, then `wrangler login`).

1. **Create the rate-limit KV namespace** and paste its id into `wrangler.toml`:
   ```bash
   wrangler kv namespace create RL
   # copy the printed id -> kv_namespaces[0].id in wrangler.toml
   ```

2. **Edit `wrangler.toml` vars** if your repo/owner/origin differ from the
   defaults (`OWNER`, `REPO`, `ALLOWED_ORIGIN`, `TOPICS_URL`, `REF`).

3. **Create a fine-grained GitHub token** at
   github.com/settings/personal-access-tokens:
   - *Resource owner*: the repo owner.
   - *Repository access*: **Only select repositories** → this repo.
   - *Permissions*: **Actions → Read and write** (nothing else).

   Store it as the worker secret:
   ```bash
   wrangler secret put GH_TOKEN
   # paste the token when prompted
   ```

4. **Deploy:**
   ```bash
   wrangler deploy
   ```
   Copy the printed URL (e.g. `https://radar-add-topic.<you>.workers.dev`).

5. **Wire the page to the worker:** set `ADD_TOPIC_ENDPOINT` near the top of the
   `<script>` in `docs/index.html` to that URL, commit, and push.

6. **Make sure the workflow is on the default branch.** `workflow_dispatch`
   dispatches against `REF` (default `main`), and the `topic` input must exist
   in the copy of `digest.yml` on that branch. Merge this branch to `main` first.

## Test

```bash
curl -X POST https://radar-add-topic.<you>.workers.dev \
  -H 'content-type: application/json' \
  -H 'Origin: https://339999tp.github.io' \
  -d '{"topic":"Quantum computing","i":6}'   # i must match topics.json
# -> {"ok":true,"topic":"Quantum computing"}   (and a run appears in Actions)
```

A wrong index/term returns `422`; too many requests return `429`.

## Optional hardening

- Add [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) and
  verify the token in the worker to stop bots.
- Tighten `RATE_PER_IP` / `RATE_GLOBAL`.
