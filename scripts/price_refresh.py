"""
Lobito Intelligence Group - Price Refresh Agent

Runs 4x per day during market hours (8am, 11am, 2pm, 4:30pm BST).
Updates only the prices block in data.json. Everything else untouched.
Cost: ~$0.005 per run (Haiku + web_search, 3 rounds max).
"""

import os, json, re, time, urllib.request
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
now_utc   = datetime.now(timezone.utc)
today     = now_utc.strftime("%A %-d %B %Y")
today_sh  = now_utc.strftime("%a %-d %b %Y")
time_str  = now_utc.strftime("%H:%M UTC")


# ── CLAUDE API WITH TOOL LOOP ─────────────────────────────────────────────────
def claude_call(system, user_message, tools=None, max_tokens=300, attempt=0):
    messages  = [{"role": "user", "content": user_message}]
    body_dict = {
        "model":    "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "system":   system,
        "messages": messages,
    }
    if tools:
        body_dict["tools"] = tools

    last_text = ""
    for turn in range(3):  # hard cap - prevents token explosion
        body = json.dumps(body_dict).encode()
        req  = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            print(f"  HTTP {e.code}: {err[:150]}")
            if e.code == 429 and attempt < 2:
                wait = 65 if attempt == 0 else 90
                print(f"  Rate limit - waiting {wait}s...")
                time.sleep(wait)
                return claude_call(system, user_message, tools, max_tokens, attempt + 1)
            raise

        stop    = data.get("stop_reason", "")
        content = data.get("content", [])
        last_text = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()

        if stop == "end_turn" or not tools:
            return last_text

        if stop == "tool_use":
            results = []
            for b in content:
                if b.get("type") == "tool_use":
                    raw = b.get("content", "")
                    # Truncate large search results to prevent token explosion
                    if isinstance(raw, str) and len(raw) > 2000:
                        raw = raw[:2000] + "\n[truncated]"
                    results.append({
                        "type":        "tool_result",
                        "tool_use_id": b["id"],
                        "content":     raw,
                    })
            body_dict["messages"] = body_dict["messages"] + [
                {"role": "assistant", "content": content},
                {"role": "user",      "content": results},
            ]
            continue

        return last_text

    return last_text


# ── PRICE FETCH ───────────────────────────────────────────────────────────────
def fetch_prices():
    system = (
        "You are a commodity price agent. Find two prices and return them as two lines. "
        "Start your response with COBALT: on the first character. Nothing else."
    )
    prompt = (
        f"Today is {today}.\n\n"
        "Search for cobalt price USD per tonne and copper price USD per tonne from Trading Economics.\n"
        "Cobalt is under Commodities > Industrial Metals on tradingeconomics.com/commodity/cobalt\n"
        "Copper is at tradingeconomics.com/commodity/copper\n"
        "If markets are closed, use the last settlement price shown - that IS the current price.\n"
        "Convert $/lb to $/t by multiplying by 2204.62 if needed.\n\n"
        f"Return ONLY these two lines:\n"
        f"COBALT: $[number]/t - Trading Economics - {today_sh}\n"
        f"COPPER: $[number]/t - Trading Economics - {today_sh}"
    )

    return claude_call(
        system=system,
        user_message=prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        max_tokens=150,
    )


# ── PARSE PRICE ───────────────────────────────────────────────────────────────
def parse_price(text, metal):
    """Extract price from response. Uses - as separator (no middot)."""
    m = re.search(rf'{metal}:\s*(.+)', text, re.IGNORECASE)
    if not m:
        return None
    line = m.group(1).strip()
    if "UNAVAILABLE" in line.upper():
        return None

    # Extract numeric value
    num = re.search(r'\$([\d,]+)', line)
    if not num:
        # Try extracting any number in plausible range
        ranges = {"cobalt": (30000, 90000), "copper": (7000, 20000)}
        lo, hi = ranges.get(metal.lower(), (1000, 100000))
        for nm in re.finditer(r'\b(\d[\d,]*(?:\.\d+)?)\b', line):
            raw = nm.group(1).replace(",", "")
            try:
                val = float(raw)
                if lo <= val <= hi:
                    return {
                        "raw":          f"${int(val):,}/t - Trading Economics - {today_sh}",
                        "value":        int(val),
                        "source":       "Trading Economics",
                        "date":         today_sh,
                        "refreshed_at": now_utc.isoformat(),
                    }
            except ValueError:
                continue
        return None

    value = int(num.group(1).replace(",", ""))
    # Split on dash separator (not middot)
    parts = [p.strip() for p in line.split("-")]
    return {
        "raw":          line,
        "value":        value,
        "source":       parts[1] if len(parts) > 1 else "Trading Economics",
        "date":         parts[2] if len(parts) > 2 else today_sh,
        "refreshed_at": now_utc.isoformat(),
    }


# ── MAIN ─────────────────────────────────────────────────────────────────────
print(f"Price refresh - {time_str}")

try:
    price_text = fetch_prices()
    print(f"  Raw: {price_text[:200]}")
except Exception as e:
    print(f"  Price fetch failed: {e}")
    price_text = ""

cobalt = parse_price(price_text, "COBALT")
copper = parse_price(price_text, "COPPER")

# Load or create data.json
try:
    with open("data.json") as f:
        data = json.load(f)
    print("  Loaded existing data.json")
except FileNotFoundError:
    print("  data.json not found - creating skeleton")
    data = {
        "generated_at":  now_utc.isoformat(),
        "date":          today,
        "date_short":    today_sh,
        "quality_ok":    True,
        "quality_flags": [],
        "prices":        {},
        "brief":         None,
        "signals":       [],
        "status": {
            "lobito":   {"state": "SUSPENDED", "level": "critical",
                         "value": "Halted since 12 April",
                         "note":  "Flooding at Cubal and Benguela. +10-15% logistics premium."},
            "chemical": {"state": "DISRUPTED",  "level": "critical",
                         "value": "Leaching orders cancelled",
                         "note":  "CMOC, Glencore affected. Output cuts possible 7-10 days."},
            "quota":    {"state": "WATCH",       "level": "high",
                         "value": "Strategic reserve active",
                         "note":  "DRC formalised state stockpile authority Apr 2026."},
            "policy":   {"state": "ACTIVE",      "level": "blue",
                         "value": "EU-US pact Q2 expected",
                         "note":  "REsourceEU launched 13 Apr. Section 232 copper tariffs live."},
        },
    }

# Ensure prices key exists
if "prices" not in data:
    data["prices"] = {}

# Update prices
if cobalt:
    data["prices"]["cobalt"] = cobalt
    print(f"  Cobalt: {cobalt['raw']}")
else:
    print("  Cobalt: no price found - keeping existing")

if copper:
    data["prices"]["copper"] = copper
    print(f"  Copper: {copper['raw']}")
else:
    print("  Copper: no price found - keeping existing")

data["last_price_refresh"] = now_utc.isoformat()

with open("data.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"  data.json written at {time_str}")
