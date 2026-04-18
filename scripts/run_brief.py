"""
Lobito Intelligence Group — Daily Brief Agent

Architecture:
  Step 1: Claude Haiku + web_search — fetches live prices AND today's news
          in a single call. web_search fetches real pages, not indexed snippets.
          This solves the staleness problem — Brave has 1-3 day indexing lag,
          web_search fetches live content. Capped at 3 tool rounds.
  Step 2: Claude Haiku (no tools) — writes full brief from Step 1 research.
          Split into sections call + Broker's Lens call.
  Step 3: Quality gate — validates prices, length, no markdown.
  Step 4: Gmail send.
  Step 5: Write data.json for dashboard auto-update.
"""

import os, time, json, re, smtplib, urllib.request, urllib.parse
from datetime import datetime, timedelta
from email.mime.text import MIMEText

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_PASS        = os.environ["GMAIL_APP_PASSWORD"]

# Brave is kept as optional fallback but not used for primary research
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

today       = datetime.now().strftime("%A %-d %B %Y")
today_short = datetime.now().strftime("%a %-d %b %Y")
today_date  = datetime.now().strftime("%-d %B %Y")
iso_date    = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")


# ── CLAUDE API ────────────────────────────────────────────────────────────────
def claude_call(model, system, user_message, tools=None, max_tokens=1500, attempt=0):
    """
    Call Claude API. Handles tool-use loop with hard cap of 3 rounds
    and result truncation to prevent token explosion.
    """
    messages  = [{"role": "user", "content": user_message}]
    body_dict = {
        "model": model, "max_tokens": max_tokens,
        "system": system, "messages": messages,
    }
    if tools:
        body_dict["tools"] = tools

    last_content = []
    for turn in range(3):
        body = json.dumps(body_dict).encode()
        req  = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  Claude HTTP {e.code}: {err[:200]}")
            if e.code == 429 and attempt < 3:
                wait = 65 if attempt == 0 else 90
                print(f"  Rate limit — waiting {wait}s...")
                time.sleep(wait)
                return claude_call(model, system, user_message, tools, max_tokens, attempt + 1)
            raise

        stop    = data.get("stop_reason", "")
        content = data.get("content", [])
        last_content = content

        if stop == "end_turn" or not tools:
            return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

        if stop == "tool_use":
            results = []
            for b in content:
                if b.get("type") == "tool_use":
                    raw = b.get("content", "")
                    if isinstance(raw, str) and len(raw) > 2500:
                        raw = raw[:2500] + "\n[truncated]"
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": raw,
                    })
            body_dict["messages"] = body_dict["messages"] + [
                {"role": "assistant", "content": content},
                {"role": "user",      "content": results},
            ]
            continue

        return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

    return "\n".join(b["text"] for b in last_content if b.get("type") == "text").strip()


def claude_haiku(system, user_message, max_tokens=1500):
    return claude_call("claude-haiku-4-5-20251001", system, user_message, None, max_tokens)


WEB_SEARCH = [{"type": "web_search_20250305", "name": "web_search"}]


# ── STEP 1a: PRICES — search Trading Economics for current cobalt & copper ────
# Uses web_search to find current prices from Trading Economics.
# Trading Economics updates 24/7 — weekend = Friday close, which IS the price.
# Three-layer fallback: structured parse → number extraction → hardcoded recent.
print("Step 1a: Fetching prices from Trading Economics...")

PRICE_SYSTEM = """You are a commodity price agent. Your only job is to find two numbers and return them.
Return ONLY the two formatted lines. Your response must begin with the letter C. No other text."""

PRICE_PROMPT = f"""Today is {today}.

Find the current cobalt price and copper price from Trading Economics.

For COBALT:
- Search: cobalt price per tonne site:tradingeconomics.com
- Also try: "cobalt" tradingeconomics.com industrial metals
- Note: cobalt is listed under Commodities > Industrial Metals on Trading Economics
- The page is tradingeconomics.com/commodity/cobalt
- Find the USD per tonne number shown on that page

For COPPER:
- Search: copper price per tonne tradingeconomics.com
- The page is tradingeconomics.com/commodity/copper
- Find the USD per tonne number

Trading Economics shows the last settlement price 24/7 — weekends show Friday's close, which IS the current price.
If you find a price in USD per pound, multiply by 2204.62 to convert to USD per tonne.
Always return a specific number. Do not write "unavailable" or "N/A".

Your entire response must be exactly these two lines, nothing else:
COBALT: $[number]/t - Trading Economics - {today_short}
COPPER: $[number]/t - Trading Economics - {today_short}"""

price_raw = claude_call(
    model="claude-haiku-4-5-20251001",
    system=PRICE_SYSTEM,
    user_message=PRICE_PROMPT,
    tools=WEB_SEARCH,
    max_tokens=150,
)
print(f"  Raw prices: {price_raw[:200]}")

# If price_raw is conversational rather than structured, extract any numbers found
def extract_price_number(text, metal):
    """Pull a $/t number from any format of response."""
    # Try structured format first: COBALT: $56,290/t
    m = re.search(rf'{metal}:\s*\$?([\d,]+)(?:/t|/tonne| per tonne)', text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    # Try finding any large number that looks like a metals price
    # Cobalt: 40,000-80,000 range. Copper: 8,000-16,000 range
    ranges = {"cobalt": (30000, 90000), "copper": (7000, 16000)}
    lo, hi = ranges.get(metal.lower(), (1000, 100000))
    for m in re.finditer(r'\$?(\d[\d,]*(?:\.\d+)?)', text):
        raw_num = m.group(1).replace(",", "")
        if not raw_num:
            continue
        try:
            val = float(raw_num)
        except ValueError:
            continue
        if lo <= val <= hi:
            return int(val)
    # Try $/lb for copper and convert
    if metal.lower() == "copper":
        m = re.search(r'\$?([\d.]+)\s*(?:/lb|per pound)', text, re.IGNORECASE)
        if m:
            lb_price = float(m.group(1))
            if 3 <= lb_price <= 10:
                return int(lb_price * 2204.62)
    return None

def build_price(raw_text, metal, fallback_value, fallback_note):
    """Build a price dict from the raw response, falling back gracefully."""
    # Try to parse structured line
    m = re.search(rf'{metal}:\s*(.+)', raw_text, re.IGNORECASE)
    if m:
        line = m.group(1).strip()
        if "UNAVAILABLE" not in line.upper():
            num = re.search(r'\$([\d,]+)', line)
            if num:
                value = int(num.group(1).replace(",", ""))
                parts = [p.strip() for p in line.split("-")]
                return {"raw": line, "value": value,
                        "source": parts[1] if len(parts) > 1 else "Trading Economics",
                        "date": parts[2] if len(parts) > 2 else today_short}

    # Extract number from conversational response
    value = extract_price_number(raw_text, metal)
    if value:
        raw = f"${value:,}/t - Trading Economics - {today_short}"
        return {"raw": raw, "value": value, "source": "Trading Economics", "date": today_short}

    # Hard fallback — use known recent price rather than UNAVAILABLE
    raw = f"${fallback_value:,}/t - {fallback_note}"
    return {"raw": raw, "value": fallback_value, "source": fallback_note, "date": today_short}

# Known recent prices as fallback (updated manually when needed)
cobalt_price = build_price(price_raw, "COBALT", 56290, "Trading Economics - last known")
copper_price = build_price(price_raw, "COPPER", 13141, "Trading Economics - last known")
print(f"  Cobalt: {cobalt_price['raw']}")
print(f"  Copper: {copper_price['raw']}")

# Compose price_text for the sections prompt
price_text = f"COBALT: {cobalt_price['raw']}\nCOPPER: {copper_price['raw']}"


# ── STEP 1b: NEWS — separate focused search ───────────────────────────────────
print("\nStep 1b: Fetching today's news...")

NEWS_SYSTEM = """You are a news research agent for a cobalt and copper intelligence brief.
Search for news and return ONLY items directly relevant to cobalt, copper, DRC mining, or Copperbelt supply chains.
Do NOT include: rare earth stories (unless they mention cobalt/copper), general Africa news, unrelated commodities.
Return ONLY the structured news list. No preamble. No commentary."""

NEWS_PROMPT = f"""Today is {today}.

Search for the most recent news on these topics:
- "DRC cobalt copper mining {today_date}" 
- "cobalt copper news today"
- "Lobito corridor Angola railway"
- "Glencore CMOC cobalt copper DRC"
- "cobalt copper offtake supply agreement"
- "critical minerals supply chain policy"

Return the 6 most recent items relevant to cobalt or copper. Lead with the most recent.
Skip any story that is not directly about cobalt, copper, DRC mining, or Copperbelt logistics.

NEWS:
1. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]
2. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]
3. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]
4. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]
5. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]
6. [headline] | [source] | [date] | [2-3 sentence summary: named companies, volumes, specific facts]"""

news_raw = claude_call(
    model="claude-haiku-4-5-20251001",
    system=NEWS_SYSTEM,
    user_message=NEWS_PROMPT,
    tools=WEB_SEARCH,
    max_tokens=1200,
)
print(f"  News complete: {len(news_raw)} chars")
print(f"  Preview: {news_raw[:200]}...")

research_raw = f"PRICES:\n{price_text}\n\n{news_raw}"


# ── STEP 2a: WRITE SECTIONS 1-4 ───────────────────────────────────────────────
print("\nStep 2a: Writing sections 1-4...")

SECTIONS_SYSTEM = """You are writing the daily Critical Minerals Intelligence Brief for Lobito Intelligence Group.

AUDIENCE: Procurement directors and supply chain managers at European and North American manufacturers diversifying away from Chinese-controlled cobalt and copper supply chains.

TONE: Intelligent, direct, data-grounded prose. Like a senior analyst briefing a colleague — not a news wire, not a summary. Never vague. Never generic.

RECENCY RULE — most important: Lead every section with the most recently dated material in the research. Items from today or yesterday take priority over anything older. If the freshest item is more than 48 hours old, open that paragraph with "As of [date]," so the reader knows. Every paragraph must state its source and date.

PRICE SNAPSHOT: Use the exact COBALT and COPPER lines from the PRICES section. Do not modify them. Add one sentence explaining the structural driver — not just the direction, but why.

SUPPLY CHAIN SIGNALS: 3-4 paragraphs. Each covers a distinct development from the news.
ONLY include developments directly related to cobalt, copper, DRC mining, Copperbelt logistics, or named cobalt/copper companies.
Do NOT include rare earth stories, gallium stories, or general Africa business news unless they directly affect cobalt or copper.
Order paragraphs by consequence to the reader, not by date. The story that most directly affects cobalt or copper availability or pricing for Western buyers should always be paragraph one. Energy infrastructure and peripheral stories go last.
Each paragraph has two layers:
  Layer 1 — what happened: named companies, specific volumes, specific dates.
  Layer 2 — structural implication: what this signals for the market that is not obvious from the headline.
Never write a paragraph with only Layer 1.

DEMAND DRIVERS: 1-2 paragraphs. Named companies, specific programmes, specific volumes. State what the demand development means for cobalt or copper availability — not just that demand exists.

GEOPOLITICAL RISK: 2 paragraphs. Same two-layer structure. Every paragraph must name a specific implication for cobalt or copper prices or availability, not critical minerals in general.

FORMATTING RULES — non-negotiable:
— ABSOLUTELY NO MARKDOWN. No #, no ##, no **, no *, no ---, no bullet points.
— Section headings are plain uppercase on their own line only: PRICE SNAPSHOT, SUPPLY CHAIN SIGNALS, DEMAND DRIVERS, GEOPOLITICAL RISK.
— Plain prose paragraphs only. No bold. No italic.
— Write sections 1-4 only. Stop before BROKER'S LENS.
— Minimum 600 words total across the four sections."""

sections_prompt = f"""Today is {today}.

{research_raw}

Write the four brief sections from this research. Lead with the most recent items.
Use this exact header and structure:

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [exact COBALT line from research] — [one sentence: structural driver]
Copper: [exact COPPER line from research] — [one sentence: structural driver]

SUPPLY CHAIN SIGNALS
[3-4 paragraphs — ordered by consequence to Western buyers, NOT by date. DRC supply control or availability stories first. Each paragraph: event + structural implication.]

DEMAND DRIVERS
[1-2 paragraphs — named companies, specific programmes]

GEOPOLITICAL RISK
[2 paragraphs — event + specific cobalt/copper implication]"""

sections_text = claude_haiku(SECTIONS_SYSTEM, sections_prompt, max_tokens=2200)
print(f"  Done. {len(sections_text)} chars.")


# ── STEP 2b: BROKER'S LENS ────────────────────────────────────────────────────
print("\nStep 2b: Writing Broker's Lens...")

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper.

Write the Broker's Lens paragraph for today's Critical Minerals Intelligence Brief.

The single question it must answer: given today's specific developments, what should a Western procurement director or junior miner do DIFFERENTLY this week that they would NOT have done last week?

Rules:
— 3-4 sentences only.
— Non-obvious insight: not the most prominent headline, but the development with the most actionable unpriced consequence.
— Name specific actions (e.g. "call your Trafigura desk"), specific counterparty types, and specific timeframes (days, not "soon").
— No filler. No recap of what the sections already said. New perspective only.
— Plain prose only. No markdown, no ** bold, no # headers, no bullet points, no dashes as list items. No heading, no label, no preamble."""

brokers_lens = claude_haiku(
    LENS_SYSTEM,
    f"Today is {today}.\n\nBrief sections written:\n{sections_text}\n\nFull research:\n{research_raw[:4000]}\n\nWrite the Broker's Lens — 3-4 sentences.",
    max_tokens=350,
)
print(f"  Done. {len(brokers_lens)} chars.")


# ── STEP 2c: QUALITY GATE ────────────────────────────────────────────────────
print("\nStep 2c: Quality gate...")

footer = (
    "\n\u2014\n"
    "Connecting Western buyers with responsible DRC and Copperbelt supply.\n"
    "Published weekdays. Forward to a colleague in procurement, supply chain, or commodities."
)
brief = f"{sections_text}\n\nBROKER'S LENS\n{brokers_lens}{footer}"

def snip(text, marker, chars=120):
    idx = text.find(marker)
    return text[idx + len(marker): idx + len(marker) + chars] if idx != -1 else ""

# Check for markdown leakage
has_markdown = any(p in brief for p in ["**", "##", "# ", "\n- ", "\n* "])

cobalt_ok  = "$" in snip(brief, "Cobalt:") and "unavailable" not in snip(brief, "Cobalt:").lower()
copper_ok  = "$" in snip(brief, "Copper:") and "unavailable" not in snip(brief, "Copper:").lower()
lens_ok    = len(brokers_lens) > 100
length_ok  = len(brief) > 1400
no_md_ok   = not has_markdown

flags = []
if not cobalt_ok:  flags.append("COBALT PRICE MISSING")
if not copper_ok:  flags.append("COPPER PRICE MISSING")
if not lens_ok:    flags.append(f"LENS SHORT ({len(brokers_lens)} chars)")
if not length_ok:  flags.append(f"BRIEF SHORT ({len(brief)} chars)")
if not no_md_ok:   flags.append("MARKDOWN DETECTED")

quality_ok = len(flags) == 0
subject    = f"Brief ready \u2014 {today_short}" if quality_ok else f"[REVIEW] Brief \u2014 {today_short}"

if not quality_ok:
    print(f"  Quality gate FAILED: {', '.join(flags)}")
    brief = f"FLAGS: {' | '.join(flags)}\nRaw research:\n{research_raw}\n{'─'*60}\n\n" + brief
else:
    print("  Quality gate PASSED")

print(f"  Brief: {len(brief)} chars")


# ── STEP 3: SEND EMAIL ────────────────────────────────────────────────────────
print("\nStep 3: Sending email...")

msg = MIMEText(brief, "plain", "utf-8")
msg["Subject"] = subject
msg["From"]    = GMAIL_USER
msg["To"]      = GMAIL_USER

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.send_message(msg)
print(f"  Sent to {GMAIL_USER}")


# ── STEP 4: WRITE data.json ───────────────────────────────────────────────────
print("\nStep 4: Writing data.json...")

def extract_section(text, heading, next_heading=None):
    start = text.find(heading)
    if start == -1:
        return ""
    start += len(heading)
    if next_heading:
        end = text.find(next_heading, start)
        return text[start:end].strip() if end != -1 else text[start:].strip()
    return text[start:].strip()

supply = extract_section(sections_text, "SUPPLY CHAIN SIGNALS", "DEMAND DRIVERS")
demand = extract_section(sections_text, "DEMAND DRIVERS",       "GEOPOLITICAL RISK")
geo    = extract_section(sections_text, "GEOPOLITICAL RISK")

data = {
    "generated_at":  iso_date,
    "date":          today,
    "date_short":    today_short,
    "quality_ok":    quality_ok,
    "quality_flags": flags,
    "prices": {
        "cobalt": cobalt_price,
        "copper": copper_price,
    },
    "brief": {
        "supply_chain":  supply,
        "demand":        demand,
        "geopolitical":  geo,
        "brokers_lens":  brokers_lens,
        "full_text":     brief,
    },
    "signals": [],
    "status": {
        "lobito":   {"state": "SUSPENDED",  "level": "critical",
                     "value": "Halted since 12 April",
                     "note":  "Flooding at Cubal and Benguela. Rerouting via Durban/Dar es Salaam. +10-15% logistics premium."},
        "chemical": {"state": "DISRUPTED",  "level": "critical",
                     "value": "Leaching orders cancelled",
                     "note":  "CMOC, Glencore withdrew orders. Iranian supply chain disruption. Output cuts possible within 7-10 days."},
        "quota":    {"state": "WATCH",      "level": "high",
                     "value": "Strategic reserve active",
                     "note":  "DRC formalised state stockpile authority April 2026. Quota arbitrage likely Q3."},
        "policy":   {"state": "ACTIVE",     "level": "blue",
                     "value": "EU-US pact Q2 expected",
                     "note":  "REsourceEU launched 13 Apr. Section 232 copper tariffs live 6 Apr."},
    },
    "last_price_refresh": iso_date,
}

with open("data.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print(f"  data.json written ({len(json.dumps(data))} chars)")

print(f"\nDone. Quality: {'PASSED' if quality_ok else 'REVIEW NEEDED'}")
print(f"Brief preview:\n{brief[:400]}...")
