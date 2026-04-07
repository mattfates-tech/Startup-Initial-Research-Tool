"""Inbound email webhook server for the startup evaluation tool.

Flow:
  1. User forwards a startup pitch email to evaluate@<MAILGUN_DOMAIN>
  2. Mailgun routes it to POST /inbound on this server
  3. Claude (haiku) extracts company name, URL, CEO, deck link from the email
  4. The evaluator (claude-opus-4-6 + web_search) produces a diligence memo
  5. The memo is emailed back to the sender via Mailgun

Setup (one-time):
  1. Sign up at mailgun.com, add and verify a domain (or use the sandbox).
  2. In Mailgun → Receiving → Create Route:
       Filter:  match_recipient("evaluate@<your-domain>")
       Action:  forward("https://<your-server>/inbound")
       Priority: 10
  3. Copy your Mailgun API key (Settings → API Keys → Private API key).
  4. Set environment variables (see .env.example).
  5. Run:  python email_server.py
     Or with gunicorn:  gunicorn email_server:app

For local testing, expose port 5000 with:
  ngrok http 5000
Then set the Mailgun route URL to your ngrok HTTPS URL + /inbound.

Environment variables (all required unless noted):
  ANTHROPIC_API_KEY   - Anthropic API key
  MAILGUN_API_KEY     - Mailgun private API key
  MAILGUN_DOMAIN      - e.g. mg.yourdomain.com
  EVAL_EMAIL          - The inbound address, e.g. evaluate@mg.yourdomain.com
  MAILGUN_WEBHOOK_KEY - (optional) Mailgun webhook signing key for request verification
                        Found in Mailgun → Webhooks → HTTP webhook signing key
"""

import base64
import hashlib
import hmac
import logging
import os
import tempfile
from pathlib import Path

import markdown
import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, request

from email_extractor import extract_startup_info, find_deck_url_in_body
from startup_eval import run_evaluation

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MAILGUN_API_KEY = os.environ["MAILGUN_API_KEY"]
MAILGUN_DOMAIN = os.environ["MAILGUN_DOMAIN"]
EVAL_EMAIL = os.environ.get("EVAL_EMAIL", f"evaluate@{MAILGUN_DOMAIN}")
MAILGUN_WEBHOOK_KEY = os.environ.get("MAILGUN_WEBHOOK_KEY", "")

MAILGUN_API_BASE = "https://api.mailgun.net/v3"


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def _verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """Return True if the webhook is genuinely from Mailgun."""
    if not MAILGUN_WEBHOOK_KEY:
        return True  # Verification disabled — only do this in dev.
    digest = hmac.new(
        key=MAILGUN_WEBHOOK_KEY.encode(),
        msg=(timestamp + token).encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, signature)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_reply(to: str, subject: str, memo_markdown: str) -> None:
    """Send the diligence memo back to the requester via Mailgun."""
    memo_html = markdown.markdown(memo_markdown, extensions=["tables"])
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
  h1 {{ border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }}
  h2 {{ margin-top: 32px; color: #2c2c2c; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f5f5f5; }}
  blockquote {{ border-left: 4px solid #ccc; margin: 0; padding-left: 16px; color: #555; }}
  code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 24px 0; }}
</style>
</head>
<body>
{memo_html}
</body>
</html>"""

    resp = requests.post(
        f"{MAILGUN_API_BASE}/{MAILGUN_DOMAIN}/messages",
        auth=("api", MAILGUN_API_KEY),
        data={
            "from": f"Startup Evaluator <{EVAL_EMAIL}>",
            "to": [to],
            "subject": subject,
            "text": memo_markdown,
            "html": html_body,
        },
        timeout=30,
    )
    resp.raise_for_status()
    log.info("Reply sent to %s (Mailgun id: %s)", to, resp.json().get("id"))


def send_error(to: str, original_subject: str, error_msg: str) -> None:
    """Send a brief error email if extraction or evaluation fails."""
    body = (
        f"Sorry, I wasn't able to evaluate this startup.\n\n"
        f"Reason: {error_msg}\n\n"
        f"Please reply with the company name, website URL, and CEO name "
        f"clearly stated, and I'll try again.\n\n"
        f"— Startup Evaluator"
    )
    requests.post(
        f"{MAILGUN_API_BASE}/{MAILGUN_DOMAIN}/messages",
        auth=("api", MAILGUN_API_KEY),
        data={
            "from": f"Startup Evaluator <{EVAL_EMAIL}>",
            "to": [to],
            "subject": f"Re: {original_subject} — evaluation failed",
            "text": body,
        },
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------

def _extract_pdf_attachment() -> str | None:
    """Save the first PDF attachment to a temp file; return its path or None."""
    count = int(request.form.get("attachment-count", 0))
    for i in range(1, count + 1):
        attachment = request.files.get(f"attachment-{i}")
        if attachment and attachment.content_type == "application/pdf":
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            attachment.save(tmp.name)
            log.info("Saved PDF attachment: %s (%d bytes)", attachment.filename, Path(tmp.name).stat().st_size)
            return tmp.name
    return None


# ---------------------------------------------------------------------------
# Main webhook
# ---------------------------------------------------------------------------

@app.route("/inbound", methods=["POST"])
def inbound():
    # Verify signature.
    token = request.form.get("token", "")
    timestamp = request.form.get("timestamp", "")
    signature = request.form.get("signature", "")
    if not _verify_mailgun_signature(token, timestamp, signature):
        log.warning("Invalid Mailgun webhook signature — rejected")
        abort(403)

    sender = request.form.get("sender", "")
    subject = request.form.get("subject", "")
    # Prefer stripped text (no quoted history) but fall back to full plain body.
    body = request.form.get("stripped-text") or request.form.get("body-plain", "")
    # Include HTML body as fallback for URL extraction even if we don't read it directly.
    html_body = request.form.get("body-html", "")

    log.info("Inbound email from=%s subject=%r body_len=%d", sender, subject, len(body))

    if not sender or not body:
        log.warning("Empty sender or body — ignoring")
        return Response("ok", status=200)

    # --- Extract structured info ---
    try:
        info = extract_startup_info(body)
        log.info("Extraction result: %s", info)
    except Exception as exc:
        log.exception("Extraction failed")
        send_error(sender, subject, str(exc))
        return Response("ok", status=200)

    company = info.get("company_name")
    url = info.get("url")
    ceo = info.get("ceo")
    deck_url = info.get("deck_url") or find_deck_url_in_body(body) or find_deck_url_in_body(html_body)
    confidence = info.get("confidence", "medium")

    # Validate we have the minimum required fields.
    missing = [f for f, v in [("company name", company), ("website URL", url), ("CEO name", ceo)] if not v]
    if missing:
        msg = f"Could not determine: {', '.join(missing)}. (extraction confidence: {confidence})"
        log.warning(msg)
        send_error(sender, subject, msg)
        return Response("ok", status=200)

    # --- Handle deck (PDF attachment takes priority over a linked URL) ---
    pdf_path = _extract_pdf_attachment()
    deck = pdf_path or deck_url  # pdf_path is a local filesystem path; deck_url is an HTTP URL

    log.info(
        "Running evaluation: company=%r url=%r ceo=%r deck=%r",
        company, url, ceo, deck,
    )

    # --- Run evaluation ---
    try:
        memo = run_evaluation(company, url, ceo, deck)
    except Exception as exc:
        log.exception("Evaluation failed")
        send_error(sender, subject, f"Evaluation error: {exc}")
        return Response("ok", status=200)
    finally:
        # Clean up temp PDF file if we created one.
        if pdf_path:
            Path(pdf_path).unlink(missing_ok=True)

    # --- Reply ---
    reply_subject = f"Diligence Memo: {company}"
    try:
        send_reply(sender, reply_subject, memo)
    except Exception as exc:
        log.exception("Failed to send reply email")
        return Response("send_error", status=500)

    return Response("ok", status=200)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return {"status": "ok", "eval_email": EVAL_EMAIL}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting server on port %d, receiving at %s", port, EVAL_EMAIL)
    app.run(host="0.0.0.0", port=port)
