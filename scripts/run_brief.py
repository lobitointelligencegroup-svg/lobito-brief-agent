"""
Lobito Intelligence Group — Daily Brief Agent
Architecture:
  Step 1: Brave News Search API — 8 targeted queries, freshness=pd (past 24h)
           Fast JSON responses, no long connections, ~1s per query
  Step 2: Claude Haiku — synthesises raw search results into formatted brief
           No web search tool, pure writing, completes in ~5s
  Step 3: Gmail — sends draft to inbox
"""

import os
import time
import json
import smtplib
import urllib.request
import urllib.parse
from datetime import datetime
from email.mime.text import MIMEText

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BRAVE_API_KEY     = os.environ["BRAVE_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_PASS        = os.environ["GMAIL_APP_PASSWORD"]

today       = datetime.now().strftime("%A %-d %B %Y")
today_short = datetime.now().strftime("%a %-d %b %Y")

# ── BRAVE SEARCH ──────────────────────────────────────────────────────────────
def brave_news(query, count=5):
    """Call Brave News Search API. Returns list of {title, description, url, age}."""
    params = urllib.parse.urlencode({
        "q":         query,
        "count":     count,
        "freshness": "pw",   # past week — pd (past day) too aggressive for mining news
        "safesearch":"off",
    })
    url = f"https://api.search.brave.com/res/v1/news/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept":               "application/json",
        "Accept-Encoding":      "identity",
        "X-Subscription-Token": BRAVE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("results", [])
            return [
                {
                    "title":       r.get("title", ""),
                    "description": r.get("description", ""),
                    "url":         r.get("url", ""),
                    "age":         r.get("age", ""),
                    "source":      r.get("meta_url", {}).get("hostname", ""),
                }
                for r in results
            ]
    except Exception as e:
        print(f"  Brave search failed for '{query}': {e}")
        return []


def brave_web(query, count=3):
    """Call Brave Web Search API for price verification."""
    params = urllib.parse.urlencode({
        "q":         query,
        "count":     count,
        "freshness": "pw",
        "safesearch":"off",
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept":               "application/json",
        "Accept-Encoding":      "identity",
        "X-Subscription-Token": BRAVE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("web", {}).get("results", [])
            return [
                {
                    "title":       r.get("title", ""),
                    "description": r.get("description", ""),
                    "url":         r.get("url", ""),
                    "age":         r.get("age", ""),
                    "source":      r.get("meta_url", {}).get("hostname", ""),
                }
                for r in results
            ]
    except Exception as e:
        print(f"  Brave web search failed for '{query}': {e}")
        return []


def format_results(results, label):
    """Format search results as readable text for Claude."""
    if not results:
        return f"[{label}: no results found in past 24h]\n"
    lines = [f"[{label}]"]
    for r in results:
        lines.append(f"- {r['title']}")
        if r['description']:
            lines.append(f"  {r['description'][:200]}")
        if r['source']:
            lines.append(f"  Source: {r['source']}  Age: {r['age']}")
    return "\n".join(lines) + "\n"


# ── CLAUDE API ────────────────────────────────────────────────────────────────
def claude_haiku(system, user_message, attempt=0):
    """Call Claude Haiku — no web search, pure text generation."""
    body = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "system":     system,
        "messages":   [{"role": "user", "content": user_message}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":        ANTHROPIC_API_KEY,
            "anthropic-version":"2023-06-01",
            "content-type":     "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return "\n".join(
                b["text"] for b in data.get("content", []) if b.get("type") == "text"
            ).strip()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"  Claude HTTP {e.code}: {body_text}")
        if e.code == 429 and attempt < 3:
            wait = (2 ** attempt) * 15
            print(f"  Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            return claude_haiku(system, user_message, attempt + 1)
        raise


# ── STEP 1: RESEARCH ──────────────────────────────────────────────────────────
print("Step 1: Searching live data via Brave...")

# DEBUG — print raw response from first query to diagnose empty results
print("  DEBUG: Testing Brave API with first query...")
test_params = urllib.parse.urlencode({
    "q": "cobalt price",
    "count": 3,
    "freshness": "pw",
    "safesearch": "off",
})
test_url = f"https://api.search.brave.com/res/v1/web/search?{test_params}"
test_req = urllib.request.Request(test_url, headers={
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "X-Subscription-Token": BRAVE_API_KEY,
})
try:
    with urllib.request.urlopen(test_req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        print(f"  HTTP status: {resp.status}")
        print(f"  Response (first 1000 chars): {raw[:1000]}")
except Exception as e:
    print(f"  DEBUG ERROR: {e}")

# 8 targeted searches — designed to capture everything Perplexity Computer found
searches = [
    ("cobalt price",                                               "web",  5),
    ("copper price LME",                                           "web",  5),
    ("DRC cobalt export Congo mining",                             "news", 5),
    ("cobalt copper offtake deal supply agreement",                "news", 5),
    ("Lobito corridor Angola railway minerals",                    "news", 4),
    ("critical minerals supply chain policy 2026",                 "news", 4),
    ("Glencore cobalt copper mining",                              "news", 4),
    ("CMOC Trafigura Mercuria cobalt copper",                      "news", 3),
]

all_research = []
total_queries = 0

for query, search_type, count in searches:
    print(f"  [{search_type}] {query}")
    if search_type == "news":
        results = brave_news(query, count)
    else:
        results = brave_web(query, count)
    label = query[:50]
    all_research.append(format_results(results, label))
    total_queries += 1
    time.sleep(0.3)  # polite delay, well within 50 req/s limit

research_text = "\n".join(all_research)
print(f"  Done. {total_queries} queries, {len(research_text)} characters of research.")
print(f"  Sample:\n{research_text[:400]}\n...")


# ── STEP 2: WRITE BRIEF ───────────────────────────────────────────────────────
print("\nStep 2: Writing brief with Claude Haiku...")

brief_system = """You are the editorial writer for Lobito Intelligence Group, a critical minerals intelligence publisher focused on cobalt and copper supply chains.

Write the daily brief using ONLY the search results provided. Every fact, price, company name, and development must come from the search results. Do not invent or assume anything not present in the results.

Write in intelligent editorial prose. Be specific — name companies, volumes, dates, and prices exactly as they appear in the results. The Broker's Lens must contain a non-obvious, actionable insight that a procurement director could act on today."""

brief_prompt = f"""Today is {today}.

Write the Critical Minerals Intelligence Brief from these search results.
Results are from the past week — prioritise the most recent items.
For the Price Snapshot, use the most recent price figures you can find in the results.
If an item appears older than 3 days, note it as background context rather than today's news.

{research_text}

Use this exact format — no deviations:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [most recent price from results, source, date] - [one sentence on drivers]
Copper: [most recent price from results, source, date] - [one sentence on drivers]

SUPPLY CHAIN SIGNALS
[2-3 paragraphs. Named companies, specific volumes, specific dates from results only. Include any deals, MOUs, offtake agreements, or supply partnerships found.]

GEOPOLITICAL RISK
[1-2 paragraphs. DRC policy, export controls, quota developments, logistics disruptions from results only. Named actors and specific timelines.]

DEMAND DRIVERS
[1 paragraph. EV, aerospace, defence, grid — named companies and programmes from results only.]

BROKER'S LENS
[3-4 sentences. Based strictly on today's results: what should a Western procurement director or junior miner do differently THIS WEEK? Name specific actions, counterparty types, and timeframes. Never use "it is worth noting" or "the situation remains fluid".]

-
Connecting Western buyers with responsible DRC and Copperbelt supply.
Published weekdays. Forward to a colleague in procurement, supply chain, or commodities."""

brief = claude_haiku(brief_system, brief_prompt)
print(f"  Done. {len(brief)} characters.")

if len(brief) < 100:
    brief = f"Brief generation failed on {today}.\n\nRaw research:\n{research_text[:2000]}"


# ── STEP 3: SEND EMAIL ────────────────────────────────────────────────────────
print("\nStep 3: Sending email...")

msg = MIMEText(brief, "plain", "utf-8")
msg["Subject"] = f"Brief ready - {today_short}"
msg["From"]    = GMAIL_USER
msg["To"]      = GMAIL_USER

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.send_message(msg)

print(f"Done. Sent to {GMAIL_USER}")
print(f"\nBrief preview:\n{brief[:500]}...")
