"""Simulate a Mailgun inbound webhook POST to test the full pipeline locally.

Usage:
    python test_inbound.py \
        --to your@email.com \
        --company "Acme AI" \
        --url "https://acme.ai" \
        --ceo "Jane Doe" \
        [--deck https://docsend.com/view/...]

Or use a canned sample pitch email:
    python test_inbound.py --to your@email.com --sample

The script POSTs to http://localhost:8000/inbound exactly as Mailgun would,
then prints the HTTP response. The evaluation runs and the reply is sent to
--to via the Mailgun sandbox.

Make sure email_server.py is running first:
    python email_server.py
"""

import argparse
import sys

import requests

SAMPLE_EMAIL = """
---------- Forwarded message ---------
From: Sarah Chen <sarah@luminary.ai>
Date: Mon, 7 Apr 2026 09:14:22 -0700
Subject: Luminary AI - Seed Round
To: invest@vcfirm.com

Hi,

I wanted to introduce you to Luminary AI. We're building the first AI-native
legal research platform specifically for mid-market law firms (50–500 attorneys).

Our product replaces the $4,200/seat/year Westlaw subscription with an AI
assistant trained on case law, statutes, and firm-specific precedents, at
$800/seat/year. We launched in January and have 12 paying firms (480 seats)
with zero churn.

Company website: https://luminary.ai
Pitch deck: https://docsend.com/view/luminary-seed-2026

CEO: Sarah Chen (former Casetext eng lead, Stanford CS/JD)
Co-founder: Marcus Webb (ex-Thomson Reuters product, 10 years legal tech)

Happy to set up a call.
Sarah
""".strip()


def simulate_inbound(server_url: str, sender: str, subject: str, body: str) -> None:
    payload = {
        "recipient": "evaluate@mg.yourdomain.com",
        "sender": sender,
        "from": f"Test Sender <{sender}>",
        "subject": subject,
        "body-plain": body,
        "stripped-text": body,
        "body-html": f"<pre>{body}</pre>",
        "attachment-count": "0",
        # Mailgun signature fields — verification is skipped when MAILGUN_WEBHOOK_KEY is unset
        "token": "test-token",
        "timestamp": "1712500000",
        "signature": "test-signature",
    }
    print(f"POSTing to {server_url}/inbound ...")
    resp = requests.post(f"{server_url}/inbound", data=payload, timeout=300)
    print(f"Status: {resp.status_code}")
    print(f"Body:   {resp.text}")
    if resp.status_code == 200:
        print(f"\nEvaluation triggered. Check {sender} for the reply memo.")
    else:
        print("\nSomething went wrong — check the server logs.")


def main():
    p = argparse.ArgumentParser(description="Simulate Mailgun inbound webhook.")
    p.add_argument("--to", required=True, help="Your email address (reply will be sent here)")
    p.add_argument("--server", default="http://localhost:8000", help="Server URL (default: http://localhost:8000)")
    p.add_argument("--sample", action="store_true", help="Use built-in sample pitch email")
    p.add_argument("--company", help="Company name (used when --sample is not set)")
    p.add_argument("--url", help="Company website URL")
    p.add_argument("--ceo", help="CEO name")
    p.add_argument("--deck", help="Deck URL (optional)")
    args = p.parse_args()

    if args.sample:
        body = SAMPLE_EMAIL
        subject = "Luminary AI - Seed Round (forwarded)"
    else:
        if not all([args.company, args.url, args.ceo]):
            p.error("--company, --url, and --ceo are required unless using --sample")
        lines = [
            f"Company: {args.company}",
            f"Website: {args.url}",
            f"CEO: {args.ceo}",
        ]
        if args.deck:
            lines.append(f"Pitch deck: {args.deck}")
        body = "\n".join(lines)
        subject = f"{args.company} — evaluation request"

    simulate_inbound(args.server, args.to, subject, body)


if __name__ == "__main__":
    main()
