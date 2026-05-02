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
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
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

# Founder's personal WhatsApp number. Defaults to the same value as
# BUSINESS_NUMBER but can be set separately on Render if the founder's
# personal phone differs from the registered business number. Both are
# blocked from receiving auto-replies, both are skipped on inbound.
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "919217470151")

# Dashboard config — DASHBOARD_KEY gates /dashboard-data; override on Render.
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "changeme")

# SQLite path for message logs. Spec asks for /tmp/glamshelf_logs.db; using
# tempfile.gettempdir() resolves to /tmp on Render Linux and stays portable
# for local Windows dev — same effective path on production.
DB_PATH = os.path.join(tempfile.gettempdir(), "glamshelf_logs.db")

# Recent message-id dedup. Backed by a short-lived cache file in the OS
# temp dir so the dedup set survives worker restarts within a single
# deploy — without this, every Render worker recycle re-opens the
# WATI echo loop because in-memory state is gone.
#
# Caveats:
#   - File is wiped on Render redeploy (ephemeral filesystem) — that's fine,
#     a redeploy means new code anyway.
#   - Not synchronised across multiple gunicorn workers, but Render uses 1
#     by default. With concurrent workers worst-case is occasional duplicate
#     processing, not a true loop.
DEDUP_CACHE_FILE = os.path.join(tempfile.gettempdir(), "glamshelf_seen_ids.txt")
DEDUP_MAX_AGE_SECONDS = 60 * 60  # 1 hour — long enough to cover the loop window


def _load_seen_ids() -> set[str]:
    """Read recent message IDs from the cache file and prune anything older
    than DEDUP_MAX_AGE_SECONDS. Rewrites the file with only the valid
    entries so it doesn't grow unbounded across restarts.

    File format: one entry per line, "<unix_timestamp>\t<msg_id>".
    """
    if not os.path.exists(DEDUP_CACHE_FILE):
        return set()
    cutoff = time.time() - DEDUP_MAX_AGE_SECONDS
    valid: list[tuple[str, str]] = []
    try:
        with open(DEDUP_CACHE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("\t", 1)
                if len(parts) != 2:
                    continue
                ts_str, mid = parts
                try:
                    if float(ts_str) >= cutoff:
                        valid.append((ts_str, mid))
                except ValueError:
                    continue
    except Exception as e:
        print(f"[DEDUP] Failed to load cache: {type(e).__name__}: {e}")
        return set()

    # Rewrite with only the still-valid entries (best effort — silently
    # ignore failures so a corrupt cache never breaks the webhook).
    try:
        with open(DEDUP_CACHE_FILE, "w", encoding="utf-8") as f:
            for ts_str, mid in valid:
                f.write(f"{ts_str}\t{mid}\n")
    except Exception as e:
        print(f"[DEDUP] Failed to rewrite cache: {type(e).__name__}: {e}")

    return {mid for _, mid in valid}


def _persist_seen_id(msg_id: str) -> None:
    """Append a freshly-processed message id to the cache file. Best effort."""
    try:
        with open(DEDUP_CACHE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.time()}\t{msg_id}\n")
    except Exception as e:
        print(f"[DEDUP] Failed to persist {msg_id}: {type(e).__name__}: {e}")


_seen_ids: set[str] = _load_seen_ids()
print(f"[DEDUP] Loaded {len(_seen_ids)} recent message ids from {DEDUP_CACHE_FILE}")


def _init_db() -> None:
    """Create the message_logs table and supporting indexes if missing.

    Schema:
      id          INTEGER  primary key
      ts          REAL     unix timestamp (float)
      wa_id       TEXT     customer phone (or empty for early errors)
      sender_name TEXT     WATI senderName field
      msg_text    TEXT     inbound text body
      status      TEXT     AUTO / ESCALATE / DEDUP / PROTECTED / ERROR
      reply_text  TEXT     drafted reply (AUTO/ESCALATE only)
      latency_ms  INTEGER  webhook→dispatch elapsed ms (None for skips)
      error       TEXT     stringified exception (ERROR rows only)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                wa_id TEXT,
                sender_name TEXT,
                msg_text TEXT,
                status TEXT NOT NULL,
                reply_text TEXT,
                latency_ms INTEGER,
                error TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON message_logs(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_status ON message_logs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_wa_id ON message_logs(wa_id)")
        conn.commit()
        conn.close()
        print(f"[DB] Initialized {DB_PATH}")
    except Exception as e:
        print(f"[DB] Failed to init: {type(e).__name__}: {e}")


def _log_message(
    wa_id: str,
    sender_name: str,
    msg_text: str,
    status: str,
    reply_text: str | None = None,
    latency_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Insert one row into message_logs.

    All failures swallowed — a DB problem must never break the webhook
    response (we always return 200 to WATI).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO message_logs "
            "(ts, wa_id, sender_name, msg_text, status, reply_text, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                wa_id,
                sender_name,
                msg_text,
                status,
                reply_text,
                latency_ms,
                error,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] _log_message failed: {type(e).__name__}: {e}")


_init_db()

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


def normalize_wa(number: str) -> str:
    """Reduce a phone number to comparable digits.

    Strips non-digit characters and any leading zeros, so "+91 92174 70151",
    "0919217470151", and "919217470151" all compare equal. Used for safe
    cross-format equality checks against BUSINESS_NUMBER / OWNER_NUMBER.
    """
    return "".join(c for c in (number or "") if c.isdigit()).lstrip("0")


def send_whatsapp_reply(wa_id: str, reply_text: str) -> None:
    """Send an outbound WhatsApp text message to a customer via WATI.

    Endpoint choice — sendSessionMessage vs sendTemplateMessage:
      - /api/v1/sendSessionMessage/{wa_id} — used for replies WITHIN the
        24-hour session window after a customer's last inbound message.
        This is always our case: auto-replies fire only in direct response
        to a webhook event, so we're guaranteed to be in-session.
      - /api/v1/sendTemplateMessage — required for messages OUTSIDE the
        24h window, must use a pre-approved HSM template, takes different
        fields (messageType, template name, parameters). Not used here.

    Field shape: sendSessionMessage takes `messageText` as a URL QUERY
    PARAMETER, not a JSON body field. Putting it in the body causes WATI
    to respond with {"result": false, "info": "message text can not be
    empty"} (HTTP 200 — see the result-check note below). The body is
    sent empty. Fields like messageType / isHSM / conversationId belong
    to sendTemplateMessage and would be ignored or rejected here.

    IMPORTANT — WATI returns HTTP 200 even on logical failures. The real
    outcome lives in the JSON body as {"result": true|false, "info": ...}.
    We log the full response body so any failures are visible in Render
    logs, and we treat result=false as a failure even on HTTP 200.

    All failures are logged and swallowed — the /webhook handler must
    always return 200 to WATI to prevent retries / duplicate replies.
    """
    if not WATI_API_KEY or not WATI_ENDPOINT:
        print("[WATI] Skipped: WATI_API_KEY or WATI_ENDPOINT not set")
        return

    # Defense in depth: never auto-send to the business or owner number,
    # even if some future change in the inbound filter ever lets one through.
    # Compared on normalized digits so format quirks can't slip past.
    target = normalize_wa(wa_id)
    if target and target in {normalize_wa(BUSINESS_NUMBER), normalize_wa(OWNER_NUMBER)}:
        print(f"[WATI] BLOCKED outbound to protected number {wa_id}")
        return

    endpoint = WATI_ENDPOINT.rstrip("/")
    url = f"{endpoint}/api/v1/sendSessionMessage/{wa_id}"
    headers = {
        "Authorization": f"Bearer {WATI_API_KEY}",
        "Content-Type": "application/json",
    }
    params = {"messageText": reply_text}

    # Log the fully-prepared URL (with messageText URL-encoded) so we can
    # see exactly what WATI receives.
    full_url = requests.Request("POST", url, params=params).prepare().url
    print(f"[WATI] POST {full_url}")
    print(f"[WATI] Body: {{}} (empty — messageText is in the query string)")

    try:
        response = requests.post(
            url,
            headers=headers,
            params=params,
            json={},
            timeout=WATI_TIMEOUT_SECONDS,
        )

        # Log the full response body — WATI's actual status is here, not
        # just in the HTTP code. Truncated to 1000 chars to stay readable.
        body_preview = (response.text or "(empty body)")[:1000]
        print(f"[WATI] HTTP {response.status_code}")
        print(f"[WATI] Response body: {body_preview}")

        # Parse the response and surface result=false even on HTTP 200.
        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict) and data.get("result") is False:
            info = data.get("info") or data.get("message") or "(no detail)"
            print(f"[WATI] API rejected the message: {info}")
        elif response.ok:
            print(f"[WATI] Sent reply to {wa_id} ({len(reply_text)} chars)")
        else:
            print(f"[WATI] HTTP failure {response.status_code}")
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
    db_status = "ok"
    total_logged = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        total_logged = conn.execute("SELECT COUNT(*) FROM message_logs").fetchone()[0]
        conn.close()
    except Exception as e:
        db_status = f"error: {type(e).__name__}: {e}"

    return jsonify({
        "status": "ok",
        "brain_present": BRAIN_FILE.exists(),
        "brain_path": str(BRAIN_FILE),
        "model": MODEL,
        "anthropic_api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "db": db_status,
        "total_logged": total_logged,
        "seen_ids_cached": len(_seen_ids),
    })


@app.route("/dashboard")
def dashboard():
    """Serve the static control-panel HTML, gated by the same DASHBOARD_KEY
    query parameter as /dashboard-data. Pure HTML — the page itself fetches
    /dashboard-data?key=... from JS and renders the JSON client-side."""
    key = request.args.get("key", "")
    if key != DASHBOARD_KEY:
        return "Unauthorized", 401
    return render_template("glamshelf-twin-control-panel.html")


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
        # Start timer here so latency_ms covers the entire handler — the
        # except block also references t_start so it must be set before
        # anything that could raise inside the try.
        t_start = time.time()
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

        # Don't process messages from our own business or owner number.
        # Compared on normalized digits so format quirks (+91, 0091, spaces,
        # etc.) can't slip past the equality check.
        normalized = normalize_wa(wa_id)
        if normalized in {normalize_wa(BUSINESS_NUMBER), normalize_wa(OWNER_NUMBER)}:
            print(f"[WEBHOOK] Skipped: message from protected number {wa_id}")
            _log_message(wa_id, sender_name, text_body, status="PROTECTED")
            return jsonify({"status": "ok"}), 200

        # PRIMARY LOOP DEFENSE — dedup by message id.
        # WATI fires webhook events for our outbound replies too, but those
        # echo events do NOT carry an owner/isOwner/fromMe flag in the
        # payload (confirmed empirically). What they DO have is the same
        # message id, repeated. Persisting the seen set across worker
        # restarts is what stops the loop after a redeploy / worker recycle.
        if msg_id and msg_id in _seen_ids:
            print(f"[WEBHOOK] Skipped: duplicate message id {msg_id}")
            _log_message(wa_id, sender_name, text_body, status="DEDUP")
            return jsonify({"status": "ok"}), 200
        if msg_id:
            _seen_ids.add(msg_id)
            _persist_seen_id(msg_id)

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
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log_message(
                wa_id, sender_name, text_body,
                status="AUTO", reply_text=reply, latency_ms=elapsed_ms,
            )
        elif classification == "DRAFT+APPROVE":
            send_telegram_notification(
                classification, text_body, reply, sender_info=sender_info
            )
            print(f"[DRAFT] Notified founder for {wa_id}")
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log_message(
                wa_id, sender_name, text_body,
                status="DRAFT", reply_text=reply, latency_ms=elapsed_ms,
            )
        elif classification == "ESCALATE":
            send_telegram_notification(
                classification, text_body, reply, sender_info=sender_info
            )
            print(f"[ESCALATE] Notified founder for {wa_id}")
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log_message(
                wa_id, sender_name, text_body,
                status="ESCALATE", reply_text=reply, latency_ms=elapsed_ms,
            )
        else:
            print(f"[WEBHOOK] Unknown classification {classification!r} — no dispatch")

        print("=" * 60 + "\n")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        # Catch-all so we always respond 200 to WATI no matter what.
        print(f"[WEBHOOK] EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
        # Log the error too — wrapped in its own try/except because at this
        # point any of t_start / wa_id / sender_name / text_body could be
        # undefined if the exception fired very early.
        try:
            elapsed_ms = int((time.time() - locals().get("t_start", time.time())) * 1000)
            _log_message(
                locals().get("wa_id", "") or "",
                locals().get("sender_name", "") or "",
                locals().get("text_body", "") or "",
                status="ERROR",
                error=str(e),
                latency_ms=elapsed_ms,
            )
        except Exception:
            pass
        print("=" * 60 + "\n")
        return jsonify({"status": "ok"}), 200


@app.route("/dashboard-data", methods=["GET"])
def dashboard_data():
    """JSON snapshot of message logs for the founder's live dashboard.

    Auth: ?key=<DASHBOARD_KEY> query param. Override DASHBOARD_KEY in
    Render env vars; the default 'changeme' is intentionally embarrassing
    so the env var is the only sane way to use this in production.

    Sections:
      kpis           — today's counts and latency stats (IST midnight onward)
      conversations  — last 50 actionable rows (excludes DEDUP/PROTECTED)
      customers      — per-wa_id summary (msg_count, last_seen, last_status)
      daily_volume   — last 7 days, grouped by IST date (YYYY-MM-DD)
      error_log      — last 20 ERROR / ESCALATE / slow (>5s) rows
      bulk_spike     — distinct senders in last 30min, is_spike flag if >=5
    """
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"error": "unauthorized"}), 401

    try:
        # IST midnight as a unix timestamp — matches Udit's working day.
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_unix = midnight_ist.timestamp()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # ---- KPIs (today) ----
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status='AUTO' THEN 1 END), 0) AS auto_replied,
                COALESCE(SUM(CASE WHEN status='ESCALATE' THEN 1 END), 0) AS escalated,
                COALESCE(SUM(CASE WHEN status='DEDUP' THEN 1 END), 0) AS dedup_skips,
                COALESCE(SUM(CASE WHEN status='PROTECTED' THEN 1 END), 0) AS protected_blocks,
                COALESCE(SUM(CASE WHEN status='ERROR' THEN 1 END), 0) AS errors,
                AVG(latency_ms) AS avg_latency_ms,
                MAX(latency_ms) AS max_latency_ms
            FROM message_logs WHERE ts >= ?
            """,
            (midnight_unix,),
        )
        r = cur.fetchone()
        kpis = {
            "total": r[0] or 0,
            "auto_replied": r[1] or 0,
            "escalated": r[2] or 0,
            "dedup_skips": r[3] or 0,
            "protected_blocks": r[4] or 0,
            "errors": r[5] or 0,
            "avg_latency_ms": int(r[6]) if r[6] is not None else None,
            "max_latency_ms": int(r[7]) if r[7] is not None else None,
        }

        # ---- Conversations: last 50, exclude DEDUP/PROTECTED ----
        cur.execute(
            """
            SELECT id, ts, wa_id, sender_name, msg_text, status, reply_text, latency_ms, error
            FROM message_logs
            WHERE status NOT IN ('DEDUP', 'PROTECTED')
            ORDER BY ts DESC LIMIT 50
            """
        )
        conversations = [
            {
                "id": row[0], "ts": row[1], "wa_id": row[2],
                "sender_name": row[3], "msg_text": row[4], "status": row[5],
                "reply_text": row[6], "latency_ms": row[7], "error": row[8],
            }
            for row in cur.fetchall()
        ]

        # ---- Customers: aggregated per wa_id, with last_status ----
        # Self-join on (wa_id, MAX(ts)) is portable across SQLite versions and
        # avoids a per-row correlated subquery.
        cur.execute(
            """
            SELECT
                ml.wa_id,
                ml.sender_name,
                cnt.msg_count,
                ml.ts AS last_seen,
                ml.status AS last_status
            FROM message_logs ml
            JOIN (
                SELECT wa_id, COUNT(*) AS msg_count, MAX(ts) AS max_ts
                FROM message_logs
                WHERE wa_id IS NOT NULL AND wa_id != ''
                GROUP BY wa_id
            ) cnt ON ml.wa_id = cnt.wa_id AND ml.ts = cnt.max_ts
            ORDER BY ml.ts DESC
            LIMIT 100
            """
        )
        customers = [
            {
                "wa_id": row[0], "sender_name": row[1],
                "msg_count": row[2], "last_seen": row[3], "last_status": row[4],
            }
            for row in cur.fetchall()
        ]

        # ---- Daily volume: last 7 days, IST date buckets ----
        cur.execute(
            """
            SELECT
                DATE(ts, 'unixepoch', '+5 hours', '+30 minutes') AS day_ist,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status='AUTO' THEN 1 END), 0) AS auto_replied,
                COALESCE(SUM(CASE WHEN status='ESCALATE' THEN 1 END), 0) AS escalated
            FROM message_logs
            WHERE ts >= ?
            GROUP BY day_ist
            ORDER BY day_ist DESC
            """,
            (time.time() - 7 * 86400,),
        )
        daily_volume = [
            {
                "date": row[0], "total": row[1],
                "auto_replied": row[2], "escalated": row[3],
            }
            for row in cur.fetchall()
        ]

        # ---- Error log: last 20 noteworthy rows ----
        cur.execute(
            """
            SELECT id, ts, wa_id, sender_name, msg_text, status, latency_ms, error
            FROM message_logs
            WHERE status IN ('ERROR', 'ESCALATE') OR latency_ms > 5000
            ORDER BY ts DESC LIMIT 20
            """
        )
        error_log = [
            {
                "id": row[0], "ts": row[1], "wa_id": row[2],
                "sender_name": row[3], "msg_text": row[4], "status": row[5],
                "latency_ms": row[6], "error": row[7],
            }
            for row in cur.fetchall()
        ]

        # ---- Bulk spike: distinct senders in last 30 min ----
        cur.execute(
            """
            SELECT COUNT(DISTINCT wa_id) FROM message_logs
            WHERE ts >= ? AND wa_id IS NOT NULL AND wa_id != ''
            """,
            (time.time() - 30 * 60,),
        )
        distinct_count = cur.fetchone()[0] or 0
        bulk_spike = {
            "distinct_senders_last_30min": distinct_count,
            "is_spike": distinct_count >= 5,
        }

        conn.close()

        return jsonify({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "kpis": kpis,
            "conversations": conversations,
            "customers": customers,
            "daily_volume": daily_volume,
            "error_log": error_log,
            "bulk_spike": bulk_spike,
        })

    except Exception as e:
        print(f"[DASHBOARD] error: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


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
