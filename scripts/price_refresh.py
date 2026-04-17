"""
Lobito Intelligence Group — Price Refresh Agent

Runs 4x per day during market hours (8am, 11am, 2pm, 4:30pm BST).
Does ONE thing: fetches current cobalt and copper prices via Claude web_search,
updates the prices block in data.json, and writes it back.

Cost per run: ~1 Claude Sonnet call, max_tokens=250 = ~$0.002
Daily cost: ~$0.008 (4 runs). Monthly: ~$0.16. Negligible.

Everything else in data.json (signals, brief, status) is left untouched —
those are only updated by the 6am full brief run.
"""

import os, json, re, urllib.request
from datetime import datetime, timezone

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
now_utc = datetime.now(timezone.utc)
today   = now_utc.strftime("%A %-d %B %Y")
time_str = now_utc.strftime("%H:%M UTC")

# ── CLAUDE WEB SEARCH (price fetch only) ─────────────────────────────────────
def fetch_prices():
    """Use Claude Sonnet + web_search to get current prices. max_tokens=250 = fast + cheap."""
    body_dict = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 250,
        "system":     "You are a commodity price data retrieval agent. Return only the two price lines requested. Nothing else.",
        "messages":   [{"role": "user", "content": f"""Today is {today}, time is {time_str}.
Search for current cobalt price per tonne USD and current LME copper cash price per tonne USD.
Check: Fastmarkets, LME.com, Trading Economics, Kitco, Metal Bulletin.
Return ONLY these two lines:
COBALT: $[price]/t · [source] · [date]
COPPER: $[price]/t · [source] · [date]
If unavailable write UNAVAILABLE."""}],
        "tools":      [{"type": "web_search_20250305", "name": "web_search"}],
    }

    messages = body_dict["messages"]
    for _ in range(6):  # tool-use loop
        body = json.dumps(body_dict).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
        stop   = data.get("stop_reason","")
        content = data.get("content",[])
        if stop == "end_turn":
            return "\n".join(b["text"] for b in content if b.get("type")=="text").strip()
        if stop == "tool_use":
            tool_results = [{"type":"tool_result","tool_use_id":b["id"],"content":b.get("content","")}
                            for b in content if b.get("type")=="tool_use"]
            body_dict["messages"] = messages + [
                {"role":"assistant","content":content},
                {"role":"user","content":tool_results}]
            messages = body_dict["messages"]
            continue
        return "\n".join(b["text"] for b in content if b.get("type")=="text").strip()
    return ""

def parse_price(text, metal):
    m = re.search(rf'{metal}:\s*(.+)', text, re.IGNORECASE)
    if not m:
        return None
    line = m.group(1).strip()
    if "UNAVAILABLE" in line.upper():
        return None
    num = re.search(r'\$([\d,]+)', line)
    value = int(num.group(1).replace(",","")) if num else None
    parts = [p.strip() for p in line.split("·")]
    return {
        "raw":    line,
        "value":  value,
        "source": parts[1] if len(parts) > 1 else "",
        "date":   parts[2] if len(parts) > 2 else now_utc.strftime("%-d %b %Y"),
        "refreshed_at": now_utc.isoformat(),
    }

# ── MAIN ─────────────────────────────────────────────────────────────────────
print(f"Price refresh — {time_str}")
price_text = fetch_prices()
print(f"  Raw: {price_text}")

cobalt = parse_price(price_text, "COBALT")
copper = parse_price(price_text, "COPPER")

# Load existing data.json (created by full brief run)
try:
    with open("data.json") as f:
        data = json.load(f)
except FileNotFoundError:
    # data.json doesn't exist yet (first ever run before brief has run)
    # Create a minimal skeleton so the dashboard has something to load
    data = {
        "generated_at": now_utc.isoformat(),
        "date": today,
        "date_short": now_utc.strftime("%a %-d %b %Y"),
        "quality_ok": True,
        "quality_flags": [],
        "prices": {},
        "brief": None,
        "signals": [],
        "status": {
            "lobito":   {"state":"SUSPENDED","level":"critical","value":"Halted since 12 April","note":"Flooding at Cubal & Benguela. +10–15% logistics premium."},
            "chemical": {"state":"DISRUPTED","level":"critical","value":"Leaching orders cancelled","note":"CMOC, Glencore affected. Output cuts possible within 7–10 days."},
            "quota":    {"state":"WATCH","level":"high","value":"Strategic reserve active","note":"DRC formalised state stockpile authority Apr 2026."},
            "policy":   {"state":"ACTIVE","level":"blue","value":"EU–US pact Q2 expected","note":"REsourceEU launched 13 Apr. Section 232 copper tariffs live."},
        },
    }

# Update only the prices block — leave everything else untouched
if cobalt:
    data["prices"]["cobalt"] = cobalt
    print(f"  Cobalt updated: {cobalt['raw']}")
else:
    print("  Cobalt: no price found — keeping existing")

if copper:
    data["prices"]["copper"] = copper
    print(f"  Copper updated: {copper['raw']}")
else:
    print("  Copper: no price found — keeping existing")

data["last_price_refresh"] = now_utc.isoformat()

with open("data.json", "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"  data.json written — prices refreshed at {time_str}")
