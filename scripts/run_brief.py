import os
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
import anthropic

API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]
today = datetime.now().strftime("%A %-d %B %Y")
today_short = datetime.now().strftime("%a %-d %b %Y")

client = anthropic.Anthropic(api_key=API_KEY, timeout=180.0)


def call_api(model, system, messages, use_web_search=False, attempt=0):
    kwargs = {
        "model": model,
        "max_tokens": 1000,
        "system": system,
        "messages": messages,
    }
    if use_web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    try:
        return client.messages.create(**kwargs)
    except anthropic.RateLimitError:
        if attempt < 3:
            wait = (2 ** attempt) * 20
            print(f"Rate limited. Waiting {wait}s (retry {attempt+1}/3)...")
            time.sleep(wait)
            return call_api(model, system, messages, use_web_search, attempt + 1)
        raise
    except anthropic.APIStatusError as e:
        print(f"API error {e.status_code}: {e.message}")
        raise


def extract_text(response):
    return "\n".join(
        b.text for b in response.content if b.type == "text"
    ).strip()


# ── STEP 1: RESEARCH ──────────────────────────────────────────────────────────
# Sonnet + web search (required for web search tool)
# Capped at 1000 tokens — bullet points only, no prose
print("Step 1: Researching live market data...")

research_system = (
    "You are a commodity market data collector. "
    "Search the web and return raw factual findings only. "
    "No analysis. Bullet points only. "
    "Every item must include a source name and publication date."
)

research_prompt = (
    "Today is " + today + ". Search for and return raw data on these topics:\n\n"
    "1. LME cobalt cash price today (USD/t) with source and date\n"
    "2. LME copper 3-month price today (USD/t) with source and date\n"
    "3. Any DRC cobalt export quota or supply policy news from the last 48 hours\n"
    "4. Any Western buyer-supplier deals, MOUs, or offtake agreements "
    "in cobalt or copper from last 48 hours\n"
    "5. Any Lobito Corridor developments from last 48 hours\n"
    "6. Any other significant cobalt or copper supply chain news from last 48 hours\n\n"
    "Format: plain bullet points. Source and date required for every item. "
    "Discard anything older than 48 hours."
)

messages = [{"role": "user", "content": research_prompt}]
data = call_api("claude-sonnet-4-6", research_system, messages, use_web_search=True)

# Agentic loop for web search turns
turn = 0
while data.stop_reason == "tool_use" and turn < 6:
    turn += 1
    print(f"  Search turn {turn}...")
    time.sleep(5)

    # Append assistant response
    assistant_content = [b.model_dump() for b in data.content]
    messages.append({"role": "assistant", "content": assistant_content})

    # Build tool results
    tool_results = [
        {"type": "tool_result", "tool_use_id": b.id, "content": "completed"}
        for b in data.content if b.type == "tool_use"
    ]
    messages.append({"role": "user", "content": tool_results})
    data = call_api("claude-sonnet-4-6", research_system, messages, use_web_search=True)

research = extract_text(data)
print(f"  Research done. {len(research)} characters.")
if research:
    print(f"  Sample: {research[:300]}")

if len(research) < 50:
    research = (
        "No research data retrieved for " + today + ". "
        "Check web search is enabled at console.anthropic.com"
    )


# ── STEP 2: BRIEF WRITING ─────────────────────────────────────────────────────
# Haiku — no web search, pure formatting task
print("\nStep 2: Writing brief...")
time.sleep(10)

brief_system = (
    "You are the editorial writer for Lobito Intelligence Group. "
    "Write the daily intelligence brief using only the research provided. "
    "Do not add any information not present in the research. "
    "Do not invent prices, companies, or events. "
    "Write in intelligent editorial prose — specific, never generic. "
    "The Broker's Lens must contain a non-obvious actionable insight "
    "derived from today's specific findings."
)

brief_prompt = (
    "Write today's Critical Minerals Intelligence Brief from the research below.\n"
    "Use only what is in the research. If a section has nothing relevant, "
    "write one sentence noting no significant developments.\n\n"
    "TODAY: " + today + "\n\n"
    "RESEARCH:\n" + research + "\n\n"
    "FORMAT - use exactly this structure:\n\n"
    "Lobito Intelligence Group\n"
    "Critical Minerals Intelligence\n"
    + today + " - Daily Brief\n\n"
    "PRICE SNAPSHOT\n"
    "Cobalt: [exact price and source from research] - [one sentence context]\n"
    "Copper: [exact price and source from research] - [one sentence context]\n\n"
    "SUPPLY CHAIN SIGNALS\n"
    "[2-3 paragraphs. Named companies, specific volumes, "
    "specific dates from research only.]\n\n"
    "GEOPOLITICAL RISK\n"
    "[1-2 paragraphs. Named actors, specific policies, "
    "specific timelines from research only.]\n\n"
    "DEMAND DRIVERS\n"
    "[1 paragraph. Named companies and programmes from research only.]\n\n"
    "BROKER'S LENS\n"
    "[3-4 sentences. What should a Western procurement director do differently "
    "THIS WEEK based on today's research? Name specific actions and timeframes. "
    "Never use 'it is worth noting' or 'the situation remains fluid'.]\n\n"
    "-\n"
    "Connecting Western buyers with responsible DRC and Copperbelt supply.\n"
    "Published weekdays. Forward to a colleague in procurement, "
    "supply chain, or commodities."
)

brief_data = call_api(
    "claude-haiku-4-5-20251001",
    brief_system,
    [{"role": "user", "content": brief_prompt}],
    use_web_search=False
)

brief = extract_text(brief_data)
print(f"  Brief done. {len(brief)} characters.")

if len(brief) < 100:
    brief = "Brief generation failed on " + today + ".\n\nResearch collected:\n" + research


# ── STEP 3: SEND EMAIL ────────────────────────────────────────────────────────
print("\nStep 3: Sending email...")

msg = MIMEText(brief, "plain", "utf-8")
msg["Subject"] = "Brief ready - " + today_short
msg["From"] = GMAIL_USER
msg["To"] = GMAIL_USER

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(GMAIL_USER, GMAIL_PASS)
    server.send_message(msg)

print("Done. Brief sent to " + GMAIL_USER)
