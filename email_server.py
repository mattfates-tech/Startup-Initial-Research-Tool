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

For local testing, expose port 8000 with:
  ngrok http 8000
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
from flask import Flask, Response, abort, render_template_string, request

from email_extractor import extract_startup_info, find_deck_url_in_body
from startup_eval import run_evaluation

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB upload cap
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    """Backstop: render any unhandled exception as a friendly error page."""
    log.exception("Unhandled exception")
    error_html = """<!DOCTYPE html><html><body style="font-family: sans-serif; max-width: 700px; margin: 60px auto; padding: 0 20px;">
<h1>Something went wrong</h1>
<pre style="background: #f8f8f8; padding: 16px; border-radius: 6px; white-space: pre-wrap;">{type}: {msg}</pre>
<p><a href="/">← Try again</a></p>
</body></html>"""
    return error_html.format(type=type(exc).__name__, msg=str(exc)), 500

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
EVAL_EMAIL = os.environ.get("EVAL_EMAIL", f"evaluate@{MAILGUN_DOMAIN}" if MAILGUN_DOMAIN else "")
MAILGUN_WEBHOOK_KEY = os.environ.get("MAILGUN_WEBHOOK_KEY", "")
MAILGUN_CONFIGURED = bool(MAILGUN_API_KEY and MAILGUN_DOMAIN)

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
    """Save the first PDF attachment to a temp file; return its path or None.

    Recognizes a file as a PDF if EITHER its content type is application/pdf
    OR its filename ends in .pdf (Outlook and some forwarders mislabel PDFs
    as application/octet-stream).
    """
    count = int(request.form.get("attachment-count", 0))
    log.info("Inbound email has %d attachment(s)", count)
    for i in range(1, count + 1):
        attachment = request.files.get(f"attachment-{i}")
        if not attachment:
            continue
        filename = attachment.filename or ""
        ctype = attachment.content_type or ""
        is_pdf = ctype == "application/pdf" or filename.lower().endswith(".pdf")
        log.info("  attachment-%d: %r content_type=%r is_pdf=%s", i, filename, ctype, is_pdf)
        if is_pdf:
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.close()  # Required on Windows so .save() can reopen the file.
            attachment.save(tmp.name)
            log.info("  -> saved as %s (%d bytes)", tmp.name, Path(tmp.name).stat().st_size)
            return tmp.name
    return None


# ---------------------------------------------------------------------------
# Main webhook
# ---------------------------------------------------------------------------

@app.route("/inbound", methods=["POST"])
def inbound():
    if not MAILGUN_CONFIGURED:
        log.warning("Received inbound POST but Mailgun is not configured")
        return Response("mailgun not configured", status=503)
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
# Browser-based web form (use this at http://localhost:8000/)
# ---------------------------------------------------------------------------

FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Startup Diligence Tool</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 680px; margin: 60px auto; padding: 0 20px; color: #1a1a1a; }
  h1 { margin-bottom: 6px; }
  p.tagline { color: #666; margin-top: 0; }
  form { display: flex; flex-direction: column; gap: 14px; margin-top: 32px; }
  label { font-weight: 600; font-size: 14px; }
  input[type=text] { padding: 10px 12px; font-size: 15px; border: 1px solid #ccc;
                     border-radius: 6px; font-family: inherit; }
  button { padding: 14px; font-size: 16px; font-weight: 600; background: #1a1a1a;
           color: white; border: 0; border-radius: 6px; cursor: pointer;
           margin-top: 8px; }
  button:hover { background: #333; }
  button:disabled { background: #999; cursor: wait; }
  .hint { color: #888; font-size: 13px; margin-top: -6px; }
</style>
</head>
<body>
  <h1>Startup Diligence Tool</h1>
  <p class="tagline">A skeptical VC memo, researched and written in ~90 seconds.</p>
  <form method="POST" action="/evaluate" enctype="multipart/form-data" onsubmit="this.querySelector('button').disabled=true; this.querySelector('button').innerText='Researching... (~90 sec)';">
    <label for="name">Company name</label>
    <input type="text" name="name" id="name" required placeholder="Anthropic">

    <label for="url">Website URL</label>
    <input type="text" name="url" id="url" required placeholder="https://anthropic.com">

    <label for="ceo">CEO name</label>
    <input type="text" name="ceo" id="ceo" required placeholder="Dario Amodei">

    <label for="deck_file">Pitch deck PDF (optional)</label>
    <input type="file" name="deck_file" id="deck_file" accept="application/pdf,.pdf">
    <div class="hint">Upload a PDF directly from your computer.</div>

    <label for="deck">— or — pitch deck link (optional)</label>
    <input type="text" name="deck" id="deck" placeholder="https://... (public PDF only)">
    <div class="hint">Gated links like Docsend require authentication and won't work.</div>

    <button type="submit">Generate diligence memo</button>
  </form>
</body>
</html>
"""

RESULT_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{{ company }} — Diligence Memo</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #1a1a1a;
         line-height: 1.55; }
  h1 { border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }
  h2 { margin-top: 32px; color: #2c2c2c; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; vertical-align: top; }
  th { background: #f5f5f5; }
  code { background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }
  hr { border: none; border-top: 1px solid #e0e0e0; margin: 24px 0; }
  a.back { display: inline-block; margin-bottom: 24px; color: #666;
           text-decoration: none; font-size: 14px; }
  a.back:hover { color: #000; }
</style>
</head>
<body>
  <a href="/" class="back">← Evaluate another company</a>
  {{ memo|safe }}
</body>
</html>
"""

ERROR_HTML = """
<!DOCTYPE html>
<html>
<body style="font-family: sans-serif; max-width: 700px; margin: 60px auto; padding: 0 20px;">
<h1>Something went wrong</h1>
<pre style="background: #f8f8f8; padding: 16px; border-radius: 6px; white-space: pre-wrap; word-wrap: break-word;">{{ error }}</pre>
<p><a href="/">← Try again</a></p>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def form():
    return render_template_string(FORM_HTML)


@app.route("/evaluate", methods=["POST"])
def evaluate():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    ceo = request.form.get("ceo", "").strip()
    deck_url = request.form.get("deck", "").strip() or None

    if not all([name, url, ceo]):
        return render_template_string(ERROR_HTML, error="Company name, URL, and CEO are all required."), 400

    # Uploaded PDF takes priority over a linked URL.
    pdf_path: str | None = None
    uploaded = request.files.get("deck_file")
    if uploaded and uploaded.filename:
        if not uploaded.filename.lower().endswith(".pdf"):
            return render_template_string(ERROR_HTML, error="Pitch deck upload must be a PDF file."), 400
        # IMPORTANT: close the tempfile handle before .save() — on Windows, you cannot
        # open a file for writing while another handle to it is open.
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        uploaded.save(tmp.name)
        pdf_path = tmp.name
        log.info("Saved uploaded deck: %s -> %s (%d bytes)",
                 uploaded.filename, pdf_path, Path(pdf_path).stat().st_size)

    deck = pdf_path or deck_url
    log.info("Web form evaluation: company=%r url=%r ceo=%r deck=%r", name, url, ceo, deck)

    try:
        memo_md = run_evaluation(name, url, ceo, deck)
        memo_html = markdown.markdown(memo_md, extensions=["tables"])
        return render_template_string(RESULT_HTML, company=name, memo=memo_html)
    except Exception as exc:
        log.exception("Evaluation failed")
        # Friendlier message for the most common failure mode.
        msg = str(exc)
        if "rate_limit_error" in msg or "RateLimitError" in type(exc).__name__:
            friendly = (
                "Anthropic API rate limit hit (30,000 input tokens/minute on this key). "
                "Please wait about 60 seconds and try again. If this happens repeatedly, "
                "the rate limit on your API key needs to be increased in the Anthropic console."
            )
            return render_template_string(ERROR_HTML, error=friendly), 429
        return render_template_string(ERROR_HTML, error=f"{type(exc).__name__}: {exc}"), 500
    finally:
        if pdf_path:
            Path(pdf_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return {"status": "ok", "mailgun_configured": MAILGUN_CONFIGURED, "eval_email": EVAL_EMAIL}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    log.info("Starting server on port %d, receiving at %s", port, EVAL_EMAIL)
    app.run(host="0.0.0.0", port=port)
