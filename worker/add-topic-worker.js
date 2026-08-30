/**
 * radar-add-topic — Cloudflare Worker
 *
 * Bridges the static feed page and the GitHub Actions "add topic" workflow.
 * The browser POSTs {topic, i} where `i` is an index into the curated allowlist
 * (docs/topics.json). The worker:
 *   1. checks the request origin,
 *   2. re-fetches the SAME allowlist and verifies list[i] === topic,
 *   3. rate-limits (per-IP + global),
 *   4. fires workflow_dispatch with the SERVER-DERIVED canonical term (list[i]),
 *      never the raw client string.
 *
 * Because only exact allowlist entries can ever be dispatched, no free-form
 * user text reaches the workflow, radar.py, or the LLM — prompt injection has
 * no surface. The GitHub token lives only in this worker's secret store.
 *
 * Bindings (see wrangler.toml):
 *   GH_TOKEN        (secret)  fine-grained PAT, Actions: Read and write, this repo only
 *   OWNER REPO WORKFLOW REF   target workflow_dispatch
 *   ALLOWED_ORIGIN            e.g. https://339999tp.github.io
 *   TOPICS_URL               public URL of docs/topics.json
 *   RL                       (KV) rate-limit counters (optional but recommended)
 *   RATE_PER_IP RATE_GLOBAL RATE_WINDOW_MS
 */

function corsHeaders(origin, allowed) {
  const h = {
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  if (origin && origin === allowed) h["Access-Control-Allow-Origin"] = allowed;
  return h;
}

function json(body, status, extra) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...(extra || {}) },
  });
}

async function getAllowlist(env, ctx) {
  const url = env.TOPICS_URL;
  const cache = caches.default;
  const key = new Request(url);
  let res = await cache.match(key);
  if (!res) {
    res = await fetch(url, { cf: { cacheTtl: 300, cacheEverything: true } });
    if (res.ok && ctx) ctx.waitUntil(cache.put(key, res.clone()));
  }
  if (!res.ok) throw new Error("allowlist fetch failed");
  const data = await res.json();
  return Array.isArray(data.topics) ? data.topics.map((t) => String(t).trim()) : [];
}

async function rateLimit(env, ip) {
  if (!env.RL) return { ok: true };
  const windowMs = Number(env.RATE_WINDOW_MS || 3600000);
  const perIp = Number(env.RATE_PER_IP || 5);
  const globalMax = Number(env.RATE_GLOBAL || 50);
  const ttl = Math.max(60, Math.ceil(windowMs / 1000));
  const bucket = Math.floor(Date.now() / windowMs);

  const ipKey = `ip:${bucket}:${ip}`;
  const gKey = `g:${bucket}`;
  const ipVal = Number((await env.RL.get(ipKey)) || 0);
  if (ipVal >= perIp) return { ok: false, reason: "per-ip" };
  const gVal = Number((await env.RL.get(gKey)) || 0);
  if (gVal >= globalMax) return { ok: false, reason: "global" };

  // Coarse (non-atomic) counters — fine for abuse throttling.
  await env.RL.put(ipKey, String(ipVal + 1), { expirationTtl: ttl });
  await env.RL.put(gKey, String(gVal + 1), { expirationTtl: ttl });
  return { ok: true };
}

async function dispatch(env, term) {
  const api =
    `https://api.github.com/repos/${env.OWNER}/${env.REPO}` +
    `/actions/workflows/${env.WORKFLOW}/dispatches`;
  return fetch(api, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "radar-add-topic-worker",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: env.REF || "main", inputs: { topic: term } }),
  });
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const allowed = env.ALLOWED_ORIGIN || "";
    const cors = corsHeaders(origin, allowed);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== "POST") {
      return json({ error: "method not allowed" }, 405, cors);
    }
    // Block cross-site browser calls: if an Origin is present it must match.
    if (origin && origin !== allowed) {
      return json({ error: "forbidden origin" }, 403, cors);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "invalid JSON" }, 400, cors);
    }

    const i = Number.isInteger(body.i) ? body.i : parseInt(body.i, 10);
    const term = typeof body.topic === "string" ? body.topic.trim() : "";
    if (!Number.isInteger(i) || i < 0 || !term) {
      return json({ error: "bad request" }, 400, cors);
    }

    let list;
    try {
      list = await getAllowlist(env, ctx);
    } catch {
      return json({ error: "allowlist unavailable" }, 503, cors);
    }
    // The index and the term must both point at the same allowlist entry.
    if (i >= list.length || list[i] !== term) {
      return json({ error: "not in allowlist (refresh the page)" }, 422, cors);
    }

    const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
    const rl = await rateLimit(env, ip);
    if (!rl.ok) {
      return json({ error: "rate limited", scope: rl.reason }, 429, cors);
    }

    // Dispatch the CANONICAL server-side term, never the raw client string.
    const canonical = list[i];
    const gh = await dispatch(env, canonical);
    if (gh.status === 204) {
      return json({ ok: true, topic: canonical }, 202, cors);
    }
    const detail = await gh.text().catch(() => "");
    return json(
      { error: "dispatch failed", status: gh.status, detail: detail.slice(0, 300) },
      502,
      cors,
    );
  },
};
