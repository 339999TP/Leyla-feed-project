#!/usr/bin/env python3
"""
research-radar: a configurable digest of new developments in sci/tech fields.

Pipeline: fetch (arXiv + RSS + PubMed, all from a trusted allowlist)
          -> drop already-seen items (SQLite store)
          -> keyword prefilter (cheap, keeps LLM cost down)
          -> LLM judges relevance + maturity stage + significance per item
          -> filter by your per-topic thresholds
          -> render HTML + Markdown digest
          -> deliver (write file always; email optional)

Scheduling is external (cron / GitHub Actions). This script runs the pipeline once.
No LLM key needed to try it: run with --dry-run to use the built-in mock scorer.
"""

import argparse
import datetime as dt
import warnings
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

warnings.filterwarnings("ignore", category=DeprecationWarning)

import feedparser
import requests
import yaml

# Maturity ladder, lowest to highest. Your per-topic `min_stage` is checked against this.
STAGE_ORDER = ["discovery", "lab", "prototype", "commercial", "scaled"]
STAGE_LABEL = {
    "discovery":  "Discovery / new finding",
    "lab":        "Lab demonstration",
    "prototype":  "Prototype / pilot",
    "commercial": "Commercial / on market",
    "scaled":     "Scaled deployment",
}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def stage_idx(name):
    try:
        return STAGE_ORDER.index(name)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("lookback_days", 7)
    cfg.setdefault("keyword_prefilter", True)
    cfg.setdefault("max_per_source", 60)
    cfg.setdefault("sources", {})
    cfg["sources"].setdefault("arxiv_categories", [])
    cfg["sources"].setdefault("rss", [])
    cfg["sources"].setdefault("pubmed_queries", [])
    cfg.setdefault("llm", {})
    cfg["llm"].setdefault("provider", "mock")
    cfg["llm"].setdefault("model", "")
    cfg["llm"].setdefault("batch_size", 8)
    cfg.setdefault("delivery", {})
    cfg["delivery"].setdefault("site_dir", "./site")
    cfg["delivery"].setdefault("email", {"enabled": False})
    for t in cfg.get("topics", []):
        t.setdefault("keywords", [])
        t.setdefault("min_stage", "discovery")
        t.setdefault("min_significance", 1)
    return cfg


# ---------------------------------------------------------------------------
# Seen-items store (so "new" means new since the last run)
# ---------------------------------------------------------------------------

def open_store(path="radar_seen.sqlite"):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen "
        "(key TEXT PRIMARY KEY, url TEXT, title TEXT, first_seen TEXT)"
    )
    con.commit()
    return con


def item_key(item):
    basis = (item.get("url") or "") + "|" + norm_title(item.get("title", ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def is_seen(con, key):
    cur = con.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
    return cur.fetchone() is not None


def mark_seen(con, item):
    con.execute(
        "INSERT OR IGNORE INTO seen (key, url, title, first_seen) VALUES (?,?,?,?)",
        (item_key(item), item.get("url", ""), item.get("title", ""),
         utcnow().isoformat()),
    )


def norm_title(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (t or "").lower())).strip()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

UA = {"User-Agent": "research-radar/1.0 (personal digest tool)"}
# Some outlets reject non-browser agents; feedparser needs a plain string.
UA_STR = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124 Safari/537.36")


def parsed_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        v = getattr(entry, attr, None)
        if v:
            return dt.datetime(*v[:6])
    return None


def fetch_arxiv(categories, max_results):
    items = []
    for cat in categories:
        url = (
            "http://export.arxiv.org/api/query?"
            f"search_query=cat:{cat}&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={max_results}"
        )
        feed = feedparser.parse(url, agent=UA_STR)
        for e in feed.entries:
            items.append({
                "source": f"arXiv:{cat}",
                "source_type": "academic",
                "title": e.get("title", "").replace("\n", " ").strip(),
                "summary": e.get("summary", "").replace("\n", " ").strip(),
                "url": e.get("link", ""),
                "published": parsed_date(e),
            })
    return items


def fetch_rss(urls, max_results):
    items = []
    for url in urls:
        try:
            feed = feedparser.parse(url, agent=UA_STR)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! RSS error {url}: {exc}", file=sys.stderr)
            continue
        src = feed.feed.get("title", url) if getattr(feed, "feed", None) else url
        for e in feed.entries[:max_results]:
            summary = e.get("summary", "") or e.get("description", "")
            summary = re.sub(r"<[^>]+>", "", summary).replace("\n", " ").strip()
            items.append({
                "source": src,
                "source_type": "news",
                "title": e.get("title", "").replace("\n", " ").strip(),
                "summary": summary,
                "url": e.get("link", ""),
                "published": parsed_date(e),
            })
    return items


def fetch_pubmed(queries, max_results):
    items = []
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    for q in queries:
        try:
            r = requests.get(
                f"{base}/esearch.fcgi",
                params={"db": "pubmed", "term": q, "retmode": "json",
                        "sort": "date", "retmax": max_results},
                headers=UA, timeout=30,
            )
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                continue
            s = requests.get(
                f"{base}/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
                headers=UA, timeout=30,
            ).json().get("result", {})
            for pid in ids:
                rec = s.get(pid)
                if not rec:
                    continue
                pub = None
                if rec.get("pubdate"):
                    for fmt in ("%Y %b %d", "%Y %b", "%Y"):
                        try:
                            pub = dt.datetime.strptime(rec["pubdate"], fmt)
                            break
                        except ValueError:
                            continue
                items.append({
                    "source": f"PubMed: {q}",
                    "source_type": "academic",
                    "title": rec.get("title", "").strip(),
                    "summary": rec.get("title", "").strip(),  # esummary has no abstract
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                    "published": pub,
                })
        except Exception as exc:  # noqa: BLE001
            print(f"  ! PubMed error '{q}': {exc}", file=sys.stderr)
    return items


def gather(cfg):
    s = cfg["sources"]
    mx = cfg["max_per_source"]
    items = []
    if s["arxiv_categories"]:
        print(f"  arXiv: {len(s['arxiv_categories'])} categories")
        items += fetch_arxiv(s["arxiv_categories"], mx)
    if s["rss"]:
        print(f"  RSS: {len(s['rss'])} feeds")
        items += fetch_rss(s["rss"], mx)
    if s["pubmed_queries"]:
        print(f"  PubMed: {len(s['pubmed_queries'])} queries")
        items += fetch_pubmed(s["pubmed_queries"], mx)
    return items


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def within_lookback(item, days):
    if item.get("published") is None:
        return True  # unknown date: keep, let LLM/thresholds decide
    return item["published"] >= utcnow() - dt.timedelta(days=days)


def candidate_topics(item, topics):
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    hits = []
    for t in topics:
        for kw in t["keywords"]:
            if kw.lower() in text:
                hits.append(t["name"])
                break
    return hits


# ---------------------------------------------------------------------------
# Scoring: LLM providers + a keyless mock
# ---------------------------------------------------------------------------

SCORE_INSTRUCTIONS = """You are triaging science/technology items for a reader who
only wants genuine breakthroughs and concrete events -- not incremental progress,
reviews, or speculation.

For each item decide which ONE of the reader's topics it best fits, or "none".
Set relevant=true ONLY if the item is really about that topic itself. An item that
merely mentions a topic term in passing is NOT relevant -- e.g. a telescope that
happens to use superconducting sensors is not a "Superconductors" item.

MATURITY STAGE -- how far along the development is:
  discovery  = new finding, theory, or first observation
  lab        = demonstrated in a lab / proof of concept
  prototype  = working prototype, pilot, or engineering scale-up
  commercial = product announced, on the market, or commercially available
  scaled     = mass deployment / widespread real-world adoption

SIGNIFICANCE -- for a reader who wants only breakthroughs and events:
  5 = landmark breakthrough or headline event (e.g. fusion net-energy gain, first
      direct image of an Earth-like exoplanet, a verified room-temperature
      superconductor, a major probe landing, or a telescope's first light)
  4 = significant advance or real milestone event (a first-of-its-kind result, a
      confirmed detection, a working first demonstration, a mission reaching a
      target)
  3 = notable but incremental
  2 = routine progress or refinement
  1 = review, roundup, commentary, or speculative modelling
Prefer concrete empirical results and events over theoretical or incremental
preprints. When unsure between two scores, choose the lower one.

Return ONLY a JSON array, one object per item, no prose, no code fences:
[{"i":0,"topic":"<topic name or none>","relevant":true,"stage":"prototype",
  "significance":4,"summary":"<=25 word plain-English summary"}]
"""


def build_score_prompt(batch, topics):
    tlist = "\n".join(f"- {t['name']}: {t.get('description','')}" for t in topics)
    lines = []
    for i, it in enumerate(batch):
        lines.append(
            f'[{i}] source={it["source"]}\nTITLE: {it["title"]}\n'
            f'ABSTRACT: {it["summary"][:700]}'
        )
    return (
        f"{SCORE_INSTRUCTIONS}\nREADER'S TOPICS:\n{tlist}\n\nITEMS:\n"
        + "\n\n".join(lines)
    )


def parse_json_array(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    return json.loads(m.group(0)) if m else json.loads(text)


def call_anthropic(prompt, model):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["LLM_API_KEY"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model or "claude-3-5-sonnet-latest", "max_tokens": 1500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def call_openai(prompt, model):
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": model or "gpt-4o-mini",
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def call_gemini(prompt, model):
    model = model or "gemini-1.5-flash"
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": os.environ["LLM_API_KEY"]},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


PROVIDERS = {"anthropic": call_anthropic, "openai": call_openai, "gemini": call_gemini}

# Keyword heuristics for the keyless mock scorer.
STAGE_HINTS = [
    ("scaled",     ["mass production", "rolled out", "widespread", "deployed at scale",
                    "millions of"]),
    ("commercial", ["on sale", "available now", "commercially", "launches", "launched",
                    "shipping", "on the market", "product", "now available"]),
    ("prototype",  ["prototype", "pilot", "demonstrat", "scale-up", "engineering",
                    "working model", "field test"]),
    ("lab",        ["in the lab", "in mice", "in vitro", "proof of concept",
                    "laboratory", "clinical trial"]),
]
SIG_HINTS = ["breakthrough", "first", "record", "milestone", "unprecedented",
             "major", "landmark"]


def mock_score(batch, topics):
    out = []
    for i, it in enumerate(batch):
        text = (it["title"] + " " + it["summary"]).lower()
        cands = candidate_topics(it, topics)
        topic = cands[0] if cands else "none"
        stage = "discovery"
        for name, hints in STAGE_HINTS:
            if any(h in text for h in hints):
                stage = name
                break
        sig = 3 + sum(1 for h in SIG_HINTS if h in text)
        sig = max(1, min(5, sig))
        summary = re.split(r"(?<=[.!?])\s", it["summary"].strip())[0][:200] \
            or it["title"]
        out.append({"i": i, "topic": topic, "relevant": topic != "none",
                    "stage": stage, "significance": sig, "summary": summary})
    return out


def score_items(items, topics, cfg, force_mock=False):
    provider = "mock" if force_mock else cfg["llm"]["provider"]
    bs = cfg["llm"]["batch_size"]
    scored = []
    for start in range(0, len(items), bs):
        batch = items[start:start + bs]
        if provider == "mock":
            results = mock_score(batch, topics)
        else:
            prompt = build_score_prompt(batch, topics)
            try:
                results = parse_json_array(PROVIDERS[provider](prompt, cfg["llm"]["model"]))
            except Exception as exc:  # noqa: BLE001
                print(f"  ! scoring batch failed ({exc}); skipping batch",
                      file=sys.stderr)
                continue
        by_i = {r["i"]: r for r in results if isinstance(r, dict) and "i" in r}
        for j, it in enumerate(batch):
            r = by_i.get(j)
            if r:
                it.update({
                    "topic": r.get("topic", "none"),
                    "relevant": bool(r.get("relevant")),
                    "stage": r.get("stage", "discovery"),
                    "significance": int(r.get("significance", 1) or 1),
                    "llm_summary": r.get("summary", ""),
                })
                scored.append(it)
    return scored


def passes_thresholds(item, topics_by_name):
    if not item.get("relevant") or item.get("topic", "none") == "none":
        return False
    t = topics_by_name.get(item["topic"])
    if not t:
        return False
    if stage_idx(item["stage"]) < stage_idx(t["min_stage"]):
        return False
    if item["significance"] < t["min_significance"]:
        return False
    return True


# ---------------------------------------------------------------------------
# Feed archive (JSON) + optional email
# ---------------------------------------------------------------------------

def to_record(it):
    return {
        "id": item_key(it),
        "title": it["title"],
        "url": it["url"],
        "source": it["source"],
        "source_type": it.get("source_type", ""),
        "topic": it["topic"],
        "stage": it["stage"],
        "significance": it["significance"],
        "summary": it.get("llm_summary") or it["summary"][:280],
        "published": it["published"].isoformat() if it.get("published") else None,
        "added": utcnow().isoformat() + "Z",
    }


def merge_feed(site_dir, records, topics, cap=400):
    """Append new records to the growing feed.json, newest first, deduped."""
    os.makedirs(site_dir, exist_ok=True)
    path = os.path.join(site_dir, "feed.json")
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f).get("items", [])
        except Exception:  # noqa: BLE001
            existing = []
    have = {r.get("id") for r in existing}
    added = [r for r in records if r["id"] not in have]
    items = (added + existing)
    items.sort(key=lambda r: r.get("added", ""), reverse=True)
    items = items[:cap]
    data = {
        "generated": utcnow().isoformat() + "Z",
        "topics": [t["name"] for t in topics],
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path, len(added)


def email_new(records, cfg):
    em = cfg["delivery"].get("email", {})
    if not em.get("enabled") or not records:
        return
    rows = ""
    for r in records:
        stars = "\u2605" * r["significance"] + "\u2606" * (5 - r["significance"])
        rows += (f'<div style="margin:12px 0"><a href="{r["url"]}">{r["title"]}</a>'
                 f'<br><small>{r["topic"]} \u00b7 {stars} \u00b7 '
                 f'{STAGE_LABEL.get(r["stage"], r["stage"])} \u00b7 {r["source"]}'
                 f'</small><br>{r["summary"]}</div>')
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Research radar \u2014 {len(records)} new"
    msg["From"] = em["from_addr"]
    msg["To"] = ", ".join(em["to_addrs"])
    msg.attach(MIMEText("New items in your radar.", "plain"))
    msg.attach(MIMEText(f"<div>{rows}</div>", "html"))
    with smtplib.SMTP(em["smtp_host"], em.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(em["username"], os.environ["EMAIL_PASSWORD"])
        s.send_message(msg)
    print(f"  emailed {len(records)} new items to {msg['To']}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_feeds(cfg):
    print("Validating RSS feeds...")
    for url in cfg["sources"]["rss"]:
        try:
            f = feedparser.parse(url, agent=UA_STR)
            n = len(f.entries)
            title = f.feed.get("title", "?") if getattr(f, "feed", None) else "?"
            flag = "OK " if n else "EMPTY"
            print(f"  [{flag}] {n:>3} entries — {title}  <{url}>")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {url}: {exc}")


def run(cfg, force_mock=False):
    topics = cfg.get("topics", [])
    topics_by_name = {t["name"]: t for t in topics}
    con = open_store()

    print("Fetching sources...")
    items = gather(cfg)
    print(f"  fetched {len(items)} raw items")

    items = [it for it in items if within_lookback(it, cfg["lookback_days"])]
    print(f"  {len(items)} within last {cfg['lookback_days']} days")

    fresh, seen_now = [], set()
    for it in items:
        k = item_key(it)
        if k in seen_now or is_seen(con, k):
            continue
        seen_now.add(k)
        fresh.append(it)
    print(f"  {len(fresh)} not seen before")

    if cfg["keyword_prefilter"]:
        fresh = [it for it in fresh if candidate_topics(it, topics)]
        print(f"  {len(fresh)} match a topic keyword (prefilter)")

    provider = "mock" if force_mock else cfg["llm"]["provider"]
    print(f"Scoring with provider='{provider}'...")
    scored = score_items(fresh, topics, cfg, force_mock=force_mock)

    passed = [it for it in scored if passes_thresholds(it, topics_by_name)]
    print(f"  {len(passed)} cleared your thresholds")

    # Mark everything we evaluated as seen, so next run doesn't re-score it.
    for it in scored:
        mark_seen(con, it)
    con.commit()

    print("Delivering...")
    records = [to_record(it) for it in passed]
    site_dir = cfg["delivery"].get("site_dir", "./site")
    path, n_added = merge_feed(site_dir, records, topics)
    print(f"  feed: +{n_added} new items -> {path}")
    email_new(records, cfg)
    return path


def main():
    ap = argparse.ArgumentParser(description="research-radar digest tool")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the keyless mock scorer (no LLM key needed)")
    ap.add_argument("--validate-feeds", action="store_true",
                    help="check that configured RSS feeds parse, then exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.validate_feeds:
        validate_feeds(cfg)
        return
    run(cfg, force_mock=args.dry_run)


if __name__ == "__main__":
    main()
