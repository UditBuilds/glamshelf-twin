"""
Glam Shelf Twin — Phase 0
Flask app that drafts WhatsApp customer-service replies in The Glam Shelf voice.

Runs on:
  - Local Windows: `python app.py` → Flask dev server on http://localhost:5000
  - Render (Linux): `gunicorn app:app` via Procfile, binds to $PORT

Auth: ANTHROPIC_API_KEY environment variable.
  - Local: put it in .env (loaded by python-dotenv)
  - Render: set it in the service's Environment dashboard
"""

import json
import os
import sys
import traceback
from functools import wraps
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Load .env for local dev. On Render, env vars come from the dashboard
# and python-dotenv silently no-ops if .env is missing.
# override=True so .env wins over any stale empty env vars in the parent shell.
load_dotenv(override=True)

# Best-effort UTF-8 line-buffered stdout/stderr so [INFO] prints (and 🤍 emoji
# in Claude responses) appear cleanly. Some hosting environments wrap stdout
# in a stream that doesn't support reconfigure — never let that crash startup.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    except Exception:
        pass

app = Flask(__name__)

# Session secret for cookie signing. Override SECRET_KEY in Render's env vars.
app.secret_key = os.environ.get("SECRET_KEY", "gs-twin-secret-xk92")

# Single-user password gate. Override APP_PASSWORD in Render's env vars.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "glamshelf2026")

PROJECT_DIR = Path(__file__).parent.resolve()
BRAIN_FILE = PROJECT_DIR / "brain" / "brain.md"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# Telegram notification config. Override on Render via env vars.
# SECURITY NOTE: The defaults below are committed to source — fine for a
# private internal tool but rotate the bot token if the repo ever goes public.
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "***REVOKED-TELEGRAM-TOKEN***"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6733243879")
TELEGRAM_TIMEOUT_SECONDS = 5

# WATI (WhatsApp Business API) config. Set these in Render env vars.
# WATI_ENDPOINT format: https://live-mt-server.wati.io/<account_id>
WATI_API_KEY = os.environ.get("WATI_API_KEY", "")
WATI_ENDPOINT = os.environ.get("WATI_ENDPOINT", "")
WATI_TIMEOUT_SECONDS = 10

# The Glam Shelf's WhatsApp Business number. The webhook ignores any inbound
# event where waId equals this number — prevents the twin from replying to
# itself if WATI ever loops outbound / own messages through the webhook.
BUSINESS_NUMBER = os.environ.get("BUSINESS_NUMBER", "919217470151")

# Anthropic client picks up ANTHROPIC_API_KEY from the environment.
client = Anthropic()


def send_telegram_notification(
    classification: str,
    customer_message: str,
    reply: str,
    sender_info: str | None = None,
) -> None:
    """Fire a Telegram message to the founder for DRAFT+APPROVE and ESCALATE.

    AUTO classifications send nothing (the reply was safe to send as-is and
    Udit doesn't need to be paged about it).

    sender_info is optional — when present (e.g. when called from the WATI
    webhook), it's prepended to the message so Udit knows which WhatsApp
    contact to reply to.

    All failures (network, Telegram API errors, missing token, etc.) are
    logged and swallowed — Telegram is a side effect, never a blocker for
    the /api/draft response or the /webhook 200 reply.
    """
    if classification == "AUTO":
        return  # No notification needed for safe replies.

    sender_block = f"From: {sender_info}\n\n" if sender_info else ""

    if classification == "DRAFT+APPROVE":
        text = (
            "🟡 DRAFT + APPROVE\n\n"
            f"{sender_block}"
            "Customer said:\n"
            f'"{customer_message}"\n\n'
            "Drafted reply:\n"
            f'"{reply}"\n\n'
            "→ Review and send manually from your WhatsApp Business app."
        )
    elif classification == "ESCALATE":
        text = (
            "🔴 ESCALATE — Take over directly\n\n"
            f"{sender_block}"
            "Customer said:\n"
            f'"{customer_message}"\n\n'
            "Suggested holding reply:\n"
            f'"{reply}"\n\n'
            "→ Do NOT send the reply. Handle this yourself."
        )
    else:
        # Unknown / malformed classification — don't spam Telegram.
        print(f"[TG] Skipped: unknown classification {classification!r}")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    try:
        response = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        if response.ok:
            print(f"[TG] Sent {classification} notification ({len(text)} chars)")
        else:
            print(
                f"[TG] Telegram returned {response.status_code}: "
                f"{response.text[:300]}"
            )
    except requests.RequestException as e:
        print(f"[TG] Network error: {type(e).__name__}: {e}")
    except Exception as e:
        # Defensive — never let a Telegram bug break the API call.
        print(f"[TG] Unexpected error: {type(e).__name__}: {e}")


def send_whatsapp_reply(wa_id: str, reply_text: str) -> None:
    """Send an outbound WhatsApp message to a customer via WATI.

    Used for AUTO-classified replies in the /webhook handler. All failures
    are logged and swallowed — the webhook must always return 200 to WATI
    or WATI will retry and we'll send duplicates.
    """
    if not WATI_API_KEY or not WATI_ENDPOINT:
        print("[WATI] Skipped: WATI_API_KEY or WATI_ENDPOINT not set")
        return

    endpoint = WATI_ENDPOINT.rstrip("/")
    url = f"{endpoint}/api/v1/sendSessionMessage/{wa_id}"
    headers = {
        "Authorization": f"Bearer {WATI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"messageText": reply_text}

    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=WATI_TIMEOUT_SECONDS
        )
        if response.ok:
            print(f"[WATI] Sent reply to {wa_id} ({len(reply_text)} chars)")
        else:
            print(
                f"[WATI] Returned {response.status_code}: "
                f"{response.text[:300]}"
            )
    except requests.RequestException as e:
        print(f"[WATI] Network error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[WATI] Unexpected error: {type(e).__name__}: {e}")


def draft_reply_logic(message: str, order_context: str = "") -> tuple[str, str, str]:
    """Core twin pipeline — load brain, call Claude, parse classification.

    Returns (classification, reply, raw_response).
      - classification: "AUTO" | "DRAFT+APPROVE" | "ESCALATE", or "" if parse failed
      - reply: drafted message text, or "" if parse failed
      - raw_response: exactly what Claude returned (after fence stripping)

    Used by both /api/draft (which returns raw_response to the browser)
    and /webhook (which dispatches based on classification).

    Raises if brain.md is missing or the Claude API call fails — callers
    must catch and decide how to surface the error.
    """
    if not BRAIN_FILE.exists():
        raise FileNotFoundError(f"brain file not found at {BRAIN_FILE}")

    brain = load_brain()
    raw = ask_claude(brain, message, order_context)

    classification = ""
    reply = ""
    try:
        parsed = json.loads(raw)
        classification = (parsed.get("classification") or "").strip()
        reply = (parsed.get("reply") or "").strip()
    except json.JSONDecodeError:
        print("[TWIN] Claude's response wasn't valid JSON — leaving classification/reply empty")

    return classification, reply, raw


def login_required(view):
    """Gate a view behind session auth.

    Browser views (e.g. /) get a redirect to /login.
    JSON endpoints under /api/ get a 401 JSON response so the frontend
    can react gracefully instead of receiving an HTML redirect.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def load_brain() -> str:
    """Read brain.md fresh from disk on every request."""
    print(f"[BRAIN] Loading {BRAIN_FILE}")
    text = BRAIN_FILE.read_text(encoding="utf-8")
    print(f"[BRAIN] Loaded {len(text)} chars")
    return text


def build_user_message(customer_message: str, order_context: str) -> str:
    """The per-request user prompt. The brain itself goes in the `system`
    parameter (with cache_control) — see ask_claude()."""
    return (
        "Customer WhatsApp message:\n"
        f"{customer_message}\n\n"
        "Order context (may be empty):\n"
        f"{order_context or '(none provided)'}\n\n"
        "Based strictly on the brain file in your system context, do two things:\n"
        "1. Classify this situation as AUTO, DRAFT+APPROVE, or ESCALATE per Section 5 rules\n"
        "2. Draft the reply in The Glam Shelf's voice per Section 4 playbook\n\n"
        "Return ONLY raw JSON. Absolutely no markdown code fences. No ```json blocks. "
        "No prose, greeting, or commentary before or after the JSON. Your response "
        "MUST start with the character { and MUST end with the character }.\n"
        "Use this exact shape:\n"
        '{ "classification": "AUTO" | "DRAFT+APPROVE" | "ESCALATE", "reply": "..." }'
    )


def strip_markdown_fences(text: str) -> tuple[str, bool]:
    """Remove ```json ... ``` or ``` ... ``` wrapping from Claude's response.

    Returns (cleaned_text, was_fenced). The boolean lets us log whether
    Claude slipped fences in despite the prompt instruction.
    """
    cleaned = text.strip()
    was_fenced = False
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
        was_fenced = True
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
        was_fenced = True
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
        was_fenced = True
    return cleaned.strip(), was_fenced


def ask_claude(brain: str, customer_message: str, order_context: str) -> str:
    """Call the Anthropic Messages API.

    The brain content is sent as the `system` prompt with `cache_control`
    (ephemeral / 5-minute cache). Since brain.md is identical across all
    requests in a busy session, cache hits make follow-up requests
    significantly cheaper and lower-latency. The customer message and
    order context go in the user prompt and are *not* cached.
    """
    user_text = build_user_message(customer_message, order_context)
    print(
        f"[CLAUDE] Calling {MODEL} "
        f"(brain: {len(brain)} chars, user: {len(user_text)} chars)"
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": brain,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_text}],
    )

    # Combine any top-level text blocks (usually just one).
    raw = "".join(b.text for b in message.content if b.type == "text").strip()

    usage = message.usage
    print(
        f"[CLAUDE] Got {len(raw)} chars back. "
        f"Tokens — input: {usage.input_tokens}, "
        f"output: {usage.output_tokens}, "
        f"cache_create: {getattr(usage, 'cache_creation_input_tokens', 0)}, "
        f"cache_read: {getattr(usage, 'cache_read_input_tokens', 0)}"
    )

    preview = raw[:300] + ("..." if len(raw) > 300 else "")
    print(f"[CLAUDE] Raw response preview:\n        {preview}")

    cleaned, was_fenced = strip_markdown_fences(raw)
    if was_fenced:
        print("[CLAUDE] NOTE: markdown code fences were detected and stripped")
    return cleaned


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            print("[AUTH] Login successful")
            return redirect(url_for("home"))
        print("[AUTH] Login failed (wrong password)")
        error = "Incorrect password. Please try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    print("[AUTH] Logged out")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    print("[INFO] Homepage requested")
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    """Liveness probe. Render can ping this to confirm the deploy works.
    Reports whether brain.md is present so a misconfigured deploy is obvious.
    Intentionally NOT behind login_required — Render needs to hit it without auth."""
    return jsonify({
        "status": "ok",
        "brain_present": BRAIN_FILE.exists(),
        "brain_path": str(BRAIN_FILE),
        "model": MODEL,
        "anthropic_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


@app.route("/api/draft", methods=["POST"])
@login_required
def draft():
    print("\n" + "=" * 60)
    print("[DRAFT] New request received")
    data = request.get_json(silent=True) or {}
    customer_message = (data.get("customer_message") or "").strip()
    order_context = (data.get("order_context") or "").strip()

    print(f"[DRAFT] customer_message ({len(customer_message)} chars):")
    print(f"        {customer_message[:200]}{'...' if len(customer_message) > 200 else ''}")
    print(f"[DRAFT] order_context ({len(order_context)} chars)")

    if not customer_message:
        print("[DRAFT] ERROR: customer_message is empty")
        return jsonify({"error": "customer_message is required"}), 400

    if not BRAIN_FILE.exists():
        print(f"[DRAFT] ERROR: brain file missing at {BRAIN_FILE}")
        return jsonify({"error": f"brain file not found at {BRAIN_FILE}"}), 500

    try:
        classification, reply, raw_response = draft_reply_logic(
            customer_message, order_context
        )

        # Side effect: page the founder on Telegram for non-AUTO classifications.
        # Wrapped in try/except so Telegram issues never break the API response.
        try:
            if classification and reply:
                send_telegram_notification(classification, customer_message, reply)
            else:
                print("[TG] Skipped: parsed JSON missing classification or reply")
        except Exception as e:
            print(f"[TG] Wrapper error: {type(e).__name__}: {e}")

        print(f"[DRAFT] Returning raw response ({len(raw_response)} chars)")
        print("=" * 60 + "\n")
        return jsonify({"raw": raw_response})
    except Exception as e:
        print(f"[DRAFT] EXCEPTION: {type(e).__name__}: {e}")
        print("[DRAFT] Full traceback:")
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Some platforms (and WATI's URL test) send a GET to verify the
    webhook endpoint is reachable. Just respond 200 OK."""
    print("[WEBHOOK] GET verification ping")
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """WATI calls this when a customer sends us an inbound WhatsApp message.

    We ALWAYS return 200, even when nothing is processed or an internal
    error occurs — WATI retries on non-2xx responses, which would cause
    duplicate auto-replies and Telegram spam. The catch-all at the bottom
    is the safety net.

    Flow:
      type != "text" or empty body  →  200, no work
      AUTO classification           →  send_whatsapp_reply(wa_id, reply)
      DRAFT+APPROVE / ESCALATE      →  send_telegram_notification(...) with sender_info
    """
    print("\n" + "=" * 60)
    try:
        data = request.get_json(silent=True) or {}
        message_type = (data.get("type") or "").strip().lower()
        wa_id = (data.get("waId") or "").strip()
        sender_name = (data.get("senderName") or "").strip()
        # WATI sometimes sends `text` as a dict ({"body": "..."}) and sometimes
        # as a plain string. Handle both shapes defensively.
        text_field = data.get("text")
        if isinstance(text_field, dict):
            text_body = (text_field.get("body") or "").strip()
        else:
            text_body = (text_field or "").strip()
        msg_id = (data.get("id") or "").strip()

        print(
            f"[WEBHOOK] type={message_type!r} wa_id={wa_id!r} "
            f"sender={sender_name!r} msg_id={msg_id!r}"
        )

        # Skip non-text events (images, audio, video, documents, stickers, status updates).
        if message_type != "text":
            print(f"[WEBHOOK] Skipped: non-text message type {message_type!r}")
            return jsonify({"status": "ok"}), 200

        if not text_body:
            print("[WEBHOOK] Skipped: empty text body")
            return jsonify({"status": "ok"}), 200

        if not wa_id:
            print("[WEBHOOK] Skipped: missing waId")
            return jsonify({"status": "ok"}), 200

        # Don't process messages from our own business number — prevents
        # the twin from replying to itself if WATI loops outbound events.
        if wa_id == BUSINESS_NUMBER:
            print(f"[WEBHOOK] Skipped: message from business number {wa_id}")
            return jsonify({"status": "ok"}), 200

        print(f"[WEBHOOK] Processing text from {sender_name or wa_id}: {text_body[:200]}")

        if not BRAIN_FILE.exists():
            print(f"[WEBHOOK] ERROR: brain file missing at {BRAIN_FILE}")
            return jsonify({"status": "ok"}), 200

        # Run the twin. order_context is empty here — webhook doesn't have Shopify info.
        classification, reply, _raw = draft_reply_logic(text_body, "")

        if not classification or not reply:
            print(
                f"[WEBHOOK] Twin returned empty result "
                f"(classification={classification!r}, reply_len={len(reply)}). Skipping dispatch."
            )
            return jsonify({"status": "ok"}), 200

        sender_info = f"{sender_name} ({wa_id})" if sender_name else wa_id

        if classification == "AUTO":
            send_whatsapp_reply(wa_id, reply)
            print(f"[AUTO] Replied to {wa_id}")
        elif classification == "DRAFT+APPROVE":
            send_telegram_notification(
                classification, text_body, reply, sender_info=sender_info
            )
            print(f"[DRAFT] Notified founder for {wa_id}")
        elif classification == "ESCALATE":
            send_telegram_notification(
                classification, text_body, reply, sender_info=sender_info
            )
            print(f"[ESCALATE] Notified founder for {wa_id}")
        else:
            print(f"[WEBHOOK] Unknown classification {classification!r} — no dispatch")

        print("=" * 60 + "\n")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        # Catch-all so we always respond 200 to WATI no matter what.
        print(f"[WEBHOOK] EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
        print("=" * 60 + "\n")
        return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Local dev entry point — Render uses gunicorn (see Procfile) and never hits this block.
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")
    print("=" * 60)
    print("  Glam Shelf Twin — Phase 0")
    print(f"  Brain file: {BRAIN_FILE}")
    print(f"  Model:      {MODEL}")
    print(f"  Open this in your browser: http://{host}:{port}")
    print("  Press CTRL+C in this terminal to stop the server.")
    print("=" * 60)
    app.run(host=host, port=port, debug=True)
