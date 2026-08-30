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
import html
import ipaddress
import json
import os
import re
import smtplib
import socket
import sqlite3
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

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
    cfg.setdefault("feed", {})
    # Cards older than retention_days age out -- but only within a topic that has
    # at least keep_min cards, so a sparse topic never empties. 0 = never expire.
    cfg["feed"].setdefault("retention_days", 0)
    cfg["feed"].setdefault("keep_min", 5)
    for t in cfg.get("topics", []):
        t.setdefault("keywords", [])
        t.setdefault("min_stage", "discovery")
        t.setdefault("min_significance", 1)
        t.setdefault("milestone_only", False)
        t.setdefault("lookback_days", None)  # None => use the global lookback_days
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


def _is_public_url(url):
    """SSRF guard: allow only http(s) URLs whose host resolves to public IPs.
    Blocks localhost, private ranges, and cloud metadata (169.254.x.x)."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for res in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(res[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_fulltext(url, timeout=12):
    """Fetch an article/paper page and extract readable body text.
    Returns extracted text (best-effort) or '' on failure. Used to give the
    LLM more than a truncated RSS blurb so it can summarize the actual finding."""
    if not _is_public_url(url):
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA_STR}, timeout=timeout,
                         stream=True)
        # Re-validate after redirects so an allowed host can't 302 to an internal one.
        if not r.ok or not _is_public_url(r.url):
            return ""
        if "html" not in r.headers.get("content-type", "").lower():
            return ""
        # Bounded read so a huge page can't exhaust memory.
        raw = r.raw.read(2_000_000, decode_content=True)
        html_text = raw.decode(r.encoding or "utf-8", errors="replace")
        # Drop scripts, styles, and other non-content blocks
        html_text = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", html_text)
        # Prefer <article> or <main> body if present
        m = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", html_text)
        body = m.group(2) if m else html_text
        # Extract paragraph text (articles are mostly <p> tags)
        paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)
        text = " ".join(paras) if paras else body
        text = re.sub(r"(?is)<[^>]+>", " ", text)      # strip remaining tags
        text = re.sub(r"&[a-z#0-9]+;", " ", text)       # strip HTML entities
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]                              # cap for token budget
    except Exception:  # noqa: BLE001
        return ""


def enrich_fulltext(items, max_items=50):
    """For prefiltered news items with thin summaries, fetch the full article so
    the LLM summarizes the real discovery, not a truncated headline blurb.
    arXiv summaries are already full abstracts, so we skip those."""
    enriched = 0
    for it in items[:max_items]:
        # arXiv abstracts are complete; only enrich news/web items with short blurbs
        if it.get("source_type") == "academic":
            continue
        if len(it.get("summary", "")) >= 1200:
            continue
        full = fetch_fulltext(it.get("url", ""))
        if full and len(full) > len(it.get("summary", "")):
            # Keep original blurb up front, append fetched body for context
            it["summary"] = (it.get("summary", "") + " " + full).strip()[:4000]
            enriched += 1
    if enriched:
        print(f"  enriched {enriched} items with full article text")
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


def item_lookback_days(item, topics, default_days):
    """Deepest lookback window any keyword-matching topic asks for.
    Lets sparse/milestone topics reach further back without pulling deep
    history for everything -- only items matching those topics' keywords
    get the extended window; all else uses the global default."""
    days = default_days
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    for t in topics:
        td = t.get("lookback_days")
        if not td or td <= days:
            continue
        if any(kw.lower() in text for kw in t["keywords"]):
            days = td
    return days


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
ONLY wants genuine breakthroughs, discoveries, and concrete events -- not papers,
articles, or speculation.

CRITICAL FILTER: Reject anything that is:
  - A preprint or paper describing research (even landmark papers)
  - An article or news piece that discusses but does not announce a discovery
  - Speculation or theory without an observational/experimental result
  - Background or historical context
  - Multiple items grouped together (e.g., "top 5 superconductor papers")
  - Routine mission updates, personnel changes, or administrative news
  - Blog posts or opinion pieces
  - Conference announcements without a concrete result

ACCEPT ONLY concrete discoveries and events:
  - First detections, first observations, first images (actual data/imagery released)
  - Lab or field confirmation of a breakthrough result (peer-reviewed or officially announced)
  - Announcement that a prototype/device works and a milestone is reached
  - Major mission events (landing, first light, reaching a target, imagery released)
  - Confirmed records or records broken (with verification)
  - Major policy or funding that enables a specific breakthrough

For each item: decide which ONE topic it best fits, or "none".
Set relevant=true ONLY if it announces an actual discovery/event in that topic.
If it's just an article *about* the topic without a concrete discovery, mark relevant=false.

MATURITY STAGE:
  discovery  = new finding, first observation, confirmed result
  lab        = lab demonstration or proof of concept achieved
  prototype  = working prototype, pilot, or test succeeded
  commercial = product launched / commercially available
  scaled     = widespread real-world deployment

SIGNIFICANCE (only count actual discoveries/events):
  5 = landmark breakthrough or major headline event (room-temp superconductor verified,
      fusion net energy gain confirmed, first Earth-like exoplanet image, major probe
      lands successfully, telescope first light released, major policy breakthrough)
  4 = significant first or milestone (first detection confirmed, working demo achieved,
      mission reaches key target, record broken, major facility comes online)
  3 = real advance but smaller scope (incremental discovery, minor record, single lab result,
      important but niche discovery)
  2 = specialized result (narrow field, single institution, preliminary)
  1 = REJECT - not a discovery/event (or too preliminary)
Heavily prefer empirical discoveries over theory. When unsure, score lower.
Require NEWS ANNOUNCEMENT or official statement for significance 3+.

SUPERCONDUCTORS -- SPECIAL PRIORITY:
Room-temperature superconductivity is the holy grail. Give the TOP of the range to
any result that reaches, or moves meaningfully closer to, superconductivity at
ambient (room) temperature or ambient pressure -- e.g. a new material/alloy/hydride
with a higher critical temperature, a verified higher-Tc record, or a route that
lowers the pressure needed. Score these 5 (verified room-temp/ambient claim) or 4
(clear step closer: new record Tc, promising new alloy family). Ordinary
superconductor results with no bearing on the room-temp goal stay at 3 or below.

MILESTONE-ONLY TOPICS:
Some topics below are marked [MILESTONE WATCH]. For those, be EXTRA strict: accept
ONLY a concrete, dated milestone for that specific mission/instrument -- e.g. launch,
arrival/orbit insertion, first light, first data/detection, construction completion,
a record, or a headline scientific result. REJECT (relevant=false) routine progress
updates, funding/schedule news, background explainers, or generic coverage that does
not announce a milestone just happened. These require significance 4+.

SUMMARY (CRITICAL - THIS IS NOT AN ABSTRACT):
Write a 1-2 sentence summary of the DISCOVERY/EVENT ITSELF, not the paper's methodology.
- WHAT: Name the actual breakthrough result (e.g., "Room-temperature superconductor",
  "First exoplanet image", "Fusion net energy gain confirmed")
- WHY IT MATTERS: The real-world impact (e.g., "solves 40-year problem", "enables
  new observations impossible before", "proves commercial viability")

DO NOT write like a technical abstract with methods/theory. DO extract the human-readable
discovery. DO NOT copy the title verbatim. Assume reader has no background.

READABILITY (STRICT): Write for a curious non-specialist, like a good popular-science
headline. Use plain everyday words. Do NOT use unexplained jargon, chemical formulas,
acronyms, or math notation. If a technical term is unavoidable, explain it in plain words.
Prefer "a material that conducts electricity with zero resistance" over "quasi-1D pair
density modulation". If you cannot explain the result in plain language, mark relevant=false.

WRONG: "We present a novel photometric method to detect exoplanets using Fourier analysis
of stellar variability across K2 and TESS datasets"
RIGHT: "First exoplanet image around young star; James Webb's infrared imaging directly
captured light from massive planet at 10 AU"

WRONG: "Air-stable 2D superconductor Nb2Pd3Te5 with quasi-1D pair density modulation"
RIGHT: "New ultrathin material stays superconducting in open air -- a step toward practical,
easy-to-handle superconducting electronics"

Max 30 words. If result is unclear or methodology-focused, mark relevant=false.

Return ONLY JSON array, no prose:
[{"i":0,"topic":"<topic or none>","relevant":true,"stage":"discovery",
  "significance":4,"summary":"<30 words: WHAT breakthrough, WHY matters, plain English>"}]
"""


def build_score_prompt(batch, topics):
    tlist = "\n".join(
        f"- {t['name']}{' [MILESTONE WATCH]' if t.get('milestone_only') else ''}: "
        f"{t.get('description','')}"
        for t in topics
    )
    lines = []
    for i, it in enumerate(batch):
        # Use full summary (up to 2000 chars) to give Claude more context for better summaries
        lines.append(
            f'[{i}] source={it["source"]}\nTITLE: {it["title"]}\n'
            f'ABSTRACT: {it["summary"][:2000]}'
        )
    return (
        f"{SCORE_INSTRUCTIONS}\nREADER'S TOPICS:\n{tlist}\n\nITEMS:\n"
        + "\n\n".join(lines)
    )


def get_env(name, default=None):
    """Safely get environment variable with informative error message."""
    value = os.environ.get(name, default)
    if value is None:
        raise ValueError(
            f"Environment variable {name} is not set. "
            f"See README for setup instructions."
        )
    return value


def parse_json_array(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: model returned objects not wrapped in an array (NDJSON or
    # run-together objects). Decode them one at a time so a stray format
    # doesn't cost us the whole batch.
    decoder = json.JSONDecoder()
    objs, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        objs.append(obj)
        idx = end
    return objs


def parse_json_object(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else json.loads(text)


def call_anthropic(prompt, model):
    api_key = get_env("LLM_API_KEY")
    headers = {"x-api-key": api_key,
               "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    # Identity-linked (workspace-scoped) API keys require the workspace id header.
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace_id:
        headers["anthropic-workspace-id"] = workspace_id
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json={"model": model or "claude-haiku-4-5-20251001", "max_tokens": 2000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    if not r.ok:
        # Surface the API error body so failures are diagnosable in logs
        print(f"  ! Anthropic API {r.status_code}: {r.text[:400]}", file=sys.stderr)
        r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def call_openai(prompt, model):
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {get_env('LLM_API_KEY')}",
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

def clean_summary(s):
    """Remove LaTeX, HTML entities, and special characters from summary."""
    if not s:
        return ""
    # Decode HTML entities first
    s = s.replace("&rsquo;", "'").replace("&lsquo;", "'").replace("&quot;", '"')
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Remove LaTeX math: $...$ and \(...\) (non-greedy)
    s = re.sub(r'\$.*?\$', '', s)
    s = re.sub(r'\\\([^)]*\\\)', '', s)
    # Remove LaTeX commands: \lesssim, \alpha, etc (uppercase and underscore variants)
    s = re.sub(r'\\[a-zA-Z_]+', '', s)
    # Remove LaTeX braces and content
    s = re.sub(r'\{[^}]*\}', '', s)
    # Remove remaining $ signs and math-like patterns
    s = re.sub(r'[\$_^]', '', s)
    # Clean up extra spaces and line breaks
    s = re.sub(r'[\n\r]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def validate_url(url):
    """Ensure URL uses safe protocol (http/https only)."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", ""):
        raise ValueError(f"Unsafe URL protocol: {parsed.scheme}")
    return url


def to_record(it):
    summary = it.get("llm_summary") or it["summary"][:280]
    title = it["title"]
    # Clean LaTeX from title too (e.g. NbSe$_2$ -> NbSe2)
    title = re.sub(r'\$.*?\$', '', title)
    title = re.sub(r'\\[a-zA-Z_]+\{[^}]*\}', '', title)
    title = re.sub(r'\\[a-zA-Z_]+', '', title)
    title = re.sub(r'\{[^}]*\}', '', title)
    title = re.sub(r'[\$_^]', '', title)
    title = title.strip()
    return {
        "id": item_key(it),
        "title": title,
        "url": validate_url(it["url"]),
        "source": it["source"],
        "source_type": it.get("source_type", ""),
        "topic": it["topic"],
        "stage": it["stage"],
        "significance": it["significance"],
        "summary": clean_summary(summary),
        "published": it["published"].isoformat() if it.get("published") else None,
        "added": utcnow().isoformat() + "Z",
    }


def semantic_similarity(t1, t2):
    """Quick heuristic to detect if two titles describe the same discovery."""
    # Normalize: lowercase, remove punctuation, split
    def norm(t):
        return set(re.sub(r'[^\w\s]', '', t.lower()).split())
    w1, w2 = norm(t1), norm(t2)
    if len(w1) < 2 or len(w2) < 2:
        return 0
    overlap = len(w1 & w2)
    # If 60%+ of shorter title words overlap, likely same discovery
    return overlap / min(len(w1), len(w2))

def _item_ts(r):
    """Best available timestamp for a feed item: published, else added."""
    return (r.get("published") or r.get("added") or "")


def expire_old(items, retention_days, keep_min):
    """Drop cards older than retention_days -- but only inside a topic that still
    has >= keep_min cards. Sparse topics keep everything so they never empty out.
    Within a topic, the newest keep_min cards are always retained regardless of age.
    """
    if not retention_days or retention_days <= 0:
        return items
    cutoff = (utcnow() - dt.timedelta(days=retention_days)).isoformat()
    by_topic = {}
    for it in items:
        by_topic.setdefault(it.get("topic"), []).append(it)
    keep = set()
    for topic, group in by_topic.items():
        group_sorted = sorted(group, key=_item_ts, reverse=True)
        # Always keep the newest keep_min in the topic.
        for it in group_sorted[:keep_min]:
            keep.add(id(it))
        # Beyond that, keep only cards newer than the cutoff.
        for it in group_sorted[keep_min:]:
            if _item_ts(it) >= cutoff:
                keep.add(id(it))
    return [it for it in items if id(it) in keep]


def merge_feed(site_dir, records, topics, cap=400, feed_cfg=None):
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
    existing_by_id = {r.get("id"): r for r in existing}
    added = []
    for r in records:
        prev = existing_by_id.get(r["id"])
        if prev:
            # Same item seen again: refresh scored fields (e.g. improved summary)
            # but keep the original discovery/added timestamps and position.
            prev.update({
                "topic": r["topic"], "stage": r["stage"],
                "significance": r["significance"], "summary": r["summary"],
                "url": r["url"], "source": r["source"],
            })
            continue
        added.append(r)
    # Semantic dedup: if new item is same topic + similar title to recent item, skip it
    for new in added[:]:
        for existing_item in existing[:50]:  # Check only recent 50 items
            if new["topic"] == existing_item["topic"]:
                if semantic_similarity(new["title"], existing_item["title"]) > 0.6:
                    added.remove(new)
                    break
    items = (added + existing)
    items.sort(key=lambda r: r.get("added", ""), reverse=True)
    fc = feed_cfg or {}
    items = expire_old(items, fc.get("retention_days", 0), fc.get("keep_min", 5))
    items = items[:cap]
    data = {
        "generated": utcnow().isoformat() + "Z",
        "topics": [t["name"] for t in topics],
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path, len(added)


def _render_rows(records):
    rows = ""
    for r in records:
        stars = "\u2605" * r["significance"] + "\u2606" * (5 - r["significance"])
        # Escape all dynamic content to prevent HTML injection
        safe_url = html.escape(r["url"], quote=True)
        safe_title = html.escape(r["title"])
        safe_topic = html.escape(r["topic"])
        safe_stage = html.escape(STAGE_LABEL.get(r["stage"], r["stage"]))
        safe_source = html.escape(r["source"])
        safe_summary = html.escape(r["summary"])
        rows += (f'<div style="margin:12px 0"><a href="{safe_url}">{safe_title}</a>'
                 f'<br><small>{safe_topic} \u00b7 {stars} \u00b7 '
                 f'{safe_stage} \u00b7 {safe_source}'
                 f'</small><br>{safe_summary}</div>')
    return rows


def _send_email(em, subject, plain, html_body, to_addrs):
    """Send one message. Raises if config/secret is incomplete."""
    for field in ["from_addr", "smtp_host", "username"]:
        if field not in em:
            raise ValueError(f"Email config missing required field: {field}")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from_addr"]
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    email_password = get_env("EMAIL_PASSWORD")
    with smtplib.SMTP(em["smtp_host"], em.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(em["username"], email_password)
        s.send_message(msg)
    return msg["To"]


def email_new(records, cfg):
    em = cfg["delivery"].get("email", {})
    if not em.get("enabled") or not records:
        return
    if "to_addrs" not in em:
        raise ValueError("Email config missing required field: to_addrs")
    to = _send_email(em, f"Research radar \u2014 {len(records)} new",
                     "New items in your radar.",
                     f"<div>{_render_rows(records)}</div>", em["to_addrs"])
    print(f"  emailed {len(records)} new items to {to}")


def email_alert(records, cfg):
    """Fire a high-priority alert for landmark results as soon as they land,
    independent of whether the regular digest email is enabled. Gated on
    `alert_significance` in config and the EMAIL_PASSWORD secret being present;
    otherwise it silently no-ops so a missing secret never breaks a run."""
    em = cfg["delivery"].get("email", {})
    threshold = em.get("alert_significance")
    if not threshold:
        return
    hits = [r for r in records if r.get("significance", 0) >= threshold]
    if not hits:
        return
    if not os.environ.get("EMAIL_PASSWORD"):
        print(f"  ! {len(hits)} breakthrough(s) >= {threshold}/5 but EMAIL_PASSWORD "
              f"unset -- alert skipped", file=sys.stderr)
        return
    to_addrs = em.get("alert_to_addrs") or em.get("to_addrs")
    if not to_addrs:
        print("  ! alert_significance set but no to_addrs/alert_to_addrs",
              file=sys.stderr)
        return
    n = len(hits)
    subject = (f"\U0001F6A8 Breakthrough alert \u2014 {n} landmark result"
               f"{'' if n == 1 else 's'} ({threshold}/5+)")
    plain = f"{n} landmark result(s) at {threshold}/5 or above just landed."
    try:
        to = _send_email(em, subject, plain,
                         f"<div><h2>Breakthrough alert</h2>{_render_rows(hits)}</div>",
                         to_addrs)
        print(f"  \U0001F6A8 alerted {n} breakthrough(s) to {to}")
    except Exception as exc:  # noqa: BLE001  -- never let an alert break the run
        print(f"  ! breakthrough alert failed: {exc}", file=sys.stderr)


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

    items = [it for it in items
             if within_lookback(it, item_lookback_days(it, topics, cfg["lookback_days"]))]
    print(f"  {len(items)} within lookback (default {cfg['lookback_days']}d, "
          f"deeper for sparse topics)")

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

    # Fetch full article text for prefiltered news items so the LLM can
    # summarize the actual discovery rather than a truncated blurb.
    if not force_mock:
        fresh = enrich_fulltext(fresh)

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
    path, n_added = merge_feed(site_dir, records, topics,
                                feed_cfg=cfg.get("feed"))
    print(f"  feed: +{n_added} new items -> {path}")
    email_new(records, cfg)
    email_alert(records, cfg)
    return path


# ---------------------------------------------------------------------------
# Add a topic: LLM generates keywords + sources, we validate, write config,
# and seed the feed so the new topic shows up right away.
# ---------------------------------------------------------------------------

TOPIC_GEN_INSTRUCTIONS = """You configure a science/tech news radar that surfaces
ONLY genuine breakthroughs and concrete events (first detections, confirmed results,
working prototypes, mission milestones) -- not routine papers or explainer articles.

Given a TOPIC NAME, return ONLY a JSON object (no prose, no markdown) shaped exactly:
{
  "name": "<clean display name, Title Case>",
  "description": "<one sentence, <=140 chars: what breakthroughs this topic covers>",
  "keywords": ["<8-14 distinctive lowercase phrases that appear in the titles/abstracts
                of real results in this field -- specific technical terms, named methods,
                instruments; NOT generic single words like 'energy' or 'space'>"],
  "min_stage": "discovery",
  "min_significance": 3,
  "arxiv_categories": ["<0-3 REAL arXiv category ids from the official taxonomy, e.g.
                        quant-ph, cond-mat.supr-con, astro-ph.CO, q-bio.PE; [] if none fit>"],
  "pubmed_queries": ["<0-2 PubMed search queries, ONLY for biology/medical/chemistry
                      topics; [] otherwise>"],
  "rss": ["<0-4 RSS/Atom feed URLs from reputable, well-known outlets or journals that
           actually cover this topic; only URLs you are confident exist verbatim>"]
}
Rules:
- keywords: what a real breakthrough headline in this field would literally contain.
- arxiv_categories: only ids you know are real (https://arxiv.org/category_taxonomy).
  When unsure, return [] rather than inventing one.
- rss: only feeds you are confident resolve at that exact URL; prefer major outlets.
  When unsure, return [] rather than guessing a broken URL.
- Return JSON only.
"""


def mock_topic_spec(name):
    """Keyless fallback used by --dry-run: no LLM, just a minimal spec."""
    base = name.strip().lower()
    kws = [base] + ([base[:-1]] if base.endswith("s") and len(base) > 3 else [])
    disp = name.strip()
    return {
        "name": disp.title() if disp.islower() else disp,
        "description": f"Breakthroughs and concrete events in {disp}.",
        "keywords": list(dict.fromkeys(k for k in kws if k)),
        "min_stage": "discovery",
        "min_significance": 3,
        "arxiv_categories": [],
        "pubmed_queries": [],
        "rss": [],
    }


def generate_topic_spec(name, cfg, force_mock=False):
    provider = "mock" if force_mock else cfg["llm"]["provider"]
    if provider == "mock" or provider not in PROVIDERS:
        return mock_topic_spec(name)
    prompt = f"{TOPIC_GEN_INSTRUCTIONS}\nTOPIC NAME: {name}\n"
    raw = PROVIDERS[provider](prompt, cfg["llm"]["model"])
    return parse_json_object(raw)


def validate_arxiv_category(cat):
    try:
        url = ("http://export.arxiv.org/api/query?"
               f"search_query=cat:{cat}&max_results=1")
        feed = feedparser.parse(url, agent=UA_STR)
        return len(feed.entries) > 0
    except Exception:  # noqa: BLE001
        return False


def validate_rss_feed(url):
    try:
        if urlparse(url).scheme not in ("http", "https"):
            return False
        feed = feedparser.parse(url, agent=UA_STR)
        return len(feed.entries) > 0
    except Exception:  # noqa: BLE001
        return False


def validate_pubmed_query(q):
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        r = requests.get(f"{base}/esearch.fcgi",
                         params={"db": "pubmed", "term": q,
                                 "retmode": "json", "retmax": 1},
                         headers=UA, timeout=30)
        return bool(r.json().get("esearchresult", {}).get("idlist"))
    except Exception:  # noqa: BLE001
        return False


def write_config_additions(path, topic_block, arxiv, pubmed, rss):
    """Append a topic + new sources to config.yaml, preserving comments/formatting."""
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap, CommentedSeq
    except ImportError as exc:  # noqa: BLE001
        raise SystemExit(
            "--add-topic needs ruamel.yaml to edit the config without dropping "
            "comments. Install it: pip install ruamel.yaml"
        ) from exc

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096
    # Match the config's existing block style: list items indented under their
    # key with the dash offset by 2 (e.g. "  - name" / "    - item").
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    with open(path, encoding="utf-8") as f:
        data = yaml_rt.load(f)

    data.setdefault("sources", {})
    src = data["sources"]

    def merge_list(key, additions, casefold=False):
        if not additions:
            return
        cur = src.get(key)
        if not isinstance(cur, list):
            cur = CommentedSeq()
            src[key] = cur
        seen = {(str(x).lower() if casefold else str(x)) for x in cur}
        for a in additions:
            k = a.lower() if casefold else a
            if k not in seen:
                cur.append(a)
                seen.add(k)

    merge_list("arxiv_categories", arxiv)
    merge_list("pubmed_queries", pubmed)
    merge_list("rss", rss, casefold=True)

    tb = CommentedMap()
    tb["name"] = topic_block["name"]
    tb["description"] = topic_block["description"]
    kw = CommentedSeq(topic_block["keywords"])
    kw.fa.set_flow_style()  # render as [a, b, c] like the existing topics
    tb["keywords"] = kw
    tb["min_stage"] = topic_block["min_stage"]
    tb["min_significance"] = topic_block["min_significance"]

    if not isinstance(data.get("topics"), list):
        data["topics"] = CommentedSeq()
    data["topics"].append(tb)

    with open(path, "w", encoding="utf-8") as f:
        yaml_rt.dump(data, f)


def backfill_topic(cfg, topic_name, force_mock=False):
    """Seed the feed with the just-added topic. Ignores the seen-store so items
    already fetched for other topics can surface under the new one."""
    topics = cfg.get("topics", [])
    topics_by_name = {t["name"]: t for t in topics}
    new_topic = topics_by_name.get(topic_name)
    if not new_topic:
        print(f"  ! could not find new topic '{topic_name}' after write")
        return
    con = open_store()
    print("Backfilling feed for new topic (fetching sources)...")
    items = gather(cfg)
    items = [it for it in items if within_lookback(it, cfg["lookback_days"])]

    fresh, seen_now = [], set()
    for it in items:
        k = item_key(it)
        if k in seen_now:
            continue
        seen_now.add(k)
        if candidate_topics(it, [new_topic]):
            fresh.append(it)
    print(f"  {len(fresh)} items match '{topic_name}' keywords")

    if not force_mock:
        fresh = enrich_fulltext(fresh)
    scored = score_items(fresh, [new_topic], cfg, force_mock=force_mock)
    passed = [it for it in scored if passes_thresholds(it, topics_by_name)]
    print(f"  {len(passed)} cleared thresholds for '{topic_name}'")

    for it in scored:
        mark_seen(con, it)
    con.commit()

    records = [to_record(it) for it in passed]
    site_dir = cfg["delivery"].get("site_dir", "./site")
    path, n_added = merge_feed(site_dir, records, topics,
                                feed_cfg=cfg.get("feed"))
    print(f"  feed: +{n_added} new items -> {path}")


def load_allowlist(cfg):
    """Curated topics.json used to gate what --add-topic will accept when
    RADAR_ENFORCE_ALLOWLIST is set (defense in depth behind the worker)."""
    site_dir = cfg["delivery"].get("site_dir", "./site")
    for p in (os.path.join(site_dir, "topics.json"),
              os.path.join("docs", "topics.json")):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return [str(t).strip() for t in json.load(f).get("topics", [])]
            except Exception:  # noqa: BLE001
                return []
    return []


def add_topic(cfg, config_path, name, force_mock=False):
    name = (name or "").strip()
    if not name:
        print("  ! --add-topic requires a topic name")
        return
    # Defense in depth: when enforced (the Action sets this), only exact
    # allowlist terms are accepted, so no arbitrary text reaches the LLM.
    if os.environ.get("RADAR_ENFORCE_ALLOWLIST"):
        allow = load_allowlist(cfg)
        match = next((a for a in allow if a.lower() == name.lower()), None)
        if not match:
            print(f"  ! '{name}' is not in the topic allowlist (docs/topics.json); "
                  f"refusing because RADAR_ENFORCE_ALLOWLIST is set")
            return
        name = match  # normalize to the canonical allowlist spelling
    existing = {t["name"].strip().lower() for t in cfg.get("topics", [])}
    if name.lower() in existing:
        print(f"  topic '{name}' already exists; nothing to do")
        return

    prov = "mock" if force_mock else cfg["llm"]["provider"]
    print(f"Generating topic spec for '{name}' via provider='{prov}'...")
    spec = generate_topic_spec(name, cfg, force_mock=force_mock)

    disp = (spec.get("name") or name).strip() or name
    keywords = [k.strip() for k in (spec.get("keywords") or []) if k and str(k).strip()]
    if not any(k.lower() == disp.lower() for k in keywords):
        keywords.insert(0, disp.lower())
    keywords = list(dict.fromkeys(k.lower() for k in keywords))
    description = (spec.get("description") or f"Breakthroughs in {disp}.").strip()
    min_stage = spec.get("min_stage", "discovery")
    if min_stage not in STAGE_ORDER:
        min_stage = "discovery"
    try:
        min_sig = max(1, min(5, int(spec.get("min_significance", 3))))
    except (TypeError, ValueError):
        min_sig = 3

    print(f"  description: {description}")
    print(f"  keywords ({len(keywords)}): {', '.join(keywords)}")

    new_arxiv, new_pubmed, new_rss = [], [], []
    if not force_mock:
        print("  validating suggested sources...")
        for cat in (spec.get("arxiv_categories") or []):
            cat = str(cat).strip()
            if not cat:
                continue
            if validate_arxiv_category(cat):
                new_arxiv.append(cat)
            else:
                print(f"    - skipped invalid arXiv category: {cat}")
        for q in (spec.get("pubmed_queries") or []):
            q = str(q).strip()
            if not q:
                continue
            if validate_pubmed_query(q):
                new_pubmed.append(q)
            else:
                print(f"    - skipped PubMed query (no results): {q}")
        for u in (spec.get("rss") or []):
            u = str(u).strip()
            if not u:
                continue
            if validate_rss_feed(u):
                new_rss.append(u)
            else:
                print(f"    - skipped unreachable RSS feed: {u}")
    print(f"  sources added: +{len(new_arxiv)} arXiv, "
          f"+{len(new_pubmed)} PubMed, +{len(new_rss)} RSS")

    topic_block = {
        "name": disp, "description": description, "keywords": keywords,
        "min_stage": min_stage, "min_significance": min_sig,
    }
    write_config_additions(config_path, topic_block, new_arxiv, new_pubmed, new_rss)
    print(f"  wrote {config_path} (+1 topic)")

    cfg2 = load_config(config_path)
    backfill_topic(cfg2, disp, force_mock=force_mock)
    print(f"Done. '{disp}' is now tracked and seeded into the feed.")


def main():
    ap = argparse.ArgumentParser(description="research-radar digest tool")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the keyless mock scorer (no LLM key needed)")
    ap.add_argument("--validate-feeds", action="store_true",
                    help="check that configured RSS feeds parse, then exit")
    ap.add_argument("--add-topic", metavar="NAME",
                    help="use the LLM to generate keywords + sources for a new "
                         "topic, add it to the config, and seed the feed")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.validate_feeds:
        validate_feeds(cfg)
        return
    if args.add_topic:
        add_topic(cfg, args.config, args.add_topic, force_mock=args.dry_run)
        return
    run(cfg, force_mock=args.dry_run)


if __name__ == "__main__":
    main()
