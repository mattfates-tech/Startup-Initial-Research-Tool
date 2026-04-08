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

from anthropic import Anthropic, BadRequestError, RateLimitError

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
- Cite sources via the web_search tool's native citations — every factual claim from
  search should be anchored to a searched page. If you cannot verify something, write
  "unverified" rather than guessing.
- Output ONLY the memo. Do not include any preamble, thinking, or narration about your
  research process. Your first character must be `#` (the top-level heading).

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


def _call_model(client, messages):
    """Call the model, automatically retrying once on a rate-limit error."""
    import time
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
            messages=messages,
        )
    except RateLimitError as exc:
        # Honor the API's retry-after hint, capped at 90 seconds.
        retry_after = 60
        try:
            hint = exc.response.headers.get("retry-after")
            if hint:
                retry_after = min(int(float(hint)), 90)
        except Exception:
            pass
        print(f"Rate limit hit — sleeping {retry_after}s then retrying once")
        time.sleep(retry_after)
        return client.messages.create(
            model=MODEL,
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
            messages=messages,
        )


def run_evaluation(name: str, url: str, ceo: str, deck: str | None) -> str:
    client = Anthropic()

    messages = [build_user_message(name, url, ceo, deck)]
    try:
        resp = _call_model(client, messages)
    except BadRequestError as exc:
        # If the deck URL can't be fetched (e.g. Docsend requires auth, dead link),
        # retry without it and note the failure in the prompt so the memo reflects it.
        if deck and "Unable to download the file" in str(exc):
            print(f"Deck fetch failed for {deck!r} — retrying without deck")
            messages = [build_user_message(name, url, ceo, None)]
            messages[0]["content"][-1]["text"] += (
                f"\n\nNote: A pitch deck link was provided ({deck}) but could not be "
                "fetched (likely gated behind authentication or a dead link). "
                "Proceed with the memo based on web research alone, and flag in the "
                "memo that the deck was unavailable for review."
            )
            resp = _call_model(client, messages)
        else:
            raise

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
            # Extract bare domain.
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
