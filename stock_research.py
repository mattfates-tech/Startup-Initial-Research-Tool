"""Morning stock research agent: produces a daily briefing with 3 top picks.

Researches current market conditions via web search and writes a Markdown
briefing recommending exactly three public stocks, with the reasoning,
catalysts, and risks behind each pick.

Usage:
    python stock_research.py                       # print briefing to stdout
    python stock_research.py --out briefing.md     # write to a file
    python stock_research.py --email you@x.com     # email via Mailgun
    python stock_research.py --focus "AI and semiconductors"

Scheduling: .github/workflows/morning-stocks.yml runs this every weekday
morning before the US market opens and emails the result.

Environment variables:
  ANTHROPIC_API_KEY    - required
  MAILGUN_API_KEY      - required only for email delivery
  MAILGUN_DOMAIN       - required only for email delivery
  STOCK_REPORT_EMAIL   - default recipient (overridden by --email)

This tool is for research and educational purposes only. It is not
financial advice.
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from anthropic import Anthropic

MODEL = "claude-opus-4-8"
MAX_CONTINUATIONS = 5  # safety cap on pause_turn resumes from server-side search

SYSTEM_PROMPT = """You are a rigorous sell-side equity research analyst writing a \
pre-market morning briefing. Every trading day you scan the market and recommend \
the three most attractive public stocks to research further today.

Rules:
- Use the web_search tool aggressively: today's pre-market news, yesterday's
  closes, earnings reports and guidance, analyst actions, macro data releases,
  and sector momentum. Your picks must be grounded in CURRENT information from
  search, never from memory — prices and news change daily.
- Recommend exactly 3 stocks, all listed on major US exchanges (NYSE/Nasdaq) or
  available as US ADRs. Diversify: avoid three picks from the same narrow theme
  unless the evidence is overwhelming.
- A good pick has a concrete, near-term catalyst or a clear mispricing argument —
  "great company" is not a thesis. Be specific about WHY TODAY.
- Be honest about risk. Every pick must include what would make the thesis wrong.
- Cite sources via the web_search tool's native citations — anchor every factual
  claim (prices, earnings figures, analyst actions) to a searched page. If you
  cannot verify a number, write "unverified" rather than guessing.
- Output ONLY the briefing. No preamble or narration about your research process.
  Your first character must be `#` (the top-level heading).

Output a Markdown briefing with exactly these sections, in this order:

# Morning Stock Briefing — {date}

## Market Snapshot
3-5 sentences: where futures/indices stand, the macro events and earnings on
today's calendar, and the prevailing tone (risk-on/risk-off) with why.

## Pick 1: {Company} ({TICKER})
**Thesis (one line):** the core argument in a single sentence.
- **What they do:** one line.
- **Why now:** the specific catalyst or setup — news, earnings, guidance,
  analyst action, technical level, or valuation dislocation. With numbers.
- **Valuation & momentum:** current price/recent move, relevant multiples or
  growth rates vs. peers.
- **Key risks:** 2-3 bullets — what kills this thesis.
- **Suggested horizon:** swing (days-weeks) / position (months) / long-term.

## Pick 2: {Company} ({TICKER})
Same structure as Pick 1.

## Pick 3: {Company} ({TICKER})
Same structure as Pick 1.

## Watchlist
2-4 names that almost made the cut, one line each on what you're waiting for.

## Disclaimer
One short paragraph: this is automated research for educational purposes, not
financial advice; do your own diligence; past performance does not guarantee
future results.
"""


def build_user_message(focus: str | None) -> dict:
    today = date.today().strftime("%A, %B %d, %Y")
    text = (
        f"Today is {today}. Produce this morning's stock briefing.\n\n"
        "Start by searching for today's pre-market movers, overnight market news, "
        "this week's earnings calendar, and any macro releases scheduled for today. "
        "Then dig into your candidate picks before writing the briefing."
    )
    if focus:
        text += (
            f"\n\nReader's focus request for today: {focus}. Weight your picks "
            "toward this where the evidence supports it, but do not force weak "
            "picks just to match the theme."
        )
    return {"role": "user", "content": text}


def run_research(focus: str | None = None) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set. Locally: add it to .env or export it. "
            "In GitHub Actions: add it as a repository secret "
            "(Settings → Secrets and variables → Actions)."
        )
    client = Anthropic()
    messages = [build_user_message(focus)]

    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 15}]

    # Server-side search can pause the turn at its iteration limit; re-send to
    # let the server resume where it left off.
    for _ in range(MAX_CONTINUATIONS):
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]

    # Walk text blocks, inlining web_search citations as [domain] tags.
    parts: list[str] = []
    for b in resp.content:
        if getattr(b, "type", None) != "text":
            continue
        chunk = b.text
        cites = getattr(b, "citations", None) or []
        domains: list[str] = []
        seen: set[str] = set()
        for c in cites:
            url = getattr(c, "url", None) or ""
            if not url:
                continue
            dom = url.split("://", 1)[-1].split("/", 1)[0]
            if dom.startswith("www."):
                dom = dom[4:]
            if dom and dom not in seen:
                seen.add(dom)
                domains.append(dom)
        if domains:
            chunk = chunk + " [" + ", ".join(domains) + "]"
        parts.append(chunk)
    text = "".join(parts)

    # Strip any preamble before the first Markdown heading.
    if text.lstrip().startswith("# "):
        return text.lstrip()
    idx = text.find("\n# ")
    if idx != -1:
        return text[idx + 1 :]
    return text


def send_briefing(to: str, briefing_markdown: str) -> None:
    """Email the briefing via Mailgun (same account the eval tool uses)."""
    import markdown
    import requests

    api_key = os.environ.get("MAILGUN_API_KEY", "")
    domain = os.environ.get("MAILGUN_DOMAIN", "")
    if not (api_key and domain):
        sys.exit("Email delivery requires MAILGUN_API_KEY and MAILGUN_DOMAIN")

    body_html = markdown.markdown(briefing_markdown, extensions=["tables"])
    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family: Georgia, serif; max-width: 720px; margin: 0 auto; padding: 24px; color: #222;">
{body_html}
</body>
</html>"""

    resp = requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", api_key),
        data={
            "from": f"Morning Stocks <stocks@{domain}>",
            "to": [to],
            "subject": f"Morning Stock Briefing — {date.today().strftime('%b %d, %Y')}",
            "text": briefing_markdown,
            "html": html,
        },
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Briefing emailed to {to} (Mailgun id: {resp.json().get('id')})")


def main():
    p = argparse.ArgumentParser(description="Daily 3-pick stock research briefing.")
    p.add_argument("--out", help="Write briefing to this file")
    p.add_argument(
        "--email",
        nargs="?",
        const="",
        help="Email the briefing (defaults to STOCK_REPORT_EMAIL env var)",
    )
    p.add_argument("--focus", help="Optional theme to weight picks toward")
    args = p.parse_args()

    briefing = run_research(args.focus)

    if args.out:
        Path(args.out).write_text(briefing)
        print(f"Wrote briefing to {args.out}")
    if args.email is not None:
        to = args.email or os.environ.get("STOCK_REPORT_EMAIL", "")
        if not to:
            sys.exit("No recipient: pass --email <addr> or set STOCK_REPORT_EMAIL")
        send_briefing(to, briefing)
    if not args.out and args.email is None:
        print(briefing)


if __name__ == "__main__":
    main()
