"""
Lobito Intelligence Group — Daily Brief Agent
Architecture:
  Step 1: Brave Search API — 10 targeted queries (8 news/context + 2 dedicated price queries)
           Fast JSON responses, no long connections, ~1s per query
  Step 2a: Claude Haiku — writes sections 1-4 (Price, Supply, Geo, Demand) from search results
  Step 2b: Claude Haiku — separate focused call writes Broker's Lens only
  Step 2c: Quality gate — checks for real price numbers and minimum length before sending
  Step 3:  Gmail — sends brief to inbox, flags quality failures in subject line
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
    """Call Brave News Search API. Returns list of {title, description, url, age, source}."""
    params = urllib.parse.urlencode({
        "q":          query,
        "count":      count,
        "freshness":  "pw",      # past week — pd too aggressive for mining news indexing lag
        "safesearch": "off",
    })
    url = f"https://api.search.brave.com/res/v1/news/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept":               "application/json",
        "Accept-Encoding":      "identity",   # no gzip — prevents silent JSON parse failures
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
        print(f"  Brave news search failed for '{query}': {e}")
        return []


def brave_web(query, count=5):
    """Call Brave Web Search API. Used for price data and broader context."""
    params = urllib.parse.urlencode({
        "q":          query,
        "count":      count,
        "freshness":  "pw",
        "safesearch": "off",
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept":               "application/json",
        "Accept-Encoding":      "identity",   # no gzip
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
    """Format search results as readable text block for Claude."""
    if not results:
        return f"[{label}: no results]\n"
    lines = [f"[{label}]"]
    for r in results:
        lines.append(f"- {r['title']}")
        if r["description"]:
            lines.append(f"  {r['description'][:220]}")
        if r["source"]:
            lines.append(f"  Source: {r['source']}  Age: {r['age']}")
    return "\n".join(lines) + "\n"


# ── CLAUDE API ────────────────────────────────────────────────────────────────
def claude_haiku(system, user_message, max_tokens=1500, attempt=0):
    """Call Claude Haiku — pure text generation, no tools."""
    body = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   [{"role": "user", "content": user_message}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
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
            print(f"  Rate limited — waiting {wait}s...")
            time.sleep(wait)
            return claude_haiku(system, user_message, max_tokens, attempt + 1)
        raise


# ── STEP 1: RESEARCH ──────────────────────────────────────────────────────────
print("Step 1: Searching live data via Brave...")

# Search queries — 10 total
# Queries 1-2: dedicated price data (web search, prioritise pages with $/t figures)
# Queries 3-10: news and context for the four brief sections
searches = [
    # ── PRICE QUERIES (web) ──────────────────────────────────────────────────
    ("cobalt hydroxide price per tonne 2026",              "web",  5),
    ("LME copper cash price per tonne today",              "web",  5),
    # ── CONTEXT QUERIES (news) ───────────────────────────────────────────────
    ("DRC cobalt export Congo mining 2026",                "news", 5),
    ("cobalt copper offtake deal supply agreement",        "news", 5),
    ("Lobito corridor Angola railway minerals",            "news", 4),
    ("critical minerals supply chain policy 2026",         "news", 4),
    ("Glencore cobalt copper mining",                      "news", 4),
    ("CMOC Trafigura Mercuria cobalt copper",              "news", 3),
    ("EV battery cobalt demand aerospace defence",         "news", 3),
    ("DRC mining quota reserve export controls",           "news", 3),
]

# Split results into price-specific and general buckets so the prompt can
# direct Claude to look in the right place for price figures.
price_research  = []
general_research = []
total_queries   = 0

for i, (query, search_type, count) in enumerate(searches):
    print(f"  [{search_type}] {query}")
    if search_type == "news":
        results = brave_news(query, count)
    else:
        results = brave_web(query, count)
    label = query[:55]
    formatted = format_results(results, label)
    if i < 2:                        # first two queries are price-dedicated
        price_research.append(formatted)
    else:
        general_research.append(formatted)
    total_queries += 1
    time.sleep(0.3)

price_text   = "\n".join(price_research)
general_text = "\n".join(general_research)
research_text = price_text + "\n" + general_text

print(f"  Done. {total_queries} queries, {len(research_text)} chars of research.")
print(f"  Price data sample:\n{price_text[:400]}\n...")


# ── STEP 2a: WRITE SECTIONS 1–4 ──────────────────────────────────────────────
print("\nStep 2a: Writing sections 1-4 with Claude Haiku...")

sections_system = """You are the editorial writer for Lobito Intelligence Group, a critical minerals intelligence publisher focused on cobalt and copper supply chains.

Write using ONLY the search results provided. Every fact, price, company name, and development must come from the results. Do not invent or add anything not present.

PRICE RULE — this is the most important rule in this prompt:
You MUST write a specific number in $/t or $/lb for both cobalt and copper.
Look first in the PRICE DATA section of the results — these are dedicated price queries.
If you find a number, use it exactly as written, with the source and date.
If you genuinely cannot find a specific number anywhere in the results, write:
  [price unavailable — check lme.com / tradingeconomics.com]
Never write a qualitative description (e.g. "prices remained firm") in place of a number.

Write in intelligent editorial prose. Name companies, volumes, and dates exactly as they appear."""

sections_prompt = f"""Today is {today}.

Write sections 1–4 of the Critical Minerals Intelligence Brief from these search results.
Prioritise the most recent items. Items older than 4 days are background context, not today's news.

=== PRICE DATA (dedicated price queries — check here first for $/t figures) ===
{price_text}

=== NEWS AND CONTEXT ===
{general_text}

Use this exact format — output sections 1–4 only, nothing else:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [specific $/t figure, source name, date] — [one sentence on the price driver]
Copper: [specific $/t or $/lb figure, source name, date] — [one sentence on the price driver]

SUPPLY CHAIN SIGNALS
[2–3 paragraphs. Named companies, specific volumes, specific dates from results only.
Include deals, MOUs, offtake agreements, supply partnerships, operational disruptions.]

GEOPOLITICAL RISK
[1–2 paragraphs. DRC policy, export controls, quota changes, logistics disruptions.
Named actors, specific countries, concrete timelines.]

DEMAND DRIVERS
[1 paragraph. EV, aerospace, defence, grid storage — named companies and programmes only.]"""

sections_text = claude_haiku(sections_system, sections_prompt, max_tokens=1200)
print(f"  Done. {len(sections_text)} chars.")


# ── STEP 2b: WRITE BROKER'S LENS ─────────────────────────────────────────────
print("\nStep 2b: Writing Broker's Lens with focused Claude Haiku call...")

lens_system = """You are a senior physical commodity broker with 20 years in cobalt and copper.
You read market intelligence and identify the ONE development that is not yet fully priced or acted on.

Your Broker's Lens is 3–4 sentences. Rules:
- Identify a single non-obvious signal from this week's news — not the most prominent headline, but the one with the most actionable consequence
- State specifically what a Western procurement director or junior producer should DO in the next 7 days
- Name counterparty types (e.g. "call your Trafigura or Mercuria contact"), specific actions, and timeframes
- Never write "it is worth noting", "the situation remains fluid", or any other filler
- Never summarise what is already obvious from the headline news
- Base everything strictly on the research provided — no invented facts"""

lens_prompt = f"""Today is {today}.

Here are the four brief sections already written:
{sections_text}

Here is the full research those sections were drawn from:
{research_text}

Now write the Broker's Lens paragraph only — 3–4 sentences, no heading, no preamble.
Identify the single most actionable non-obvious insight in this week's data.
What should a procurement director do differently THIS WEEK because of it?"""

brokers_lens = claude_haiku(lens_system, lens_prompt, max_tokens=300)
print(f"  Done. {len(brokers_lens)} chars.")


# ── STEP 2c: ASSEMBLE AND QUALITY GATE ───────────────────────────────────────
print("\nStep 2c: Assembling brief and running quality gate...")

footer = (
    "\n—\n"
    "Connecting Western buyers with responsible DRC and Copperbelt supply.\n"
    "Published weekdays. Forward to a colleague in procurement, supply chain, or commodities."
)

brief = f"{sections_text}\n\nBROKER'S LENS\n{brokers_lens}{footer}"

# Quality gate checks
has_price_number = any(marker in brief for marker in ["$/t", "$/lb", "/tonne", "$5", "$6", "$7", "$8", "$9", "$1", "$2", "$3", "$4"])
has_cobalt_price = "Cobalt:" in brief and "unavailable" not in brief.split("Cobalt:")[1][:80]
has_copper_price = "Copper:" in brief and "unavailable" not in brief.split("Copper:")[1][:80]
has_brokers_lens = len(brokers_lens) > 80
long_enough      = len(brief) > 900

quality_flags = []
if not has_price_number:
    quality_flags.append("NO PRICE NUMBERS FOUND")
if not has_cobalt_price:
    quality_flags.append("COBALT PRICE MISSING OR UNAVAILABLE")
if not has_copper_price:
    quality_flags.append("COPPER PRICE MISSING OR UNAVAILABLE")
if not has_brokers_lens:
    quality_flags.append("BROKER'S LENS TOO SHORT")
if not long_enough:
    quality_flags.append(f"BRIEF TOO SHORT ({len(brief)} chars)")

quality_ok = len(quality_flags) == 0

if quality_ok:
    print("  Quality gate: PASSED")
    subject = f"Brief ready — {today_short}"
else:
    print(f"  Quality gate: FAILED — {', '.join(quality_flags)}")
    warning_block = (
        f"⚠️  QUALITY FLAGS: {' | '.join(quality_flags)}\n"
        f"Review before publishing. Raw research appended below.\n"
        f"{'─' * 60}\n\n"
    )
    raw_appendix = f"\n\n{'─' * 60}\nRAW RESEARCH (for manual price lookup):\n\n{price_text}"
    brief = warning_block + brief + raw_appendix
    subject = f"⚠️ Brief needs review — {today_short}"

print(f"  Final brief: {len(brief)} chars.")


# ── STEP 3: SEND EMAIL ────────────────────────────────────────────────────────
print("\nStep 3: Sending email...")

msg = MIMEText(brief, "plain", "utf-8")
msg["Subject"] = subject
msg["From"]    = GMAIL_USER
msg["To"]      = GMAIL_USER

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.send_message(msg)

print(f"Done. Sent to {GMAIL_USER}")
print(f"Subject: {subject}")
print(f"\nBrief preview:\n{brief[:600]}...")
