"""
Lobito Intelligence Group — Daily Brief Agent

Architecture:
  Step 1a: Claude Sonnet + web_search tool — fetches ONLY cobalt and copper
           prices from live sources. Short task, max_tokens=250, ~8s.
           Runs on Anthropic infrastructure so bypasses GitHub egress restrictions.
  Step 1b: Brave Search API — 8 news/context queries. Already proven to work.
  Step 2a: Claude Haiku (no tools) — writes sections 1-4 using brief engine prompt.
  Step 2b: Claude Haiku (no tools) — writes Broker's Lens as separate focused call.
  Step 2c: Quality gate — validates prices and length before sending.
  Step 3:  Gmail — sends to inbox; subject line flags quality failures.
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

        # If no tools or stop_reason is end_turn, extract text and return
        if stop_reason == "end_turn" or not tools:
            return "\n".join(
                b["text"] for b in content if b.get("type") == "text"
            ).strip()

        # If Claude wants to use a tool, handle it and continue
        if stop_reason == "tool_use":
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    # web_search handles itself on Anthropic's side —
                    # we just need to pass the result back
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block["id"],
                        "content":     block.get("content", ""),
                    })
            # Append assistant response and tool results to messages
            body_dict["messages"] = body_dict["messages"] + [
                {"role": "assistant", "content": content},
                {"role": "user",      "content": tool_results},
            ]
            continue

        # Fallback: return whatever text exists
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
# Uses claude-sonnet-4-6 with web_search tool.
# Task is intentionally minimal — two numbers only — so it completes in ~8s.
# This runs on Anthropic's infrastructure, bypassing GitHub egress restrictions.
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
print("\nStep 1b: Fetching news context via Brave Search...")

def brave_news(query, count=5):
    params = urllib.parse.urlencode({
        "q": query, "count": count, "freshness": "pw", "safesearch": "off",
    })
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/news/search?{params}",
        headers={
            "Accept":               "application/json",
            "Accept-Encoding":      "identity",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
    )
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
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={
            "Accept":               "application/json",
            "Accept-Encoding":      "identity",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
    )
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


def fmt(results, label):
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


news_searches = [
    ("DRC cobalt export Congo mining 2026",                "news", 5),
    ("cobalt copper offtake deal supply agreement",        "news", 5),
    ("Lobito corridor Angola railway minerals",            "news", 4),
    ("critical minerals supply chain policy 2026",         "news", 4),
    ("Glencore cobalt copper mining",                      "news", 4),
    ("CMOC Trafigura Mercuria cobalt copper",              "news", 3),
    ("EV battery cobalt demand aerospace defence",         "news", 3),
    ("DRC mining quota reserve export controls",           "news", 3),
]

news_parts = []
for query, search_type, count in news_searches:
    print(f"  [{search_type}] {query}")
    results = brave_news(query, count) if search_type == "news" else brave_web(query, count)
    news_parts.append(fmt(results, query[:55]))
    time.sleep(0.3)

news_text = "\n".join(news_parts)
print(f"  Done. {len(news_text)} chars of news context.")


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

=== NEWS AND CONTEXT (past week) ===
{news_text}

Write sections 1-4 using this exact structure:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [paste cobalt line from LIVE PRICES above, then add one sentence on driver]
Copper: [paste copper line from LIVE PRICES above, then add one sentence on driver]

SUPPLY CHAIN SIGNALS
[2-3 paragraphs]

GEOPOLITICAL RISK
[1-2 paragraphs]

DEMAND DRIVERS
[1 paragraph]"""

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

# Prices are good if they contain a $ sign and no UNAVAILABLE
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
