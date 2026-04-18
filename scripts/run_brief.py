"""
Lobito Intelligence Group — Daily Brief Agent

Architecture:
  Step 1a: Claude Sonnet + web_search — live cobalt/copper prices (~8s, short task)
  Step 1b: Brave Search — 8 news/context queries
  Step 2a: Claude Haiku — writes sections 1-4 (brief engine prompt)
  Step 2b: Claude Haiku — writes Broker's Lens (separate focused call)
  Step 2c: Quality gate
  Step 3:  Gmail — sends email
  Step 4:  Write data.json — structured output for dashboard auto-update
           JSON is committed to repo by GitHub Actions after this script runs.
           Dashboard fetches it on load. Zero extra API cost.
"""

import os, time, json, re, smtplib, urllib.request, urllib.parse
from datetime import datetime
from email.mime.text import MIMEText

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
BRAVE_API_KEY     = os.environ["BRAVE_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]
GMAIL_PASS        = os.environ["GMAIL_APP_PASSWORD"]

today       = datetime.now().strftime("%A %-d %B %Y")
today_short = datetime.now().strftime("%a %-d %b %Y")
iso_date    = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

# ── CLAUDE API ────────────────────────────────────────────────────────────────
def claude_call(model, system, user_message, tools=None, max_tokens=1500, attempt=0):
    messages = [{"role": "user", "content": user_message}]
    body_dict = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
def claude_call(model, system, user_message, tools=None, max_tokens=1500, attempt=0):
    messages = [{"role": "user", "content": user_message}]
    body_dict = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        body_dict["tools"] = tools
    for turn in range(3):  # hard cap at 3 tool rounds — prevents exponential context accumulation
        body = json.dumps(body_dict).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            print(f"  Claude HTTP {e.code}: {body_text[:200]}")
            if e.code == 429 and attempt < 3:
                # 429 = rate limit hit. Wait for the per-minute window to reset.
                # Tier 1 limit is 50k input tokens/min — wait 65s to clear it fully.
                wait = 65 if attempt == 0 else 90
                print(f"  Rate limit hit — waiting {wait}s for window to reset...")
                time.sleep(wait)
                return claude_call(model, system, user_message, tools, max_tokens, attempt + 1)
            raise
        stop_reason = data.get("stop_reason", "")
        content     = data.get("content", [])
        if stop_reason == "end_turn" or not tools:
            return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
        if stop_reason == "tool_use":
            tool_results = []
            for b in content:
                if b.get("type") == "tool_use":
                    # Truncate each search result to 2,000 chars to prevent context explosion
                    raw_content = b.get("content", "")
                    if isinstance(raw_content, str) and len(raw_content) > 2000:
                        raw_content = raw_content[:2000] + "\n[truncated]"
                    elif isinstance(raw_content, list):
                        # content may be a list of blocks
                        raw_content = str(raw_content)[:2000] + "\n[truncated]"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": raw_content,
                    })
            body_dict["messages"] = body_dict["messages"] + [
                {"role": "assistant", "content": content},
                {"role": "user",      "content": tool_results}]
            continue
        return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
    # If we hit the turn cap, return whatever text we have
    return "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

def claude_haiku(system, user_message, max_tokens=1500):
    return claude_call("claude-haiku-4-5-20251001", system, user_message, None, max_tokens)

# ── BRAVE SEARCH ──────────────────────────────────────────────────────────────
def brave_news(query, count=5):
    params = urllib.parse.urlencode({"q": query, "count": count, "freshness": "pw", "safesearch": "off"})
    req = urllib.request.Request(f"https://api.search.brave.com/res/v1/news/search?{params}",
        headers={"Accept": "application/json", "Accept-Encoding": "identity", "X-Subscription-Token": BRAVE_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return [{"title": r.get("title",""), "description": r.get("description",""),
                     "age": r.get("age",""), "source": r.get("meta_url",{}).get("hostname","")}
                    for r in data.get("results", [])]
    except Exception as e:
        print(f"  Brave news failed '{query}': {e}"); return []

def brave_web(query, count=5):
    params = urllib.parse.urlencode({"q": query, "count": count, "freshness": "pw", "safesearch": "off"})
    req = urllib.request.Request(f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={"Accept": "application/json", "Accept-Encoding": "identity", "X-Subscription-Token": BRAVE_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return [{"title": r.get("title",""), "description": r.get("description",""),
                     "age": r.get("age",""), "source": r.get("meta_url",{}).get("hostname","")}
                    for r in data.get("web",{}).get("results", [])]
    except Exception as e:
        print(f"  Brave web failed '{query}': {e}"); return []

def fmt(results, label):
    if not results: return f"[{label}: no results]\n"
    lines = [f"[{label}]"]
    for r in results:
        lines.append(f"- {r['title']}")
        if r["description"]: lines.append(f"  {r['description'][:220]}")
        if r["source"]: lines.append(f"  Source: {r['source']}  Age: {r['age']}")
    return "\n".join(lines) + "\n"

# ── STEP 1a: LIVE PRICES ──────────────────────────────────────────────────────
# Uses Haiku + web_search (NOT Sonnet — Haiku is 10x cheaper and equally capable
# for fetching two numbers). Tool rounds capped at 3 in claude_call above.
print("Step 1a: Fetching live prices via Claude Haiku web_search...")

price_text = claude_call(
    model="claude-haiku-4-5-20251001",
    system="""You are a price retrieval agent. You must output EXACTLY two lines and nothing else.
Do not write any introduction, explanation, or commentary before or after the two lines.
Do not write "Let me search" or "I found" or any other text.
Output format is non-negotiable. Two lines only. Start your response with COBALT:""",
    user_message=f"""Today is {today}. Search for cobalt price USD/t and LME copper cash price USD/t.
Use Trading Economics, Kitco, or LME.com. If a price is older than 48 hours, still use it and state the date.
Output EXACTLY these two lines and nothing else — start immediately with COBALT:

COBALT: $[number]/t · [source] · [date]
COPPER: $[number]/t · [source] · [date]""",
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    max_tokens=300,
)
print(f"  Prices: {price_text}")

# Parse price lines for JSON
def parse_price(text, metal):
    """Extract price string like '$56,290/t · LME · 17 Apr 2026'"""
    m = re.search(rf'{metal}:\s*(.+)', text, re.IGNORECASE)
    if m:
        line = m.group(1).strip()
        if "UNAVAILABLE" in line.upper():
            return {"raw": "unavailable", "value": None, "source": "", "date": ""}
        # Extract numeric value
        num = re.search(r'\$([\d,]+)', line)
        value = int(num.group(1).replace(",","")) if num else None
        # Extract source and date
        parts = line.split("·")
        source = parts[1].strip() if len(parts) > 1 else ""
        date   = parts[2].strip() if len(parts) > 2 else today_short
        return {"raw": line, "value": value, "source": source, "date": date}
    return {"raw": "unavailable", "value": None, "source": "", "date": ""}

cobalt_price = parse_price(price_text, "COBALT")
copper_price = parse_price(price_text, "COPPER")

# ── STEP 1b: NEWS ─────────────────────────────────────────────────────────────
print("\nStep 1b: Fetching news context...")
news_searches = [
    ("DRC cobalt export Congo mining 2026",                "news", 5),
    ("cobalt copper offtake deal supply agreement",        "news", 5),
    ("Lobito corridor Angola railway minerals",            "news", 4),
    ("critical minerals supply chain policy 2026",         "news", 4),
    ("Glencore cobalt copper mining",                      "news", 4),
    ("CMOC Trafigura Mercuria cobalt copper",              "news", 3),
    ("EV battery cobalt demand aerospace defence",         "news", 3),
    ("DRC mining quota reserve export controls",           "news", 3),
    ("cobalt copper market analysis outlook 2026",         "news", 4),  # analyst commentary layer
]
news_parts = []
for query, search_type, count in news_searches:
    print(f"  [{search_type}] {query}")
    results = brave_news(query, count) if search_type == "news" else brave_web(query, count)
    news_parts.append(fmt(results, query[:55]))
    time.sleep(0.3)
news_text = "\n".join(news_parts)
print(f"  Done. {len(news_text)} chars.")

# ── STEP 2a: WRITE SECTIONS 1-4 ──────────────────────────────────────────────
print("\nStep 2a: Writing sections 1-4...")

SECTIONS_SYSTEM = """You are writing the daily Critical Minerals Intelligence Brief for Lobito Intelligence Group.
AUDIENCE: procurement directors and supply chain managers at European and North American manufacturers diversifying away from Chinese-controlled supply chains.
TONE: Intelligent, direct, data-grounded prose. Like a senior analyst briefing a colleague, not a news wire. Never vague. Never generic.

PRICE SNAPSHOT RULES: Use the exact price lines from LIVE PRICES. Do not modify them. Format: $XX,XXX/t · Source · Date — one sentence on the structural driver (not just direction — explain why).

SUPPLY CHAIN SIGNALS: 3-4 paragraphs. Each paragraph covers a distinct development.
Structure each paragraph as two layers:
  Layer 1 — what happened: named companies, specific volumes, specific dates from the results.
  Layer 2 — structural implication: why this matters beyond the headline. What does it signal about the market, the counterparty, or the supply chain? What should a reader infer that isn't obvious?
Never write a paragraph that is only Layer 1. Every signal needs its implication stated.

DEMAND DRIVERS: 1-2 paragraphs. Named end-use sectors, specific companies, specific programmes. State what the demand development means for cobalt or copper availability specifically — not just that demand exists.

GEOPOLITICAL RISK: 2 paragraphs. Same two-layer structure — event then implication. Name government agencies, specific policies, concrete enforcement timelines where available. Every paragraph must name a specific implication for cobalt or copper — not critical minerals in general.

RULES:
— No filler: "it is worth noting", "in conclusion", "the situation remains fluid", "amid ongoing"
— Name every company and country — never "a major miner" or "a Western buyer"
— Cite source and approximate date for every factual claim
— ABSOLUTELY NO MARKDOWN: no # or ## headers, no ** bold, no * italic, no bullet points, no dashes as list items, no --- dividers
— Section headings are plain uppercase text on their own line: PRICE SNAPSHOT, SUPPLY CHAIN SIGNALS, etc.
— Write sections 1-4 only — stop before Broker's Lens
— Minimum 600 words across the four sections"""

# Hard cap on news_text to prevent input token rate limit (50k/min Tier 1)
# 12,000 chars ≈ 3,000 tokens — leaves plenty of headroom with system prompt
news_text_trimmed = news_text[:12000] if len(news_text) > 12000 else news_text
if len(news_text) > 12000:
    print(f"  News text trimmed: {len(news_text)} → 12,000 chars to stay within token limits")

sections_prompt = f"""Today is {today}.
=== LIVE PRICES (use exactly as written) ===
{price_text}
=== NEWS AND CONTEXT (past week) ===
{news_text_trimmed}
Write sections 1-4. Be thorough — each signal deserves its implication stated, not just its headline.

Lobito Intelligence Group
Critical Minerals Intelligence
{today} - Daily Brief

PRICE SNAPSHOT
Cobalt: [exact line from LIVE PRICES] — [one sentence: structural driver, not just direction]
Copper: [exact line from LIVE PRICES] — [one sentence: structural driver, not just direction]

SUPPLY CHAIN SIGNALS
[3-4 paragraphs — each with event + structural implication]

DEMAND DRIVERS
[1-2 paragraphs — named companies and what the demand development means for cobalt/copper availability]

GEOPOLITICAL RISK
[2 paragraphs — event + specific implication for cobalt or copper, not critical minerals generally]"""

sections_text = claude_haiku(SECTIONS_SYSTEM, sections_prompt, max_tokens=2200)
print(f"  Done. {len(sections_text)} chars.")

# ── STEP 2b: BROKER'S LENS ────────────────────────────────────────────────────
print("\nStep 2b: Writing Broker's Lens...")

LENS_SYSTEM = """You are a senior physical commodity broker with 20 years in cobalt and copper.
Write the Broker's Lens: given today's specific developments, what should a Western procurement director or junior miner do DIFFERENTLY this week that they would NOT have done last week?
Rules: 3-4 sentences. Non-obvious insight — not the headline, but the most actionable unpriced consequence. Name specific actions, counterparty types, timeframes. No filler. No recap. Facts only from research provided. Plain prose only — no heading, no markdown, no ** bold, no # symbols, no bullet points."""

brokers_lens = claude_haiku(LENS_SYSTEM,
    f"Today is {today}.\nSections written:\n{sections_text}\nFull research:\n{price_text}\n{news_text_trimmed}\nWrite Broker's Lens — 3-4 sentences.",
    max_tokens=350)
print(f"  Done. {len(brokers_lens)} chars.")

# ── STEP 2c: QUALITY GATE ────────────────────────────────────────────────────
print("\nStep 2c: Quality gate...")

footer = "\n—\nConnecting Western buyers with responsible DRC and Copperbelt supply.\nPublished weekdays. Forward to a colleague in procurement, supply chain, or commodities."
brief = f"{sections_text}\n\nBROKER'S LENS\n{brokers_lens}{footer}"

def snip(text, marker, chars=120):
    idx = text.find(marker)
    return text[idx+len(marker):idx+len(marker)+chars] if idx != -1 else ""

cobalt_ok = "$" in snip(brief,"Cobalt:") and "unavailable" not in snip(brief,"Cobalt:").lower()
copper_ok = "$" in snip(brief,"Copper:") and "unavailable" not in snip(brief,"Copper:").lower()
lens_ok   = len(brokers_lens) > 100
length_ok = len(brief) > 1400  # raised from 900 — richer brief should clear 1,400 chars easily

flags = []
if not cobalt_ok: flags.append("COBALT PRICE MISSING")
if not copper_ok: flags.append("COPPER PRICE MISSING")
if not lens_ok:   flags.append(f"LENS SHORT ({len(brokers_lens)})")
if not length_ok: flags.append(f"BRIEF SHORT ({len(brief)})")

quality_ok = len(flags) == 0
subject = f"Brief ready — {today_short}" if quality_ok else f"[REVIEW] Brief — {today_short}"
if not quality_ok:
    brief = f"WARNING: {' | '.join(flags)}\nRaw prices: {price_text}\n{'─'*60}\n\n" + brief
print(f"  {'PASSED' if quality_ok else 'FAILED — '+', '.join(flags)}")

# ── STEP 3: EMAIL ─────────────────────────────────────────────────────────────
print("\nStep 3: Sending email...")
msg = MIMEText(brief, "plain", "utf-8")
msg["Subject"] = subject
msg["From"]    = GMAIL_USER
msg["To"]      = GMAIL_USER
with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.send_message(msg)
print(f"  Sent to {GMAIL_USER}")

# ── STEP 4: WRITE data.json FOR DASHBOARD ─────────────────────────────────────
# This file is committed to the repo by GitHub Actions after this script runs.
# The dashboard fetches it on load — zero extra API calls, zero extra cost.
print("\nStep 4: Writing data.json for dashboard...")

# Parse brief sections into structured fields
def extract_section(text, heading, next_heading=None):
    """Pull a named section out of the brief text."""
    start = text.find(heading)
    if start == -1:
        return ""
    start += len(heading)
    if next_heading:
        end = text.find(next_heading, start)
        return text[start:end].strip() if end != -1 else text[start:].strip()
    return text[start:].strip()

supply  = extract_section(sections_text, "SUPPLY CHAIN SIGNALS", "GEOPOLITICAL RISK")
geo     = extract_section(sections_text, "GEOPOLITICAL RISK",    "DEMAND DRIVERS")
demand  = extract_section(sections_text, "DEMAND DRIVERS")

# Build signals list from news_parts — pull top 6 Brave results as signal stubs
# These get enriched in the dashboard but give it real today's titles
raw_signals = []
for block in news_parts[:6]:
    lines = block.strip().split("\n")
    title = next((l.lstrip("- ") for l in lines if l.startswith("- ")), "")
    desc  = next((l.strip() for l in lines if l.startswith("  ") and not l.strip().startswith("Source:")), "")
    src   = next((l.replace("Source:","").split("Age:")[0].strip() for l in lines if "Source:" in l), "")
    age   = next((l.split("Age:")[-1].strip() for l in lines if "Age:" in l), "")
    if title:
        raw_signals.append({"title": title, "description": desc, "source": src, "age": age})

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
        "price_snapshot": snip(sections_text, "PRICE SNAPSHOT", 600).strip(),
        "supply_chain":   supply,
        "geopolitical":   geo,
        "demand":         demand,
        "brokers_lens":   brokers_lens,
        "full_text":      brief,
    },

    "signals": raw_signals,

    # Status board — updated manually or can be overridden by adding
    # a separate status.json file; these are sensible live defaults
    "status": {
        "lobito":   {"state": "SUSPENDED", "level": "critical",
                     "value": "Halted since 12 April",
                     "note": "Flooding at Cubal & Benguela. Rerouting via Durban/Dar. +10–15% logistics premium."},
        "chemical": {"state": "DISRUPTED",  "level": "critical",
                     "value": "Leaching orders cancelled",
                     "note": "CMOC, Glencore withdrew orders. Iranian supply chain disruption. Output cuts possible within 7–10 days."},
        "quota":    {"state": "WATCH",      "level": "high",
                     "value": "Strategic reserve active",
                     "note": "DRC formalised state stockpile authority Apr 2026. Quota arbitrage likely Q3."},
        "policy":   {"state": "ACTIVE",     "level": "blue",
                     "value": "EU–US pact Q2 expected",
                     "note": "REsourceEU launched 13 Apr. Section 232 copper tariffs live 6 Apr."},
    },
}

with open("data.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"  data.json written — {len(json.dumps(data))} chars")
print(f"\nDone. Brief: {len(brief)} chars. Quality: {'OK' if quality_ok else 'REVIEW NEEDED'}")
