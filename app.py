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

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
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


def _require_env(name: str) -> str:
    """Read a required env var or raise on missing/empty.

    Used for the three auth-critical vars (SECRET_KEY, APP_PASSWORD,
    DASHBOARD_KEY) that previously had insecure hardcoded defaults
    visible in the public GitHub source. Fail-fast on startup is far
    safer than booting with a default that anyone reading the repo
    could exploit.

    If you're hitting this on a new deploy, set the missing var in
    Render → Environment and redeploy.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Required env var {name!r} is not set or is empty. "
            f"Refusing to start with an insecure default — please set it on Render."
        )
    return value


# Session secret for cookie signing. MUST be set in Render env vars —
# without a stable secret, session cookies are unsigned/forgeable.
app.secret_key = _require_env("SECRET_KEY")

# Single-user password gate for the browser drafter UI. MUST be set on
# Render — the previous "glamshelf2026" default was readable in the
# public GitHub source.
APP_PASSWORD = _require_env("APP_PASSWORD")

PROJECT_DIR = Path(__file__).parent.resolve()
BRAIN_FILE = PROJECT_DIR / "brain" / "brain.md"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# ----- Vision (image understanding) config -----
#
# When a WATI inbound event has type=image, the handler downloads the
# image, sends it to Claude's vision API for order-info extraction, and
# then either (a) synthesizes a text query and runs the normal Claude
# reply pipeline, or (b) on low confidence / failure, sends the
# deterministic fallback reply asking the customer to type their order ID.
#
# Uses the same MODEL constant as text replies — claude-sonnet-4-6 has
# vision capability built in; no separate vision-only model needed.
VISION_MAX_TOKENS = 512                # extraction output is short JSON
VISION_DOWNLOAD_TIMEOUT_SECONDS = 10   # per spec — give up fast on slow WATI media

VISION_SYSTEM_PROMPT = """You are analyzing a customer image for The Glam Shelf, an Indian eyelash brand. Identify what kind of image it is and extract whatever's useful.

FIRST, classify the image into ONE of:
- "order_screenshot" → screenshot of an order confirmation, payment receipt, tracking page, invoice, or anything order-related
- "eye_photo" → a close-up of a customer's eye(s) or face showing eyes — they're asking for a lash recommendation based on their eye shape
- "product_photo" → a photo of lashes (ours or competitor's), a swatch, or makeup look reference
- "other" → anything else (selfie without eyes visible, food, random scene, blurry, etc.)

THEN extract the relevant fields based on image_type:

For order_screenshot: order_id, payment_status, amount, product, customer_name, date.
For eye_photo: eye_shape (one of: "hooded", "monolid", "almond", "round", "downturned", or null if unclear).
For product_photo / other: leave extraction fields null.

Respond ONLY in this JSON format (always include every key — use null when not applicable):
{
  "image_type": "order_screenshot" | "eye_photo" | "product_photo" | "other",
  "order_id": "1042" or null,
  "payment_status": "paid" or null,
  "amount": "849" or null,
  "product": "GS1 Luxe Light Lash Tray" or null,
  "customer_name": "Priya" or null,
  "eye_shape": "hooded" or null,
  "confidence": "high" or "low"
}

Use confidence "high" only when you're genuinely sure about image_type AND have at least one useful extraction. If unsure or the image is too blurry/dark to read, return image_type as your best guess but set confidence to "low" and leave extraction fields null."""

# Deterministic reply used when vision can't make sense of the image
# (low confidence, download failure, or no image URL in payload).
# Intentionally NEUTRAL — the image might not be order-related at all
# (product photo, Instagram screenshot, lash inspo, anything). Asking
# "could you type out the order ID?" sounds wrong when the customer
# sent a product photo. Mirrored verbatim in brain.md Section 1.5
# IMAGE RECEIVED rule so the documented fallback matches what fires.
FALLBACK_VISION_REPLY = (
    "Thanks for sharing! Could you tell me a little more about what you're looking for? 🤍"
)

# Telegram notification config. Set both on Render → Environment.
# No default for TELEGRAM_BOT_TOKEN — a previous default value was the live
# token, which GitGuardian flagged. Now empty → if the env var isn't set on
# Render, send_telegram_notification() short-circuits with a "Skipped" log
# instead of authenticating with a secret committed to source.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SECONDS = 5

# WATI (WhatsApp Business API) config. Set these in Render env vars.
# WATI_ENDPOINT format: https://live-mt-server.wati.io/<account_id>
WATI_API_KEY = os.environ.get("WATI_API_KEY", "")
WATI_ENDPOINT = os.environ.get("WATI_ENDPOINT", "")
WATI_TIMEOUT_SECONDS = 10

# The Glam Shelf's WhatsApp Business number. The webhook ignores any inbound
# event where waId equals this number — prevents the twin from replying to
# itself if WATI ever loops outbound / own messages through the webhook.
# Default removed (was the founder's personal number visible in public
# GitHub source). Empty → the protected-number check just won't match
# anything, which is a safer failure mode than baking PII into the repo.
BUSINESS_NUMBER = os.environ.get("BUSINESS_NUMBER", "")

# Founder's personal WhatsApp number. Same rationale as BUSINESS_NUMBER —
# default removed because it was personal PII. Must be set on Render for
# the protected-number filter to work.
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")

# Dashboard config — DASHBOARD_KEY gates /dashboard, /dashboard-data,
# /inventory-debug, /review-debug. MUST be set on Render — the previous
# "changeme" default was readable in the public GitHub source and would
# have allowed anyone to access the dashboard if env var were missing.
DASHBOARD_KEY = _require_env("DASHBOARD_KEY")

# SQLite path for message logs.
#   - Legacy/local default: <tempdir>/glamshelf_logs.db (ephemeral on Render).
#   - Production on Render: set DASHBOARD_DB_PATH to a path on a mounted
#     persistent disk, e.g. /var/data/glamshelf_logs.db. The disk needs to
#     be created in Render → Settings → Disks (any small size, mounted at
#     /var/data). Without that, every redeploy still wipes the DB.
#   - On startup, if a legacy /tmp DB exists and the persistent path is
#     empty, _init_db() copies the file across once so historical rows
#     aren't lost when you flip on the persistent disk.
_LEGACY_DB_PATH = os.path.join(tempfile.gettempdir(), "glamshelf_logs.db")
DB_PATH = os.environ.get("DASHBOARD_DB_PATH", _LEGACY_DB_PATH)

# GitHub backup config — when all three env vars are set, the SQLite DB
# is restored from GitHub on cold start (if no local copy) and backed up
# every BACKUP_INTERVAL_SECONDS thereafter, plus once at startup.
# Use a private repo + a token scoped to repo (or "Contents: read/write"
# on a fine-grained PAT). All three must be set; missing any → skip silently.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. Uditkumar05ai/glamshelf-backup
# Tolerate someone pasting a full URL by mistake — strip the github.com
# prefix and any trailing slash so "https://github.com/owner/repo" and
# "owner/repo" both resolve to the canonical "owner/repo" form expected
# by the GitHub Contents API. This is the exact mistake that caused the
# earlier 404s during initial setup.
GITHUB_REPO = GITHUB_REPO.replace("https://github.com/", "").rstrip("/")
GITHUB_BACKUP_PATH = os.environ.get("GITHUB_BACKUP_PATH", "glamshelf_logs.db")
BACKUP_INTERVAL_SECONDS = 60 * 60

# Shopify webhook secret — used to HMAC-verify inbound order webhooks at
# /shopify-webhook. Get this from Shopify Admin → Notifications → Webhooks.
# Missing/empty value causes every shopify-webhook POST to 401, which is
# the safe default until the secret is set.
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")

# Live inventory source — Shopify's public storefront /products.json.
#
# No auth needed: every Shopify store exposes a public read-only feed at
# <store-domain>/products.json that returns up to 250 products per page
# with their variants. This is the same JSON Shopify themes consume on
# the storefront, so it's safe to hit from anywhere with no API token.
#
# Trade-off vs the Admin API: this endpoint does NOT expose
# inventory_quantity (numeric units). Each variant only carries an
# `available` boolean. We use that boolean to mark IN STOCK / SOLD OUT
# — sufficient for Claude to decide when to use the out-of-stock script
# without us having to manage a Shopify Admin App token.
SHOPIFY_PRODUCTS_URL = "https://glamshelf.in/products.json"
SHOPIFY_PRODUCTS_LIMIT = 250  # the endpoint's max page size
SHOPIFY_TIMEOUT_SECONDS = 8

# 5-minute in-memory cache for live inventory. Single-entry dict — the
# formatted block (string) and the unix timestamp it was fetched at.
# Empty-string entries are NOT cached: a transient Shopify outage
# shouldn't pin a no-data result for the full TTL. Only successful
# fetches set fetched_at.
INVENTORY_CACHE_TTL_SECONDS = 300
_inventory_cache: dict = {"text": "", "fetched_at": 0.0}

# Instagram DM webhook config.
#
# IMPORTANT — there are TWO Instagram messaging APIs and they need
# different tokens. Glam Shelf Twin uses the newer "Instagram Login"
# flow (graph.instagram.com), NOT the older Messenger Platform
# (graph.facebook.com). Generating the wrong token type produces
# "Object 'me' does not exist" or "missing permissions" errors that
# are unrelated to the actual access — the host simply doesn't
# recognise the token holder.
#
# Token generation path (Meta Developer Console):
#   App → Use cases → Instagram → Generate access tokens (Section 2)
#   Required permission: instagram_business_manage_messages
#   Token format: starts with IGAA... or sometimes EAAx... (NOT plain EAA/EAAS)
#
# Env vars:
#   INSTAGRAM_VERIFY_TOKEN          arbitrary string for hub.challenge handshake
#   INSTAGRAM_PAGE_ACCESS_TOKEN     long-lived IG user token (see above)
#   INSTAGRAM_PAGE_ID               IG Business Account ID (e.g. 17841479591075688)
#   INSTAGRAM_API_BASE              optional override; default targets the IG
#                                   Login API. Set to https://graph.facebook.com/v22.0
#                                   only if migrating back to the Messenger
#                                   Platform with a Page Access Token.
# Missing required vars → GET handshake always 403; POST processes locally
# but can't send replies.
INSTAGRAM_VERIFY_TOKEN = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "")


def _clean_meta_token(raw: str) -> str:
    """Defensive cleanup for Meta access tokens pasted into env vars.

    Strips trailing/leading whitespace (newlines included), surrounding
    single or double quotes, and a leading "Bearer " if the founder pasted
    an entire header value. Without this, a stray newline or quote in the
    Render env var produces Meta's HTTP 400 'Cannot parse access token'
    even though the token itself is valid — the most common production
    paste mistake.
    """
    s = (raw or "").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


INSTAGRAM_PAGE_ACCESS_TOKEN = _clean_meta_token(
    os.environ.get("INSTAGRAM_PAGE_ACCESS_TOKEN", "")
)

# Instagram-connected Account ID. Visible in Meta Business Suite under
# the Instagram account → Account info, or by hitting the /me endpoint
# with the IG Login token. Used as the explicit subject in the messages
# URL — required because the `me` alias is unreliable across IG flows.
# Empty / unset → falls back to "me", which works for some token flavors.
INSTAGRAM_PAGE_ID = os.environ.get("INSTAGRAM_PAGE_ID", "").strip()

# API base URL. Default targets the Instagram Graph API (Instagram Login
# flow) which is where instagram_business_manage_messages tokens have
# scope. Override only if migrating back to the Messenger Platform.
INSTAGRAM_API_BASE = os.environ.get(
    "INSTAGRAM_API_BASE", "https://graph.instagram.com/v22.0"
).rstrip("/")

INSTAGRAM_TIMEOUT_SECONDS = 10

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

# In-memory TTL cache for brain.md content. We were re-reading ~40KB off
# disk on every webhook call — wasteful when the file changes maybe once
# a day. Cache for 5 minutes; refresh transparently on the next request
# after expiry. _brain_cache_text=None means "never loaded yet".
BRAIN_CACHE_TTL_SECONDS = 300
_brain_cache_text: str | None = None
_brain_cache_loaded_at: float = 0.0

# Human-takeover pause register. When Udit sends an outbound WATI message
# containing "#pause" (typically appended to a real reply to the customer),
# that customer's wa_id is added here with a 4-hour expiry. While present,
# the WATI webhook handler short-circuits before any Claude call so the
# twin stops auto-replying — the human is on it. "#resume" removes the
# entry immediately. The register is in-memory only (resets on Render
# restart, which is acceptable; the brain's Section 7 protocol covers
# this anyway).
PAUSED_TTL_SECONDS = 4 * 60 * 60  # 4 hours
paused_numbers: dict[str, float] = {}  # wa_id -> expiry unix timestamp

# Bot's-own-outbound recognition. When send_whatsapp_reply() ships a reply,
# we register the text (and ideally the WATI-assigned msg_id) here so the
# subsequent WATI outbound webhook for THAT message is correctly attributed
# to the bot, not to Udit. Without this, the outbound handler would tag
# every bot reply as "HUMAN_UDIT" → the safety net would then suppress all
# AUTO replies for 4h after every bot reply, breaking the whole flow.
# In-memory, short TTL — WATI's outbound webhook typically fires within a
# couple of seconds of the send call. 5 minutes is generous.
BOT_OUTBOUND_DEDUP_TTL_SECONDS = 5 * 60
_bot_recent_replies: dict[str, float] = {}  # reply text -> expiry unix timestamp

# DB safety-net check window — if a HUMAN_UDIT row exists in the last
# HUMAN_HANDLING_WINDOW_SECONDS, the inbound flow suppresses Claude.
# Matches the brain's Section 7 "4+ hours of silence to resume" rule.
HUMAN_HANDLING_WINDOW_SECONDS = 4 * 60 * 60

# Shipping-update dedup is now persisted in the `shipping_notifications`
# SQLite table (see _init_db). The DB is the single source of truth —
# survives Render redeploys, which means we can never accidentally
# double-send a "shipped" message after the worker restarts. The
# in-memory `_sent_shipping_updates` set that previously lived here was
# removed; use the _was_shipping_sent / _mark_shipping_sent helpers
# (defined alongside the other DB helpers below) instead.

# Post-delivery review-request scheduler.
#
# When _process_shipping_event handles a "delivered" event we schedule a
# threading.Timer to fire REVIEW_DELAY_SECONDS later and send a single
# WhatsApp message asking the customer for a review. Dedup is keyed by
# order_id so an order can only ever schedule one review (even if Shopify
# fires the delivered webhook multiple times for retries / edits).
#
# WARNING — in-memory state. The Timer thread + the _scheduled_reviews
# dict do not survive a Render worker restart. If Render redeploys
# during the 10-day window the review just doesn't send for the orders
# in flight. This is documented and accepted per spec — durable
# scheduling would require Postgres + a separate worker, out of scope.
#
# REVIEW_DELAY_SECONDS is a module-level constant so tests can monkey-
# patch it (e.g. set to 60 for a 1-minute verification end-to-end)
# without touching the scheduling logic.
REVIEW_DELAY_SECONDS = 10 * 24 * 60 * 60   # 864000s = 10 days
_scheduled_reviews: dict[str, dict] = {}

REVIEW_REQUEST_TEMPLATE = (
    "Hi {first_name}! Hope you're loving your lashes from The Glam Shelf 🤍\n\n"
    "If you have a minute, a quick review on our website would mean so much "
    "to us — it helps other girls find us too!\n\n"
    "→ glamshelf.in/pages/reviews\n\n"
    "And if you've worn them, we'd love to see! Tag us @glamshelfstore on "
    "Instagram 🤍\n\n"
    "— Team The Glam Shelf"
)

# ----- Telegram DRAFT inline-button approval flow -----
#
# When the WATI webhook classifies a message as DRAFT+APPROVE, instead of
# sending a plain Telegram notification we send a message with three
# inline buttons (✅ Send as-is / ✏️ Edit / ⛔ Skip) and register the
# pending draft in _pending_drafts. The /telegram-callback endpoint
# receives the button tap (or Udit's edited text) and actions it.
#
# State is in-memory only — a worker restart loses any pending drafts.
# Acceptable: orphaned Telegram buttons just return an "Already handled"
# toast via the dedup check (draft_id not in _pending_drafts), and Udit
# gets a fresh draft on the next inbound from the same customer.
#
# Key is the short draft_id (8 hex chars from secrets.token_hex(4)) so
# callback_data fits in Telegram's 64-byte hard limit alongside action
# and customer phone.
PENDING_DRAFT_TTL_SECONDS = 24 * 60 * 60     # opportunistic prune cutoff
EDIT_TIMEOUT_SECONDS = 10 * 60                # 10 min per spec
_pending_drafts: dict[str, dict] = {}


def _is_paused(wa_id: str) -> bool:
    """Return True if this number is currently in a human-takeover window.
    Also opportunistically prunes any expired entries so the dict stays
    bounded — no separate cleanup job needed."""
    now = time.time()
    expired = [num for num, exp in paused_numbers.items() if exp < now]
    for num in expired:
        del paused_numbers[num]
        print(f"[PAUSE] Auto-expired for {num} (4h elapsed)")
    return wa_id in paused_numbers


def _pause_number(wa_id: str, ttl_seconds: int = PAUSED_TTL_SECONDS) -> None:
    """Add `wa_id` (or Instagram sender_id — the dict is just keyed by
    string) to the pause register with a TTL. While paused, the inbound
    handlers short-circuit before any Claude call and the customer gets
    no auto-replies.

    Used by:
      - _handle_pause_directive — when Udit types "#pause" outbound
      - WATI webhook ESCALATE branch — auto-pause after holding reply
      - Instagram webhook ESCALATE branch — same

    Idempotent: extending the pause window (re-pausing an already-paused
    number) just resets the expiry. Caller should log the auto-pause
    with their own channel-specific prefix so the founder can grep.
    """
    if not wa_id:
        return
    paused_numbers[wa_id] = time.time() + ttl_seconds


def _record_bot_outbound(reply_text: str, wati_response_data: dict | None = None) -> None:
    """Register a bot-sent reply so the subsequent WATI outbound webhook
    event for the same message is identified as bot-originated (not Udit's).

    Two tracking signals:
      - text content (always): added to _bot_recent_replies with a TTL.
        When the outbound webhook arrives, we check whether the inbound
        text matches a recently-sent reply.
      - msg id (when WATI's API response gives us one): added to _seen_ids
        proactively so the existing dedup gate catches the echo cleanly.

    Different WATI plans return the msg-id under different keys; we try
    the common ones and degrade gracefully if none are present.
    """
    if reply_text:
        # Prune expired entries opportunistically.
        now = time.time()
        for old_text in list(_bot_recent_replies):
            if _bot_recent_replies[old_text] < now:
                del _bot_recent_replies[old_text]
        _bot_recent_replies[reply_text] = now + BOT_OUTBOUND_DEDUP_TTL_SECONDS

    if isinstance(wati_response_data, dict):
        # Try several known key paths for the outbound msg id.
        candidates = []
        for k in ("id", "messageId", "message_id", "mid"):
            v = wati_response_data.get(k)
            if isinstance(v, str) and v:
                candidates.append(v)
        nested = wati_response_data.get("message") or wati_response_data.get("messageContact") or {}
        if isinstance(nested, dict):
            for k in ("id", "messageId", "mid"):
                v = nested.get(k)
                if isinstance(v, str) and v:
                    candidates.append(v)
        for mid in candidates:
            _seen_ids.add(mid)
            _persist_seen_id(mid)
            print(f"[WATI] Pre-registered bot's outbound msg_id={mid} in dedup set")
            break  # one msg id is enough; if there were several, they'd refer to the same send


def _is_bot_outbound(text_body: str) -> bool:
    """Was this exact text shipped by the bot in the last few minutes?"""
    if not text_body or text_body not in _bot_recent_replies:
        return False
    if _bot_recent_replies[text_body] < time.time():
        # Expired; clean up while we're here.
        del _bot_recent_replies[text_body]
        return False
    return True


def _is_outbound_event(data: dict) -> bool:
    """Best-effort detection that a WATI webhook event is an OUTBOUND message
    (sent FROM the business TO a customer), not an inbound customer message.

    WATI's payload schema varies across plans/accounts. We check every known
    direction-indicator field; if any clearly says outbound, we treat it as
    such. Returns False (= treat as inbound) when no signal is present —
    safer to leave existing inbound handling intact than to silently swallow
    a customer message.

    Callers also have the option of using the dedicated /wati-outbound
    endpoint, which treats every event as outbound regardless of payload
    shape — useful when WATI is configured to send outbound events to a
    separate URL.
    """
    if not isinstance(data, dict):
        return False
    # Boolean flags — any one being truthy strongly implies outbound.
    if data.get("owner") is True:
        return True
    if data.get("isOwner") is True:
        return True
    if data.get("fromMe") is True:
        return True
    # String-valued event/direction fields.
    event_type = (data.get("eventType") or "").strip().lower()
    if event_type in ("messagesent", "message_sent", "messagecreated", "message_created", "sent", "outbound"):
        return True
    direction = (data.get("direction") or "").strip().lower()
    if direction in ("outbound", "out", "sent", "outgoing"):
        return True
    return False


def _udit_replied_recently(wa_id: str, window_seconds: int = HUMAN_HANDLING_WINDOW_SECONDS) -> bool:
    """Return True if a HUMAN_UDIT row exists for this wa_id within window_seconds.

    Safety-net check that runs in the inbound flow BEFORE the Claude call.
    Mirrors brain.md Section 7 "Human Takeover Protocol": when Udit has
    replied manually in the recent past, the twin stays silent. Catches
    the case where Udit forgets to type the in-memory #pause directive.

    All failures return False (don't block inbound on a DB hiccup).
    """
    if not wa_id:
        return False
    try:
        cutoff = time.time() - window_seconds
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM message_logs "
            "WHERE wa_id = ? AND status = 'HUMAN_UDIT' AND ts >= ? "
            "LIMIT 1",
            (wa_id, cutoff),
        )
        hit = cur.fetchone() is not None
        conn.close()
        return hit
    except Exception as e:
        print(f"[HUMAN_HANDLING] Safety-net DB check failed: {type(e).__name__}: {e}")
        return False


def _process_wati_outbound(data: dict) -> None:
    """Handle a single WATI outbound event — Udit's manual reply OR the
    bot's own send echoing back. Distinguishes via _is_bot_outbound and
    only logs Udit's manual replies as HUMAN_UDIT.

    Used by both the dedicated /wati-outbound endpoint and the outbound
    branch inside /webhook.
    """
    wa_id = (data.get("waId") or "").strip()
    sender_name = (data.get("senderName") or "").strip()
    text_field = data.get("text")
    if isinstance(text_field, dict):
        text_body = (text_field.get("body") or "").strip()
    else:
        text_body = (text_field or "").strip()
    msg_id = (data.get("id") or "").strip()

    if not wa_id or not text_body:
        print(f"[OUTBOUND] Skipped: missing wa_id or empty text "
              f"(wa_id={wa_id!r}, len(text)={len(text_body)})")
        return

    # If this exact text was sent by the bot recently → echo, not Udit's
    # message. Skip silently. Also pre-mark msg_id in dedup so other code
    # paths (e.g. accidental delivery to /webhook) treat it as a known
    # echo.
    if _is_bot_outbound(text_body):
        print(f"[OUTBOUND] Skipped: bot's own outbound echo for {wa_id}")
        if msg_id:
            _seen_ids.add(msg_id)
            _persist_seen_id(msg_id)
        return

    # #pause / #resume directives ride along on outbound messages too.
    # Tag those as PAUSE_DIRECTIVE so the conversation-history view stays
    # clean; the actual pause state is in the in-memory paused_numbers dict.
    directive = _handle_pause_directive(wa_id, text_body)
    if directive is not None:
        _log_message(
            wa_id, sender_name, text_body,
            status="PAUSE_DIRECTIVE",
            reply_text=text_body,
        )
        if msg_id:
            _seen_ids.add(msg_id)
            _persist_seen_id(msg_id)
        return

    # Plain Udit-manual-reply path. Log as HUMAN_UDIT so the safety-net
    # check (_udit_replied_recently) on the next inbound suppresses the
    # twin's auto-reply for 4h.
    _log_message(
        wa_id, sender_name, text_body,
        status="HUMAN_UDIT",
        reply_text=text_body,
    )
    if msg_id:
        _seen_ids.add(msg_id)
        _persist_seen_id(msg_id)
    print(f"[HUMAN_UDIT] Logged manual reply for {wa_id} (sender={sender_name!r}, {len(text_body)} chars)")


def _handle_pause_directive(wa_id: str, text_body: str) -> str | None:
    """Detect #pause / #resume directives embedded in a webhook event.

    Returns:
      "pause"   if "#pause" appeared in text_body (caller should stop
                processing — directive has been recorded)
      "resume"  if "#resume" appeared (entry removed if present)
      None      no directive — caller continues normal flow

    Designed to ride along inside a real outbound message Udit sent
    through WATI to the customer (e.g. "Sure, looking into it. #pause").
    WATI fires a webhook event for those outbound messages with the
    customer's wa_id as the subject — that wa_id is what we register.
    Customers accidentally typing "#pause" would pause themselves;
    acceptable since these strings are unusual enough that it's rare.
    """
    lower = text_body.lower()
    if "#pause" in lower:
        paused_numbers[wa_id] = time.time() + PAUSED_TTL_SECONDS
        print(
            f"[PAUSE] Human takeover activated for {wa_id} "
            f"(expires in {PAUSED_TTL_SECONDS}s = 4h)"
        )
        return "pause"
    if "#resume" in lower:
        if wa_id in paused_numbers:
            del paused_numbers[wa_id]
            print(f"[PAUSE] Human takeover released for {wa_id}")
        else:
            print(f"[PAUSE] #resume seen for {wa_id} but no active pause to clear")
        return "resume"
    return None


def _init_db() -> None:
    """Create the message_logs table and supporting indexes if missing.

    On startup also performs a one-time copy from the legacy /tmp DB to
    the configured DB_PATH if (a) the persistent path is in use and
    different from /tmp, (b) the legacy file exists, and (c) the
    persistent path doesn't exist yet. This preserves any rows captured
    before the persistent disk was wired up.

    Schema:
      id          INTEGER  primary key
      ts          REAL     unix timestamp (float)
      wa_id       TEXT     customer phone (or empty for early errors)
      sender_name TEXT     WATI senderName field
      msg_text    TEXT     inbound text body
      status      TEXT     AUTO / DRAFT / ESCALATE / DEDUP / PROTECTED / ERROR
      reply_text  TEXT     drafted reply (AUTO/DRAFT/ESCALATE only)
      latency_ms  INTEGER  webhook→dispatch elapsed ms (None for skips)
      error       TEXT     stringified exception (ERROR rows only)
    """
    # Ensure parent dir exists for persistent paths (e.g. /var/data).
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            print(f"[DB] Failed to create parent dir {parent}: {type(e).__name__}: {e}")

    # One-time legacy migration.
    if (
        DB_PATH != _LEGACY_DB_PATH
        and os.path.exists(_LEGACY_DB_PATH)
        and not os.path.exists(DB_PATH)
    ):
        try:
            shutil.copy2(_LEGACY_DB_PATH, DB_PATH)
            print(f"[DB] Migrated legacy DB {_LEGACY_DB_PATH} -> {DB_PATH}")
        except Exception as e:
            print(f"[DB] Legacy migration failed: {type(e).__name__}: {e}")

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

        # Shopify orders — populated by /shopify-webhook, queried at webhook
        # time to inject "Recent order" context into Claude's prompt.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                customer_phone TEXT,
                customer_name TEXT,
                product_names TEXT,
                total_price TEXT,
                order_status TEXT,
                created_at TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(customer_phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_logged ON orders(logged_at)")

        # Instagram DM exchange log — separate table from message_logs so
        # WhatsApp dashboard counts stay clean and channel-specific.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS instagram_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT,
                message_text TEXT,
                reply_text TEXT,
                timestamp TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ig_sender ON instagram_logs(sender_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ig_logged ON instagram_logs(logged_at)")

        # One-time migration: add `source` column to instagram_logs if it
        # doesn't exist. Used to tag rows like HUMAN_UDIT_INSTAGRAM so
        # the inbound handler can detect (via SQL, restart-safe) that
        # Udit replied manually on Instagram and short-circuit Claude.
        # Schema additions on existing prod DBs need ALTER TABLE since
        # CREATE TABLE IF NOT EXISTS doesn't add new columns.
        try:
            existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(instagram_logs)").fetchall()]
            if "source" not in existing_cols:
                conn.execute("ALTER TABLE instagram_logs ADD COLUMN source TEXT")
                print("[DB] Migrated instagram_logs: added `source` column")
        except Exception as e:
            print(f"[DB] instagram_logs source migration failed: {type(e).__name__}: {e}")

        # Shipping notification dedup. (order_id, message_type) primary
        # key so INSERT OR IGNORE in _mark_shipping_sent is atomic — even
        # if two webhook deliveries race, only one row lands and only one
        # message ships. Survives Render redeploys, so the "fulfilled but
        # never notified" recovery flow (/shopify-order-update) can trust
        # this table as the source of truth across restarts.
        #
        # message_type is one of: "shipped" / "out_for_delivery" /
        # "delivered" / "tracking" — matches the event keys
        # _process_shipping_event uses today.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shipping_notifications (
                order_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                phone TEXT,
                order_number TEXT,
                sent_at REAL NOT NULL,
                PRIMARY KEY (order_id, message_type)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_shipping_sent_at ON shipping_notifications(sent_at)")

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


def _load_conversation_history(wa_id: str) -> list[dict]:
    """Pull up to 30 recent (customer msg, bot reply) exchanges with this
    wa_id from the last 7 days, oldest first.

    The 30-turn / 7-day window is sized for multi-day threads — refund
    flows, return pickups, and Udit-handled escalations often span days,
    and the twin needs to see the resolution context from earlier in
    the thread so it doesn't restart the conversation as if it's a
    fresh complaint when the customer follows up with "any update?".

    Only AUTO and ESCALATE rows are eligible — those are the only statuses
    that reflect a real reply the customer actually saw. DRAFT replies were
    sent over Telegram to the founder, not delivered to WhatsApp, so
    including them would mislead Claude into thinking the customer saw
    text they never did.

    Token-cost note: 30 turns × ~150 chars avg ≈ 4-5k chars of extra
    context per Claude call. Well within Sonnet 4.6's window, and the
    user-message portion of the prompt isn't cached, so this is a real
    additive cost. Still net cheaper than re-asking the customer for
    info already in the thread.

    Failures are logged-and-swallowed → returns []. Caller falls back to a
    plain single-turn call.
    """
    if not wa_id:
        return []
    try:
        cutoff = time.time() - 7 * 24 * 3600  # 7 days
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, msg_text, reply_text
            FROM message_logs
            WHERE wa_id = ?
              AND status IN ('AUTO', 'ESCALATE')
              AND ts >= ?
              AND msg_text IS NOT NULL
              AND reply_text IS NOT NULL
            ORDER BY ts DESC
            LIMIT 30
            """,
            (wa_id, cutoff),
        )
        rows = cur.fetchall()
        conn.close()
        rows.reverse()  # oldest -> newest
        history = [
            {"ts": r[0], "msg_text": r[1], "reply_text": r[2]} for r in rows
        ]
        print(f"[MEMORY] Loaded {len(history)} messages for {wa_id}")
        return history
    except Exception as e:
        print(f"[MEMORY] Failed to load history for {wa_id}: {type(e).__name__}: {e}")
        return []


def _verify_shopify_hmac(raw_body: bytes, hmac_header: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification of a Shopify webhook body.

    Shopify computes HMAC-SHA256 of the raw request body using the
    webhook secret, base64-encodes it, and sends the result in
    X-Shopify-Hmac-Sha256. We must verify against the RAW body, not a
    re-serialized JSON — so the route reads request.get_data() before
    any parsing.

    Returns False (never raises) if the secret isn't configured, the
    header is missing, or the digests don't match.
    """
    if not SHOPIFY_WEBHOOK_SECRET or not hmac_header:
        return False
    try:
        computed = base64.b64encode(
            hmac.new(
                SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
                raw_body,
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return hmac.compare_digest(computed, hmac_header)
    except Exception as e:
        print(f"[SHOPIFY] HMAC verify error: {type(e).__name__}: {e}")
        return False


def _phone_to_10digit(raw: str) -> str:
    """Reduce any phone string to the 10-digit Indian mobile form.

    Strips non-digits, then drops the leading "91" country code if the
    result is 12 digits, or a leading "0" if it's 11 digits. Used both
    when storing Shopify orders and when matching a WhatsApp wa_id
    against the orders table.

    "+91 98765 43210" -> "9876543210"
    "919876543210"     -> "9876543210"
    "9876543210"        -> "9876543210"
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) >= 12 and digits.startswith("91"):
        digits = digits[-10:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _phone_to_wa_id(raw: str) -> str:
    """Convert any Indian phone string to WATI's "91XXXXXXXXXX" wa_id format.

    Sibling of _phone_to_10digit but in the opposite direction — produces
    the country-code-prefixed form WATI uses as the recipient ID when
    sending session messages. Returns "" if there aren't enough digits
    to be a plausible mobile number, so the caller can decide whether
    to skip the send entirely.

    Reuses _phone_to_10digit so the parsing rules stay consistent — any
    string it accepts gets a "91" prefixed; anything it rejects (wrong
    length, junk) returns "".

    "+91 98765 43210" -> "919876543210"
    "09876543210"      -> "919876543210"
    "9876543210"        -> "919876543210"
    "919876543210"      -> "919876543210"
    """
    ten = _phone_to_10digit(raw)
    if len(ten) != 10:
        return ""
    return "91" + ten


def _log_shopify_order(
    order_id: str,
    customer_phone: str,
    customer_name: str,
    product_names: str,
    total_price: str,
    order_status: str,
    created_at: str,
) -> None:
    """Insert one row into orders. Failures swallowed — same pattern as
    _log_message; we never break the webhook response on a DB hiccup."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO orders "
            "(order_id, customer_phone, customer_name, product_names, "
            " total_price, order_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                order_id,
                customer_phone,
                customer_name,
                product_names,
                total_price,
                order_status,
                created_at,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] _log_shopify_order failed: {type(e).__name__}: {e}")


def _lookup_recent_order(wa_id: str) -> str:
    """If this customer placed an order in the last 30 days, return a
    one-line summary suitable to inject into Claude's prompt. Empty
    string otherwise (or on any failure — treated as "no context").

    Format matches what Claude expects to see under "Order context":
        Recent order: #<id> — <product names> — ₹<total> — <status>
    """
    if not wa_id:
        return ""
    phone10 = _phone_to_10digit(wa_id)
    if not phone10:
        return ""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT order_id, product_names, total_price, order_status
            FROM orders
            WHERE customer_phone = ?
              AND logged_at >= datetime('now', '-30 days')
            ORDER BY logged_at DESC
            LIMIT 1
            """,
            (phone10,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return ""
        order_id, product_names, total_price, order_status = row
        line = (
            f"Recent order: #{order_id} — {product_names} — "
            f"₹{total_price} — {order_status}"
        )
        print(f"[ORDER] Found recent order #{order_id} for {wa_id}")
        return line
    except Exception as e:
        print(f"[ORDER] Lookup failed for {wa_id}: {type(e).__name__}: {e}")
        return ""


def _mark_shipping_sent(
    order_id: str,
    message_type: str,
    phone: str | None = None,
    order_number: str | None = None,
) -> None:
    """Persist that a shipping notification was sent for this
    (order_id, message_type) so subsequent webhook deliveries OR a
    Render redeploy can't double-send the same message.

    `message_type` is one of: "shipped", "out_for_delivery", "delivered",
    "tracking" — matches the keys _process_shipping_event uses.

    Uses INSERT OR IGNORE so duplicate calls don't update timestamps —
    the original send_at is preserved as the canonical "when we first
    notified the customer" record.

    Silent fail per [SHIPPING-DB] convention. By the time this is called
    the message has already shipped to the customer; a DB hiccup must
    NOT propagate or it'd 500 the webhook handler.
    """
    if not order_id or not message_type:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO shipping_notifications "
            "(order_id, message_type, phone, order_number, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(order_id), message_type, phone, order_number, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(
            f"[SHIPPING-DB] _mark_shipping_sent failed for "
            f"{order_id}/{message_type}: {type(e).__name__}: {e}"
        )


def _was_shipping_sent(order_id: str, message_type: str) -> bool:
    """Check whether a notification of this type has already been sent
    for this order. Used by _process_shipping_event and the new
    /shopify-order-update handler for dedup across webhook retries +
    Render restarts.

    Returns False on any DB error — FAIL-OPEN, because the cost of one
    duplicate message is far lower than the cost of silently swallowing
    a legitimate shipping notification. A DB hiccup must never block a
    real send.
    """
    if not order_id or not message_type:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM shipping_notifications "
            "WHERE order_id = ? AND message_type = ? LIMIT 1",
            (str(order_id), message_type),
        )
        hit = cur.fetchone() is not None
        conn.close()
        return hit
    except Exception as e:
        print(
            f"[SHIPPING-DB] _was_shipping_sent check failed for "
            f"{order_id}/{message_type}: {type(e).__name__}: {e}"
        )
        return False


def _send_instagram_reply(sender_id: str, text: str) -> None:
    """Send an outbound Instagram DM via the Meta Graph Messages API.

    Endpoint: POST https://graph.facebook.com/v19.0/me/messages
    Auth via ?access_token=... query param (Meta's documented pattern).
    Body: {"recipient": {"id": <sender>}, "message": {"text": <reply>}}

    All exceptions are logged-and-swallowed — Instagram delivery must
    never break the webhook 200 response. Missing access token →
    skipped silently with a single log line.
    """
    if not INSTAGRAM_PAGE_ACCESS_TOKEN:
        print("[INSTAGRAM] Skipped: INSTAGRAM_PAGE_ACCESS_TOKEN not set")
        return
    if not sender_id or not text:
        return

    # INSTAGRAM_PAGE_ID resolves to the Instagram Business Account ID when
    # set, else "me" as a fallback. INSTAGRAM_API_BASE defaults to the
    # Instagram Graph API (graph.instagram.com) — the host where IG Login
    # tokens with instagram_business_manage_messages have scope. Hitting
    # graph.facebook.com with an IG-flow token produces the misleading
    # "Object with ID 'me' does not exist due to missing permissions"
    # error: it's not a permissions issue, it's the wrong host.
    page_ref = INSTAGRAM_PAGE_ID or "me"
    url = f"{INSTAGRAM_API_BASE}/{page_ref}/messages"
    params = {"access_token": INSTAGRAM_PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": sender_id},
        "message": {"text": text},
    }

    try:
        resp = requests.post(
            url, params=params, json=payload, timeout=INSTAGRAM_TIMEOUT_SECONDS
        )
        if resp.ok:
            print(f"[INSTAGRAM] Sent reply to {sender_id} ({len(text)} chars)")
        else:
            # On failure, surface diagnostic info about the token so
            # config issues are obvious from Render logs without leaking
            # the secret itself. Length + 4-char prefix is enough to tell
            # whether the env var loaded, was truncated, or carried garbage.
            tok_len = len(INSTAGRAM_PAGE_ACCESS_TOKEN)
            tok_prefix = INSTAGRAM_PAGE_ACCESS_TOKEN[:4] if tok_len else "(empty)"
            print(
                f"[INSTAGRAM] Failed: HTTP {resp.status_code} "
                f"{resp.text[:300]} "
                f"(token len={tok_len}, prefix={tok_prefix!r})"
            )
    except requests.RequestException as e:
        print(f"[INSTAGRAM] Network error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[INSTAGRAM] Unexpected error: {type(e).__name__}: {e}")


def _log_instagram(
    sender_id: str,
    message_text: str,
    reply_text: str,
    timestamp: str,
    source: str | None = None,
) -> None:
    """Insert one Instagram exchange into instagram_logs.

    `source` is an optional tag. Today's only special value is
    "HUMAN_UDIT_INSTAGRAM" — used to record that Udit manually replied
    on Instagram so the inbound handler can short-circuit (similar to
    the WATI HUMAN_UDIT safety net).

    Failures swallowed — same pattern as _log_message and
    _log_shopify_order. Backup loop captures this table at the next
    tick like every other table on the same SQLite file.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO instagram_logs "
            "(sender_id, message_text, reply_text, timestamp, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (sender_id, message_text, reply_text, timestamp, source),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] _log_instagram failed: {type(e).__name__}: {e}")


def _udit_replied_recently_ig(sender_id: str, window_seconds: int = HUMAN_HANDLING_WINDOW_SECONDS) -> bool:
    """Return True if Udit manually replied on Instagram to this sender
    within `window_seconds`. Used by _process_instagram_event as a
    restart-safe sibling of the WATI _udit_replied_recently safety net.

    A HUMAN_UDIT_INSTAGRAM row is written by _process_instagram_event
    when Meta delivers an event with sender_id == INSTAGRAM_PAGE_ID
    (Udit's manual outbound from the IG app), with the recipient
    customer's ID stored as instagram_logs.sender_id. So the query is:
    "does a HUMAN_UDIT_INSTAGRAM row exist for this customer in the last
    4h?"

    Returns False on any failure (fail-open, same convention as the WATI
    helper) — the in-memory paused_numbers register is the primary gate;
    this DB check is the post-restart backup.
    """
    if not sender_id:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM instagram_logs "
            "WHERE sender_id = ? "
            "AND source = 'HUMAN_UDIT_INSTAGRAM' "
            "AND logged_at >= datetime('now', ?) "
            "LIMIT 1",
            (sender_id, f"-{int(window_seconds)} seconds"),
        )
        hit = cur.fetchone() is not None
        conn.close()
        return hit
    except Exception as e:
        print(f"[HUMAN_HANDLING_IG] DB check failed for {sender_id}: {type(e).__name__}: {e}")
        return False


def _load_instagram_history(sender_id: str) -> list[dict]:
    """Pull up to 30 recent (DM, reply) exchanges with this Instagram
    sender from the last 7 days, oldest first.

    Matches _load_conversation_history's window (30 turns × 7 days) so
    multi-day IG threads (refund/return/collab follow-ups) keep their
    resolution context. Same rationale as the WATI helper — without
    this, the twin restarts the conversation as if it's a new complaint
    when the customer follows up with "any update?".

    Returns the same shape as _load_conversation_history so it can be
    handed straight to ask_claude(history=...) — list of dicts with
    msg_text / reply_text / ts. ts is a placeholder (0) here since we
    don't need it for the prompt construction.

    HUMAN_UDIT_INSTAGRAM rows (Udit's manual IG-app replies) are
    excluded via the `message_text IS NOT NULL` filter because those
    rows are written with empty message_text — they're for the safety-net
    check, not the conversation context Claude should reply to.

    Failures return [] silently so the call falls back to single-turn
    behavior, identical to a brand-new sender.
    """
    if not sender_id:
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT message_text, reply_text
            FROM instagram_logs
            WHERE sender_id = ?
              AND message_text IS NOT NULL
              AND message_text != ''
              AND reply_text IS NOT NULL
              AND logged_at >= datetime('now', '-7 days')
            ORDER BY logged_at DESC
            LIMIT 30
            """,
            (sender_id,),
        )
        rows = cur.fetchall()
        conn.close()
        rows.reverse()  # oldest first
        history = [
            {"ts": 0, "msg_text": r[0], "reply_text": r[1]} for r in rows
        ]
        print(f"[INSTAGRAM] Loaded {len(history)} history turns for {sender_id}")
        return history
    except Exception as e:
        print(f"[INSTAGRAM] History load failed for {sender_id}: {type(e).__name__}: {e}")
        return []


def _github_backup_configured() -> bool:
    """All three env vars must be set for backup/restore to even attempt
    network calls. Token and repo are mandatory; backup path has a default."""
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_contents_url() -> str:
    # GITHUB_BACKUP_PATH is a path-within-repo (e.g. "glamshelf_logs.db").
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_PATH}"


def _restore_db_from_github() -> None:
    """Pull the latest backup from GitHub if there's no local DB yet.

    Order matters: this runs BEFORE _init_db() so a freshly-deployed
    Render instance with no /tmp DB picks up the previous deploy's data.
    If a local DB already exists (e.g. legacy /tmp file or persistent
    disk re-mount), we skip — never clobber live data.
    """
    if os.path.exists(DB_PATH):
        return  # Local copy already present; don't overwrite.
    if not _github_backup_configured():
        print("[RESTORE] Skipped: GITHUB_TOKEN or GITHUB_REPO not set")
        return
    try:
        resp = requests.get(_github_contents_url(), headers=_github_headers(), timeout=30)
        if resp.status_code == 404:
            print("[RESTORE] No backup found, starting fresh")
            return
        if not resp.ok:
            print(f"[RESTORE] Failed: HTTP {resp.status_code} {resp.text[:200]}")
            return
        payload = resp.json()
        content_b64 = (payload.get("content") or "").replace("\n", "")
        if not content_b64:
            print("[RESTORE] Failed: response had no content field")
            return
        raw = base64.b64decode(content_b64)
        parent = os.path.dirname(DB_PATH)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(DB_PATH, "wb") as f:
            f.write(raw)
        print(f"[RESTORE] DB restored from GitHub ({len(raw)} bytes)")
    except Exception as e:
        print(f"[RESTORE] Failed: {type(e).__name__}: {e}")


def _backup_db_to_github() -> None:
    """Push the current DB file to GitHub via the Contents API.

    Idempotent — uses the existing file's SHA when present (required by
    the API for updates). All exceptions are logged and swallowed; the
    backup loop never crashes the app.
    """
    if not _github_backup_configured():
        print("[BACKUP] Skipped: env vars not set")
        return
    if not os.path.exists(DB_PATH):
        print(f"[BACKUP] Skipped: DB file not found at {DB_PATH}")
        return
    try:
        with open(DB_PATH, "rb") as f:
            raw = f.read()
        content_b64 = base64.b64encode(raw).decode("ascii")

        # Look up the existing file's SHA — required when updating an
        # existing path. 404 (file doesn't exist yet) is the create case.
        existing_sha: str | None = None
        try:
            head = requests.get(
                _github_contents_url(), headers=_github_headers(), timeout=15
            )
            if head.ok:
                existing_sha = head.json().get("sha")
            elif head.status_code != 404:
                print(
                    f"[BACKUP] SHA lookup returned {head.status_code}: "
                    f"{head.text[:200]} — proceeding as create"
                )
        except Exception as e:
            print(f"[BACKUP] SHA lookup error ({type(e).__name__}: {e}) — proceeding as create")

        payload = {
            "message": f"auto-backup glamshelf_logs.db @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "content": content_b64,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        resp = requests.put(
            _github_contents_url(), headers=_github_headers(), json=payload, timeout=60
        )
        if resp.ok:
            print(f"[BACKUP] DB backed up to GitHub ({len(raw)} bytes)")
        else:
            print(f"[BACKUP] Failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[BACKUP] Failed: {type(e).__name__}: {e}")


def _backup_loop_tick() -> None:
    """Single tick: back up, then schedule the next tick. Always re-arms,
    even when the backup itself raises, so a transient error doesn't kill
    the loop."""
    try:
        _backup_db_to_github()
    except Exception as e:
        print(f"[BACKUP] Loop tick crashed: {type(e).__name__}: {e}")
    finally:
        t = threading.Timer(BACKUP_INTERVAL_SECONDS, _backup_loop_tick)
        t.daemon = True
        t.start()


def _start_backup_loop() -> None:
    """Kick off the periodic backup. Initial backup runs immediately in a
    background thread so it can't block startup; subsequent ticks fire on
    the timer cadence. All threads are daemons — they won't block process
    shutdown when Render recycles the worker."""
    if not _github_backup_configured():
        print("[BACKUP] Skipped: env vars not set (loop disabled)")
        return
    threading.Thread(target=_backup_loop_tick, daemon=True).start()


_restore_db_from_github()
_init_db()
_start_backup_loop()

# Anthropic client picks up ANTHROPIC_API_KEY from the environment.
client = Anthropic()


def send_telegram_notification(
    classification: str,
    customer_message: str,
    reply: str,
    sender_info: str | None = None,
    channel: str = "WhatsApp",
    customer_id: str | None = None,
) -> None:
    """Fire a Telegram message to the founder for DRAFT+APPROVE and ESCALATE.

    AUTO classifications send nothing (the reply was safe to send as-is and
    Udit doesn't need to be paged about it).

    sender_info is optional — when present (e.g. when called from the WATI
    or Instagram webhook), it's prepended to the message so Udit knows
    which contact to reply to.

    `channel` (default "WhatsApp") tunes the action-footer phrasing per
    channel: WhatsApp says "from your WhatsApp Business app" and ESCALATE
    instructs to NOT send (because the WATI handler suppresses the reply
    on ESCALATE/DRAFT). Instagram says "from your Instagram DMs" and
    ESCALATE notes the holding reply was already sent (because the IG
    handler ships every classification's reply). Default preserves the
    existing WATI behavior byte-for-byte.

    `customer_id` is the wa_id (WATI) or IG sender_id. When provided AND
    classification is ESCALATE, a single inline button "🛑 Stop bot for
    this customer" is attached to the Telegram message. Tapping it routes
    to _handle_telegram_callback's pause_escalate handler, which calls
    _pause_number(customer_id) for 4h. Without customer_id (e.g. the
    /api/draft browser flow) the message goes out as plain text — same
    as before.

    All failures (network, Telegram API errors, missing token, etc.) are
    logged and swallowed — Telegram is a side effect, never a blocker for
    the /api/draft response or the /webhook 200 reply.
    """
    if classification == "AUTO":
        return  # No notification needed for safe replies.

    sender_block = f"From: {sender_info}\n\n" if sender_info else ""

    # Channel-specific phrasing for the action footer. WhatsApp branch is
    # the verbatim original wording; Instagram branch reflects that the
    # IG handler already shipped the customer reply.
    if channel == "Instagram":
        approve_destination = "your Instagram DMs"
        escalate_action = (
            "→ Holding reply already sent on Instagram. "
            "Take over the conversation directly."
        )
    else:
        approve_destination = "your WhatsApp Business app"
        escalate_action = "→ Do NOT send the reply. Handle this yourself."

    if classification == "DRAFT+APPROVE":
        text = (
            "🟡 DRAFT + APPROVE\n\n"
            f"{sender_block}"
            "Customer said:\n"
            f'"{customer_message}"\n\n'
            "Drafted reply:\n"
            f'"{reply}"\n\n'
            f"→ Review and send manually from {approve_destination}."
        )
    elif classification == "ESCALATE":
        text = (
            "🔴 ESCALATE — Take over directly\n\n"
            f"{sender_block}"
            "Customer said:\n"
            f'"{customer_message}"\n\n'
            "Suggested holding reply:\n"
            f'"{reply}"\n\n'
            f"{escalate_action}"
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

    # ESCALATE-only: attach a single inline button so Udit can pause the
    # twin for this customer with one tap, without leaving Telegram. The
    # callback_data fits well under Telegram's 64-byte limit:
    # "action:pause_escalate|id:<id>" ≈ 30-52 bytes.
    if classification == "ESCALATE" and customer_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {
                    "text": "🛑 Stop bot for this customer",
                    "callback_data": f"action:pause_escalate|id:{customer_id}",
                }
            ]]
        }

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


# ===== Telegram inline-button DRAFT approval flow =====

def _telegram_api(method: str, payload: dict) -> dict | None:
    """POST to Telegram Bot API. Returns parsed JSON on success, None on
    any failure. Never raises — Telegram side effects are non-critical.

    Used by the DRAFT-button flow (send_draft_for_approval and the
    /telegram-callback handlers). The legacy send_telegram_notification
    above predates this helper and still has its own inline requests
    call; intentionally left alone to keep that codepath byte-identical.
    """
    if not TELEGRAM_BOT_TOKEN:
        print(f"[TELEGRAM DRAFT] {method} skipped: TELEGRAM_BOT_TOKEN not set")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        if not resp.ok:
            print(f"[TELEGRAM DRAFT] {method} HTTP {resp.status_code}: {resp.text[:300]}")
            return None
        return resp.json()
    except requests.RequestException as e:
        print(f"[TELEGRAM DRAFT] {method} network error: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"[TELEGRAM DRAFT] {method} unexpected error: {type(e).__name__}: {e}")
        return None


def _is_authorized_telegram_chat(chat_id) -> bool:
    """Only honor callback/message events from the configured owner chat.

    Without this gate, anyone who discovers /telegram-callback could
    trigger WhatsApp sends on your behalf. We check the chat id from the
    incoming Telegram update against TELEGRAM_CHAT_ID — if mismatched,
    the handler silently drops the event.
    """
    if not TELEGRAM_CHAT_ID:
        return False
    try:
        return str(chat_id) == str(TELEGRAM_CHAT_ID)
    except Exception:
        return False


def send_draft_for_approval(
    customer_number: str,
    customer_name: str,
    customer_message: str,
    reply_text: str,
) -> bool:
    """Send a Telegram message with [✅ Send as-is | ✏️ Edit | ⛔ Skip]
    inline buttons and register the draft in _pending_drafts so the
    /telegram-callback handler can action it.

    Returns True if the buttoned message was sent and state was registered;
    False on any failure (caller may fall back to plain-text notification).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM DRAFT] Skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    draft_id = secrets.token_hex(4)  # 8 hex chars → safe for 64-byte callback_data limit
    sender_block = (
        f"{customer_name} ({customer_number})" if customer_name else customer_number
    )
    text = (
        "🟡 DRAFT + APPROVE\n\n"
        f"From: {sender_block}\n\n"
        "Customer said:\n"
        f'"{customer_message}"\n\n'
        "Drafted reply:\n"
        f'"{reply_text}"'
    )

    # callback_data must be ≤ 64 bytes (Telegram hard limit). Our format:
    #   "action:<verb>|num:<wa_id>|id:<8-hex>"
    # Worst case: action:send (11) + |num: (5) + 12 wa_id + |id: (4) + 8 = 40 bytes.
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Send as-is", "callback_data": f"action:send|num:{customer_number}|id:{draft_id}"},
            {"text": "✏️ Edit",       "callback_data": f"action:edit|num:{customer_number}|id:{draft_id}"},
            {"text": "⛔ Skip",        "callback_data": f"action:skip|num:{customer_number}|id:{draft_id}"},
        ]]
    }

    resp = _telegram_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "reply_markup": keyboard,
    })
    if not resp or not resp.get("ok"):
        return False

    result = resp.get("result") or {}
    _pending_drafts[draft_id] = {
        "reply_text": reply_text,
        "customer_number": customer_number,
        "customer_name": customer_name,
        "customer_message": customer_message,
        "original_text": text,
        "telegram_chat_id": (result.get("chat") or {}).get("id"),
        "telegram_message_id": result.get("message_id"),
        "awaiting_edit": False,
        "created_at": time.time(),
    }

    # Opportunistic prune — clean entries older than TTL so the dict
    # stays bounded even if some drafts are never actioned.
    cutoff = time.time() - PENDING_DRAFT_TTL_SECONDS
    for stale_id in [k for k, v in _pending_drafts.items() if v["created_at"] < cutoff]:
        del _pending_drafts[stale_id]

    print(f"[TELEGRAM DRAFT] Sent buttoned draft id={draft_id} for {customer_number}")
    return True


def _parse_callback_data(data: str) -> dict:
    """Parse 'action:send|num:919...|id:abc12345' into a dict.

    Robust to missing fields; returns whatever keys were present. Caller
    validates required fields.
    """
    out: dict = {}
    for part in (data or "").split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k] = v
    return out


def _finalize_draft_message(
    chat_id, message_id, original_text: str, suffix: str
) -> None:
    """Strip the inline keyboard from a draft message and append a status
    line so Udit can see what happened without scrolling. Best effort —
    failures here just mean the buttons stick around looking active, but
    the dedup check (draft_id not in _pending_drafts) still prevents
    duplicate actions on subsequent taps.
    """
    _telegram_api("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": []},
    })
    _telegram_api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": (original_text + "\n\n" + suffix)[:4096],  # Telegram message length limit
    })


def _handle_telegram_callback(cb: dict) -> None:
    """Process a single inline-button tap (callback_query).

    Answers the callback first (Telegram requires it within ~30s or the
    button shows a loading spinner forever), then does the action.
    """
    callback_id = cb.get("id")
    data_str = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    if not _is_authorized_telegram_chat(chat_id):
        print(f"[TELEGRAM DRAFT] Ignored callback from unauthorized chat {chat_id}")
        # Still answer so the user's button doesn't spin forever.
        if callback_id:
            _telegram_api("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": "Not authorized"
            })
        return

    parsed = _parse_callback_data(data_str)
    action = parsed.get("action")
    draft_id = parsed.get("id")
    customer_number = parsed.get("num")

    # ESCALATE "🛑 Stop bot for this customer" button — handled first
    # because it has no _pending_drafts state. The `id` in the callback
    # is the customer's wa_id (WATI) or sender_id (IG), and we just need
    # to pause that number for 4h.
    if action == "pause_escalate":
        customer_id = parsed.get("id") or ""
        if not customer_id:
            _telegram_api("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": "No customer id"
            })
            print("[ESCALATE-PAUSE] Tap missing id — ignored")
            return
        _pause_number(customer_id)
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "✅ Bot paused for this customer",
        })
        # Strip the button and append a status footer so the chat history
        # reads cleanly. Best effort — same _finalize-style edit pattern
        # as the DRAFT buttons use.
        original_text = (msg.get("text") or "")
        _telegram_api("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        _telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": (original_text + "\n\n🛑 Bot paused 4h — you're handling this")[:4096],
        })
        print(f"[ESCALATE-PAUSE] Udit tapped Stop bot — paused {customer_id} for 4h")
        return

    # Atomic pop — dict.pop is thread-safe in CPython, so two rapid taps
    # racing into this function only one of them gets the draft; the other
    # gets None and falls through to the "already handled" dedup branch.
    # Without this, a fast double-tap on "Send as-is" could pass the
    # "if not draft" check twice and double-send to the customer.
    #
    # The edit branch below re-inserts the draft (with awaiting_edit=True)
    # because the follow-up text message handler needs to find it.
    draft = _pending_drafts.pop(draft_id, None) if draft_id else None

    # Dedup — second tap on same button (or post-restart orphan).
    if not draft:
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": "Already handled"
        })
        print(f"[TELEGRAM DRAFT] Tap on stale draft id={draft_id} — already handled")
        return

    customer_name = draft.get("customer_name") or ""
    name_for_display = customer_name or customer_number
    original_text = draft.get("original_text") or (msg.get("text") or "")

    if action == "send":
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": "Sending…"
        })
        send_whatsapp_reply(customer_number, draft["reply_text"])
        _finalize_draft_message(
            chat_id, message_id, original_text,
            f"✅ Sent to {name_for_display}",
        )
        # Already popped at top — no del needed.
        print(f"[TELEGRAM DRAFT] Send-as-is for {customer_number} (draft {draft_id})")

    elif action == "edit":
        # Re-insert the draft so the next text message from this chat can
        # find it via _handle_telegram_message. Flip awaiting_edit so the
        # message handler routes to the edit flow.
        draft["awaiting_edit"] = True
        draft["edit_started_at"] = time.time()
        _pending_drafts[draft_id] = draft
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": "Send your edit"
        })
        # Remove buttons immediately so a second tap doesn't re-trigger.
        _telegram_api("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"✏️ Please send your edited message for {name_for_display}:",
        })
        # Schedule a 10-min auto-skip timer in case Udit walks away.
        timer = threading.Timer(EDIT_TIMEOUT_SECONDS, _edit_timeout_check, args=(draft_id,))
        timer.daemon = True
        timer.start()
        print(f"[TELEGRAM DRAFT] Awaiting edit for {customer_number} (draft {draft_id})")

    elif action == "skip":
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": "Skipped"
        })
        _finalize_draft_message(
            chat_id, message_id, original_text,
            "⛔ Skipped — handle manually in WATI",
        )
        # Already popped at top — no del needed.
        print(f"[TELEGRAM DRAFT] Skipped for {customer_number} (draft {draft_id})")

    else:
        # Malformed callback_data (unknown action verb). The atomic pop
        # already removed the draft; we don't re-insert because we don't
        # know how to recover. The draft is effectively skipped, which is
        # the safer failure mode than re-inserting in an unknown state.
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": "Unknown action"
        })
        print(f"[TELEGRAM DRAFT] Unknown action {action!r} on draft {draft_id} — draft dropped")


def _handle_telegram_message(msg: dict) -> None:
    """Process a regular text message from Telegram.

    Today's only purpose: complete an in-flight Edit flow. If any pending
    draft is marked awaiting_edit for this chat, the next text message
    from Udit becomes the edited reply (sent to WATI verbatim).

    Anything else (chat messages from Udit not tied to a pending edit) is
    logged and ignored.
    """
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    if not _is_authorized_telegram_chat(chat_id):
        return  # silently drop anything from unauthorized chats

    # Find oldest awaiting-edit draft from this chat. If somehow there are
    # multiple, the oldest is the most likely one Udit meant — but in
    # practice there's at most one because hitting Edit removes buttons
    # from that message immediately.
    target_id: str | None = None
    target_started: float = float("inf")
    for did, d in _pending_drafts.items():
        if (
            d.get("awaiting_edit")
            and d.get("telegram_chat_id") == chat_id
            and d.get("edit_started_at", float("inf")) < target_started
        ):
            target_id = did
            target_started = d.get("edit_started_at", float("inf"))

    if not target_id:
        # Not part of an edit flow — could be Udit typing anything in the
        # bot chat. Ignore (no command system yet).
        return

    draft = _pending_drafts[target_id]
    customer_number = draft["customer_number"]
    customer_name = draft.get("customer_name") or ""
    name_for_display = customer_name or customer_number

    send_whatsapp_reply(customer_number, text)
    _telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": f"✅ Sent your edit to {name_for_display}",
    })

    # Annotate the original draft message so the chat history reads cleanly.
    orig_chat = draft.get("telegram_chat_id")
    orig_msg = draft.get("telegram_message_id")
    original_text = draft.get("original_text") or ""
    if orig_chat and orig_msg:
        _finalize_draft_message(
            orig_chat, orig_msg, original_text,
            f"✏️ Edited and sent to {name_for_display}",
        )

    del _pending_drafts[target_id]
    print(f"[TELEGRAM DRAFT] Edit completed for {customer_number} (draft {target_id})")


def _edit_timeout_check(draft_id: str) -> None:
    """Fires EDIT_TIMEOUT_SECONDS after Edit was tapped. If the draft is
    still awaiting an edit at that point, auto-skip and notify Telegram.

    No-op if the user already sent the edit, hit Skip, or the worker
    restarted (draft would no longer be in _pending_drafts).
    """
    draft = _pending_drafts.get(draft_id)
    if not draft or not draft.get("awaiting_edit"):
        return  # already actioned
    customer_number = draft.get("customer_number") or "(unknown)"
    customer_name = draft.get("customer_name") or ""
    name_for_display = customer_name or customer_number
    print(f"[DRAFT] Edit timed out for {customer_number}")

    chat_id = draft.get("telegram_chat_id")
    if chat_id:
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": f"⏱️ Edit timed out for {name_for_display} — auto-skipped",
        })
    # Also strip buttons / annotate the original message if we still have its id.
    orig_msg = draft.get("telegram_message_id")
    original_text = draft.get("original_text") or ""
    if chat_id and orig_msg:
        _finalize_draft_message(
            chat_id, orig_msg, original_text,
            "⏱️ Edit timed out — auto-skipped",
        )
    _pending_drafts.pop(draft_id, None)


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
            # Register the outbound so WATI's subsequent outbound webhook
            # (echoing this same message back) is identified as bot-originated
            # rather than Udit's manual reply — prevents HUMAN_UDIT mis-tagging
            # that would otherwise suppress the AUTO flow.
            _record_bot_outbound(reply_text, data if isinstance(data, dict) else None)
        else:
            print(f"[WATI] HTTP failure {response.status_code}")
    except requests.RequestException as e:
        print(f"[WATI] Network error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[WATI] Unexpected error: {type(e).__name__}: {e}")


def send_whatsapp_template(
    wa_id: str,
    template_name: str,
    parameters: list[dict],
    broadcast_name: str | None = None,
) -> bool:
    """Send a WhatsApp template (HSM) message via WATI's sendTemplateMessage API.

    Template messages work OUTSIDE WATI's 24-hour session window — the
    failure mode that sessionMessage hits with "Ticket has been expired"
    when the customer hasn't messaged us in the last 24h. Used for
    transactional notifications (order shipped, etc.) where the customer
    may not have messaged us before.

    Args:
      wa_id: recipient phone in "91XXXXXXXXXX" format
      template_name: WATI template name as configured in the WATI dashboard
        (e.g. "shipping_notification_template")
      parameters: list of {"name": "...", "value": "..."} dicts matching
        the variable names configured in the WATI template. WATI expects
        named parameters (not positional {{1}}/{{2}} placeholders) in the
        request body — the names map to the placeholders server-side.
      broadcast_name: optional broadcast label for WATI's analytics
        (defaults to template_name)

    Returns:
      True if WATI accepted the message (HTTP 2xx and result=true),
      False on any failure — network error, HTTP error, WATI rejection
      (e.g. template not approved, parameters mismatched), missing config.
      NEVER raises — caller can fall back to send_whatsapp_reply.

    Same auth + base URL as send_whatsapp_reply — only the path differs
    (sendTemplateMessage vs sendSessionMessage). Per the WATI_ENDPOINT
    comment at the top of this module, the existing prod value already
    points at live-mt-server.wati.io which serves both endpoints.
    """
    if not WATI_API_KEY or not WATI_ENDPOINT:
        print("[WATI-TEMPLATE] Skipped: WATI_API_KEY or WATI_ENDPOINT not set")
        return False
    if not wa_id or not template_name:
        print("[WATI-TEMPLATE] Skipped: missing wa_id or template_name")
        return False

    # Defense in depth: never auto-send to business/owner numbers.
    target = normalize_wa(wa_id)
    if target and target in {normalize_wa(BUSINESS_NUMBER), normalize_wa(OWNER_NUMBER)}:
        print(f"[WATI-TEMPLATE] BLOCKED outbound to protected number {wa_id}")
        return False

    endpoint = WATI_ENDPOINT.rstrip("/")
    url = f"{endpoint}/api/v1/sendTemplateMessage/{wa_id}"
    headers = {
        "Authorization": f"Bearer {WATI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "template_name": template_name,
        "broadcast_name": broadcast_name or template_name,
        "parameters": parameters,
    }

    print(
        f"[WATI-TEMPLATE] POST {url} template={template_name!r} "
        f"params_count={len(parameters)}"
    )

    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=WATI_TIMEOUT_SECONDS
        )

        # Same logging pattern as send_whatsapp_reply — WATI returns
        # logical failures inside the JSON body even on HTTP 200, so we
        # log the body verbatim (truncated) and parse for result=false.
        body_preview = (response.text or "(empty body)")[:1000]
        print(f"[WATI-TEMPLATE] HTTP {response.status_code}")
        print(f"[WATI-TEMPLATE] Response body: {body_preview}")

        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict) and data.get("result") is False:
            info = data.get("info") or data.get("message") or "(no detail)"
            print(f"[WATI-TEMPLATE] API rejected: {info}")
            return False
        if not response.ok:
            print(f"[WATI-TEMPLATE] HTTP failure {response.status_code}")
            return False

        print(f"[WATI-TEMPLATE] Sent template {template_name!r} to {wa_id}")

        # Pre-register the outbound for HUMAN_UDIT echo detection. We
        # can't pre-register the rendered text (WATI renders the template
        # server-side), but the msg_id from the response is enough — the
        # subsequent WATI outbound webhook will carry the same id and
        # _seen_ids will catch it. Empty text param is a no-op.
        _record_bot_outbound("", data if isinstance(data, dict) else None)
        return True
    except requests.RequestException as e:
        print(f"[WATI-TEMPLATE] Network error: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        print(f"[WATI-TEMPLATE] Unexpected error: {type(e).__name__}: {e}")
        return False


def get_live_inventory() -> str:
    """Fetch current stock for every product in Shopify and return a
    plaintext block suitable for prepending to the brain on every Claude
    call.

    Source: Shopify's public storefront /products.json endpoint — no
    auth required. Each variant exposes an `available` boolean (NOT a
    numeric quantity), so the block marks every product as either IN
    STOCK or SOLD OUT with no unit counts:

        [LIVE INVENTORY - checked now]
        GS1 Luxe Light Lash Tray: IN STOCK
        GS3 Luxe Light Half Lash Tray: SOLD OUT
        ...

    Returns "" on any failure — network error, HTTP error, malformed
    JSON, anything. The caller treats "" as "no live data, continue with
    the brain as-is" so a Shopify outage never breaks the webhook.
    NEVER raises.

    Cached in-memory for 5 minutes per worker (INVENTORY_CACHE_TTL_SECONDS).
    Important: only SUCCESSFUL fetches are cached. If the call fails we
    return "" without caching, so the next customer message will re-try
    rather than wait out the full TTL behind a transient error.

    Product titles are echoed verbatim from Shopify — no mapping table
    here, so a product rename in Shopify takes effect on the next 5-min
    cache rollover with no brain.md change.
    """
    now = time.time()
    age = now - _inventory_cache["fetched_at"]
    if _inventory_cache["text"] and age < INVENTORY_CACHE_TTL_SECONDS:
        print(f"[INVENTORY] Cache hit (age {age:.0f}s, TTL {INVENTORY_CACHE_TTL_SECONDS}s)")
        return _inventory_cache["text"]

    params = {"limit": SHOPIFY_PRODUCTS_LIMIT}

    try:
        resp = requests.get(
            SHOPIFY_PRODUCTS_URL, params=params, timeout=SHOPIFY_TIMEOUT_SECONDS
        )
        if not resp.ok:
            # Status + first 200 chars is enough to diagnose 404 (wrong
            # path), 429 (rate limited), or 5xx from Render logs.
            print(
                f"[INVENTORY] Shopify HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return ""
        products = (resp.json() or {}).get("products") or []
    except requests.RequestException as e:
        print(f"[INVENTORY] Network error: {type(e).__name__}: {e}")
        return ""
    except Exception as e:
        # Defensive — JSON decode error, unexpected payload shape, anything.
        print(f"[INVENTORY] Unexpected error: {type(e).__name__}: {e}")
        return ""

    lines = ["[LIVE INVENTORY - checked now]"]
    for p in products:
        title = (p.get("title") or "").strip()
        variants = p.get("variants") or []
        if not title or not variants:
            continue
        # The public storefront endpoint exposes `available` (bool) per
        # variant — true means at least one unit is in stock, false means
        # sold out. Variants where the field is absent (very old themes)
        # get skipped rather than guessed.
        available = variants[0].get("available")
        if available is None:
            continue
        if available:
            lines.append(f"{title}: IN STOCK")
        else:
            lines.append(f"{title}: SOLD OUT")

    # If Shopify returned products but none had usable availability data,
    # we'd still produce a one-line block (just the header). That's not
    # useful for Claude and would consume system-prompt tokens for
    # nothing — return "" so the brain prompt is unchanged.
    if len(lines) == 1:
        print(f"[INVENTORY] Shopify returned {len(products)} products but none had availability data")
        return ""

    block = "\n".join(lines) + "\n"
    _inventory_cache["text"] = block
    _inventory_cache["fetched_at"] = now
    print(
        f"[INVENTORY] Fetched {len(products)} products from Shopify "
        f"({len(lines) - 1} with availability)"
    )
    return block


def draft_reply_logic(
    message: str,
    order_context: str = "",
    history: list[dict] | None = None,
    source: str = "WhatsApp",
) -> tuple[str, str, str]:
    """Core twin pipeline — load brain, call Claude, parse classification.

    Returns (classification, reply, raw_response).
      - classification: "AUTO" | "DRAFT+APPROVE" | "ESCALATE", or "" if parse failed
      - reply: drafted message text, or "" if parse failed
      - raw_response: exactly what Claude returned (after fence stripping)

    Used by /api/draft (browser drafter), /webhook (WATI WhatsApp), and
    /instagram-webhook (Meta DM). Each caller passes whatever extras
    apply: history for ongoing conversations, source for the channel
    label, order_context for Shopify recent-order injection.

    `source` defaults to "WhatsApp" so /api/draft and the WATI webhook
    are byte-identical to the previous behavior; the Instagram webhook
    passes source="Instagram DM".

    Raises if brain.md is missing or the Claude API call fails — callers
    must catch and decide how to surface the error.
    """
    if not BRAIN_FILE.exists():
        raise FileNotFoundError(f"brain file not found at {BRAIN_FILE}")

    brain = _load_brain_cached()

    # Prepend live Shopify inventory to the brain so Claude always sees
    # current stock at the very top of the system prompt. Empty string on
    # any failure (silent fallback) — brain alone is still a complete
    # working prompt; the inventory block is additive context. See
    # get_live_inventory() doc for details. The 5-minute cache there
    # plus Anthropic's 5-minute ephemeral system-prompt cache mean the
    # system prompt's content changes at most ~once per 5 minutes; the
    # cache churn cost is acceptable for the freshness gain.
    live_stock = get_live_inventory()
    if live_stock:
        brain = live_stock + "\n\n" + brain

    raw = ask_claude(brain, message, order_context, history=history, source=source)

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
    """Read brain.md fresh from disk. Always hits the filesystem.

    Most callers should go through _load_brain_cached() instead; this
    function is the raw IO primitive and the inner read for the cache.
    """
    print(f"[BRAIN] Loading {BRAIN_FILE}")
    text = BRAIN_FILE.read_text(encoding="utf-8")
    print(f"[BRAIN] Loaded {len(text)} chars")
    return text


def _load_brain_cached() -> str:
    """Return the brain text, using a 5-minute in-memory TTL cache.

    Cache states:
      - fresh (age < TTL): log "[BRAIN] Cache hit" and return cached text
      - empty or expired:  log "[BRAIN] Cache miss, reloading" and re-read

    Failure handling: if disk read fails AND we have a previously cached
    value, fall back to the cached value so a transient FS issue doesn't
    take down the webhook. If there's no cached value, propagate the
    error (same behavior as load_brain() before the cache existed).
    """
    global _brain_cache_text, _brain_cache_loaded_at

    age = time.time() - _brain_cache_loaded_at
    if _brain_cache_text is not None and age < BRAIN_CACHE_TTL_SECONDS:
        print(f"[BRAIN] Cache hit (age {age:.0f}s, TTL {BRAIN_CACHE_TTL_SECONDS}s)")
        return _brain_cache_text

    print("[BRAIN] Cache miss, reloading")
    try:
        text = load_brain()
    except Exception as e:
        if _brain_cache_text is not None:
            print(
                f"[BRAIN] Reload failed ({type(e).__name__}: {e}); "
                "serving last cached value"
            )
            return _brain_cache_text
        raise

    _brain_cache_text = text
    _brain_cache_loaded_at = time.time()
    return text


def build_user_message(
    customer_message: str,
    order_context: str,
    source: str = "WhatsApp",
) -> str:
    """The per-request user prompt. The brain itself goes in the `system`
    parameter (with cache_control) — see ask_claude().

    `source` labels the channel in the prompt header so Claude knows
    where the message came from. Default "WhatsApp" keeps every existing
    caller (/api/draft, WATI /webhook) byte-identical to the previous
    behavior. Instagram callers pass "Instagram DM"."""
    return (
        f"Customer {source} message:\n"
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


def _extract_wati_image_url(data: dict) -> str:
    """Try the known WATI payload locations for a media image URL.

    The existing webhook handler historically dropped non-text events
    without ever inspecting the image-shaped payload, so we have no
    on-record knowledge of WATI's exact field layout for images. This
    function probes the common WATI patterns from the past few plan
    versions and returns the first HTTP(S) URL it finds:

      data.data            (sometimes the URL is dumped here as a string)
      data.mediaUrl
      data.image           (string form)
      data.media.url / .link / .uri
      data.image.url / .link / .uri
      data.data.url / .link / .uri

    Returns "" if no URL was found — caller falls back to the
    deterministic "please type your order ID" reply. The full top-level
    keys are logged once per call so the first real image event reveals
    the actual layout if extraction misses.
    """
    found: list[str] = []

    def _push(v: object) -> None:
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            found.append(v)

    # Direct fields — sometimes the URL is the value, not nested.
    _push(data.get("data"))
    _push(data.get("mediaUrl"))
    _push(data.get("image"))

    # Nested objects under common keys.
    for key in ("media", "image", "data"):
        sub = data.get(key)
        if isinstance(sub, dict):
            for inner in ("url", "link", "uri"):
                _push(sub.get(inner))

    return found[0] if found else ""


def _extract_image_info(image_url: str) -> dict | None:
    """Download a WATI media image and extract order info via Claude Vision.

    Returns the parsed extraction dict on success (with possibly null
    fields and a "confidence" marker), or None on any failure — download
    timeout, HTTP error, vision API error, JSON parse failure, anything.
    NEVER raises.

    The vision call is intentionally SEPARATE from the main reply pipeline
    (ask_claude). This keeps the system prompts cleanly scoped: vision's
    job is ONLY structured extraction, not voice / classification. The
    extracted info is then handed to the main pipeline as a synthesized
    text query, so the brain's reply rules still drive the response.
    """
    if not image_url:
        return None

    # ----- Step 1: download the image bytes -----
    # WATI's media URLs sometimes require the same Bearer token used for
    # sendSessionMessage; sometimes they're plain CDN URLs that 401 when
    # auth headers are present. We try with auth first, fall back to no
    # auth if that returns 401/403.
    headers_with_auth = {}
    if WATI_API_KEY:
        headers_with_auth["Authorization"] = f"Bearer {WATI_API_KEY}"

    image_bytes: bytes | None = None
    content_type: str = "image/jpeg"
    try:
        resp = requests.get(
            image_url, headers=headers_with_auth, timeout=VISION_DOWNLOAD_TIMEOUT_SECONDS
        )
        if resp.status_code in (401, 403) and headers_with_auth:
            # Retry without auth — some WATI plans hand back signed CDN URLs.
            resp = requests.get(
                image_url, timeout=VISION_DOWNLOAD_TIMEOUT_SECONDS
            )
        if not resp.ok:
            print(f"[VISION] Download HTTP {resp.status_code} for {image_url[:120]}")
            return None
        image_bytes = resp.content
        raw_ct = resp.headers.get("Content-Type", "image/jpeg")
        # Strip "; charset=..." parameters and validate the prefix.
        candidate_ct = raw_ct.split(";")[0].strip().lower()
        if candidate_ct.startswith("image/") and candidate_ct in (
            "image/jpeg", "image/png", "image/gif", "image/webp"
        ):
            content_type = candidate_ct
        else:
            # Default to jpeg if Content-Type is missing or non-standard;
            # Claude's vision API accepts the four formats above.
            content_type = "image/jpeg"
    except requests.RequestException as e:
        print(f"[VISION] Download network error: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        print(f"[VISION] Download unexpected error: {type(e).__name__}: {e}")
        return None

    if not image_bytes:
        print("[VISION] Download returned empty body")
        return None

    print(f"[VISION] Downloaded image ({len(image_bytes)} bytes, type={content_type})")

    # ----- Step 2: send to Claude Vision for extraction -----
    raw = ""  # so it's defined for the except branch below
    try:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        message = client.messages.create(
            model=MODEL,
            max_tokens=VISION_MAX_TOKENS,
            system=VISION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract any order information visible in this screenshot per the system prompt. Return ONLY raw JSON.",
                    },
                ],
            }],
        )
        raw = "".join(b.text for b in message.content if b.type == "text").strip()
        cleaned, _ = strip_markdown_fences(raw)
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            print(f"[VISION] Parsed JSON was not a dict: {type(parsed).__name__}")
            return None
        return parsed
    except json.JSONDecodeError as e:
        print(f"[VISION] JSON parse error: {e}; raw={raw[:200]!r}")
        return None
    except Exception as e:
        print(f"[VISION] Claude Vision error: {type(e).__name__}: {e}")
        return None


def ask_claude(
    brain: str,
    customer_message: str,
    order_context: str,
    history: list[dict] | None = None,
    source: str = "WhatsApp",
) -> str:
    """Call the Anthropic Messages API.

    The brain content is sent as the `system` prompt with `cache_control`
    (ephemeral / 5-minute cache). Since brain.md is identical across all
    requests in a busy session, cache hits make follow-up requests
    significantly cheaper and lower-latency. The customer message and
    order context go in the user prompt and are *not* cached.

    `history`, when provided, is a list of {ts, msg_text, reply_text} dicts
    representing prior exchanges with the same customer (oldest first).
    Each entry becomes a user/assistant pair preceding the current
    user message, so Claude treats the request as a real multi-turn
    conversation rather than a one-shot question.

    `source` labels the channel ("WhatsApp" by default, "Instagram DM"
    for IG webhook calls). Surfaced in the per-request user prompt
    header; doesn't affect the cached system prompt.
    """
    user_text = build_user_message(customer_message, order_context, source=source)
    print(
        f"[CLAUDE] Calling {MODEL} "
        f"(brain: {len(brain)} chars, user: {len(user_text)} chars, "
        f"history: {len(history) if history else 0} turns, source: {source})"
    )

    # Build the messages list. When history is non-empty, prior exchanges
    # are interleaved as alternating user/assistant turns BEFORE the current
    # task-shaped user message. Empty / None history → single-turn (unchanged).
    messages: list[dict] = []
    if history:
        for turn in history:
            messages.append({"role": "user", "content": turn["msg_text"]})
            messages.append({"role": "assistant", "content": turn["reply_text"]})
    messages.append({"role": "user", "content": user_text})

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
        messages=messages,
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
    total_orders = 0
    total_instagram = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        total_logged = conn.execute("SELECT COUNT(*) FROM message_logs").fetchone()[0]
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        total_instagram = conn.execute("SELECT COUNT(*) FROM instagram_logs").fetchone()[0]
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
        "total_orders": total_orders,
        "total_instagram": total_instagram,
        "seen_ids_cached": len(_seen_ids),
        # Normalized protected numbers — diagnostic so misconfigured env vars
        # are obvious from the public health probe. Phone numbers, not secrets.
        "protected_numbers": [
            normalize_wa(BUSINESS_NUMBER),
            normalize_wa(OWNER_NUMBER),
        ],
    })


@app.route("/inventory-debug")
def inventory_debug():
    """Diagnostic endpoint for live Shopify inventory.

    Gated by the same DASHBOARD_KEY as /dashboard-data. Returns whatever
    get_live_inventory() currently has — empty string means the call
    failed silently (check Render logs for [INVENTORY] lines). Cache
    TTL is 5 minutes; refresh by waiting it out or restarting the worker.

    Response shape:
        {
            "inventory": "<the formatted block, possibly empty>",
            "cached_age_seconds": <float, 0 on first call after restart>,
            "shopify_products_url": "<string>"
        }
    """
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"error": "unauthorized"}), 401
    block = get_live_inventory()
    return jsonify({
        "inventory": block,
        "cached_age_seconds": (
            round(time.time() - _inventory_cache["fetched_at"], 1)
            if _inventory_cache["fetched_at"]
            else None
        ),
        "shopify_products_url": SHOPIFY_PRODUCTS_URL,
    })


@app.route("/review-debug")
def review_debug():
    """Diagnostic endpoint showing currently scheduled review requests.

    Gated by the same DASHBOARD_KEY as /dashboard-data. Reads the
    in-memory _scheduled_reviews dict — empty after every worker
    restart even if reviews were scheduled before. Useful for confirming
    a 'delivered' shipping event actually scheduled the follow-up.

    Response shape:
        {
            "scheduled_reviews": [
                {
                    "order_id": "...",
                    "order_number": "#1042",
                    "customer_number": "919...",
                    "customer_name": "Priya",
                    "scheduled_at": <unix ts>,
                    "fires_in_hours": 239.5
                },
                ...
            ],
            "total": <int>,
            "review_delay_seconds": REVIEW_DELAY_SECONDS
        }
    """
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"error": "unauthorized"}), 401

    now = time.time()
    items = []
    # Iterate over a snapshot copy — _scheduled_reviews can be mutated
    # concurrently by a daemon timer thread (_send_review_request pops
    # entries on fire). Iterating the live dict would raise
    # RuntimeError: dictionary changed size during iteration. The
    # try/except is a belt-and-suspenders fallback in case the copy()
    # itself races.
    try:
        reviews_copy = dict(_scheduled_reviews)
        for order_id, entry in reviews_copy.items():
            scheduled_at = entry.get("scheduled_at") or 0
            fires_at = scheduled_at + REVIEW_DELAY_SECONDS
            remaining_seconds = max(0.0, fires_at - now)
            items.append({
                "order_id": order_id,
                "order_number": entry.get("order_number") or "",
                "customer_number": entry.get("customer_number") or "",
                "customer_name": entry.get("customer_name") or "",
                "scheduled_at": scheduled_at,
                "fires_in_hours": round(remaining_seconds / 3600, 2),
            })
        items.sort(key=lambda x: x["fires_in_hours"])
    except Exception as e:
        print(f"[REVIEW-DEBUG] Error reading _scheduled_reviews: {type(e).__name__}: {e}")
        return jsonify({"error": "internal", "detail": f"{type(e).__name__}: {e}"}), 500

    return jsonify({
        "scheduled_reviews": items,
        "total": len(items),
        "review_delay_seconds": REVIEW_DELAY_SECONDS,
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

        # Allow text + image past the front gate. Everything else (audio,
        # video, documents, stickers, status updates) is silently dropped.
        # Image events get downloaded + sent to Claude Vision further down
        # AFTER the safety nets (dedup, pause, HUMAN_UDIT) — we don't want
        # to burn a Vision API call on a duplicate webhook delivery or
        # while a human takeover is active.
        if message_type not in ("text", "image"):
            print(f"[WEBHOOK] Skipped: unsupported message type {message_type!r}")
            return jsonify({"status": "ok"}), 200

        # text_body emptiness only matters for text events — image events
        # may legitimately have no caption.
        if message_type == "text" and not text_body:
            print("[WEBHOOK] Skipped: empty text body")
            return jsonify({"status": "ok"}), 200

        if not wa_id:
            print("[WEBHOOK] Skipped: missing waId")
            return jsonify({"status": "ok"}), 200

        # OUTBOUND BRANCH — when WATI delivers an outbound event to this
        # same endpoint (some plans do; others use a separate URL — see
        # /wati-outbound below), divert to the dedicated handler. This
        # path is responsible for distinguishing the bot's own send from
        # Udit's manual reply and tagging accordingly (HUMAN_UDIT or
        # PAUSE_DIRECTIVE). The existing inbound dedup + pause-directive
        # scan keeps applying to inbound events.
        if _is_outbound_event(data):
            print(f"[WEBHOOK] Outbound event detected (wa_id={wa_id!r})")
            _process_wati_outbound(data)
            return jsonify({"status": "ok"}), 200

        # Inbound from this point on. The existing #pause / #resume
        # directive scan still applies in case a customer types one
        # (rare, and the false-positive cost is just self-pausing
        # themselves for 4h — see _handle_pause_directive doc).
        directive = _handle_pause_directive(wa_id, text_body)
        if directive is not None:
            if msg_id:
                _seen_ids.add(msg_id)
                _persist_seen_id(msg_id)
            _log_message(
                wa_id, sender_name, text_body, status=f"PAUSE_CMD_{directive.upper()}"
            )
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

        # Human-takeover gate. If Udit previously sent "#pause" for this
        # number (within the last 4h), short-circuit before any Claude
        # call — log only, no reply. Mirrors brain.md Section 7.
        if _is_paused(wa_id):
            print(f"[PAUSED] Skipping reply — human takeover active for {wa_id}")
            _log_message(wa_id, sender_name, text_body, status="PAUSED")
            return jsonify({"status": "ok"}), 200

        # SAFETY NET — if Udit replied manually in the last 4 hours
        # (HUMAN_UDIT row in DB), suppress the auto-reply. Catches the
        # case where he forgot to type #pause but did respond from WATI.
        # This is the fallback for brain.md Section 7 + Guardrail 41.
        if _udit_replied_recently(wa_id):
            print(f"[HUMAN_HANDLING] Udit replied recently — skipping auto-reply for {wa_id}")
            _log_message(wa_id, sender_name, text_body or "[image]", status="HUMAN_HANDLING")
            return jsonify({"status": "human_handling"}), 200

        # ----- VISION BRANCH -----
        # For image events: download + extract via Claude Vision. Three outcomes:
        #   (a) high confidence + order_id found → synthesize a text query
        #       (e.g. "My order ID is #1042") and fall through to the
        #       normal text Claude pipeline below
        #   (b) high confidence but no order_id → synthesize a context-rich
        #       message ("I sent a screenshot — product: GS1, amount ₹849…")
        #       and fall through to the normal pipeline
        #   (c) low confidence / failure / no URL → send the deterministic
        #       FALLBACK_VISION_REPLY directly via WATI and return
        if message_type == "image":
            # Diagnostic on every image event — lets the founder grep Render
            # logs to see what WATI's payload actually contains. Useful while
            # the URL extraction is still calibrated against unknown plan
            # variations.
            print(f"[VISION] Image event payload keys: {sorted(data.keys())[:30]}")

            image_url = _extract_wati_image_url(data)
            extracted: dict | None = None
            if image_url:
                print(f"[VISION] Image URL resolved: {image_url[:120]}")
                try:
                    extracted = _extract_image_info(image_url)
                except Exception as e:
                    print(f"[VISION] Unexpected error during extraction: {type(e).__name__}: {e}")
                    extracted = None
            else:
                print("[VISION] No image URL found in payload — will fall back")

            confidence = (extracted or {}).get("confidence", "").lower() if extracted else ""
            order_id = (extracted or {}).get("order_id") if extracted else None
            image_type = ((extracted or {}).get("image_type") or "").lower() if extracted else ""
            eye_shape = (extracted or {}).get("eye_shape") if extracted else None

            if extracted and confidence == "high" and image_type == "eye_photo":
                # Path (a-eye): customer sent a close-up of their eye for a
                # lash recommendation. Synthesize a query that triggers the
                # brain's Section 4 eye-shape rules. If vision couldn't tell
                # the exact shape, fall through to a generic "look at my eye
                # and recommend" — brain will ask one short follow-up.
                if eye_shape:
                    text_body = (
                        f"I just sent a close-up photo of my eye — my eye shape looks "
                        f"{eye_shape}. Can you recommend a lash for me?"
                    )
                    print(f"[VISION] Eye photo confidence=high shape={eye_shape!r} — synthesized eye-shape recommendation query")
                else:
                    text_body = (
                        "I just sent a close-up photo of my eye — my eye shape was unclear. "
                        "Can you recommend a lash for me?"
                    )
                    print(f"[VISION] Eye photo confidence=high but shape unclear — synthesized generic recommendation query")
            elif extracted and confidence == "high" and order_id:
                # Path (a-order): synthesize text and fall through.
                synth_parts = [f"My order ID is #{order_id}"]
                amt = extracted.get("amount")
                if amt:
                    synth_parts.append(f"(₹{amt})")
                name = extracted.get("customer_name")
                if name:
                    synth_parts.append(f"— name: {name}")
                text_body = " ".join(synth_parts)
                print(f"[VISION] Extracted order_id={order_id} confidence=high — synthesized text: {text_body!r}")
            elif extracted and confidence == "high":
                # Path (b): high confidence, no order_id, not an eye photo —
                # likely an order screenshot without a visible ID, or a
                # product photo. Synthesize whatever context we have.
                parts = []
                if extracted.get("customer_name"):
                    parts.append(f"name: {extracted['customer_name']}")
                if extracted.get("product"):
                    parts.append(f"product: {extracted['product']}")
                if extracted.get("amount"):
                    parts.append(f"amount: ₹{extracted['amount']}")
                if extracted.get("payment_status"):
                    parts.append(f"payment: {extracted['payment_status']}")
                detail = "; ".join(parts) if parts else "no specific details visible"
                text_body = f"I just sent a screenshot of my order — {detail}. Can you help me with this?"
                print(f"[VISION] Extracted info confidence=high but no order_id (image_type={image_type!r}) — synthesized context")
            else:
                # Path (c): low confidence, unrecognized image type, or no URL.
                print(f"[VISION] Low confidence / unrecognized image (confidence={confidence!r} type={image_type!r}) — falling back to neutral reply")
                send_whatsapp_reply(wa_id, FALLBACK_VISION_REPLY)
                elapsed_ms = int((time.time() - t_start) * 1000)
                _log_message(
                    wa_id, sender_name, "[image]",
                    status="AUTO", reply_text=FALLBACK_VISION_REPLY,
                    latency_ms=elapsed_ms,
                )
                print("=" * 60 + "\n")
                return jsonify({"status": "ok"}), 200

        print(f"[WEBHOOK] Processing text from {sender_name or wa_id}: {text_body[:200]}")

        if not BRAIN_FILE.exists():
            print(f"[WEBHOOK] ERROR: brain file missing at {BRAIN_FILE}")
            return jsonify({"status": "ok"}), 200

        # Pull recent context for this customer so Claude sees the
        # ongoing conversation, not just the latest message in isolation.
        # Best-effort — failures inside _load_conversation_history return []
        # and we fall through to a single-turn call.
        history = _load_conversation_history(wa_id)

        # If the same customer has a Shopify order in the last 30 days,
        # surface it to Claude as order_context. Empty string when no
        # match → falls through to "(none provided)" placeholder, same
        # behaviour as before.
        order_line = _lookup_recent_order(wa_id)

        # Run the twin.
        classification, reply, _raw = draft_reply_logic(text_body, order_line, history=history)

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
            # New buttoned approval flow: Telegram message with
            # ✅ Send as-is / ✏️ Edit / ⛔ Skip inline buttons. State is
            # registered in _pending_drafts; the actual customer reply
            # ships from the /telegram-callback handler when Udit taps
            # Send or completes an Edit. Falls back to the legacy plain-
            # text notification if the buttoned send fails (missing
            # Telegram config, network error, etc.) so Udit always gets
            # *some* heads-up about the pending draft.
            sent_with_buttons = send_draft_for_approval(
                customer_number=wa_id,
                customer_name=sender_name,
                customer_message=text_body,
                reply_text=reply,
            )
            if not sent_with_buttons:
                send_telegram_notification(
                    classification, text_body, reply, sender_info=sender_info
                )
            print(f"[DRAFT] Notified founder for {wa_id} (buttons={sent_with_buttons})")
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log_message(
                wa_id, sender_name, text_body,
                status="DRAFT", reply_text=reply, latency_ms=elapsed_ms,
            )
        elif classification == "ESCALATE":
            send_telegram_notification(
                classification, text_body, reply,
                sender_info=sender_info, customer_id=wa_id,
            )
            print(f"[ESCALATE] Notified founder for {wa_id}")
            # Auto-pause this number for 4h so subsequent messages from
            # the same customer don't re-trigger Claude + Telegram. The
            # founder is now handling the conversation; the twin should
            # stay out of the way. Manual #pause/#resume still work as
            # before (this uses the same paused_numbers register), and
            # the HUMAN_UDIT safety net is a separate, additive check
            # that also short-circuits inbound when Udit replies via WATI.
            _pause_number(wa_id)
            print(f"[ESCALATE] Auto-paused {wa_id} for 4h after holding reply sent")
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


@app.route("/wati-outbound", methods=["POST"])
def wati_outbound():
    """Dedicated outbound-message webhook for WATI plans that allow
    configuring inbound and outbound URLs separately.

    Treats EVERY event arriving here as outbound, regardless of payload
    shape. Use this URL in WATI Dashboard → Webhooks → Outgoing Message
    Webhook URL if your WATI plan exposes that setting. If your plan
    uses a single webhook URL for both directions, leave WATI pointed
    at /webhook (which auto-detects outbound via the same logic) and
    ignore this endpoint.

    Always returns 200 so WATI doesn't retry on internal errors.
    """
    print("\n" + "=" * 60)
    try:
        data = request.get_json(silent=True) or {}
        # Diagnostic — log the keys of the first few events so we can
        # see what WATI actually sends if detection misbehaves.
        print(f"[OUTBOUND] payload keys: {sorted(data.keys())[:20]}")
        _process_wati_outbound(data)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[OUTBOUND] EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"status": "ok"}), 200


@app.route("/shopify-webhook", methods=["POST"])
def shopify_webhook():
    """Receive Shopify order webhooks, verify HMAC, log to the orders table.

    Shopify expects 200 on success. We return:
      - 401 with [WEBHOOK] Invalid signature when HMAC verification fails
      - 200 in every other case (parse errors, DB hiccups), so Shopify
        doesn't retry forever and create duplicate rows
    """
    # Use raw bytes — JSON re-serialization would break HMAC verification.
    raw_body = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not _verify_shopify_hmac(raw_body, hmac_header):
        print("[WEBHOOK] Invalid signature")
        return jsonify({"error": "invalid signature"}), 401

    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        print(f"[SHOPIFY] Invalid JSON: {e}")
        return jsonify({"status": "ok"}), 200

    try:
        order_id = str(data.get("id") or "")
        customer = data.get("customer") or {}
        shipping = data.get("shipping_address") or {}

        # Phone: prefer shipping_address.phone, fall back to customer.phone.
        raw_phone = shipping.get("phone") or customer.get("phone") or ""
        customer_phone = _phone_to_10digit(raw_phone)

        customer_name = customer.get("first_name") or ""

        line_items = data.get("line_items") or []
        product_names = ", ".join(
            (item.get("title") or "") for item in line_items if item
        )

        total_price = str(data.get("total_price") or "")
        order_status = data.get("financial_status") or ""
        created_at = data.get("created_at") or ""

        _log_shopify_order(
            order_id=order_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            product_names=product_names,
            total_price=total_price,
            order_status=order_status,
            created_at=created_at,
        )

        print(
            f"[SHOPIFY] Logged order #{order_id} "
            f"phone={customer_phone or '(none)'} name={customer_name or '(none)'} "
            f"status={order_status}"
        )
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[SHOPIFY] EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"status": "ok"}), 200


def _send_review_request(order_id: str) -> None:
    """Timer callback — fires REVIEW_DELAY_SECONDS after a 'delivered'
    shipping event. Sends the review-request WhatsApp template via WATI
    and clears the entry from _scheduled_reviews.

    Wrapped in try/except so a transient failure (WATI down, customer
    number gone bad, anything) never crashes the daemon thread. No retry
    on failure — review nudges are nice-to-have, not critical.
    """
    try:
        entry = _scheduled_reviews.get(order_id)
        if not entry:
            print(f"[REVIEW] Timer fired for order_id={order_id} but no entry — likely already sent or cancelled")
            return

        customer_number = entry.get("customer_number") or ""
        customer_name = entry.get("customer_name") or ""
        order_number = entry.get("order_number") or order_id

        if not customer_number:
            # Shouldn't happen because _schedule_review_request gates on
            # this, but defensive — if state was corrupted somehow, skip
            # rather than send to nobody.
            print(f"[REVIEW] No phone for order {order_number} at fire time — skipping")
            _scheduled_reviews.pop(order_id, None)
            return

        # First-name fallback so the greeting reads naturally if Shopify
        # didn't include the recipient's first name on the fulfillment.
        first_name = customer_name or "there"

        print(
            f"[REVIEW] Sending review request for order {order_number} "
            f"to {customer_number} (name={customer_name or '(none)'})"
        )
        message = REVIEW_REQUEST_TEMPLATE.format(first_name=first_name)
        send_whatsapp_reply(customer_number, message)
        _scheduled_reviews.pop(order_id, None)
        print(f"[REVIEW] Sent review request for order {order_number} to {customer_number}")
    except Exception as e:
        print(f"[REVIEW] Send error for order_id={order_id}: {type(e).__name__}: {e}")
        traceback.print_exc()
        _scheduled_reviews.pop(order_id, None)


def _schedule_review_request(
    order_id: str,
    order_number: str,
    customer_number: str,
    customer_name: str,
) -> None:
    """Schedule a single review-request WhatsApp message for REVIEW_DELAY_SECONDS
    from now. Idempotent: if a review is already scheduled for this
    order_id, do nothing (dedup gate).

    Called from _process_shipping_event after a 'delivered' message ships
    successfully. Failure modes:
      - missing customer_number → log + skip (no recipient to send to)
      - missing order_id → log + skip (can't dedup without a key)
      - already-scheduled → log + skip (dedup)
    """
    if not order_id:
        print("[REVIEW] No order_id — skipping review schedule")
        return
    if not customer_number:
        print(f"[REVIEW] No phone for order {order_number or order_id} — skipping review schedule")
        return
    if order_id in _scheduled_reviews:
        print(f"[REVIEW] Already scheduled for order {order_number or order_id} — dedup skip")
        return

    now = time.time()
    _scheduled_reviews[order_id] = {
        "customer_number": customer_number,
        "customer_name": customer_name,
        "order_number": order_number,
        "scheduled_at": now,
        "delivered_at": now,
    }
    timer = threading.Timer(REVIEW_DELAY_SECONDS, _send_review_request, args=(order_id,))
    timer.daemon = True
    timer.start()

    hours = REVIEW_DELAY_SECONDS / 3600
    print(
        f"[REVIEW] Scheduled review request for order {order_number or order_id} "
        f"({customer_number}) — fires in {hours:g}h"
    )
    print("[REVIEW] Note: timer is in-memory — will reset on Render restart")


def _extract_tracking_number(fulfillment: dict) -> str:
    """Pull the tracking number from a Shopify fulfillment payload.

    Shopify exposes tracking under several keys depending on plan version
    and which fulfillment service populated it:
      - fulfillment.tracking_number             (single, most common)
      - fulfillment.tracking_numbers[0]         (array form)
      - fulfillment.tracking_info.number        (newer nested object)

    Returns the first non-empty string found, stripped, or "" if none.
    """
    direct = (fulfillment.get("tracking_number") or "").strip() if isinstance(fulfillment.get("tracking_number"), str) else ""
    if direct:
        return direct

    arr = fulfillment.get("tracking_numbers")
    if isinstance(arr, list) and arr:
        first = arr[0]
        if isinstance(first, str) and first.strip():
            return first.strip()

    info = fulfillment.get("tracking_info")
    if isinstance(info, dict):
        nested = info.get("number")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    return ""


def _process_shipping_event(topic: str, fulfillment: dict) -> None:
    """Decide whether a Shopify fulfillment webhook should trigger a
    customer-facing WhatsApp update, build the message per the configured
    templates, and dispatch via send_whatsapp_reply.

    `topic` is the lower-cased X-Shopify-Topic header value:
      - "fulfillments/create" → "Order shipped" template
      - "fulfillments/update" → check shipment_status:
            "out_for_delivery" → out-for-delivery template
            "delivered"        → delivered template
            anything else      → no message, silent acknowledge

    Dedup: (order_id, event_type) pairs are persisted to the
    shipping_notifications SQLite table via _mark_shipping_sent /
    _was_shipping_sent, so retries / re-pushes / Render restarts can't
    re-trigger the same customer message.

    All errors bubble up to the route handler (which wraps in try/except
    and always returns 200). Errors prefixed with [SHIPPING ERROR] in logs.
    """
    order_id = str(fulfillment.get("order_id") or fulfillment.get("id") or "")

    # Shopify's `name` on a fulfillment is like "#1042.1" or "#1042-1"
    # (order number + fulfillment sequence). Strip the suffix so the
    # customer sees "#1042". Also strip a leading "#" because the
    # message templates supply their own.
    order_name_raw = (fulfillment.get("name") or "").strip()
    order_number = order_name_raw
    for sep in (".", "-"):
        if sep in order_number:
            order_number = order_number.split(sep, 1)[0]
            break
    order_number = order_number.lstrip("#")
    if not order_number:
        order_number = order_id  # fallback if name was missing

    shipment_status = (fulfillment.get("shipment_status") or "").strip().lower()

    # Decide which status template (if any) applies.
    #
    # Shopify fires fulfillments/create when an order is fulfilled in
    # Shopify Admin (or when Shiprocket pushes the initial fulfillment),
    # but the shipment_status on that create event is often empty — the
    # carrier hasn't reported a status yet. Shiprocket then fires
    # fulfillments/update as the parcel moves through pickup → transit →
    # out for delivery → delivered. The status mapping below covers both
    # the create flow (no shipment_status) and the Shiprocket update
    # flow.
    #
    # Dedup keys are (order_id, event), so "shipped" can only fire once
    # per order regardless of whether create or in_transit triggered it.
    #
    # pickup_scheduled / pickup_failed are known pre-shipment statuses —
    # we don't send the customer a message for them, but we do NOT
    # early-return here because the same webhook might also carry a
    # tracking number that needs its own follow-up handling below.
    event: str | None = None
    pre_shipment_silent = False
    if topic == "fulfillments/create":
        event = "shipped"
    elif topic == "fulfillments/update":
        if shipment_status == "in_transit":
            # First Shiprocket status after pickup — treat as "we shipped".
            event = "shipped"
        elif shipment_status == "out_for_delivery":
            event = "out_for_delivery"
        elif shipment_status == "delivered":
            event = "delivered"
        elif shipment_status in ("pickup_scheduled", "pickup_failed"):
            pre_shipment_silent = True

    # Extract customer info upfront. The fulfillment payload's `destination`
    # block is a copy of the shipping address — most reliable source of
    # name + phone for the recipient. We resolve it now because BOTH the
    # tracking follow-up below and the status message below need it.
    destination = fulfillment.get("destination") or {}
    first_name = (destination.get("first_name") or "").strip()
    phone_raw = (destination.get("phone") or "").strip()
    wa_id = _phone_to_wa_id(phone_raw)
    greeting_name = first_name or "there"

    # Tracking number can appear under several field paths — extract once.
    tracking_number = _extract_tracking_number(fulfillment)

    # ----- TRACKING FOLLOW-UP -----
    # Only on fulfillments/update. The fulfillments/create flow embeds
    # tracking inline in the "shipped" message (and marks the tracking
    # dedup key after sending, see below), so a subsequent update with
    # the same tracking won't double-message the customer.
    #
    # This block runs INDEPENDENTLY of the status message — both can
    # fire on the same webhook if it carries a status change AND a
    # tracking number. Dedup key (order_id, "tracking") guarantees the
    # follow-up only goes out once per order.
    if topic == "fulfillments/update":
        if not tracking_number:
            print(f"[SHIPPING] No tracking number in payload for order #{order_number}")
        elif _was_shipping_sent(order_id, "tracking"):
            print(f"[SHIPPING] Already sent tracking for order #{order_number} — dedup skip")
        elif not wa_id:
            print(f"[SHIPPING] No phone for tracking follow-up on order #{order_number}, skipping")
        else:
            tracking_message = (
                f"Hi {greeting_name}! Here's your tracking link for order #{order_number} 🤍\n\n"
                f"Track your order: https://shiprocket.in/tracking/{tracking_number}\n\n"
                f"Feel free to reach out if you need anything!"
            )
            send_whatsapp_reply(wa_id, tracking_message)
            _mark_shipping_sent(order_id, "tracking", phone=wa_id, order_number=order_number)
            print(f"[SHIPPING] Sent tracking link for order #{order_number} to {wa_id}")

    # ----- STATUS MESSAGE -----
    if pre_shipment_silent:
        print(
            f"[SHIPPING] Silent ack for shipment_status={shipment_status!r} "
            f"(order #{order_number}) — known pre-shipment status, no customer message"
        )
        return

    if event is None:
        print(
            f"[SHIPPING] No template for topic={topic!r} "
            f"shipment_status={shipment_status!r} (order #{order_number}) — "
            f"silent acknowledge"
        )
        return

    # Dedup BEFORE building/sending the status message.
    if _was_shipping_sent(order_id, event):
        print(
            f"[SHIPPING] Already sent {event!r} for order #{order_number} "
            f"(order_id={order_id}) — dedup skip"
        )
        return

    if not wa_id:
        print(f"[SHIPPING] No phone number for order #{order_number}, skipping")
        return

    # Build the message body (and optionally short-circuit to template send).
    #
    # SHIPPED uses WATI template (sendTemplateMessage) when a tracking
    # number is available, because session messages fail with "Ticket has
    # been expired" outside the 24h window and the shipped notification
    # often fires when the customer hasn't messaged us at all yet.
    # Template approval reference: shipping_notification_template with
    # variables {{1}}=name, {{2}}=order_number, {{3}}=tracking_number,
    # {{4}}=carrier, plus a dynamic URL button that appends the tracking
    # number to https://shiprocket.in/tracking/.
    #
    # OUT_FOR_DELIVERY and DELIVERED continue using session messages —
    # by the time they fire, the customer has typically been in an
    # active session (asking about their order, etc.) so the 24h window
    # is less of a problem.
    message = None
    template_sent = False
    tracking_company = (fulfillment.get("tracking_company") or "").strip()

    if event == "shipped":
        if tracking_number:
            print(
                f"[SHIPPING] Sending template message for order #{order_number} to {wa_id}"
            )
            template_sent = send_whatsapp_template(
                wa_id=wa_id,
                template_name="shipping_notification_template",
                parameters=[
                    {"name": "name", "value": greeting_name},
                    {"name": "order_number", "value": f"#{order_number}"},
                    {"name": "tracking_number", "value": tracking_number},
                    {"name": "carrier", "value": tracking_company or "Shiprocket"},
                    # tracking_url variable is just the tracking number;
                    # the template's button has the
                    # https://shiprocket.in/tracking/ prefix baked in.
                    {"name": "tracking_url", "value": tracking_number},
                ],
            )
            if template_sent:
                print(f"[SHIPPING] Template message sent for order #{order_number}")
            else:
                print(f"[SHIPPING] Template failed, falling back to session message")
        else:
            print(
                f"[SHIPPING] No tracking number for order #{order_number} — "
                f"template requires tracking, using session message"
            )

        # Session-message body (fallback OR no-tracking path).
        if not template_sent:
            estimated_delivery = (fulfillment.get("estimated_delivery_at") or "").strip()
            lines = [
                f"Hi {greeting_name}! Your The Glam Shelf order #{order_number} "
                f"has been shipped 🤍",
                "",
            ]
            if tracking_number:
                lines.append(f"Tracking: https://shiprocket.in/tracking/{tracking_number}")
            if tracking_company:
                lines.append(f"Carrier: {tracking_company}")
            if estimated_delivery:
                lines.append(f"Estimated delivery: {estimated_delivery}")
            lines.append("")
            lines.append("Feel free to reach out if you need anything!")
            message = "\n".join(lines)
    elif event == "out_for_delivery":
        message = (
            f"Hi {greeting_name}! Your The Glam Shelf order #{order_number} "
            f"is out for delivery today 🤍\n\n"
            f"Keep an eye out — it'll be at your door soon!"
        )
    elif event == "delivered":
        message = (
            f"Hi {greeting_name}! Your order #{order_number} has been "
            f"delivered 🤍\n\n"
            f"Hope you love your lashes! If you have any questions about "
            f"how to use them, just message us here."
        )
    else:
        return  # Unreachable, but defensive.

    # Send the session message UNLESS the template path already sent it.
    # send_whatsapp_reply handles WATI failures internally and never raises;
    # it also pre-registers the outbound text in _bot_recent_replies so the
    # subsequent WATI outbound webhook echo doesn't get mis-tagged as
    # HUMAN_UDIT.
    if not template_sent:
        if message is None:
            return  # Defensive — should never happen given the branches above.
        send_whatsapp_reply(wa_id, message)

    _mark_shipping_sent(order_id, event, phone=wa_id, order_number=order_number)

    # If the shipped message embedded the tracking line, also mark the
    # tracking dedup so a later fulfillments/update with the same
    # tracking number doesn't fire a redundant "Here's your tracking
    # link" follow-up. Only relevant for the "shipped" event — the
    # out_for_delivery / delivered templates don't embed tracking.
    if event == "shipped" and tracking_number:
        _mark_shipping_sent(order_id, "tracking", phone=wa_id, order_number=order_number)

    print(
        f"[SHIPPING] Sent {event!r} update for order #{order_number} "
        f"to {wa_id} (name={first_name or '(none)'})"
    )

    # Post-delivery hook: when an order is marked delivered, schedule a
    # review-request WhatsApp for REVIEW_DELAY_SECONDS later. The
    # scheduler dedupes by order_id so retried "delivered" webhooks only
    # schedule once. Failure here never blocks the shipping confirmation
    # already sent above — _schedule_review_request swallows everything.
    if event == "delivered":
        try:
            _schedule_review_request(
                order_id=order_id,
                order_number=f"#{order_number}" if order_number else "",
                customer_number=wa_id,
                customer_name=first_name,
            )
        except Exception as e:
            print(f"[REVIEW] Schedule call failed for order #{order_number}: {type(e).__name__}: {e}")


@app.route("/shopify-fulfillment", methods=["POST"])
def shopify_fulfillment():
    """Receive Shopify fulfillments/create and fulfillments/update webhooks,
    verify HMAC, and dispatch a customer-facing WhatsApp shipping update
    via WATI per _process_shipping_event.

    Three triggers handled:
      - fulfillments/create → "Order shipped" message with tracking link
      - fulfillments/update + shipment_status=out_for_delivery → OFD message
      - fulfillments/update + shipment_status=delivered → delivered message
    All other update statuses (in_transit, attempted_delivery, etc.) are
    silently acknowledged (200) with no customer message.

    Auth: same SHOPIFY_WEBHOOK_SECRET that gates /shopify-webhook. Each
    Shopify webhook subscription must be registered with the matching
    secret for HMAC verification to pass.

    Returns:
      - 401 on HMAC mismatch (Shopify will retry; secret must be wrong)
      - 200 in every other case (parse errors, internal exceptions, no
        template match) — Shopify treats 200 as delivered and won't retry,
        which is what we want for invalid/uninteresting events
    """
    raw_body = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    topic = (request.headers.get("X-Shopify-Topic") or "").strip().lower()

    if not _verify_shopify_hmac(raw_body, hmac_header):
        print("[SHIPPING] Invalid HMAC signature")
        return jsonify({"error": "invalid signature"}), 401

    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        print(f"[SHIPPING] Invalid JSON: {e}")
        return jsonify({"status": "ok"}), 200

    try:
        _process_shipping_event(topic, data)
    except Exception as e:
        # [SHIPPING ERROR] prefix lets the founder grep for failures.
        print(f"[SHIPPING ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200


def _process_order_update(payload: dict) -> None:
    """Decide whether an orders/updated event should fire a recovery
    "shipped" notification. Called by shopify_order_update after HMAC
    verification.

    Why this exists: when an order is archived in Shopify Admin before
    the fulfillments/* webhooks fire, the shipping notification never
    gets sent. When the order is later unarchived, Shopify re-fires
    orders/updated but NOT fulfillments/*, so the regular shipping flow
    misses it. This handler catches that "fulfilled but never notified"
    case by reading the order's current fulfillment_status and dedup
    table.

    Conditions for sending (all must be true):
      - fulfillment_status == "fulfilled"
      - financial_status == "paid"
      - _was_shipping_sent(order_id, "shipped") is False
      - phone is resolvable
    """
    order_id = str(payload.get("id") or "")
    order_number_raw = (payload.get("name") or "").strip()
    order_number = order_number_raw.lstrip("#") or order_id

    fulfillment_status = (payload.get("fulfillment_status") or "").strip().lower()
    financial_status = (payload.get("financial_status") or "").strip().lower()

    if fulfillment_status != "fulfilled":
        print(
            f"[ORDER-UPDATE] Order #{order_number} not fulfilled "
            f"(status={fulfillment_status or 'unfulfilled'!r}) — skipping"
        )
        return

    if financial_status != "paid":
        print(
            f"[ORDER-UPDATE] Order #{order_number} not paid "
            f"(status={financial_status or 'pending'!r}) — skipping"
        )
        return

    if _was_shipping_sent(order_id, "shipped"):
        print(f"[ORDER-UPDATE] Order #{order_number} already notified — skipping")
        return

    shipping = payload.get("shipping_address") or {}
    customer = payload.get("customer") or {}
    raw_phone = (shipping.get("phone") or customer.get("phone") or "").strip()
    wa_id = _phone_to_wa_id(raw_phone)
    if not wa_id:
        print(f"[ORDER-UPDATE] Order #{order_number} has no phone — skipping")
        return

    first_name = (customer.get("first_name") or "").strip()
    greeting_name = first_name or "there"

    # Tracking info from the first fulfillment if any are attached to
    # the order. Same multi-path extraction as _process_shipping_event.
    fulfillments = payload.get("fulfillments") or []
    fulfillment = fulfillments[0] if fulfillments and isinstance(fulfillments[0], dict) else {}
    tracking_number = _extract_tracking_number(fulfillment)
    tracking_company = (fulfillment.get("tracking_company") or "").strip()

    print(
        f"[ORDER-UPDATE] Order #{order_number} fulfilled + not yet notified — "
        f"sending shipping message"
    )

    # Mirror _process_shipping_event's send strategy: template first
    # (works outside 24h WATI session window), fall back to session
    # message if template fails OR no tracking number available.
    template_sent = False
    if tracking_number:
        template_sent = send_whatsapp_template(
            wa_id=wa_id,
            template_name="shipping_notification_template",
            parameters=[
                {"name": "name", "value": greeting_name},
                {"name": "order_number", "value": f"#{order_number}"},
                {"name": "tracking_number", "value": tracking_number},
                {"name": "carrier", "value": tracking_company or "Shiprocket"},
                {"name": "tracking_url", "value": tracking_number},
            ],
        )
        if template_sent:
            print(f"[ORDER-UPDATE] Template message sent for order #{order_number}")
        else:
            print(f"[ORDER-UPDATE] Template failed, falling back to session message")

    if not template_sent:
        lines = [
            f"Hi {greeting_name}! Your The Glam Shelf order #{order_number} "
            f"has been shipped 🤍",
            "",
        ]
        if tracking_number:
            lines.append(f"Tracking: https://shiprocket.in/tracking/{tracking_number}")
        if tracking_company:
            lines.append(f"Carrier: {tracking_company}")
        lines.append("")
        lines.append("Feel free to reach out if you need anything!")
        send_whatsapp_reply(wa_id, "\n".join(lines))

    _mark_shipping_sent(order_id, "shipped", phone=wa_id, order_number=order_number)
    # Same convention as _process_shipping_event: if we embedded tracking
    # in this shipped message, mark tracking dedup so a later
    # fulfillments/update doesn't fire a redundant follow-up.
    if tracking_number:
        _mark_shipping_sent(order_id, "tracking", phone=wa_id, order_number=order_number)


@app.route("/shopify-order-update", methods=["POST"])
def shopify_order_update():
    """Receive Shopify orders/updated webhook. Catches the
    "fulfilled but never notified" case — most commonly when an order
    is archived in Shopify Admin before the fulfillments/* webhook
    fires, then unarchived later (Shopify re-fires orders/updated on
    unarchive but NOT fulfillments/*).

    Auth: same SHOPIFY_WEBHOOK_SECRET that gates /shopify-webhook and
    /shopify-fulfillment.

    Returns:
      - 401 on HMAC mismatch (Shopify will retry; secret must be wrong)
      - 200 in every other case (parse errors, internal exceptions, no
        action needed) — Shopify treats 200 as delivered and won't retry
    """
    raw_body = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")

    if not _verify_shopify_hmac(raw_body, hmac_header):
        print("[ORDER-UPDATE] Invalid HMAC signature")
        return jsonify({"error": "invalid signature"}), 401

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        print(f"[ORDER-UPDATE] Invalid JSON: {e}")
        return jsonify({"status": "ok"}), 200

    try:
        _process_order_update(payload)
    except Exception as e:
        print(f"[ORDER-UPDATE] Handler error: {type(e).__name__}: {e}")
        traceback.print_exc()

    return jsonify({"status": "ok"}), 200


@app.route("/telegram-callback", methods=["POST"])
def telegram_callback():
    """Telegram webhook endpoint — handles both inline-button taps
    (callback_query updates, used by the DRAFT approval flow) and regular
    text messages (used by the Edit completion sub-flow).

    Register with Telegram via:
      POST https://api.telegram.org/bot<TOKEN>/setWebhook
        ?url=https://glamshelf-twin.onrender.com/telegram-callback
        &allowed_updates=["callback_query","message"]

    Auth: only events whose chat.id matches TELEGRAM_CHAT_ID are honored
    (see _is_authorized_telegram_chat). Unauthorized events get silently
    dropped (callback queries are answered with "Not authorized" so the
    button doesn't spin).

    Always returns 200 so Telegram doesn't retry on internal hiccups.
    """
    try:
        update = request.get_json(silent=True) or {}

        # Inline-button tap.
        cb = update.get("callback_query")
        if cb:
            _handle_telegram_callback(cb)
            return jsonify({"status": "ok"}), 200

        # Regular text message — only meaningful if Udit is in the middle
        # of an Edit flow. Otherwise ignored.
        msg = update.get("message")
        if msg:
            _handle_telegram_message(msg)
            return jsonify({"status": "ok"}), 200

        # Other update types (edited_message, channel_post, etc.) — ignore.
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[TELEGRAM DRAFT] Webhook handler error: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"status": "ok"}), 200


@app.route("/instagram-webhook", methods=["GET"])
def instagram_webhook_verify():
    """Meta's webhook verification handshake.

    On webhook setup, Meta sends a GET with hub.mode=subscribe,
    hub.verify_token=<your token>, hub.challenge=<random string>.
    We must echo hub.challenge back as plain text 200 only when the
    token matches. Otherwise 403.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge") or ""

    if (
        mode == "subscribe"
        and INSTAGRAM_VERIFY_TOKEN
        and token == INSTAGRAM_VERIFY_TOKEN
    ):
        print("[INSTAGRAM] Webhook verification accepted")
        return challenge, 200
    print(f"[INSTAGRAM] Webhook verification refused (mode={mode!r})")
    return "forbidden", 403


@app.route("/instagram-webhook", methods=["POST"])
def instagram_webhook():
    """Receive Instagram DM webhook events from Meta.

    Always returns 200 — Meta retries on non-2xx, which would create
    duplicate replies. Echoes (messages we sent) and non-text events
    are silently ignored. Each text DM runs through the full twin
    pipeline (history → brain → Claude) and the reply ships back via
    the Graph API.
    """
    print("\n" + "=" * 60)
    try:
        data = request.get_json(silent=True) or {}
        for entry in data.get("entry", []) or []:
            for event in entry.get("messaging", []) or []:
                _process_instagram_event(event)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[INSTAGRAM] EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
        return jsonify({"status": "ok"}), 200


def _process_instagram_event(event: dict) -> None:
    """Handle a single messaging event. Failures are absorbed into log
    lines so one bad event can't take down the rest of the batch."""
    try:
        sender_id = ((event.get("sender") or {}).get("id") or "").strip()
        recipient_id = ((event.get("recipient") or {}).get("id") or "").strip()
        message = event.get("message") or {}

        # Echoes are messages WE sent via the Send API (Meta loops them back).
        # Skip silently — our outbound dispatcher already logged them.
        if message.get("is_echo"):
            return

        text = (message.get("text") or "").strip()

        # HUMAN_UDIT_INSTAGRAM detection — when Udit manually replies to
        # a customer from the Instagram app, Meta fires a webhook with
        # sender=PAGE_ID and recipient=customer. We need to:
        #   1. Log it as HUMAN_UDIT_INSTAGRAM so subsequent inbounds
        #      from that customer can short-circuit (restart-safe).
        #   2. Auto-pause the customer's sender_id (= recipient_id of
        #      this event) for 4h so the twin stops replying.
        # Order matters: this check goes BEFORE the empty-text/no-sender
        # gates because Udit's manual sends always have text and a
        # well-formed sender, but we don't want any other downstream
        # processing to fire on them.
        if INSTAGRAM_PAGE_ID and sender_id == INSTAGRAM_PAGE_ID:
            if not recipient_id:
                print("[HUMAN_UDIT_IG] sender is page but no recipient_id — skipping")
                return
            timestamp = str(event.get("timestamp") or "")
            _log_instagram(
                sender_id=recipient_id,   # store under the customer's id
                message_text="",
                reply_text=text,
                timestamp=timestamp,
                source="HUMAN_UDIT_INSTAGRAM",
            )
            _pause_number(recipient_id)
            print(f"[HUMAN_UDIT_IG] Udit replied on Instagram to {recipient_id} — auto-paused 4h")
            return

        if not text:
            # Non-text event (image, sticker, reaction, etc.) — silent skip.
            return

        if not sender_id:
            print("[INSTAGRAM] Skipped: missing sender.id")
            return

        msg_id = (message.get("mid") or "").strip()
        timestamp = str(event.get("timestamp") or "")

        print(f"[INSTAGRAM] DM from {sender_id}: {text[:200]}")

        # Reuse the same dedup set the WATI webhook uses — sender ID + mid
        # collisions across channels would be astronomically improbable.
        if msg_id and msg_id in _seen_ids:
            print(f"[INSTAGRAM] Skipped: duplicate mid {msg_id}")
            return
        if msg_id:
            _seen_ids.add(msg_id)
            _persist_seen_id(msg_id)

        # Pause gate — same paused_numbers register the WATI flow uses.
        # The dict is keyed by string, so IG sender_ids and wa_ids coexist
        # cleanly. When the WATI/IG ESCALATE branches auto-pause a number
        # after sending the holding reply, subsequent inbound messages
        # from that sender short-circuit here without invoking Claude or
        # paging the founder again.
        if _is_paused(sender_id):
            print(f"[PAUSED] Skipping reply — auto-pause active for IG sender {sender_id}")
            return

        # DB-backed safety net (survives Render restarts). If Udit
        # manually replied on Instagram in the last 4h — recorded as a
        # HUMAN_UDIT_INSTAGRAM row by the page-id detection above — skip
        # silently. Mirrors the WATI _udit_replied_recently safety net.
        if _udit_replied_recently_ig(sender_id):
            print(f"[HUMAN_HANDLING_IG] Udit replied to {sender_id} on Instagram recently — skipping")
            return

        # Best-effort order context. Sender IDs are 17-digit FB IDs and
        # won't match Indian phone numbers in the orders table — function
        # returns "" for the no-match case, which is fine.
        order_line = _lookup_recent_order(sender_id)

        # Multi-turn context, IG-side only.
        history = _load_instagram_history(sender_id)

        try:
            classification, reply, _raw = draft_reply_logic(
                text, order_line, history=history, source="Instagram DM"
            )
        except Exception as e:
            print(f"[INSTAGRAM] Twin pipeline failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            return

        if not reply:
            print(
                f"[INSTAGRAM] Twin returned empty reply "
                f"(classification={classification!r}); not sending"
            )
            return

        _send_instagram_reply(sender_id, reply)
        _log_instagram(sender_id, text, reply, timestamp)
        print(f"[INSTAGRAM] Logged DM exchange for {sender_id} ({classification})")

        # Page the founder on Telegram for non-AUTO classifications,
        # mirroring the WATI webhook. AUTO sends nothing (the bot's
        # reply is safe to ship as-is and doesn't warrant a ping). The
        # IG channel is passed explicitly so the action footer reflects
        # Instagram-specific guidance (e.g. "holding reply already sent").
        # Wrapped in its own try so a Telegram outage doesn't take down
        # the IG flow.
        if classification in ("DRAFT+APPROVE", "ESCALATE"):
            try:
                ig_sender_info = f"Instagram DM — sender {sender_id}"
                send_telegram_notification(
                    classification,
                    text,
                    reply,
                    sender_info=ig_sender_info,
                    channel="Instagram",
                    customer_id=sender_id,
                )
                tag = "DRAFT" if classification == "DRAFT+APPROVE" else "ESCALATE"
                print(f"[INSTAGRAM-{tag}] Notified founder for {sender_id}")
            except Exception as tg_err:
                print(
                    f"[INSTAGRAM-TG] Notification failed: "
                    f"{type(tg_err).__name__}: {tg_err}"
                )

        # Auto-pause this IG sender for 4h after ESCALATE so subsequent
        # DMs from the same sender don't re-trigger Claude + Telegram.
        # Mirrors the WATI ESCALATE auto-pause. Note: on IG the holding
        # reply is ALREADY sent to the customer at this point (line above
        # via _send_instagram_reply), so this pause specifically prevents
        # the "customer sees repeat holding messages" bug. Manual
        # #pause/#resume still apply; the pause register is shared.
        if classification == "ESCALATE":
            _pause_number(sender_id)
            print(f"[ESCALATE] Auto-paused {sender_id} for 4h after holding reply sent")
    except Exception as e:
        print(f"[INSTAGRAM] Event handler error: {type(e).__name__}: {e}")
        traceback.print_exc()


@app.route("/dashboard-data", methods=["GET"])
def dashboard_data():
    """JSON snapshot of message logs for the founder's live dashboard.

    Auth: ?key=<DASHBOARD_KEY> query param. DASHBOARD_KEY is required
    via Render env vars — the app refuses to start without it (see
    _require_env at the top of this module).

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
        # `pending` = rows whose status isn't one of the known terminal
        # outcomes. Always 0 today (every code path writes one of the listed
        # statuses), but it's a placeholder for a future approval/queue state.
        # `closed` is hardcoded 0 — same — until a CLOSED status is introduced.
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status='AUTO' THEN 1 END), 0) AS auto_replied,
                COALESCE(SUM(CASE WHEN status='ESCALATE' THEN 1 END), 0) AS escalated,
                COALESCE(SUM(CASE WHEN status='DEDUP' THEN 1 END), 0) AS dedup_skips,
                COALESCE(SUM(CASE WHEN status='PROTECTED' THEN 1 END), 0) AS protected_blocks,
                COALESCE(SUM(CASE WHEN status='ERROR' THEN 1 END), 0) AS errors,
                COALESCE(SUM(CASE
                    WHEN status NOT IN ('AUTO','ESCALATE','DEDUP','PROTECTED','ERROR','DRAFT')
                    THEN 1 END), 0) AS pending,
                0 AS closed,
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
            "pending": r[6] or 0,
            "closed": r[7] or 0,
            "avg_latency_ms": int(r[8]) if r[8] is not None else None,
            "max_latency_ms": int(r[9]) if r[9] is not None else None,
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
            # Renamed from distinct_senders_last_30min so the frontend's
            # data.bulk_spike.unique_senders_30min reference resolves.
            "unique_senders_30min": distinct_count,
            "is_spike": distinct_count >= 5,
        }

        # ---- Health: today's webhook stats + p99 latency + status flags ----
        # Most numbers come straight off `kpis` (already today-bucketed). We
        # add latency_p99 (computed in Python from a sorted fetch — SQLite
        # has no PERCENTILE function) and an all-time row count.
        cur.execute(
            """
            SELECT latency_ms FROM message_logs
            WHERE ts >= ? AND latency_ms IS NOT NULL
            ORDER BY latency_ms ASC
            """,
            (midnight_unix,),
        )
        latencies = [row[0] for row in cur.fetchall() if row[0] is not None]
        if latencies:
            n = len(latencies)
            p99_idx = max(0, min(n - 1, int(round(0.99 * (n - 1)))))
            latency_p99_ms = int(latencies[p99_idx])
        else:
            latency_p99_ms = None

        cur.execute("SELECT COUNT(*) FROM message_logs")
        total_logged_all_time = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM orders")
        total_orders_all_time = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM instagram_logs")
        total_instagram_all_time = cur.fetchone()[0] or 0

        # Webhook uptime — fraction of today's events that didn't ERROR.
        total_today = kpis["total"]
        errors_today = kpis["errors"]
        if total_today > 0:
            webhook_uptime_pct = round(
                (total_today - errors_today) / total_today * 100, 2
            )
        else:
            webhook_uptime_pct = None

        # Claude success — among events that actually reached Claude
        # (everything except DEDUP / PROTECTED skips). DEDUP and PROTECTED
        # short-circuit before the API call; AUTO/DRAFT/ESCALATE all imply
        # a successful Claude response, ERROR implies a failed one.
        claude_attempts = max(
            0, total_today - kpis["dedup_skips"] - kpis["protected_blocks"]
        )
        claude_successes = claude_attempts - errors_today
        if claude_attempts > 0:
            claude_success_rate = round(
                claude_successes / claude_attempts * 100, 2
            )
        else:
            claude_success_rate = None

        health = {
            "render_status": "online",
            "db_path": DB_PATH,
            "total_logged": total_logged_all_time,
            "total_orders": total_orders_all_time,
            "total_instagram": total_instagram_all_time,
            "webhook_uptime_pct": webhook_uptime_pct,
            "webhook_total_today": total_today,
            "webhook_success_today": total_today - errors_today,
            "dedup_blocks_today": kpis["dedup_skips"],
            "protected_blocks_today": kpis["protected_blocks"],
            "protected_numbers": [
                normalize_wa(BUSINESS_NUMBER),
                normalize_wa(OWNER_NUMBER),
            ],
            "avg_latency_ms": kpis["avg_latency_ms"],
            "latency_p99_ms": latency_p99_ms,
            "claude_attempts_today": claude_attempts,
            "claude_successes_today": claude_successes,
            "claude_success_rate": claude_success_rate,
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
            "health": health,
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
