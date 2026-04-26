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
    ("DRC cobalt export quota ARECOMS allocation",         "news", 5),
    ("Zambia copper First Quantum Barrick Mopani 2026",    "news", 5),
    ("Lobito Atlantic Railway Trafigura Kamoa Aurubis",    "news", 5),
    ("Glencore KCC Mutanda Katanga production guidance",   "news", 4),
    ("Chemaf Mutoshi Etoile Orion DRC cobalt buyer",       "news", 4),
    ("KoBold Mingomba Vedanta KCM Zambia copper investment","news", 4),
    ("Umicore Electra Westwin cobalt sulfate refinery",    "news", 4),
    ("Aurubis Wieland Prysmian KGHM copper cathode",       "news", 4),
    ("EU CRMA strategic project critical minerals 2026",   "news", 3),
    ("Section 232 copper tariff TC RC benchmark 2026",     "news", 3),
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

SECTIONS_SYSTEM = """You are writing the daily Critical Minerals Intelligence Brief for Lobito Intelligence Group, the non-Chinese Copperbelt Western-channel specialist.

AUDIENCE: procurement directors, supply chain risk leads, and ESG officers at named Western manufacturers — Aurubis, Wieland, Prysmian, Boliden, KGHM, Atlantic Copper, Umicore, Electra Battery Materials, Westwin Elements, Sumitomo Metal Mining — plus defence and aerospace tier-1 alloy makers (Carpenter Technology, Haynes International, ATI, Aubert & Duval) and mid-tier Copperbelt producers and traders without dedicated marketing teams.

POSITIONING: copper-led commercial story, Zambia equal-weight with DRC, Lobito Corridor as the structural delivery mechanism for Western channel flow. Cobalt is the halo product — higher-margin per tonne and the policy headline — but copper is the larger commercial opportunity (~1.2-1.5 Mt non-Chinese Copperbelt vs 5-15 kt tradeable cobalt residual). Treat them in that order in the Price Snapshot.

TONE: Intelligent, direct, data-grounded prose. Like a senior physical broker briefing a procurement director, not a news wire. Never vague.

PRICE SNAPSHOT RULES — most important:
The prices have already been fetched for you and provided in the prompt under LIVE PRICES.
Use those exact numbers. Do not modify, qualify, or replace them.
Industry standard format: $XX,XXX/t · Source · Date — one sentence on driver.
List COPPER FIRST, then cobalt. Copper drives our deal book; cobalt drives our headlines.
If a price is marked UNAVAILABLE, write exactly: [price unavailable — verify at lme.com]

SUPPLY CHAIN SIGNALS: 2 paragraphs — named companies, specific volumes, specific dates from the news results only. Read like a trader's morning note. When the news supports it, lead with named Western buyers (Aurubis, Wieland, Prysmian, KGHM, Boliden, Umicore, Electra) — what they are sourcing, what they are signalling, where the volume is moving. Always name a producer or trading house when describing a flow. At least one paragraph must anchor in copper. No generic statements.

WESTERN CHANNEL WATCH: 1-2 paragraphs — the dedicated Lobito Corridor and DRC quota tracker that differentiates this brief. Cover any of: Lobito Atlantic Railway shipments and capacity (Trafigura 49.5% concession, Kamoa anode, Aurubis Hamburg flows, Baltimore landings, AfDB/DFC/AFC funding, the 1Mt by 2030 target, TAZARA counter-positioning); DRC ARECOMS quota allocations (Q4 2025 quotas were CMOC 6,500t, Glencore 3,925t, ERG 2,125t; 2026-27 annual quota ~97kt); declared production vs quota; force majeure events; payable rate movements; Western refiner expansions (Umicore Kokkola, Boliden Rönnskär, Aurubis Richmond Augusta, Electra Temiskaming). Quantify wherever the data allows: tonnes shipped, percentage of allocation, vessel names, dates. If neither thread features in today's news, use this space for the most material non-Chinese Copperbelt named-account development of the day — Mopani/IRH ramp, Vedanta KCM rebuild, KoBold Mingomba progress, Chemaf rescue status, First Quantum/Barrick Zambia capex, or comparable.

GEOPOLITICAL RISK: 1-2 paragraphs — named actors, specific policy developments, concrete timeframes. EU CRMA Strategic Project designations and the 65% single-country cap, US Section 232 / OBBBA / FEOC mechanics, EU Battery Regulation due diligence (now deferred to 18 August 2027), DRC government and Sicomines deal renegotiation, Zambia Three Million Tonne Strategy, Indonesia HPAL competitive dynamics, sanctions developments. Always name the regulation, the article number where relevant, the threshold, and the date.

DEMAND DRIVERS: 1 paragraph — named companies and programmes only. Specifically: gigafactory and CAM/PCAM commissioning under FEOC pressure, defence and aerospace alloy demand (Rolls-Royce, GE Aerospace, Pratt & Whitney, Safran, Honeywell), AI data centre copper offtake (BloombergNEF: 400 ktpa average peaking 572 kt in 2028), grid and renewables tenders. Never "EV demand remains strong."

RULES:
— Never use filler: "it is worth noting", "in conclusion", "it is important to", "in today's market"
— Always name the company, country, regulation, or specific actor behind any trend
— Every news item must cite its source and approximate date
— No markdown, no ## headers, no bullet points except in price snapshot
— Copper is the primary commercial story; cobalt is the policy and headline story
— Write sections 1-5 only (Price Snapshot, Supply Chain Signals, Western Channel Watch, Geopolitical Risk, Demand Drivers) — do not write the Broker's Lens"""

sections_prompt = f"""Today is {today}.

=== LIVE PRICES (use these exact numbers — do not modify) ===
{price_text}

=== NEWS AND CONTEXT (past week) ===
{news_text}

Write sections 1-5 using this exact structure:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Copper: [paste copper line from LIVE PRICES above, then add one sentence on driver]
Cobalt: [paste cobalt line from LIVE PRICES above, then add one sentence on driver]

SUPPLY CHAIN SIGNALS
[2 paragraphs]

WESTERN CHANNEL WATCH
[1-2 paragraphs — Lobito Corridor, DRC quotas, named Western refiner/buyer flows]

GEOPOLITICAL RISK
[1-2 paragraphs]

DEMAND DRIVERS
[1 paragraph]"""

sections_text = claude_haiku(SECTIONS_SYSTEM, sections_prompt, max_tokens=1500)
print(f"  Done. {len(sections_text)} chars.")


# ── STEP 2b: WRITE BROKER'S LENS ─────────────────────────────────────────────
print("\nStep 2b: Writing Broker's Lens...")

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper, briefing procurement directors at named Western manufacturers (Aurubis, Wieland, Prysmian, Boliden, KGHM, Atlantic Copper, Umicore, Electra, Westwin, Carpenter Technology, Haynes International, ATI, Aubert & Duval), CAM/PCAM entrants under FEOC and CRMA pressure, and mid-tier Copperbelt producers and traders without dedicated marketing teams.

Write the Broker's Lens paragraph for today's Critical Minerals Intelligence Brief.

Answer this specific question: given today's developments, what should a named Western procurement director or non-Chinese Copperbelt producer do DIFFERENTLY this week that they would NOT have done last week?

Rules:
— 3-4 sentences only
— The insight must be non-obvious: not the most prominent headline, but the development with the most actionable consequence that the market has not yet fully priced
— Name specific counterparty types or named accounts: "call your Trafigura allocation contact", "Aurubis procurement", "your Glencore book runner", "the Mopani commercial team", "Vedanta KCM marketing"
— Specify timeframes: this week, next 30 days, before LME Week — not "soon"
— When today's data supports it, prefer the copper angle over the cobalt angle — copper deals are larger, more frequent, and more decisive for Western channel building
— Lobito Corridor capacity, Aurubis-Hamburg flows, DRC quota mechanics, TC/RC benchmark moves, and Section 232 implementation are the recurring themes that procurement directors must internalise
— Never write "it is worth noting", "the situation remains fluid", "in conclusion", or any filler
— Never summarise what the other sections already say — add new perspective
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
length_ok   = len(brief) > 1200
channel_ok  = "WESTERN CHANNEL WATCH" in brief

flags = []
if not cobalt_ok:  flags.append("COBALT PRICE MISSING/UNAVAILABLE")
if not copper_ok:  flags.append("COPPER PRICE MISSING/UNAVAILABLE")
if not lens_ok:    flags.append(f"BROKER'S LENS SHORT ({len(brokers_lens)} chars)")
if not length_ok:  flags.append(f"BRIEF SHORT ({len(brief)} chars)")
if not channel_ok: flags.append("WESTERN CHANNEL WATCH SECTION MISSING")

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
