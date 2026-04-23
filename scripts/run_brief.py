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

PRICE_SYSTEM = """You are a commodity price data retrieval agent. Your only job is to find the current market price for cobalt and copper and return them in a precise structured format. Nothing else."""

PRICE_PROMPT = f"""Today is {today}.

Search for the current price of cobalt and the current LME copper price.

For cobalt: search "cobalt price per tonne USD today" and find the most recent number from Fastmarkets, Metal Bulletin, LME, Trading Economics, or Benchmark Mineral Intelligence. The industry standard is USD/tonne.

For copper: search "LME copper cash price per tonne today" and find today's LME official cash settlement or spot price in USD/tonne from LME.com, Trading Economics, or Fastmarkets.

Return ONLY this exact format, nothing else:

COBALT: $[price]/t · [source] · [date]
COPPER: $[price]/t · [source] · [date]

Example:
COBALT: $56,290/t · LME · 17 Apr 2026
COPPER: $9,820/t · LME cash · 17 Apr 2026

If you cannot find a number after searching, write:
COBALT: UNAVAILABLE
COPPER: UNAVAILABLE

Do not write anything other than these two lines."""

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
                    "age":         r.get("age", ""),
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

AUDIENCE: procurement directors and supply chain managers at European and North American manufacturers who need to diversify away from Chinese-controlled supply chains.

TONE: Intelligent, direct, data-grounded prose. Like a knowledgeable colleague, not a news wire. Never vague.

PRICE SNAPSHOT RULES — most important:
The prices have already been fetched for you and provided in the prompt under LIVE PRICES.
Use those exact numbers. Do not modify, qualify, or replace them.
Industry standard format: $XX,XXX/t · Source · Date — one sentence on driver.
If a price is marked UNAVAILABLE, write exactly: [price unavailable — verify at lme.com]

NEWS PRIORITISATION:
— Articles tagged [TODAY'S NEWS] are your primary source material. Lead every paragraph with these.
— Articles tagged [BACKGROUND CONTEXT] may be used to support or explain, but must not be the main subject of any paragraph.
— Any story listed under STORIES ALREADY COVERED YESTERDAY must not be the lead of any paragraph.

SUPPLY CHAIN SIGNALS: 2-3 paragraphs — named companies, specific volumes, specific dates from the news results only. Read like a trader's morning note. No generic statements.

GEOPOLITICAL RISK: 1-2 paragraphs — named actors, specific policy developments, concrete timeframes.

DEMAND DRIVERS: 1 paragraph — named companies and programmes only. Never "EV demand remains strong."

RULES:
— Never use filler: "it is worth noting", "in conclusion", "it is important to", "in today's market"
— Always name the company or country behind any trend
— Every news item must cite its source and approximate date
— No markdown, no ## headers, no bullet points except in price snapshot
— Write sections 1-4 only — do not write the Broker's Lens"""

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

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper.

Write the Broker's Lens paragraph for today's Critical Minerals Intelligence Brief.

Answer this specific question: given today's specific developments, what should a Western procurement director or junior miner do DIFFERENTLY this week that they would NOT have done last week?

Rules:
— 3-4 sentences only
— The insight must be non-obvious: not the most prominent headline, but the development with the most actionable consequence that the market has not yet fully priced
— Name specific actions, specific counterparty types (e.g. "call your Trafigura contact"), and specific timeframes (days, not vague "soon")
— Never write "it is worth noting", "the situation remains fluid", "in conclusion", or any filler
— Never summarise what the other four sections already say — add new perspective
— Base everything on the research provided — no invented facts
— Return the paragraph text only — no heading, no label, no preamble"""

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
lens_ok     = len(brokers_lens) > 100
length_ok   = len(brief) > 900

flags = []
if not cobalt_ok: flags.append("COBALT PRICE MISSING/UNAVAILABLE")
if not copper_ok: flags.append("COPPER PRICE MISSING/UNAVAILABLE")
if not lens_ok:   flags.append(f"BROKER'S LENS SHORT ({len(brokers_lens)} chars)")
if not length_ok: flags.append(f"BRIEF SHORT ({len(brief)} chars)")

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
