"""Extract structured startup info from a raw email body using Claude.

Returns a dict with keys: company_name, url, ceo, deck_url (or None).
Uses claude-haiku-4-5 — fast and cheap for this extraction-only task.
"""

import json
import re

from anthropic import Anthropic

EXTRACT_MODEL = "claude-haiku-4-5-20251001"

# URL patterns that indicate a pitch deck link rather than a company website.
DECK_URL_PATTERNS = re.compile(
    r"https?://(?:"
    r"docsend\.com/view/\S+"
    r"|docs\.google\.com/presentation/\S+"
    r"|slides\.google\.com/\S+"
    r"|notion\.so/\S+"
    r"|pitch\.com/\S+"
    r"|\S+\.pdf"
    r")",
    re.IGNORECASE,
)

EXTRACTION_PROMPT = """You will receive the body of an email (which may be a forwarded
startup pitch, a cold outreach from a founder, or a forwarding with commentary).

Extract the following fields and respond with ONLY a JSON object, no prose:

{
  "company_name": "string or null",
  "url": "string (company website URL) or null",
  "ceo": "string (CEO / founder name) or null",
  "deck_url": "string (link to pitch deck — Docsend, Google Slides, PDF URL, Notion, Pitch.com) or null",
  "confidence": "high | medium | low"
}

Rules:
- For `url`, prefer the company's homepage over any other link.
- For `deck_url`, prefer Docsend > Google Slides > Notion > PDF URL > other.
- If the email is a forward, focus on the *original* message content.
- If a field is genuinely absent, use null — do NOT guess.
- `confidence` reflects how certain you are about the overall extraction.
"""


def extract_startup_info(email_body: str) -> dict:
    """Return {company_name, url, ceo, deck_url, confidence} from raw email text."""
    client = Anthropic()
    resp = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=512,
        system=EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": email_body[:12000]}],
    )
    raw = resp.content[0].text.strip()
    # Strip markdown code fences if the model wraps the JSON.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fall back: try to find a JSON object inside the response.
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse extraction response: {raw!r}")


def find_deck_url_in_body(body: str) -> str | None:
    """Scan email body text for known deck-hosting URLs."""
    match = DECK_URL_PATTERNS.search(body)
    return match.group(0) if match else None
