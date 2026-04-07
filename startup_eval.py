"""Startup evaluation tool: produces a VC-style diligence memo.

Usage:
    python startup_eval.py \
        --name "Acme AI" \
        --url "https://acme.ai" \
        --ceo "Jane Doe" \
        [--deck path/to/deck.pdf | --deck https://.../deck.pdf] \
        [--out memo.md]
"""

import argparse
import base64
import sys
from pathlib import Path

from anthropic import Anthropic

MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """You are a seasoned early-stage venture capital partner with 20+ \
years of experience across seed through Series B. You have seen thousands of pitches \
and know where founders gloss over weaknesses. Your job is to produce a sharp, \
skeptical initial diligence memo on a startup.

Rules:
- Use the web_search tool aggressively to verify claims, find the founder's background,
  map competitors (both incumbents and startups), and understand market dynamics.
- Do not take the founder's framing at face value. Triangulate.
- Be direct about weaknesses. A good memo surfaces the reasons NOT to invest as
  clearly as the reasons to invest.
- Cite sources inline as [source: domain] when making factual claims from search.
- If you cannot verify something, say so explicitly rather than guessing.

Output a Markdown memo with exactly these sections, in this order:

# {Company} — Initial Diligence Memo

## TL;DR
2-4 sentences: what they do, stage signal, and your top-level take (Pass / Track /
Dig deeper / Strong interest) with one-line rationale.

## 1. Team
Founder backgrounds, prior exits/failures, domain fit, completeness of team.
**Key risks & diligence questions:** bulleted.

## 2. Market
TAM/SAM reality-check, growth drivers, timing, regulatory context.
**Key risks & diligence questions:** bulleted.

## 3. Solution
What it actually is (not marketing copy), technical defensibility, what's hard about
building it, moat hypothesis.
**Key risks & diligence questions:** bulleted.

## 4. Competition
Named incumbents AND named startups (at least 3-5 of each if they exist). For each,
one line on how the target is differentiated — and honestly whether that difference
matters.
**Key risks & diligence questions:** bulleted.

## 5. Business Model
Pricing, unit economics hypothesis, GTM, sales cycle, who actually writes the check.
**Key risks & diligence questions:** bulleted.

## 6. Exit Partners
Realistic acquirers (named) and the strategic rationale each would have. Note IPO
viability only if genuinely plausible.
**Key risks & diligence questions:** bulleted.

## Top 5 Things to Investigate Next
Ranked, concrete diligence actions — the things that would actually change your mind.
"""


def build_deck_block(deck: str | None):
    """Return a document content block for the pitch deck, or None."""
    if not deck:
        return None
    if deck.startswith("http://") or deck.startswith("https://"):
        return {
            "type": "document",
            "source": {"type": "url", "url": deck},
            "title": "Pitch Deck",
        }
    path = Path(deck)
    if not path.exists():
        sys.exit(f"Deck file not found: {deck}")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": data,
        },
        "title": path.name,
    }


def build_user_message(name: str, url: str, ceo: str, deck: str | None):
    text = (
        f"Please produce an initial VC diligence memo on this company.\n\n"
        f"- Company: {name}\n"
        f"- Website: {url}\n"
        f"- CEO: {ceo}\n"
        f"- Pitch deck: {'attached' if deck else 'not provided'}\n\n"
        "Start by searching the web for the company, the CEO's background, and the "
        "competitive landscape before writing the memo. Be rigorous and skeptical."
    )
    content: list = []
    deck_block = build_deck_block(deck)
    if deck_block:
        content.append(deck_block)
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def run_evaluation(name: str, url: str, ceo: str, deck: str | None) -> str:
    client = Anthropic()
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}]

    messages = [build_user_message(name, url, ceo, deck)]

    # Agentic loop: keep handing tool results back until Claude stops calling tools.
    while True:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        # Server-side tools (web_search) are executed by the API; we only need to
        # loop if the model returns client-side tool_use, which it shouldn't here.
        # Defensive: break to avoid an infinite loop.
        break

    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def main():
    p = argparse.ArgumentParser(description="VC-style startup diligence memo.")
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--ceo", required=True)
    p.add_argument("--deck", help="Path to pitch deck PDF or URL (optional)")
    p.add_argument("--out", help="Write memo to this file instead of stdout")
    args = p.parse_args()

    memo = run_evaluation(args.name, args.url, args.ceo, args.deck)

    if args.out:
        Path(args.out).write_text(memo)
        print(f"Wrote memo to {args.out}")
    else:
        print(memo)


if __name__ == "__main__":
    main()
