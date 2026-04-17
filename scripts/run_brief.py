"""
Lobito Intelligence Group — Daily Brief Agent
Architecture:
  Step 1: Brave Search API — 10 targeted queries
           2 dedicated price queries (web) + 8 news/context queries
           Fast JSON, identity encoding (no gzip), freshness=pw
  Step 2a: Claude Haiku — writes sections 1-4 using the EXACT brief engine
           system prompt from critical_minerals_intelligence_brief.html
  Step 2b: Claude Haiku — separate focused call writes Broker's Lens only
           using the Broker's Lens rubric from the brief engine
  Step 2c: Quality gate — checks prices and length before sending
  Step 3:  Gmail — sends to inbox; subject flags quality failures
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
    params = urllib.parse.urlencode({
        "q": query, "count": count, "freshness": "pw", "safesearch": "off",
    })
    url = f"https://api.search.brave.com/res/v1/news/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-Subscription-Token": BRAVE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return [
                {
                    "title":       r.get("title", ""),
                    "description": r.get("description", ""),
                    "age":         r.get("age", ""),
                    "source":      r.get("meta_url", {}).get("hostname", ""),
                }
                for r in data.get("results", [])
            ]
    except Exception as e:
        print(f"  Brave news failed '{query}': {e}")
        return []


def brave_web(query, count=5):
    params = urllib.parse.urlencode({
        "q": query, "count": count, "freshness": "pw", "safesearch": "off",
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-Subscription-Token": BRAVE_API_KEY,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return [
                {
                    "title":       r.get("title", ""),
                    "description": r.get("description", ""),
                    "age":         r.get("age", ""),
                    "source":      r.get("meta_url", {}).get("hostname", ""),
                }
                for r in data.get("web", {}).get("results", [])
            ]
    except Exception as e:
        print(f"  Brave web failed '{query}': {e}")
        return []


def format_results(results, label):
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

searches = [
    # Price queries (web) — dedicated to returning $/t figures
    ("cobalt hydroxide price per tonne 2026",              "web",  5),
    ("LME copper cash price per tonne today",              "web",  5),
    # Context queries (news)
    ("DRC cobalt export Congo mining 2026",                "news", 5),
    ("cobalt copper offtake deal supply agreement",        "news", 5),
    ("Lobito corridor Angola railway minerals",            "news", 4),
    ("critical minerals supply chain policy 2026",         "news", 4),
    ("Glencore cobalt copper mining",                      "news", 4),
    ("CMOC Trafigura Mercuria cobalt copper",              "news", 3),
    ("EV battery cobalt demand aerospace defence",         "news", 3),
    ("DRC mining quota reserve export controls",           "news", 3),
]

price_research  = []
general_research = []
total_queries   = 0

for i, (query, search_type, count) in enumerate(searches):
    print(f"  [{search_type}] {query}")
    results = brave_web(query, count) if search_type == "web" else brave_news(query, count)
    formatted = format_results(results, query[:55])
    if i < 2:
        price_research.append(formatted)
    else:
        general_research.append(formatted)
    total_queries += 1
    time.sleep(0.3)

price_text    = "\n".join(price_research)
general_text  = "\n".join(general_research)
research_text = price_text + "\n" + general_text

print(f"  Done. {total_queries} queries, {len(research_text)} chars.")
print(f"  Price data sample:\n{price_text[:400]}\n...")


# ── STEP 2a: WRITE SECTIONS 1-4 ──────────────────────────────────────────────
# System prompt is the exact prompt used by the HTML brief engine artifact.
# Do not simplify — the specificity is what produces quality output.
print("\nStep 2a: Writing sections 1-4 (brief engine prompt)...")

SECTIONS_SYSTEM = """You are writing the daily Critical Minerals Intelligence Brief for Lobito Intelligence Group.

AUDIENCE: procurement directors and supply chain managers at European and North American manufacturers who need to diversify away from Chinese-controlled supply chains.

TONE: Intelligent, direct, data-grounded prose. Like a knowledgeable colleague, not a news wire. Never vague.

SECTIONS TO WRITE (in this order):

PRICE SNAPSHOT: Cobalt and copper current price, direction, and one-line market context. You MUST write a specific number in $/t or $/lb for each metal. Look in the PRICE DATA section first — these are dedicated price queries. Use the number exactly as found, with source and date. If you genuinely cannot find a specific number in the results, write [price unavailable — verify at lme.com]. Never substitute a qualitative description for a number.

SUPPLY CHAIN SIGNALS: 2-3 specific named developments — companies, countries, volumes where available. No generic statements. Read like a trader's morning note — factual, named, specific.

GEOPOLITICAL RISK: Specific policy, quota, sanctions, or conflict developments affecting supply routes or DRC/producer country access. Name the government agency or company responsible. Timeframes where known.

DEMAND DRIVERS: Specific end-use sector demand news — named companies, specific volumes, specific programmes. Never write "EV demand remains strong" or any other generic statement.

RULES:
— Never use filler phrases: "it is worth noting", "in conclusion", "it is important to", "in today's market"
— Always name the company or country behind any trend you mention
— Prices must come from the input — do not invent numbers
— Every news story must name its source and approximate date
— No markdown, no ## headers, no bullet points except in price snapshot
— Write sections 1-4 only — stop before Broker's Lens"""

sections_prompt = f"""Today is {today}.

Write sections 1-4 of the Critical Minerals Intelligence Brief from these search results.
Prioritise the most recent items. Items older than 5 days are background context — label them as such.

=== PRICE DATA (dedicated price queries — check here first for $/t figures) ===
{price_text}

=== NEWS AND CONTEXT ===
{general_text}

Use this exact header and structure:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [$/t figure · source · date] — [one sentence on price driver]
Copper: [$/t or $/lb figure · source · date] — [one sentence on price driver]

SUPPLY CHAIN SIGNALS
[2-3 paragraphs — named companies, specific volumes, specific dates]

GEOPOLITICAL RISK
[1-2 paragraphs — named actors, specific policy, concrete timeframes]

DEMAND DRIVERS
[1 paragraph — named companies and programmes only]"""

sections_text = claude_haiku(SECTIONS_SYSTEM, sections_prompt, max_tokens=1100)
print(f"  Done. {len(sections_text)} chars.")


# ── STEP 2b: WRITE BROKER'S LENS ─────────────────────────────────────────────
# Separate call so the Lens gets full context budget and a dedicated rubric.
print("\nStep 2b: Writing Broker's Lens (focused call)...")

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper.

Your task is to write the Broker's Lens paragraph for today's Critical Minerals Intelligence Brief.

The Broker's Lens answers one specific question:
Given today's specific developments, what should a Western procurement director or junior miner do DIFFERENTLY this week that they would NOT have done last week?

Rules:
— 3-4 sentences only
— The insight must be non-obvious — not the most prominent headline, but the development with the most actionable consequence that the market has not yet fully priced
— Name specific actions, specific counterparty types (e.g. "call your Trafigura contact"), and specific timeframes (days, not vague "soon")
— Never write "it is worth noting", "the situation remains fluid", "in conclusion", or any filler
— Never summarise what the other four sections already say — the Lens adds new perspective, not recap
— Base everything on the research provided — no invented facts
— Write the paragraph text only — no heading, no label, no preamble"""

lens_prompt = f"""Today is {today}.

Here are the four brief sections already written:
{sections_text}

Here is the full research:
{research_text}

Write the Broker's Lens paragraph only — 3-4 sentences. Identify the single most actionable non-obvious insight in today's data. What should a procurement director do differently this week because of it?"""

brokers_lens = claude_haiku(LENS_SYSTEM, lens_prompt, max_tokens=350)
print(f"  Done. {len(brokers_lens)} chars.")


# ── STEP 2c: ASSEMBLE AND QUALITY GATE ───────────────────────────────────────
print("\nStep 2c: Quality gate...")

footer = (
    "\n—\n"
    "Connecting Western buyers with responsible DRC and Copperbelt supply.\n"
    "Published weekdays. Forward to a colleague in procurement, supply chain, or commodities."
)

brief = f"{sections_text}\n\nBROKER'S LENS\n{brokers_lens}{footer}"

def snippet_after(text, marker, chars=100):
    idx = text.find(marker)
    return text[idx + len(marker): idx + len(marker) + chars] if idx != -1 else ""

has_price_number = any(x in brief for x in ["$/t", "$/lb", "/tonne", "$5", "$6", "$7", "$8", "$9", "$1", "$2", "$3", "$4"])
cobalt_ok        = bool(snippet_after(brief, "Cobalt:")) and "unavailable" not in snippet_after(brief, "Cobalt:")
copper_ok        = bool(snippet_after(brief, "Copper:")) and "unavailable" not in snippet_after(brief, "Copper:")
lens_ok          = len(brokers_lens) > 100
length_ok        = len(brief) > 900

flags = []
if not has_price_number: flags.append("NO PRICE NUMBERS FOUND")
if not cobalt_ok:        flags.append("COBALT PRICE MISSING/UNAVAILABLE")
if not copper_ok:        flags.append("COPPER PRICE MISSING/UNAVAILABLE")
if not lens_ok:          flags.append(f"BROKER'S LENS TOO SHORT ({len(brokers_lens)} chars)")
if not length_ok:        flags.append(f"BRIEF TOO SHORT ({len(brief)} chars)")

quality_ok = len(flags) == 0

if quality_ok:
    print("  Quality gate: PASSED")
    subject = f"Brief ready — {today_short}"
else:
    print(f"  Quality gate: FAILED — {', '.join(flags)}")
    warning = (
        f"WARNING: {' | '.join(flags)}\n"
        f"Edit before publishing. Raw price research appended.\n"
        f"{'─' * 60}\n\n"
    )
    raw_appendix = f"\n\n{'─' * 60}\nRAW PRICE RESEARCH:\n\n{price_text}"
    brief   = warning + brief + raw_appendix
    subject = f"[REVIEW] Brief — {today_short}"

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
