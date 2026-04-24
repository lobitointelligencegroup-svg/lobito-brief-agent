"""
Lobito Intelligence Group — Daily Brief Agent

Architecture:
  Step 1a: Claude Sonnet + web_search tool — fetches ONLY cobalt and copper
           prices from live sources. Short task, max_tokens=250, ~8s.
           Runs on Anthropic infrastructure so bypasses GitHub egress restrictions.
  Step 1b: Brave Search API — news queries scoped to past 24h (freshness=pd),
           with day-of-week rotation to ensure each brief has a different
           editorial focus. Background context queries use freshness=pw.
  Step 1c: Freshness check — Claude reviews retrieved articles and flags
           how many are genuinely new vs. background context. If fewer than
           3 fresh stories exist, a second-pass search widens the net.
  Step 2a: Claude Haiku (no tools) — writes sections 1-4 using brief engine
           prompt. Explicitly told to ignore stories listed in used_stories.json.
  Step 2b: Claude Haiku (no tools) — writes Broker's Lens as separate focused call.
  Step 2c: Quality gate — validates prices and length before sending.
  Step 2d: Saves today's used story headlines to used_stories.json so tomorrow's
           run can explicitly avoid repeating them.
  Step 3:  Gmail — sends to inbox; subject line flags quality failures.

Freshness strategy:
  - Most queries use freshness=pd (past day) to force genuinely new material.
  - 2 background/context queries use freshness=pw (past week) for structural
    context only — the writing prompt deprioritises these.
  - Search queries rotate by day of week so each brief has a different focus:
      Monday    — supply deals and offtake agreements
      Tuesday   — geopolitics and government policy
      Wednesday — demand, EV, battery and downstream
      Thursday  — junior miners, M&A, exploration
      Friday    — logistics, corridors, infrastructure

Story deduplication:
  - used_stories.json stores the titles of stories used in the previous brief.
  - The writing prompt explicitly lists these and instructs Claude to skip them
    as primary sources (they can appear as background context only).
  - The file is updated at the end of each successful run.
  - If the file does not exist (first run), deduplication is skipped gracefully.
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

# Path for story deduplication file — stored in repo root alongside the script
USED_STORIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "used_stories.json")

now         = datetime.now()
today       = now.strftime("%A %-d %B %Y")
today_short = now.strftime("%a %-d %b %Y")
day_of_week = now.strftime("%A")  # Monday, Tuesday, ... Friday


# ── DAY-OF-WEEK SEARCH ROTATION ───────────────────────────────────────────────
# Each day has a primary focus area (freshness=pd) plus shared base queries.
# This ensures the editorial angle shifts daily even when macro news is thin.

DAY_FOCUS_QUERIES = {
    "Monday": [
        ("DRC cobalt copper offtake deal signed",              "news", 5, "pd"),
        ("cobalt copper supply agreement contract 2026",       "news", 4, "pd"),
        ("Glencore CMOC Trafigura supply deal",                "news", 4, "pd"),
    ],
    "Tuesday": [
        ("DRC Congo mining government policy regulation",      "news", 5, "pd"),
        ("US China critical minerals sanctions trade",         "news", 4, "pd"),
        ("cobalt copper export controls Africa",               "news", 4, "pd"),
    ],
    "Wednesday": [
        ("EV battery cobalt demand manufacturer 2026",         "news", 5, "pd"),
        ("lithium cobalt battery supply chain downstream",     "news", 4, "pd"),
        ("electric vehicle minerals procurement",              "news", 4, "pd"),
    ],
    "Thursday": [
        ("cobalt copper junior miner acquisition exploration", "news", 5, "pd"),
        ("DRC copper cobalt M&A investment deal",              "news", 4, "pd"),
        ("mining company fundraise equity cobalt copper",      "news", 4, "pd"),
    ],
    "Friday": [
        ("Lobito corridor Angola railway minerals",            "news", 5, "pd"),
        ("DRC copper export logistics port corridor",          "news", 4, "pd"),
        ("Copperbelt transport infrastructure update",         "news", 4, "pd"),
    ],
}

# Fallback if script runs on a weekend or key is missing
DEFAULT_FOCUS_QUERIES = [
    ("DRC cobalt copper mining news",                          "news", 5, "pd"),
    ("cobalt copper supply chain 2026",                        "news", 4, "pd"),
    ("critical minerals procurement Western buyers",           "news", 4, "pd"),
]

# Base queries run every day — background/structural context only (freshness=pw)
# These are deprioritised in the writing prompt so the model treats them as
# context rather than today's news.
BASE_CONTEXT_QUERIES = [
    ("CMOC Zijin Huayou DRC cobalt copper operations",         "news", 3, "pw"),
    ("DRC mining quota artisanal regulation Gecamines",        "news", 3, "pw"),
]

# Additional daily fresh queries that apply every day regardless of focus
DAILY_FRESH_QUERIES = [
    ("cobalt copper price LME market today",                   "news", 3, "pd"),
    ("DRC Congo mining news today",                            "news", 4, "pd"),
]


# ── CLAUDE API (base) ─────────────────────────────────────────────────────────
def claude_call(model, system, user_message, tools=None, max_tokens=1500, attempt=0):
    """
    Call Claude API. Pass tools=[] for web_search enabled calls.
    Handles tool_use loop for web_search: keeps calling until Claude
    returns a final text response with no more tool calls.
    """
    messages = [{"role": "user", "content": user_message}]
    body_dict = {
        "model":      model,
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   messages,
    }
    if tools:
        body_dict["tools"] = tools

    for turn in range(6):  # max 6 tool-use rounds
        body = json.dumps(body_dict).encode()
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
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            print(f"  Claude HTTP {e.code}: {body_text[:200]}")
            if e.code == 429 and attempt < 3:
                wait = (2 ** attempt) * 15
                print(f"  Rate limited — waiting {wait}s...")
                time.sleep(wait)
                return claude_call(model, system, user_message, tools, max_tokens, attempt + 1)
            raise

        stop_reason = data.get("stop_reason", "")
        content     = data.get("content", [])

        if stop_reason == "end_turn" or not tools:
            return "\n".join(
                b["text"] for b in content if b.get("type") == "text"
            ).strip()

        if stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block["id"],
                        "content":     block.get("content", ""),
                    })
            body_dict["messages"] = body_dict["messages"] + [
                {"role": "assistant", "content": content},
                {"role": "user",      "content": tool_results},
            ]
            continue

        return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

    return ""  # exceeded turns


def claude_haiku(system, user_message, max_tokens=1500):
    """Shorthand for no-tools Haiku calls."""
    return claude_call(
        model="claude-haiku-4-5-20251001",
        system=system,
        user_message=user_message,
        tools=None,
        max_tokens=max_tokens,
    )


# ── STEP 1a: LIVE PRICES via Claude web_search ───────────────────────────────
print("Step 1a: Fetching live prices via Claude web_search...")

PRICE_SYSTEM = """You are a commodity price data retrieval agent. Your only job is to find today's market price for cobalt and copper and return them in a precise structured format. Nothing else."""

PRICE_PROMPT = f"""Today is {today}.

You must find prices dated as close to {today} as possible. If the market is closed today, use the most recent available price and record its actual date accurately — do not guess or omit the date.

COBALT — search in this order until you find a price:
1. Search: cobalt price per tonne {today}
2. Search: cobalt hydroxide price USD tonne {now.strftime("%B %Y")}
3. Search: cobalt LME price today

Use the most recent number from: Fastmarkets, LME, Trading Economics, Benchmark Mineral Intelligence, or Metal Bulletin. Record the exact date shown on the page — do not write today's date if the page shows a different date.

COPPER — search in this order until you find a price:
1. Search: LME copper cash price {today}
2. Search: LME copper price per tonne {now.strftime("%B %Y")}
3. Search: copper price USD tonne today

Use the official LME cash settlement or spot price in USD/tonne from LME.com, Trading Economics, or Fastmarkets. Record the exact date shown — do not substitute today's date.

Return ONLY these two lines, nothing else:

COBALT: $[price]/t · [source] · [exact date from source]
COPPER: $[price]/t · [source] · [exact date from source]

Example:
COBALT: $56,290/t · Trading Economics · 22 Apr 2026
COPPER: $13,197/t · LME cash · 23 Apr 2026

If you cannot find a number after all three searches, write:
COBALT: UNAVAILABLE
COPPER: UNAVAILABLE

Do not write anything other than these two lines. Do not add explanation."""

WEB_SEARCH_TOOL = [{
    "type": "web_search_20250305",
    "name": "web_search",
}]

price_text = claude_call(
    model="claude-sonnet-4-6",
    system=PRICE_SYSTEM,
    user_message=PRICE_PROMPT,
    tools=WEB_SEARCH_TOOL,
    max_tokens=250,
)

print(f"  Prices retrieved: {price_text}")


# ── STEP 1b: NEWS CONTEXT via Brave Search ────────────────────────────────────
print(f"\nStep 1b: Fetching news context via Brave Search (day focus: {day_of_week})...")


def parse_age_to_date(age_str):
    """
    Convert Brave Search relative age strings to absolute dates.
    Brave returns values like: "33 minutes ago", "7 hours ago", "1 day ago",
    "2 days ago", "3 weeks ago". We convert these to "DD Mon YYYY" format.
    Falls back to the raw string if parsing fails.
    """
    if not age_str:
        return ""
    import re
    age_lower = age_str.lower().strip()
    # Already looks like a real date — return as-is
    if re.search(r'\d{4}', age_lower):
        return age_str
    match = re.match(r'(\d+)\s+(minute|hour|day|week|month)s?\s+ago', age_lower)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        from datetime import timedelta
        delta_map = {
            "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),
            "day":    timedelta(days=n),
            "week":   timedelta(weeks=n),
            "month":  timedelta(days=n * 30),
        }
        article_date = now - delta_map.get(unit, timedelta(0))
        return article_date.strftime("%-d %b %Y")
    return age_str  # fallback: return whatever Brave gave us


def brave_search(query, count=5, freshness="pd", search_type="news"):
    """
    Unified Brave search function.
    search_type: "news" uses the news endpoint; anything else uses web endpoint.
    freshness: "pd" = past day, "pw" = past week.
    """
    params = urllib.parse.urlencode({
        "q":          query,
        "count":      count,
        "freshness":  freshness,
        "safesearch": "off",
    })

    if search_type == "news":
        url = f"https://api.search.brave.com/res/v1/news/search?{params}"
    else:
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "Accept":               "application/json",
            "Accept-Encoding":      "identity",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if search_type == "news":
                results = data.get("results", [])
            else:
                results = data.get("web", {}).get("results", [])
            return [
                {
                    "title":       r.get("title", ""),
                    "description": r.get("description", ""),
                    "age":         parse_age_to_date(r.get("age", "")),
                    "source":      r.get("meta_url", {}).get("hostname", ""),
                    "freshness":   freshness,
                }
                for r in results
            ]
    except Exception as e:
        print(f"  Brave search failed '{query}': {e}")
        return []


def fmt(results, label, is_background=False):
    """Format search results for inclusion in the prompt."""
    if not results:
        return f"[{label}: no results]\n"
    tag = " [BACKGROUND CONTEXT — do not lead with these stories]" if is_background else " [TODAY'S NEWS — prioritise these]"
    lines = [f"[{label}{tag}]"]
    for r in results:
        lines.append(f"- {r['title']}")
        if r["description"]:
            lines.append(f"  {r['description'][:220]}")
        if r["source"]:
            lines.append(f"  Source: {r['source']}  Age: {r['age']}")
    return "\n".join(lines) + "\n"


# Load yesterday's used stories for deduplication
used_yesterday = []
if os.path.exists(USED_STORIES_PATH):
    try:
        with open(USED_STORIES_PATH, "r") as f:
            used_yesterday = json.load(f)
        print(f"  Loaded {len(used_yesterday)} stories used yesterday for deduplication.")
    except Exception as e:
        print(f"  Could not load used_stories.json: {e} — skipping deduplication.")
else:
    print("  No used_stories.json found — first run, skipping deduplication.")

# Build query list for today
focus_queries   = DAY_FOCUS_QUERIES.get(day_of_week, DEFAULT_FOCUS_QUERIES)
all_queries     = DAILY_FRESH_QUERIES + focus_queries + BASE_CONTEXT_QUERIES

news_parts      = []
all_results     = []  # flat list of all retrieved articles for freshness check

for query, search_type, count, freshness in all_queries:
    is_background = (freshness == "pw")
    print(f"  [{'bg' if is_background else 'new'}/{freshness}] {query}")
    results = brave_search(query, count=count, freshness=freshness, search_type=search_type)
    news_parts.append(fmt(results, query[:55], is_background=is_background))
    all_results.extend(results)
    time.sleep(0.3)

news_text = "\n".join(news_parts)
print(f"  Done. {len(news_text)} chars of news context, {len(all_results)} articles total.")


# ── STEP 1c: FRESHNESS CHECK ──────────────────────────────────────────────────
# Ask Claude to count genuinely new stories. If fewer than 3, run a second-pass
# search with broader terms to find additional fresh material.
print("\nStep 1c: Freshness check...")

FRESHNESS_SYSTEM = """You are a news editor checking whether a set of search results contains enough genuinely new stories to write a daily newsletter that is distinct from yesterday.

A story is "genuinely new" if it reports on an event, announcement, deal, data release, or development that occurred within the past 48 hours and is not a rephrasing of a story that ran the day before.

You will be given:
1. The headlines used in yesterday's brief (may be empty on first run).
2. Today's search results.

Return ONLY a JSON object in this exact format — no preamble, no explanation:
{"new_story_count": <integer>, "fresh_titles": [<list of titles judged genuinely new>]}"""

freshness_prompt = f"""Yesterday's used story titles:
{json.dumps(used_yesterday, indent=2) if used_yesterday else "[]"}

Today's search results:
{news_text[:3000]}

Count how many stories are genuinely new (not repeats of yesterday). Return JSON only."""

try:
    freshness_raw = claude_haiku(FRESHNESS_SYSTEM, freshness_prompt, max_tokens=400)
    # Strip any accidental markdown fences
    freshness_clean = freshness_raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    freshness_data  = json.loads(freshness_clean)
    new_story_count = freshness_data.get("new_story_count", 0)
    fresh_titles    = freshness_data.get("fresh_titles", [])
    print(f"  Freshness check: {new_story_count} genuinely new stories found.")
except Exception as e:
    print(f"  Freshness check parse error: {e} — assuming sufficient fresh content.")
    new_story_count = 3
    fresh_titles    = []

# Second-pass search if fresh content is thin
if new_story_count < 3:
    print("  Fewer than 3 fresh stories — running second-pass search with broader terms...")
    second_pass_queries = [
        ("cobalt copper mining news",                          "news", 5, "pd"),
        ("DRC Congo minerals today",                           "news", 5, "pd"),
        ("critical minerals supply chain news",                "news", 4, "pd"),
    ]
    for query, search_type, count, freshness in second_pass_queries:
        print(f"  [second-pass] {query}")
        results = brave_search(query, count=count, freshness=freshness, search_type=search_type)
        news_parts.append(fmt(results, f"[SECOND PASS] {query[:45]}", is_background=False))
        all_results.extend(results)
        time.sleep(0.3)
    news_text = "\n".join(news_parts)
    print(f"  Second pass done. Total news context: {len(news_text)} chars.")
else:
    print("  Sufficient fresh content — no second pass needed.")


# ── BUILD DEDUPLICATION BLOCK FOR PROMPT ─────────────────────────────────────
if used_yesterday:
    dedup_block = (
        "=== STORIES ALREADY COVERED YESTERDAY — DO NOT USE AS PRIMARY SOURCE ===\n"
        "The following headlines appeared in yesterday's brief. Do not write paragraphs\n"
        "that are primarily based on these stories. They may appear as background context\n"
        "only if essential, but every section must lead with something genuinely new.\n\n"
        + "\n".join(f"- {t}" for t in used_yesterday)
        + "\n"
    )
else:
    dedup_block = ""


# ── STEP 2a: WRITE SECTIONS 1-4 ──────────────────────────────────────────────
print("\nStep 2a: Writing sections 1-4...")

SECTIONS_SYSTEM = """You are writing the daily Critical Minerals Intelligence Brief for Lobito Intelligence Group.

AUDIENCE: procurement directors and supply chain managers at European and North American manufacturers who need to diversify away from Chinese-controlled supply chains. These are busy, senior people. Every sentence must earn its place — if it cannot be acted on or traded on, cut it.

TONE: Intelligent, direct, data-grounded prose. Like a knowledgeable colleague who has already read everything so you do not have to. Never vague. Never a press release.

PRICE SNAPSHOT RULES:
Use the exact prices from LIVE PRICES — do not modify, round, or replace them.
Format: $XX,XXX/t · Source · Date — one sentence identifying the specific driver today, not a generic market comment.
If a price is marked UNAVAILABLE write exactly: [price unavailable — verify at lme.com]

NEWS PRIORITISATION:
— Articles tagged [TODAY'S NEWS] are your only permitted primary sources. Every paragraph must lead with one of these.
— Articles tagged [BACKGROUND CONTEXT] may add one supporting sentence per paragraph — never the lead.
— Stories listed under STORIES ALREADY COVERED YESTERDAY must not appear as the subject of any paragraph under any circumstances.

SUPPLY CHAIN SIGNALS — 2 to 3 paragraphs, each built on a single named news event:
Each paragraph must contain: (1) a named company or named government body, (2) a specific action or number, (3) a source and date, (4) one sentence on what this means for a procurement director.
FORBIDDEN in this section: any sentence that could apply to any week, not just this one. If you cannot name the company and the specific action from today's news, write [INSUFFICIENT DATA — editor to complete] and move on. Do not invent generalisations to fill space.

GEOPOLITICAL RISK — 1 to 2 paragraphs:
Name the specific actor, the specific policy or event, and the concrete timeframe. No paragraph may open with a country name alone — open with the actor and the action. If the risk is speculative, say so explicitly and name who said it and when.

DEMAND DRIVERS — 1 paragraph only:
Name the specific company, the specific programme or contract, and the specific volume or timeline. FORBIDDEN: "EV demand remains strong", "demand continues to grow", "manufacturers are increasingly focused on", or any sentence that contains no named buyer. If today's news contains no named demand story, write [INSUFFICIENT DATA — editor to complete] rather than fabricating a generalisation.

ABSOLUTE RULES:
— No markdown, no bold, no ## headers, no bullet points anywhere except the two price snapshot lines
— No filler phrases: "it is worth noting", "in conclusion", "it is important to", "against this backdrop", "in today's market", "remains fluid"
— Every paragraph must cite its source publication and approximate date in parentheses
— Write sections 1 through 4 only — stop after DEMAND DRIVERS — do not write BROKER'S LENS under any circumstances"""

sections_prompt = f"""Today is {today}.

=== LIVE PRICES (use these exact numbers — do not modify) ===
{price_text}

{dedup_block}
=== TODAY'S NEWS AND CONTEXT ===
{news_text}

Write sections 1-4 using this exact structure:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [paste cobalt line from LIVE PRICES above, then add one sentence on driver]
Copper: [paste copper line from LIVE PRICES above, then add one sentence on driver]

SUPPLY CHAIN SIGNALS
[2-3 paragraphs — lead with stories tagged TODAY'S NEWS only]

GEOPOLITICAL RISK
[1-2 paragraphs — lead with stories tagged TODAY'S NEWS only]

DEMAND DRIVERS
[1 paragraph — lead with stories tagged TODAY'S NEWS only]"""

sections_text = claude_haiku(SECTIONS_SYSTEM, sections_prompt, max_tokens=1100)
print(f"  Done. {len(sections_text)} chars.")


# ── STEP 2b: WRITE BROKER'S LENS ─────────────────────────────────────────────
print("\nStep 2b: Writing Broker's Lens...")

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper markets.

Write a single paragraph of 3 to 4 sentences for the Broker's Lens section of today's Critical Minerals Intelligence Brief.

The one question to answer: given today's specific developments, what should a Western procurement director or junior miner do differently THIS week that they would not have done LAST week?

Rules:
— 3 to 4 sentences only — no more
— The insight must be non-obvious: ignore the most prominent headline; find the development with the most actionable consequence that the market has not yet fully priced
— Name specific actions ("call your Nouryon acid supplier today"), specific counterparty types ("your Trafigura trading desk"), and hard timeframes ("before Friday close", "within 48 hours") — never "soon" or "in the coming weeks"
— Never open with "Western procurement directors should" — open with the action or the risk
— Never summarise what sections 1 through 4 already say — the Broker's Lens must add a perspective that does not appear anywhere else in the brief
— No filler: "it is worth noting", "the situation remains fluid", "in conclusion", "against this backdrop"
— Base every claim on the research provided — no invented facts or companies
— CRITICAL: return the paragraph text only — no label, no heading, no bold text, no "BROKER'S LENS" prefix — just the raw paragraph"""

lens_prompt = f"""Today is {today}.

Sections 1-4 already written:
{sections_text}

Full research:
{price_text}

{news_text}

Write the Broker's Lens — 3-4 sentences. What is the single most actionable non-obvious insight in today's data?"""

brokers_lens = claude_haiku(LENS_SYSTEM, lens_prompt, max_tokens=350)
print(f"  Done. {len(brokers_lens)} chars.")


# ── STEP 2c: ASSEMBLE + QUALITY GATE ─────────────────────────────────────────
print("\nStep 2c: Quality gate...")

footer = (
    "\n—\n"
    "Connecting Western buyers with responsible DRC and Copperbelt supply.\n"
    "Published weekdays. Forward to a colleague in procurement, supply chain, or commodities."
)

brief = f"{sections_text}\n\nBROKER'S LENS\n{brokers_lens}{footer}"

def snip(text, marker, chars=120):
    idx = text.find(marker)
    return text[idx + len(marker): idx + len(marker) + chars] if idx != -1 else ""

cobalt_line = snip(brief, "Cobalt:")
copper_line = snip(brief, "Copper:")
cobalt_ok   = "$" in cobalt_line and "unavailable" not in cobalt_line.lower()
copper_ok   = "$" in copper_line and "unavailable" not in copper_line.lower()

# Staleness check — warn if prices are dated more than 2 days before today.
# Cobalt and copper markets are closed weekends so we allow a 2-day gap.
def price_is_stale(price_line, reference_date, max_days=2):
    """Return True if the date found in price_line is more than max_days old."""
    import re
    from datetime import timedelta
    months = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12
    }
    match = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', price_line)
    if not match:
        return False  # can't parse — don't flag
    day, mon, yr = int(match.group(1)), match.group(2).lower(), int(match.group(3))
    if mon not in months:
        return False
    from datetime import date
    price_date = date(yr, months[mon], day)
    gap = (reference_date.date() - price_date).days
    return gap > max_days

cobalt_stale = price_is_stale(cobalt_line, now)
copper_stale = price_is_stale(copper_line, now)
lens_ok     = len(brokers_lens) > 100
length_ok   = len(brief) > 900

# Check for header leak — model should never include the label in the body
lens_header_leak = "BROKER'S LENS" in brokers_lens or "**" in brokers_lens

# Check for placeholder flags left by the model when data was insufficient
has_placeholders = "[INSUFFICIENT DATA" in brief

# Check for forbidden filler phrases that indicate low-quality generalisation
filler_phrases = [
    "EV demand remains strong",
    "demand continues to grow",
    "manufacturers are increasingly focused",
    "it is worth noting",
    "against this backdrop",
    "the situation remains fluid",
    "in today's market",
]
filler_found = [p for p in filler_phrases if p.lower() in brief.lower()]

flags = []
if not cobalt_ok:       flags.append("COBALT PRICE MISSING/UNAVAILABLE")
if not copper_ok:       flags.append("COPPER PRICE MISSING/UNAVAILABLE")
if cobalt_stale:        flags.append("COBALT PRICE MAY BE STALE — verify date before publishing")
if copper_stale:        flags.append("COPPER PRICE MAY BE STALE — verify date before publishing")
if not lens_ok:         flags.append(f"BROKER'S LENS SHORT ({len(brokers_lens)} chars)")
if not length_ok:       flags.append(f"BRIEF SHORT ({len(brief)} chars)")
if lens_header_leak:    flags.append("BROKER'S LENS HEADER LEAK — remove label from body text")
if has_placeholders:    flags.append("PLACEHOLDERS PRESENT — editor must complete flagged sections")
if filler_found:        flags.append(f"FILLER PHRASES DETECTED: {'; '.join(filler_found)}")

quality_ok = len(flags) == 0

if quality_ok:
    print("  Quality gate: PASSED")
    subject = f"Brief ready — {today_short}"
else:
    print(f"  Quality gate: FAILED — {', '.join(flags)}")
    warning = (
        f"WARNING: {' | '.join(flags)}\n"
        f"Edit before publishing.\n"
        f"Raw price fetch result: {price_text}\n"
        f"{'─' * 60}\n\n"
    )
    brief   = warning + brief
    subject = f"[REVIEW] Brief — {today_short}"

print(f"  Final brief: {len(brief)} chars.")


# ── STEP 2d: SAVE USED STORIES FOR TOMORROW'S DEDUPLICATION ──────────────────
print("\nStep 2d: Saving used stories for tomorrow...")

# Collect all story titles retrieved today (freshness=pd only — background
# context stories are not the ones we need to deduplicate tomorrow)
todays_titles = [
    r["title"]
    for r in all_results
    if r.get("freshness") == "pd" and r.get("title")
]

# De-duplicate titles and cap at 30 to keep the prompt manageable
seen = set()
deduped_titles = []
for t in todays_titles:
    if t not in seen:
        seen.add(t)
        deduped_titles.append(t)
    if len(deduped_titles) >= 30:
        break

try:
    with open(USED_STORIES_PATH, "w") as f:
        json.dump(deduped_titles, f, indent=2)
    print(f"  Saved {len(deduped_titles)} titles to used_stories.json.")
except Exception as e:
    print(f"  Warning: could not save used_stories.json: {e}")


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
