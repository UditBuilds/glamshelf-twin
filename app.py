"""
Glam Shelf Twin — Phase 0
Flask app that drafts WhatsApp customer-service replies in The Glam Shelf voice.

Runs on:
  - Local Windows: `python app.py` → Flask dev server on http://localhost:5000
  - Render (Linux): `gunicorn app:app` via Procfile, binds to $PORT

Auth: DEEPSEEK_API_KEY (text replies) + ANTHROPIC_API_KEY (vision/image extraction)
  - Local: put both in .env (loaded by python-dotenv)
  - Render: set them in the service's Environment dashboard
"""

import base64
import hashlib
import hmac
import json
import os
import re
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
from html import unescape
from pathlib import Path

import requests
from openai import OpenAI
from anthropic import Anthropic
from dotenv import load_dotenv

from pricing_rules import (
    ACTION_ESCALATE,
    BULK_MIN_TRAYS,
    BULK_RATE_INR,
    INTENT_COMMIT,
    detect_bulk_commit_quantity,
    resolve_pricing_action,
)
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
DEEPSEEK_MODEL = "deepseek-chat"        # all text replies
CLAUDE_MODEL = "claude-sonnet-4-6"      # vision only (image extraction)
MAX_TOKENS = 2048

# ----- Vision (image understanding) config -----
#
# When a WATI inbound event has type=image, the handler downloads the
# image, sends it to the LLM's vision API for order-info extraction, and
# then either (a) synthesizes a text query and runs the normal reply
# pipeline, or (b) on low confidence / failure, sends the deterministic
# fallback reply asking the customer to type their order ID.
#
# Vision runs on CLAUDE_MODEL via claude_client — DeepSeek's deepseek-chat
# is text-only and rejects image inputs, so image extraction stays on
# Claude's native vision API. Text replies go through DeepSeek separately.
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

# Operator identifier the chat should be reassigned to whenever a human
# (Udit) takes over and WATI auto-reassigns the ticket away from the bot.
# WATI disables all automation for a chat the moment it's assigned to a
# human operator ("Automation will not work unless it's assigned back to
# Bot") — so after every manual reply we re-point the ticket at the Bot to
# keep the webhook alive. The literal "Bot" is what WATI's own event log
# uses ("ticket has been assigned to Bot"); override in env only if your
# WATI operator list names the AI agent differently.
WATI_BOT_OPERATOR_EMAIL = os.environ.get("WATI_BOT_OPERATOR_EMAIL", "Bot")

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

# SQLite path for ALL persistent state (message logs, Instagram logs,
# orders, shipping dedup, paused senders, pending drafts).
#   - Legacy/local default: <tempdir>/glamshelf_logs.db (ephemeral on Render).
#   - Production on Render: set DB_PATH (preferred) or DASHBOARD_DB_PATH
#     (legacy name, still honored) to a path on a mounted persistent disk,
#     e.g. /var/data/glamshelf.db. The disk needs to be created in
#     Render → Settings → Disks (any small size, mounted at /var/data).
#     Without that, every redeploy still wipes the DB — moving state into
#     SQLite only helps once this path survives restarts.
#   - On startup, if a legacy /tmp DB exists and the persistent path is
#     empty, _init_db() copies the file across once so historical rows
#     aren't lost when you flip on the persistent disk.
_LEGACY_DB_PATH = os.path.join(tempfile.gettempdir(), "glamshelf_logs.db")
DB_PATH = (
    os.environ.get("DB_PATH")
    or os.environ.get("DASHBOARD_DB_PATH")
    or _LEGACY_DB_PATH
)

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
# Startup diagnostic — the HUMAN_UDIT_IG detection in
# _process_instagram_event only works if INSTAGRAM_PAGE_ID is set (it
# compares the echo's sender.id against this value). Log presence (not
# the value) so it can be verified in Render logs without leaking the id.
print(f"[INSTAGRAM] Page ID configured: {bool(INSTAGRAM_PAGE_ID)}")

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
# that customer's wa_id is registered with a 4-hour expiry. While present,
# the WATI webhook handler short-circuits before any Claude call so the
# twin stops auto-replying — the human is on it. "#resume" removes the
# entry immediately.
#
# State lives in the paused_senders SQLite table (see _init_db), NOT in
# memory — Render's free tier restarts the process on its own schedule,
# and an in-memory register silently cut every 4h ESCALATE pause short.
# Note: SQLite only survives restarts once DB_PATH points at persistent
# storage; on the default tempdir path the table resets with the disk.
PAUSED_TTL_SECONDS = 4 * 60 * 60  # 4 hours

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
# pending draft in the pending_drafts SQLite table. The /telegram-callback
# endpoint receives the button tap (or Udit's edited text) and actions it.
#
# State lives in SQLite (see _init_db), NOT in memory — the previous
# in-memory dict dropped every pending draft whenever Render restarted
# the worker, leaving Udit tapping dead buttons ("Already handled") on
# drafts that were never sent. As with paused_senders, persistence is
# only real once DB_PATH points at storage that survives restarts.
#
# The draft dict is stored as a JSON blob keyed by the short draft_id
# (8 hex chars from secrets.token_hex(4)) so callback_data fits in
# Telegram's 64-byte hard limit alongside action and customer id.
#
# One deliberate gap: the ✏️ Edit flow's 10-minute timeout is a
# threading.Timer, which does NOT survive a restart. An awaiting-edit
# draft orphaned by a restart is cleaned up by the 24h TTL prune instead
# of the 10-min timer — Udit's edit text after a restart still works,
# because _drafts_awaiting_edit reads the DB, not thread state.
PENDING_DRAFT_TTL_SECONDS = 24 * 60 * 60     # opportunistic prune cutoff
EDIT_TIMEOUT_SECONDS = 10 * 60                # 10 min per spec


def _draft_register(draft_id: str, draft: dict) -> bool:
    """Insert or update a pending draft row. Returns False on DB failure
    so send_draft_for_approval can fall back to the plain notification
    (a buttoned Telegram message whose state was never saved would be a
    dead button)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO pending_drafts (draft_id, data, created_at) VALUES (?, ?, ?)",
            (draft_id, json.dumps(draft), draft.get("created_at") or time.time()),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DRAFT-DB] register failed for {draft_id}: {type(e).__name__}: {e}")
        return False


def _draft_get(draft_id: str) -> dict | None:
    """Read a pending draft without removing it. None if absent or on error."""
    if not draft_id:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT data FROM pending_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"[DRAFT-DB] get failed for {draft_id}: {type(e).__name__}: {e}")
        return None


def _draft_take(draft_id: str) -> dict | None:
    """Atomically remove and return a pending draft (None if absent).

    This is the restart-safe equivalent of the old dict.pop dedup: two
    rapid taps on "Send as-is" race into a BEGIN IMMEDIATE transaction;
    only one sees the row, the other gets None and hits the
    "Already handled" branch. Without the write lock, both taps could
    read the row before either deleted it and the customer would get
    the reply twice.
    """
    if not draft_id:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM pending_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM pending_drafts WHERE draft_id = ?", (draft_id,))
        conn.execute("COMMIT")
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"[DRAFT-DB] take failed for {draft_id}: {type(e).__name__}: {e}")
        return None


def _draft_delete(draft_id: str) -> None:
    """Remove a pending draft row. Best effort."""
    if not draft_id:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM pending_drafts WHERE draft_id = ?", (draft_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DRAFT-DB] delete failed for {draft_id}: {type(e).__name__}: {e}")


def _drafts_awaiting_edit(chat_id) -> list[tuple[str, dict]]:
    """All drafts flagged awaiting_edit for this Telegram chat, as
    (draft_id, draft) pairs. The awaiting_edit flag lives inside the JSON
    blob, so rows are filtered in Python — the table holds at most a
    handful of drafts at any time. Returns [] on any failure."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT draft_id, data FROM pending_drafts").fetchall()
        conn.close()
    except Exception as e:
        print(f"[DRAFT-DB] awaiting-edit scan failed: {type(e).__name__}: {e}")
        return []
    out = []
    for did, data in rows:
        try:
            d = json.loads(data)
        except Exception:
            continue
        if d.get("awaiting_edit") and d.get("telegram_chat_id") == chat_id:
            out.append((did, d))
    return out


def _drafts_prune(cutoff: float) -> None:
    """Delete drafts created before `cutoff` — the opportunistic TTL prune
    that keeps the table bounded even if drafts are never actioned."""
    try:
        conn = sqlite3.connect(DB_PATH)
        pruned = conn.execute(
            "DELETE FROM pending_drafts WHERE created_at < ?", (cutoff,)
        ).rowcount
        conn.commit()
        conn.close()
        if pruned:
            print(f"[DRAFT-DB] Pruned {pruned} stale draft(s) past 24h TTL")
    except Exception as e:
        print(f"[DRAFT-DB] prune failed: {type(e).__name__}: {e}")


def _is_paused(wa_id: str) -> bool:
    """Return True if this number is currently in a human-takeover window.
    Reads the paused_senders table (restart-safe) and opportunistically
    prunes expired rows so the table stays bounded — no cleanup job needed.

    Fails open (False) on any DB error, matching the convention of the
    other DB-backed gates (_udit_replied_recently etc.) — a DB hiccup
    shouldn't silence the bot for every customer.
    """
    if not wa_id:
        return False
    now = time.time()
    try:
        conn = sqlite3.connect(DB_PATH)
        pruned = conn.execute(
            "DELETE FROM paused_senders WHERE paused_until < ?", (now,)
        ).rowcount
        conn.commit()
        if pruned:
            print(f"[PAUSE] Auto-expired {pruned} pause(s) (4h elapsed)")
        hit = conn.execute(
            "SELECT 1 FROM paused_senders WHERE sender_id = ? AND paused_until >= ?",
            (wa_id, now),
        ).fetchone()
        conn.close()
        return hit is not None
    except Exception as e:
        print(f"[PAUSE] _is_paused DB check failed for {wa_id}: {type(e).__name__}: {e}")
        return False


def _pause_number(wa_id: str, ttl_seconds: int = PAUSED_TTL_SECONDS) -> None:
    """Add `wa_id` (or Instagram sender_id — the register is just keyed by
    string) to the paused_senders table with a TTL. While paused, the
    inbound handlers short-circuit before any Claude call and the customer
    gets no auto-replies. Survives process restarts (given a persistent
    DB_PATH), unlike the in-memory dict this replaced.

    Used by:
      - _handle_pause_directive — when Udit types "#pause" outbound
      - WATI webhook ESCALATE branch — auto-pause after holding reply
      - Instagram webhook ESCALATE branch — same

    Idempotent: extending the pause window (re-pausing an already-paused
    number) just resets the expiry. Caller should log the auto-pause
    with their own channel-specific prefix so the founder can grep.
    DB failures are logged and swallowed — same convention as _log_message.
    """
    if not wa_id:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO paused_senders (sender_id, paused_until) VALUES (?, ?)",
            (wa_id, time.time() + ttl_seconds),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[PAUSE] _pause_number DB write failed for {wa_id}: {type(e).__name__}: {e}")


def _unpause_number(wa_id: str) -> bool:
    """Remove `wa_id` from the pause register (Udit's "#resume" directive).
    Returns True if an entry was actually removed, False if there was
    nothing to clear (or the DB write failed)."""
    if not wa_id:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        removed = conn.execute(
            "DELETE FROM paused_senders WHERE sender_id = ?", (wa_id,)
        ).rowcount
        conn.commit()
        conn.close()
        return removed > 0
    except Exception as e:
        print(f"[PAUSE] _unpause_number DB write failed for {wa_id}: {type(e).__name__}: {e}")
        return False


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


# Distinctive substrings that only ever appear in the bot's own TEMPLATED
# sends (shipping/tracking + review request). Those go out via
# send_whatsapp_reply but are NOT written to message_logs as reply_text, so
# once the in-memory _bot_recent_replies TTL lapses we'd otherwise mistake
# them for a human reply. Matching these signatures keeps them attributed
# to the bot. Kept deliberately narrow (URLs / fixed template phrases) so a
# message Udit actually types can't accidentally match.
_BOT_TEXT_SIGNATURES = (
    "shiprocket.in/tracking",
    "here's your tracking link",
    "glamshelf.in/pages/reviews",
)


def _norm_text(s: str | None) -> str:
    """Whitespace-collapsed, lower-cased text for tolerant comparison.
    WATI stores outbound text byte-for-byte as we sent it, so a strip +
    internal-whitespace collapse + lowercase is enough to match reliably
    without the exact-match brittleness the old _is_bot_outbound had."""
    return " ".join((s or "").split()).strip().lower()


def _extract_wati_message_items(data) -> list:
    """Pull the message list out of a getMessages response across the
    schema variations WATI returns on different plans. Known shapes:
      {"messages": {"items": [...]}}   (most common)
      {"messages": [...]}
      {"items": [...]}
    Returns [] for anything unrecognised (never raises)."""
    if not isinstance(data, dict):
        return []
    msgs = data.get("messages")
    if isinstance(msgs, dict) and isinstance(msgs.get("items"), list):
        return msgs["items"]
    if isinstance(msgs, list):
        return msgs
    if isinstance(data.get("items"), list):
        return data["items"]
    return []


def _parse_wati_ts(item) -> float | None:
    """Best-effort unix timestamp for a WATI message item. Tries the
    numeric `timestamp` field first, then ISO-8601 `created`. Returns None
    when neither parses — caller decides how to treat undateable items."""
    if not isinstance(item, dict):
        return None
    raw = item.get("timestamp")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    created = item.get("created")
    if isinstance(created, str) and created.strip():
        try:
            from datetime import datetime
            return datetime.fromisoformat(created.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _recent_bot_texts(wa_id: str, window_seconds: int = HUMAN_HANDLING_WINDOW_SECONDS) -> set[str]:
    """Normalized set of texts the BOT actually shipped to this customer
    recently — the yardstick for telling the bot's own outbound apart from
    Udit's manual replies. Two complementary sources:

      - _bot_recent_replies (in-memory, every send_whatsapp_reply call,
        ~5-min TTL): covers EVERY outbound path (Claude replies, shipping,
        tracking, review, vision fallback) for the minutes after a send.
      - message_logs reply_text for AUTO / DRAFT / ESCALATE rows in the
        window: survives Render restarts and the 5-min TTL, durably
        covering every Claude-generated reply the customer was sent.

    Together they make bot-vs-human attribution restart-proof for the
    common case (Claude replies) and TTL-covered for templated sends.
    Never raises — degrades to whatever subset it could gather."""
    texts: set[str] = set()
    for t in list(_bot_recent_replies.keys()):
        n = _norm_text(t)
        if n:
            texts.add(n)
    if wa_id:
        try:
            cutoff = time.time() - window_seconds
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT reply_text FROM message_logs "
                "WHERE wa_id = ? AND status IN ('AUTO','DRAFT','DRAFT_SENT','ESCALATE') "
                "AND reply_text IS NOT NULL AND ts >= ? "
                "ORDER BY ts DESC LIMIT 50",
                (wa_id, cutoff),
            )
            for (rt,) in cur.fetchall():
                n = _norm_text(rt)
                if n:
                    texts.add(n)
            conn.close()
        except Exception as e:
            print(f"[HUMAN_CHECK] bot-text DB lookup failed: {type(e).__name__}: {e}")
    return texts


def _looks_like_bot_text(text: str, bot_texts: set[str]) -> bool:
    """True if `text` was (almost certainly) sent by the bot, not a human.
    Checks the durable/in-memory bot-text set first, then the narrow
    templated-send signatures."""
    n = _norm_text(text)
    if not n:
        return False
    if n in bot_texts:
        return True
    return any(sig in n for sig in _BOT_TEXT_SIGNATURES)


def _check_recent_human_reply(wa_id: str) -> bool:
    """Ask WATI directly whether a human (Udit) has replied to this chat
    more recently than the bot — the durable, restart-proof complement to
    the in-memory _is_bot_outbound / _bot_recent_replies heuristic.

    WHY: WATI tags BOTH the bot's API sends and Udit's dashboard replies as
    owner=True, with no field to tell them apart. The old detection leaned
    entirely on _bot_recent_replies (in-memory, ~5-min TTL, exact-text) — so
    after a Render restart, after 5 minutes, or on a single-character diff
    it silently failed and the twin talked over Udit (the May-13 "sorry,
    our automation system is glitching" incident: the bot re-asked for the
    order ID four times on top of Udit's manual replies).

    STRATEGY: fetch the recent message history and look at the most recent
    BUSINESS-side (owner=True) message — i.e. who spoke last on our side.
      - If that message is one the bot sent (per _recent_bot_texts /
        templated-send signatures) → the bot had the last word, no pending
        takeover → return False.
      - If it is NOT ours → a human sent it. Honour the 4h handling window
        when the message is dateable (don't pause on a stale reply from a
        long-dormant thread); if undateable, treat it as recent because
        stopping the over-reply bug is the priority here.

    Latency: one ~0.5-1s GET. The caller only invokes this when the number
    is NOT already paused, and on a positive hit we pause immediately, so
    it runs at most once per customer per 4h window.

    Never raises. Any failure (missing config, HTTP error, unparseable
    body) returns False so the caller falls through to normal Claude
    processing — a possible duplicate reply is a smaller harm than dropping
    a real customer message."""
    if not WATI_API_KEY or not WATI_ENDPOINT:
        return False
    wa_id = (wa_id or "").strip()
    if not wa_id:
        return False

    endpoint = WATI_ENDPOINT.rstrip("/")
    url = f"{endpoint}/api/v1/getMessages/{wa_id}"
    headers = {"Authorization": f"Bearer {WATI_API_KEY}"}
    params = {"pageSize": "20"}

    try:
        resp = requests.get(
            url, headers=headers, params=params, timeout=WATI_TIMEOUT_SECONDS
        )
        if not resp.ok:
            print(f"[HUMAN_CHECK] getMessages HTTP {resp.status_code} for {wa_id} — skipping check")
            return False
        try:
            data = resp.json()
        except ValueError:
            print(f"[HUMAN_CHECK] getMessages returned non-JSON for {wa_id} — skipping check")
            return False

        items = _extract_wati_message_items(data)
        if not items:
            print(f"[HUMAN_CHECK] No messages returned for {wa_id}")
            return False

        # Newest first so the first business-side hit is the latest one.
        items = sorted(items, key=lambda m: _parse_wati_ts(m) or 0.0, reverse=True)

        bot_texts = _recent_bot_texts(wa_id)
        now = time.time()

        for item in items:
            if not isinstance(item, dict) or item.get("owner") is not True:
                continue  # customer (owner=False) or malformed — ignore
            text = item.get("text")
            if isinstance(text, dict):
                text = text.get("body")
            text = (text or "").strip()
            if not text:
                continue

            # This is the most recent business-side message — it decides the
            # outcome either way, so we return on the first one we see.
            if _looks_like_bot_text(text, bot_texts):
                return False  # bot had the last word → no takeover

            ts = _parse_wati_ts(item)
            if ts is not None and (now - ts) > HUMAN_HANDLING_WINDOW_SECONDS:
                print(f"[HUMAN_CHECK] Latest manual reply for {wa_id} is older than 4h — not pausing")
                return False
            age = f"{int(now - ts)}s ago" if ts is not None else "time unknown"
            print(f"[HUMAN_CHECK] Detected manual reply for {wa_id} ({age}): {text[:80]!r}")
            return True

        # No business-side messages at all → nobody has replied → bot proceeds.
        return False
    except requests.RequestException as e:
        print(f"[HUMAN_CHECK] Network error for {wa_id}: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        print(f"[HUMAN_CHECK] Unexpected error for {wa_id}: {type(e).__name__}: {e}")
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
    # clean; the actual pause state is in the paused_senders table.
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
        # A #pause/#resume directive rides on a message Udit sent manually
        # through WATI, so WATI has already reassigned this chat to his
        # operator. Re-point it at the Bot so the webhook keeps receiving
        # the customer's messages (the in-memory pause register, not the
        # ticket assignment, is what suppresses auto-replies while paused).
        _reassign_to_bot(wa_id)
        return

    # Plain Udit-manual-reply path. Log as HUMAN_UDIT so the DB safety-net
    # check (_udit_replied_recently) on the next inbound suppresses the
    # twin's auto-reply for 4h, AND pause the number in-memory right now so
    # the very next inbound short-circuits at the cheap _is_paused gate
    # without waiting for the DB check or another WATI scan. (Previously
    # this path only wrote the DB row; the in-memory pause closes the
    # window between this manual reply and the next inbound.)
    _log_message(
        wa_id, sender_name, text_body,
        status="HUMAN_UDIT",
        reply_text=text_body,
    )
    _pause_number(wa_id)
    if msg_id:
        _seen_ids.add(msg_id)
        _persist_seen_id(msg_id)
    print(f"[HUMAN_UDIT] Logged + paused manual reply for {wa_id} (sender={sender_name!r}, {len(text_body)} chars)")
    # Udit just replied by hand in WATI, which reassigned this chat to his
    # operator and switched off automation for it. Re-point it at the Bot
    # so the twin keeps receiving this customer's future messages — the
    # HUMAN_UDIT safety net (not the ticket assignment) is what holds the
    # twin back from auto-replying for the next 4h.
    _reassign_to_bot(wa_id)


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
        _pause_number(wa_id)
        print(
            f"[PAUSE] Human takeover activated for {wa_id} "
            f"(expires in {PAUSED_TTL_SECONDS}s = 4h)"
        )
        return "pause"
    if "#resume" in lower:
        if _unpause_number(wa_id):
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

        # Pause register — previously an in-memory dict (paused_numbers)
        # that reset on every Render restart, silently cutting 4h ESCALATE
        # pauses short. sender_id is a wa_id or IG sender_id; paused_until
        # is a unix timestamp. Expired rows are pruned opportunistically
        # by _is_paused.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paused_senders (
                sender_id TEXT PRIMARY KEY,
                paused_until REAL NOT NULL
            )
            """
        )

        # Telegram DRAFT+APPROVE approval queue — previously the in-memory
        # _pending_drafts dict, which dropped every pending draft on
        # restart (buttons went dead with "Already handled"). The full
        # draft dict is stored as a JSON blob; created_at is duplicated
        # as a column for the TTL prune query.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_drafts (
                draft_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

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


def _load_wati_history(wa_id: str, max_turns: int = 30, max_age_days: int = 7) -> list[dict]:
    """Pull up to `max_turns` recent (customer msg, bot reply) exchanges
    with this wa_id from the last `max_age_days` days, oldest first.

    The 30-turn / 7-day window is sized for multi-day threads — refund
    flows, return pickups, and Udit-handled escalations often span days,
    and the twin needs to see the resolution context from earlier in
    the thread so it doesn't restart the conversation as if it's a
    fresh complaint when the customer follows up with "any update?".

    DELIVERED-ONLY status allowlist — a row is eligible only if it
    represents text the customer ACTUALLY received:
      - AUTO        : bot reply sent straight to WhatsApp.
      - ESCALATE    : holding reply for the escalation context.
      - DRAFT_SENT  : a DRAFT+APPROVE reply that Udit approved (or edited)
                      and shipped via the Telegram buttons. reply_text holds
                      the exact text delivered (the edited version on edits).

    Deliberately EXCLUDED:
      - DRAFT       : the *drafted* text at creation time. A draft may be
                      Skipped or timed-out (never sent) or Edited (sent text
                      differs), so the raw DRAFT row can't be trusted as
                      "what the customer saw". Its delivery, when it happens,
                      is recorded separately as DRAFT_SENT.
      - HUMAN_UDIT / HUMAN_HANDLING / PAUSE_DIRECTIVE / PAUSED / DEDUP /
                      PROTECTED / ERROR : not clean customer→bot exchanges
                      (no reply, Udit's own outbound, or placeholder text),
                      so feeding them as user/assistant turns would mislead.

    THE BUG THIS FIXES: the old filter was AUTO/ESCALATE only, so every
    reply delivered through the DRAFT+APPROVE → Telegram-approve flow was
    invisible to history. A customer whose substantive turns went through
    approval (e.g. a makeup artist asking for bulk options) would hit a
    full context reset on her next message — the bot would "forget"
    everything it had already told her.

    Token-cost note: 30 turns × ~150 chars avg ≈ 4-5k chars of extra
    context per Claude call. Well within the model's window; the user-
    message portion isn't cached, so it's a real additive cost — still
    net cheaper than re-asking for info already in the thread.

    Failures are logged-and-swallowed → returns []. Caller falls back to a
    plain single-turn call. Never returns None.
    """
    if not wa_id:
        return []
    try:
        cutoff = time.time() - max_age_days * 24 * 3600
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, msg_text, reply_text
            FROM message_logs
            WHERE wa_id = ?
              AND status IN ('AUTO', 'ESCALATE', 'DRAFT_SENT')
              AND ts >= ?
              AND msg_text IS NOT NULL
              AND reply_text IS NOT NULL
            ORDER BY ts DESC
            LIMIT ?
            """,
            (wa_id, cutoff, max_turns),
        )
        rows = cur.fetchall()
        conn.close()
        rows.reverse()  # oldest -> newest (Claude expects chronological order)
        history = [
            {"ts": r[0], "msg_text": r[1], "reply_text": r[2]} for r in rows
        ]
        print(f"[HISTORY] Loaded {len(history)} turns for {wa_id}")
        return history
    except Exception as e:
        print(f"[HISTORY] Failed to load history for {wa_id}: {type(e).__name__}: {e}")
        return []


# Back-compat alias — kept so any existing call site / comment that refers
# to the original name keeps working. Delegates with the default window.
def _load_conversation_history(wa_id: str) -> list[dict]:
    return _load_wati_history(wa_id)


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


# Send-failure Telegram alerting. When an outbound customer send fails
# (Instagram Graph API or WATI), Udit gets a Telegram alert so an outage
# like an expired IG token is noticed in minutes, not hours (the July 8
# token expiry ran silent for ~9h because failures only lived in Render
# logs). Rate-limited per (channel, error) signature: the first failure
# alerts, repeats of the SAME error within the window are suppressed —
# otherwise a token outage would page once per customer message.
SEND_FAILURE_ALERT_COOLDOWN_SECONDS = 1800  # 30 min
_send_failure_last_alert: dict[str, float] = {}


def _alert_send_failure(channel: str, error: str, customer_id: str) -> None:
    """Telegram-alert Udit that a customer reply failed to send.

    Reuses the same bot/chat as the draft-approval flow via _telegram_api.
    Never raises — alerting is a side effect and must not break the
    webhook 200 response, same contract as every other Telegram call.
    """
    key = f"{channel}|{error[:120]}"
    now = time.time()
    if now - _send_failure_last_alert.get(key, 0.0) < SEND_FAILURE_ALERT_COOLDOWN_SECONDS:
        print(f"[ALERT] Suppressed repeat send-failure alert ({channel}): {error[:80]}")
        return
    # Stamp before sending so a Telegram hiccup can't turn into an
    # alert-per-message storm during an outage.
    _send_failure_last_alert[key] = now

    if not TELEGRAM_CHAT_ID:
        print("[ALERT] Skipped: TELEGRAM_CHAT_ID not set")
        return
    text = (
        f"⚠️ {channel} send FAILED\n"
        f"Customer {customer_id} did not get a reply.\n"
        f"Error: {error[:300]}\n\n"
        f"(Repeats of this error muted for 30 min)"
    )
    try:
        _telegram_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"[ALERT] Telegram alert failed: {type(e).__name__}: {e}")


def _send_instagram_reply(sender_id: str, text: str) -> tuple[bool, str]:
    """Send an outbound Instagram DM via the Meta Graph Messages API.

    Endpoint: POST https://graph.facebook.com/v19.0/me/messages
    Auth via ?access_token=... query param (Meta's documented pattern).
    Body: {"recipient": {"id": <sender>}, "message": {"text": <reply>}}

    Returns (True, "") on a confirmed delivery, (False, error) on any
    failure — callers that log success MUST check this instead of
    assuming the send worked. Actual API/network failures also fire a
    rate-limited Telegram alert via _alert_send_failure.

    All exceptions are logged-and-swallowed — Instagram delivery must
    never break the webhook 200 response. Missing access token →
    skipped silently with a single log line.
    """
    if not INSTAGRAM_PAGE_ACCESS_TOKEN:
        print("[INSTAGRAM] Skipped: INSTAGRAM_PAGE_ACCESS_TOKEN not set")
        return False, "INSTAGRAM_PAGE_ACCESS_TOKEN not set"
    if not sender_id or not text:
        return False, "empty sender_id or text"

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
            # Register the sent text so the echo Meta loops back (sender ==
            # PAGE_ID, is_echo == true) is recognized as the BOT's own
            # outbound — NOT mistaken for Udit's manual reply by the
            # HUMAN_UDIT_IG detection in _process_instagram_event. Without
            # this, every bot AUTO reply's echo would auto-pause the
            # customer for 4h and the IG flow would break. Mirrors the
            # WATI _record_bot_outbound design (text-only here; IG echoes
            # are matched by text, not msg id).
            _record_bot_outbound(text)
            return True, ""
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
            # Prefer the Graph API's own error message (e.g. "Error
            # validating access token: ...") — it's stable across repeats
            # of the same failure, which is what the alert rate limit
            # keys on.
            try:
                api_msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                api_msg = ""
            error = f"HTTP {resp.status_code}: {api_msg or resp.text[:120]}"
            _alert_send_failure("Instagram", error, sender_id)
            return False, error
    except requests.RequestException as e:
        print(f"[INSTAGRAM] Network error: {type(e).__name__}: {e}")
        error = f"Network error: {type(e).__name__}"
        _alert_send_failure("Instagram", error, sender_id)
        return False, error
    except Exception as e:
        print(f"[INSTAGRAM] Unexpected error: {type(e).__name__}: {e}")
        error = f"Unexpected error: {type(e).__name__}"
        _alert_send_failure("Instagram", error, sender_id)
        return False, error


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
    helper) — the paused_senders register is the primary gate; this check
    is the additive backup for manual IG-app replies.
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

# Confirm the auto-reassign feature is wired up. Runs at import time so it
# shows in both the gunicorn (prod) and __main__ (local) boot logs.
if WATI_API_KEY and WATI_ENDPOINT:
    print(f"[REASSIGN] Auto-reassign to Bot enabled (operator={WATI_BOT_OPERATOR_EMAIL!r})")
else:
    print("[REASSIGN] Auto-reassign to Bot inactive — WATI_API_KEY/WATI_ENDPOINT not set")

# Hybrid LLM setup — two independent clients, two keys, no shared state.
#   - deepseek_client: every TEXT reply (cheap, ~90% of calls) via the
#     OpenAI-compatible SDK pointed at DeepSeek.
#   - claude_client: VISION ONLY (order screenshots + eye selfies) — DeepSeek's
#     deepseek-chat is text-only, so image extraction stays on Claude's API.
# Both keys must be set on Render / in .env (no usable default).
deepseek_client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
    base_url="https://api.deepseek.com",
)
claude_client = Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
)

print(f"[INIT] DeepSeek text client configured: {bool(os.environ.get('DEEPSEEK_API_KEY', ''))}")
print(f"[INIT] Claude vision client configured: {bool(os.environ.get('ANTHROPIC_API_KEY', ''))}")


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
    channel: WhatsApp says "from your WhatsApp Business app", Instagram
    says "from your Instagram DMs". On BOTH channels ESCALATE now means
    the handler suppressed the customer reply — the IG handler gates
    sends on classification the same way the WATI handler does. Default
    preserves the existing WATI behavior byte-for-byte.
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
            "→ No reply sent on Instagram. "
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
    channel: str = "WhatsApp",
    ig_timestamp: str = "",
) -> bool:
    """Send a Telegram message with [✅ Send as-is | ✏️ Edit | ⛔ Skip]
    inline buttons and register the draft in the pending_drafts table so
    the /telegram-callback handler can action it (restart-safe).

    `channel` ("WhatsApp" default, or "Instagram") is stored on the draft
    and decides the outbound path when Udit approves: send_whatsapp_reply
    for WhatsApp, _send_instagram_reply for Instagram. For Instagram,
    customer_number carries the IG sender_id and ig_timestamp carries the
    original event timestamp (used when logging the delivered exchange).

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
    header = "🟡 DRAFT + APPROVE"
    text = (
        f"{header}\n\n"
        f"From: {sender_block}\n\n"
        "Customer said:\n"
        f'"{customer_message}"\n\n'
        "Drafted reply:\n"
        f'"{reply_text}"'
    )

    # callback_data must be ≤ 64 bytes (Telegram hard limit). Our format:
    #   "action:<verb>|num:<wa_id-or-ig-sender-id>|id:<8-hex>"
    # Worst case: action:send (11) + |num: (5) + 17-digit IG sender_id
    # + |id: (4) + 8 = 45 bytes (12-digit wa_id: 40 bytes).
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Send as-is", "callback_data": f"action:send|num:{customer_number}|id:{draft_id}"},
            {"text": "✏️ Edit",       "callback_data": f"action:edit|num:{customer_number}|id:{draft_id}"},
            {"text": "⛔ Skip",        "callback_data": f"action:skip|num:{customer_number}|id:{draft_id}"},
        ]]
    }

    # Register the draft BEFORE sending the buttoned message: if the DB
    # write fails, we return False so the caller falls back to the plain
    # notification instead of showing Udit buttons backed by no state.
    result_placeholder = {
        "reply_text": reply_text,
        "customer_number": customer_number,
        "customer_name": customer_name,
        "customer_message": customer_message,
        "original_text": text,
        "telegram_chat_id": None,
        "telegram_message_id": None,
        "awaiting_edit": False,
        "created_at": time.time(),
        "channel": channel,
        "ig_timestamp": ig_timestamp,
    }
    if not _draft_register(draft_id, result_placeholder):
        return False

    resp = _telegram_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "reply_markup": keyboard,
    })
    if not resp or not resp.get("ok"):
        _draft_delete(draft_id)  # no buttons exist — drop the orphan row
        return False

    # Backfill the Telegram message coordinates now that the send
    # succeeded (needed by the edit flow to annotate the original message).
    result = resp.get("result") or {}
    result_placeholder["telegram_chat_id"] = (result.get("chat") or {}).get("id")
    result_placeholder["telegram_message_id"] = result.get("message_id")
    _draft_register(draft_id, result_placeholder)

    # Opportunistic prune — clean entries older than TTL so the table
    # stays bounded even if some drafts are never actioned.
    _drafts_prune(time.time() - PENDING_DRAFT_TTL_SECONDS)

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
    the dedup check (draft row already deleted) still prevents
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
    # because it has no pending_drafts state. The `id` in the callback
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

    # Atomic take — _draft_take removes-and-returns inside a BEGIN
    # IMMEDIATE transaction, so two rapid taps racing into this function
    # only one gets the draft; the other gets None and falls through to
    # the "already handled" dedup branch. Without this, a fast double-tap
    # on "Send as-is" could double-send to the customer.
    #
    # The edit branch below re-registers the draft (with awaiting_edit=True)
    # because the follow-up text message handler needs to find it.
    draft = _draft_take(draft_id) if draft_id else None

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
        if draft.get("channel") == "Instagram":
            # IG draft: customer_number holds the IG sender_id. Deliver via
            # the Graph API and record the exchange in instagram_logs so
            # _load_instagram_history sees this turn (the pending-draft row
            # was written with a NULL reply and is invisible to history).
            # No _reassign_to_bot — that's a WATI-only concept.
            _send_instagram_reply(customer_number, draft["reply_text"])
            _log_instagram(
                customer_number, draft.get("customer_message") or "",
                draft["reply_text"], draft.get("ig_timestamp") or "",
                source="DRAFT_SENT_IG",
            )
        else:
            send_whatsapp_reply(customer_number, draft["reply_text"])
            # Record the DELIVERED reply as a history-eligible exchange so the
            # twin remembers this turn on the customer's next message. Without
            # this, approved-draft replies were invisible to _load_wati_history
            # (only the un-delivered DRAFT row existed) and the conversation
            # context reset. msg_text is the customer message the draft answered.
            _log_message(
                customer_number, customer_name, draft.get("customer_message") or "",
                status="DRAFT_SENT", reply_text=draft["reply_text"],
            )
            # Approving a draft is a human takeover of the conversation — keep
            # the ticket on the Bot so the webhook keeps hearing this customer.
            _reassign_to_bot(customer_number)
        _finalize_draft_message(
            chat_id, message_id, original_text,
            f"✅ Sent to {name_for_display}",
        )
        # Already popped at top — no del needed.
        print(f"[TELEGRAM DRAFT] Send-as-is for {customer_number} (draft {draft_id})")

    elif action == "edit":
        # Re-register the draft so the next text message from this chat can
        # find it via _handle_telegram_message. Flip awaiting_edit so the
        # message handler routes to the edit flow.
        draft["awaiting_edit"] = True
        draft["edit_started_at"] = time.time()
        _draft_register(draft_id, draft)
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
        if draft.get("channel") == "Instagram":
            _finalize_draft_message(
                chat_id, message_id, original_text,
                "⛔ Skipped — handle manually in Instagram DMs",
            )
        else:
            _finalize_draft_message(
                chat_id, message_id, original_text,
                "⛔ Skipped — handle manually in WATI",
            )
            # Skip means Udit will reply by hand in WATI, which reassigns the
            # ticket to him. Pin it back to the Bot pre-emptively so the webhook
            # stays alive (idempotent — a no-op if it's still Bot-assigned).
            _reassign_to_bot(customer_number)
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
    draft: dict | None = None
    target_started: float = float("inf")
    for did, d in _drafts_awaiting_edit(chat_id):
        started = d.get("edit_started_at", float("inf"))
        if started < target_started:
            target_id = did
            draft = d
            target_started = started

    if not target_id or draft is None:
        # Not part of an edit flow — could be Udit typing anything in the
        # bot chat. Ignore (no command system yet).
        return
    customer_number = draft["customer_number"]
    customer_name = draft.get("customer_name") or ""
    name_for_display = customer_name or customer_number

    # Record the actually-sent EDITED text (not the pre-edit draft) as a
    # history-eligible exchange so future turns remember what the customer
    # really received. Mirrors the send-as-is branch in _handle_telegram_callback.
    if draft.get("channel") == "Instagram":
        _send_instagram_reply(customer_number, text)
        _log_instagram(
            customer_number, draft.get("customer_message") or "",
            text, draft.get("ig_timestamp") or "",
            source="DRAFT_SENT_IG",
        )
    else:
        send_whatsapp_reply(customer_number, text)
        _log_message(
            customer_number, customer_name, draft.get("customer_message") or "",
            status="DRAFT_SENT", reply_text=text,
        )
        # Sending an edited reply is a human takeover — keep the ticket on the
        # Bot so the webhook keeps receiving this customer's messages.
        _reassign_to_bot(customer_number)
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

    _draft_delete(target_id)
    print(f"[TELEGRAM DRAFT] Edit completed for {customer_number} (draft {target_id})")


def _edit_timeout_check(draft_id: str) -> None:
    """Fires EDIT_TIMEOUT_SECONDS after Edit was tapped. If the draft is
    still awaiting an edit at that point, auto-skip and notify Telegram.

    No-op if the user already sent the edit or hit Skip (row gone or flag
    cleared). Note the Timer itself does NOT survive a worker restart —
    an awaiting-edit draft orphaned by a restart stays actionable (the
    edit text still lands via _drafts_awaiting_edit) until the 24h TTL
    prune collects it.
    """
    draft = _draft_get(draft_id)
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
    _draft_delete(draft_id)


def normalize_wa(number: str) -> str:
    """Reduce a phone number to comparable digits.

    Strips non-digit characters and any leading zeros, so "+91 92174 70151",
    "0919217470151", and "919217470151" all compare equal. Used for safe
    cross-format equality checks against BUSINESS_NUMBER / OWNER_NUMBER.
    """
    return "".join(c for c in (number or "") if c.isdigit()).lstrip("0")


def send_whatsapp_reply(wa_id: str, reply_text: str) -> tuple[bool, str]:
    """Send an outbound WhatsApp text message to a customer via WATI.

    Returns (True, "") on a confirmed delivery, (False, error) on any
    failure — callers that log success MUST check this instead of
    assuming the send worked. Actual API/network failures also fire a
    rate-limited Telegram alert via _alert_send_failure.

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
        return False, "WATI_API_KEY or WATI_ENDPOINT not set"

    # Defense in depth: never auto-send to the business or owner number,
    # even if some future change in the inbound filter ever lets one through.
    # Compared on normalized digits so format quirks can't slip past.
    target = normalize_wa(wa_id)
    if target and target in {normalize_wa(BUSINESS_NUMBER), normalize_wa(OWNER_NUMBER)}:
        print(f"[WATI] BLOCKED outbound to protected number {wa_id}")
        return False, "blocked: protected number"

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
            error = f"WATI rejected: {info}"
            _alert_send_failure("WhatsApp", error, wa_id)
            return False, error
        elif response.ok:
            print(f"[WATI] Sent reply to {wa_id} ({len(reply_text)} chars)")
            # Register the outbound so WATI's subsequent outbound webhook
            # (echoing this same message back) is identified as bot-originated
            # rather than Udit's manual reply — prevents HUMAN_UDIT mis-tagging
            # that would otherwise suppress the AUTO flow.
            _record_bot_outbound(reply_text, data if isinstance(data, dict) else None)
            return True, ""
        else:
            print(f"[WATI] HTTP failure {response.status_code}")
            error = f"HTTP {response.status_code}: {body_preview[:120]}"
            _alert_send_failure("WhatsApp", error, wa_id)
            return False, error
    except requests.RequestException as e:
        print(f"[WATI] Network error: {type(e).__name__}: {e}")
        error = f"Network error: {type(e).__name__}"
        _alert_send_failure("WhatsApp", error, wa_id)
        return False, error
    except Exception as e:
        print(f"[WATI] Unexpected error: {type(e).__name__}: {e}")
        error = f"Unexpected error: {type(e).__name__}"
        _alert_send_failure("WhatsApp", error, wa_id)
        return False, error


def _reassign_to_bot(wa_id: str) -> None:
    """Re-point a WATI chat's ticket back at the Bot operator.

    THE root-cause fix: every time Udit manually replies to a customer in
    the WATI dashboard, WATI reassigns that chat from "Bot" to his operator
    email and DISABLES all automation for it — the twin's webhook then goes
    deaf to that customer until the chat is reassigned back to Bot. This
    helper makes that reassignment automatic, called from every spot where
    we detect a human takeover (HUMAN_UDIT outbound, #pause directive,
    ESCALATE, and the Telegram approval buttons).

    Uses WATI's assignOperator endpoint — same base URL / Bearer auth as
    send_whatsapp_reply, only the path and params differ:
        POST /api/v1/assignOperator?email=<operator>&whatsappNumber=<waId>
    The operator we assign to is WATI_BOT_OPERATOR_EMAIL (default "Bot").

    Contract (mirrors send_whatsapp_reply):
      - Idempotent: re-assigning an already-Bot-assigned chat is a no-op on
        WATI's side, so calling this twice never errors.
      - Defensive: NEVER raises. Every failure path (missing config,
        protected number, network error, WATI logical rejection on HTTP
        200) is logged with a [REASSIGN] tag and swallowed so the webhook
        always returns 200.
      - WhatsApp-only: Instagram doesn't use WATI ticketing, so the IG
        flow deliberately does NOT call this (see _process_instagram_event).
    """
    if not WATI_API_KEY or not WATI_ENDPOINT:
        print("[REASSIGN] Skipped: WATI_API_KEY or WATI_ENDPOINT not set")
        return
    if not WATI_BOT_OPERATOR_EMAIL:
        print("[REASSIGN] Skipped: WATI_BOT_OPERATOR_EMAIL not set")
        return

    wa_id = (wa_id or "").strip()
    if not wa_id:
        print("[REASSIGN] Skipped: empty wa_id")
        return

    # Defense in depth: never touch tickets for the business/owner numbers.
    target = normalize_wa(wa_id)
    if target and target in {normalize_wa(BUSINESS_NUMBER), normalize_wa(OWNER_NUMBER)}:
        print(f"[REASSIGN] Skipped protected number {wa_id}")
        return

    endpoint = WATI_ENDPOINT.rstrip("/")
    url = f"{endpoint}/api/v1/assignOperator"
    headers = {
        "Authorization": f"Bearer {WATI_API_KEY}",
        "Content-Type": "application/json",
    }
    # assignOperator takes email + whatsappNumber as URL QUERY params, the
    # same query-string convention sendSessionMessage uses (not a JSON body).
    params = {"email": WATI_BOT_OPERATOR_EMAIL, "whatsappNumber": wa_id}

    print(f"[REASSIGN] Re-pointing {wa_id} -> operator {WATI_BOT_OPERATOR_EMAIL!r}")

    try:
        response = requests.post(
            url,
            headers=headers,
            params=params,
            json={},
            timeout=WATI_TIMEOUT_SECONDS,
        )

        # WATI returns HTTP 200 even on logical failures — the real outcome
        # is in the JSON body as {"result": true|false, "info": ...}, same
        # as send_whatsapp_reply. Log the body so failures are visible.
        body_preview = (response.text or "(empty body)")[:1000]
        print(f"[REASSIGN] HTTP {response.status_code}")
        print(f"[REASSIGN] Response body: {body_preview}")

        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict) and data.get("result") is False:
            info = data.get("info") or data.get("message") or "(no detail)"
            print(f"[REASSIGN] WATI rejected the reassign: {info}")
        elif response.ok:
            print(f"[REASSIGN] Chat for {wa_id} reassigned back to Bot")
        else:
            print(f"[REASSIGN] HTTP failure {response.status_code}")
    except requests.RequestException as e:
        print(f"[REASSIGN] Network error: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[REASSIGN] Unexpected error: {type(e).__name__}: {e}")


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


def _strip_html(raw: str) -> str:
    """Reduce an HTML fragment to compact plain text: drop script/style,
    strip tags, unescape entities, collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _html_to_blocks(raw: str) -> list[str]:
    """Split an HTML fragment into plain-text blocks along block-level
    boundaries (</p>, </li>, headings, <br>, <hr>). Used for chunking
    product descriptions and policy pages — keeps semantically-related
    sentences together instead of cutting mid-thought."""
    if not raw:
        return []
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    s = re.sub(r"</(p|li|h[1-6]|div|tr|ul|ol)>|<br\s*/?>|<hr\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    blocks = [re.sub(r"\s+", " ", b).strip() for b in s.split("\n")]
    return [b for b in blocks if b]


# ----- Live policy pages (Phase 0 quick win) -----
#
# The published Shopify policy pages are the single source of truth for
# returns and shipping — injecting them alongside brain.md means a policy
# edit in Shopify Admin reaches the bot within one cache interval, and a
# brain.md-vs-store contradiction (like the v1.8 "no returns" incident)
# can't silently persist. Cached with the same TTL as brain.md.
_POLICY_PROMPT_PAGES = (
    ("Return & Refund Policy", "https://glamshelf.in/policies/refund-policy"),
    ("Shipping Policy", "https://glamshelf.in/policies/shipping-policy"),
)
_policy_cache: dict = {"text": "", "fetched_at": 0.0}


def _fetch_policy_page_html(url: str) -> str:
    """Fetch one Shopify policy page and return the policy body HTML
    (the shopify-policy__body div — clean policy text without theme
    chrome), or the whole page HTML as fallback. "" on any failure."""
    try:
        resp = requests.get(url, timeout=SHOPIFY_TIMEOUT_SECONDS)
        if not resp.ok:
            print(f"[POLICY] HTTP {resp.status_code} for {url}")
            return ""
        m = re.search(
            r'<div[^>]*class="[^"]*shopify-policy__body[^"]*"[^>]*>(.*?)</div>',
            resp.text, re.S,
        )
        return m.group(1) if m else resp.text
    except Exception as e:
        print(f"[POLICY] Fetch failed for {url}: {type(e).__name__}: {e}")
        return ""


def get_live_policies() -> str:
    """Plain-text block of the live published refund + shipping policies,
    for the system prompt. Cached for BRAIN_CACHE_TTL_SECONDS (same
    interval as brain.md). On fetch failure, serves the last good copy
    (even past TTL) rather than dropping the block; returns "" only when
    no copy has ever been fetched — callers treat "" as a no-op."""
    now = time.time()
    age = now - _policy_cache["fetched_at"]
    if _policy_cache["text"] and age < BRAIN_CACHE_TTL_SECONDS:
        return _policy_cache["text"]

    sections = []
    for name, url in _POLICY_PROMPT_PAGES:
        text = _strip_html(_fetch_policy_page_html(url))
        if text:
            sections.append(f"== {name} (live page) ==\n{text}")

    if not sections:
        if _policy_cache["text"]:
            print("[POLICY] Refresh failed — serving last cached policy text")
        return _policy_cache["text"]

    block = (
        "[LIVE STORE POLICIES - published at glamshelf.in]\n"
        "The following is the store's live published policy text, for factual "
        "reference when answering policy questions. It does not override any "
        "classification, escalation, or Never-list rule above.\n\n"
        + "\n\n".join(sections)
    )
    _policy_cache["text"] = block
    _policy_cache["fetched_at"] = now
    print(f"[POLICY] Refreshed live policy block ({len(block)} chars)")
    return block


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
        # Enrich each line with SKU and a ~200-char slice of the real
        # product description (body_html was previously fetched and
        # discarded) so the model answers detail questions from actual
        # product copy instead of brain.md's generic claims.
        parts = [f"{title}: {'IN STOCK' if available else 'SOLD OUT'}"]
        sku = (variants[0].get("sku") or "").strip()
        if sku:
            parts.append(f"SKU {sku}")
        desc = _strip_html(p.get("body_html") or "")[:200].strip()
        if desc:
            parts.append(desc)
        lines.append(" | ".join(parts))

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



# ===== RAG retrieval layer (Point A — audit 2026-07-05 §4/§7) =====
#
# Corpus: product descriptions (Shopify body_html, chunked by block) +
# 3 policy pages (returns / shipping / terms, chunked by section).
# Explicitly excluded: conversation logs, order records, anything
# customer-identified. Stock status and price are never embedded — those
# stay live API data (audit 5.2/5.3).
#
# Storage: rag_chunks (+ rag_vec vec0 virtual table when the sqlite-vec
# extension loads) in the SAME SQLite file as everything else (DB_PATH),
# so index persistence rides on the persistent-disk decision like all
# other state. If sqlite-vec can't load, retrieval falls back to a
# brute-force numpy scan over the stored embeddings — at ~40 chunks the
# difference is microseconds.
#
# Failure posture (audit 5.5): every entry point is wrapped; a missing
# model, missing index, or any exception degrades to brain.md-only
# behavior. Retrieval can enrich a reply; it must never break a webhook.
#
# Embeddings run locally via fastembed (Qdrant's ONNX Runtime port of
# all-MiniLM-L6-v2, quantized): no torch (the original torch build
# measured 481MB RSS — over the 512MB Render Starter budget), no API
# key, no per-call cost, no network dependency at query time (the
# OpenAI-API attempt died on unfunded billing). Third and intended-final
# embedding backend. The ~83MB model downloads once into
# RAG_MODEL_CACHE_DIR — kept next to the DB so a persistent-disk
# DB_PATH also makes the model survive Render redeploys (no re-download
# on boot).
RAG_EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # fastembed's quantized ONNX build (v0.8.0 name — the old AllMiniLML6V2Q enum is gone)
RAG_EMBED_DIM = 384
RAG_MODEL_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "fastembed_cache")
RAG_TOP_K = 2
RAG_CANDIDATES = 8                 # over-fetch, then apply the product filter
RAG_MAX_CONTEXT_CHARS = 2000       # ≈500 tokens (audit 5.4 budget)
RAG_CHUNK_MAX_CHARS = 500
RAG_REINDEX_INTERVAL_SECONDS = 60 * 60   # hourly fallback re-index

_RAG_POLICY_SOURCES = (
    ("Return & Refund Policy", "https://glamshelf.in/policies/refund-policy"),
    ("Shipping Policy", "https://glamshelf.in/policies/shipping-policy"),
    ("Terms of Service", "https://glamshelf.in/policies/terms-of-service"),
)

_rag_embedder = None  # fastembed TextEmbedding, loaded once at startup; None = retrieval disabled

# Gate: retrieval only fires when the message plausibly overlaps the
# product/policy corpus. A miss is a hard no-op (no embedding cost).
_RAG_TRIGGER_RE = re.compile(
    r"\b(gs ?1|gs ?2|gs ?3|kawaii|clean girl|mink|duo|trio|tray(s)?|lash(es)?|"
    r"half ?lash(es)?|band|glue|wear(s)?|reusab\w*|material(s)?|fiber(s)?|"
    r"colou?r(s)?|black|brown|shade(s)?|"
    r"return(s|ed)?|refund(s|ed)?|exchange(s|d)?|ship(ping|ped|s)?|"
    r"deliver(y|ies|ed)?|polic(y|ies)|cancel(led|lation)?|cod|payment(s)?|"
    r"track(ing)?|courier|damaged?|broken|replace(ment)?|warranty)\b",
    re.IGNORECASE,
)

# Specific-product detection for the audit 5.3 wrong-SKU guard: when the
# customer names a product, product-sourced chunks are restricted to that
# product (policy chunks always allowed).
_RAG_NAMED_PRODUCT_TERMS = (
    ("gs1", "gs1"), ("gs 1", "gs1"),
    ("gs2", "gs2"), ("gs 2", "gs2"),
    ("gs3", "gs3"), ("gs 3", "gs3"),
    ("half lash", "gs3"),
    ("kawaii", "kawaii"),
    ("clean girl", "clean-girl"),
)


def _rag_clean_broken_model_cache() -> None:
    """Remove INCOMPLETE HuggingFace-layout model dirs from the fastembed
    cache. On hosts without symlink support (Windows without Developer
    Mode — Udit's machine), the HF fetch dies mid-download and leaves a
    partial models--* dir. fastembed then treats that dir as a valid
    cache on the next boot and fails with "Could not find
    tokenizer_config.json" — permanently, on every boot, even though a
    complete tarball-extracted copy (fast-*) sits right next to it.
    Deleting the broken dir lets the loader fall through to the good
    copy or re-download cleanly. Dirs with a complete snapshot (the
    normal case on Linux/Render) are left alone.
    """
    try:
        if not os.path.isdir(RAG_MODEL_CACHE_DIR):
            return
        for name in os.listdir(RAG_MODEL_CACHE_DIR):
            if not name.startswith("models--"):
                continue
            model_dir = os.path.join(RAG_MODEL_CACHE_DIR, name)
            snap_root = os.path.join(model_dir, "snapshots")
            complete = False
            if os.path.isdir(snap_root):
                for snap in os.listdir(snap_root):
                    snap_dir = os.path.join(snap_root, snap)
                    if all(
                        os.path.exists(os.path.join(snap_dir, f))
                        for f in ("model.onnx", "tokenizer_config.json", "config.json")
                    ):
                        complete = True
                        break
            if not complete:
                shutil.rmtree(model_dir, ignore_errors=True)
                shutil.rmtree(os.path.join(RAG_MODEL_CACHE_DIR, ".locks"), ignore_errors=True)
                print(f"[RAG] Removed incomplete model cache dir {name} (interrupted download)")
    except Exception as e:
        print(f"[RAG] Model cache cleanup skipped: {type(e).__name__}: {e}")


def _rag_load_model() -> None:
    """Load the fastembed model once at startup (NOT lazily on first
    request — audit 5.5's cold-start rule). First-ever load downloads
    ~83MB into RAG_MODEL_CACHE_DIR; subsequent loads read from the cache.

    Two attempts with a broken-cache cleanup before each: an interrupted
    HF download poisons the cache dir in a way that otherwise fails
    every future boot (see _rag_clean_broken_model_cache). On final
    failure _rag_embedder stays None and retrieval is disabled for the
    process lifetime; webhooks run exactly as before the RAG layer."""
    global _rag_embedder
    try:
        from fastembed import TextEmbedding
    except Exception as e:
        _rag_embedder = None
        print(f"[RAG] fastembed not importable ({type(e).__name__}: {e}) — retrieval disabled, brain-only behavior")
        return

    for attempt in (1, 2):
        _rag_clean_broken_model_cache()
        try:
            t0 = time.time()
            _rag_embedder = TextEmbedding(
                model_name=RAG_EMBED_MODEL_NAME,
                cache_dir=RAG_MODEL_CACHE_DIR,
            )
            print(
                f"[RAG] fastembed model loaded in {time.time() - t0:.1f}s "
                f"({RAG_EMBED_MODEL_NAME}, {RAG_EMBED_DIM}d, cache={RAG_MODEL_CACHE_DIR})"
            )
            return
        except Exception as e:
            print(f"[RAG] fastembed load attempt {attempt} failed: {type(e).__name__}: {e}")

    _rag_embedder = None
    print("[RAG] fastembed model unavailable after retry — retrieval disabled, brain-only behavior")


def _rag_db() -> tuple:
    """Open a DB connection and try to load sqlite-vec into it.
    Returns (conn, vec_loaded)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn, True
    except Exception:
        return conn, False


def _rag_embed(texts: list[str]):
    """float32 embedding matrix from local fastembed inference, or None
    on ANY failure — callers treat None as "skip retrieval / keep
    existing index". No network access, no API key, no per-call cost.

    Embeddings are explicitly re-normalized (fastembed's MiniLM output
    is already unit-length, but the guarantee is what makes vec0's
    default L2 ranking equivalent to cosine ranking — cheap insurance
    against a library version changing postprocessing).
    """
    if _rag_embedder is None or not texts:
        return None
    try:
        import numpy as np
        # batch_size=8: onnxruntime's memory arena grows with the largest
        # batch it has ever run. Embedding the whole 46-chunk corpus in
        # one batch measurably balloons peak RSS — a real constraint on
        # the 512MB Render Starter instance. Small batches keep the
        # arena small; per-query cost is unaffected (queries are 1 text).
        vecs = np.asarray(list(_rag_embedder.embed(texts, batch_size=8)), dtype=np.float32)
        if vecs.shape != (len(texts), RAG_EMBED_DIM):
            print(f"[RAG] Unexpected embedding shape {vecs.shape} — skipping")
            return None
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms
    except Exception as e:
        print(f"[RAG] Embedding failed: {type(e).__name__}: {e}")
        return None


def _rag_merge_blocks(blocks: list[str], max_chars: int = RAG_CHUNK_MAX_CHARS) -> list[str]:
    """Merge text blocks into chunks of at most max_chars, never splitting
    a block unless it alone exceeds the cap."""
    chunks: list[str] = []
    current = ""
    for b in blocks:
        if len(b) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(b), max_chars):
                chunks.append(b[i:i + max_chars])
            continue
        if current and len(current) + len(b) + 1 > max_chars:
            chunks.append(current)
            current = b
        else:
            current = f"{current} {b}".strip()
    if current:
        chunks.append(current)
    return chunks


def _rag_build_corpus() -> list[tuple[str, str, str]]:
    """Assemble the (source, title, content) chunk list from live Shopify
    data. Product/policy text ONLY — no conversation logs, no orders, no
    customer identifiers, no stock status, no prices-as-text."""
    chunks: list[tuple[str, str, str]] = []

    try:
        resp = requests.get(
            SHOPIFY_PRODUCTS_URL,
            params={"limit": SHOPIFY_PRODUCTS_LIMIT},
            timeout=SHOPIFY_TIMEOUT_SECONDS,
        )
        products = (resp.json() or {}).get("products") or [] if resp.ok else []
    except Exception as e:
        print(f"[RAG] Product fetch failed: {type(e).__name__}: {e}")
        products = []

    for p in products:
        title = (p.get("title") or "").strip()
        handle = (p.get("handle") or "").strip()
        blocks = _html_to_blocks(p.get("body_html") or "")
        for c in _rag_merge_blocks(blocks):
            chunks.append((f"product:{handle}", title, c))

    for name, url in _RAG_POLICY_SOURCES:
        blocks = _html_to_blocks(_fetch_policy_page_html(url))
        for c in _rag_merge_blocks(blocks):
            chunks.append((f"policy:{name}", name, c))

    return chunks


def _rag_reindex(reason: str = "manual") -> int:
    """Rebuild the vector index from live product + policy data. Returns
    the chunk count (0 = failed or skipped). A failed fetch never wipes
    the existing index — stale beats empty."""
    if _rag_embedder is None:
        print("[RAG] Reindex skipped — no embedding model")
        return 0

    chunks = _rag_build_corpus()
    if not chunks:
        print("[RAG] Reindex aborted — corpus fetch returned nothing (keeping existing index)")
        return 0

    embs = _rag_embed([c[2] for c in chunks])
    if embs is None:
        print("[RAG] Reindex aborted — embedding failed (keeping existing index)")
        return 0

    try:
        import numpy as np
        conn, vec_loaded = _rag_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )
        if vec_loaded:
            # DROP + recreate (not DELETE): the embedding dimension is
            # baked into the vec0 table definition, so an embedding-model
            # change (e.g. the 384d MiniLM → 1536d OpenAI swap) needs a
            # fresh virtual table. Vector spaces can't be mixed.
            conn.execute("DROP TABLE IF EXISTS rag_vec")
            conn.execute(
                f"CREATE VIRTUAL TABLE rag_vec USING vec0(embedding float[{embs.shape[1]}])"
            )
        conn.execute("DELETE FROM rag_chunks")
        for i, ((source, title, content), emb) in enumerate(zip(chunks, embs), start=1):
            blob = np.asarray(emb, dtype=np.float32).tobytes()
            conn.execute(
                "INSERT INTO rag_chunks (id, source, title, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (i, source, title, content, blob),
            )
            if vec_loaded:
                conn.execute(
                    "INSERT INTO rag_vec (rowid, embedding) VALUES (?, ?)", (i, blob)
                )
        # Record which embedder built this index. Dimension alone can't
        # tell two 384d models apart (torch MiniLM vs fastembed's
        # quantized ONNX MiniLM produce different vectors) — the startup
        # check compares this name and rebuilds on mismatch so corpus
        # and queries are always embedded by the same model.
        conn.execute("CREATE TABLE IF NOT EXISTS rag_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO rag_meta (key, value) VALUES ('embed_model', ?)",
            (RAG_EMBED_MODEL_NAME,),
        )
        conn.commit()
        conn.close()
        print(f"[RAG] Reindexed {len(chunks)} chunks ({reason}; sqlite-vec={'yes' if vec_loaded else 'no, brute-force fallback'})")
        return len(chunks)
    except Exception as e:
        print(f"[RAG] Reindex failed: {type(e).__name__}: {e}")
        return 0


def _rag_reindex_async(reason: str) -> None:
    """Fire a reindex on a daemon thread so webhook handlers return fast."""
    threading.Thread(target=_rag_reindex, args=(reason,), daemon=True).start()


def _start_rag_reindex_loop() -> None:
    """Startup: index immediately if the table is empty, unreachable, or
    was built under a different embedding dimension (a model swap makes
    old vectors unusable — better an eager rebuild than every query
    failing the vec0 dimension check). Then re-index hourly as the
    fallback for missed Shopify webhooks."""
    def loop():
        count, dim, meta_model = 0, None, None
        try:
            conn = sqlite3.connect(DB_PATH)
            count = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
            row = conn.execute("SELECT embedding FROM rag_chunks LIMIT 1").fetchone()
            try:
                m = conn.execute(
                    "SELECT value FROM rag_meta WHERE key = 'embed_model'"
                ).fetchone()
                meta_model = m[0] if m else None
            except Exception:
                meta_model = None  # pre-meta index (built before this check existed)
            conn.close()
            if row and row[0]:
                dim = len(row[0]) // 4  # float32 blob → dimension
        except Exception:
            count = 0
        if count == 0:
            _rag_reindex("startup")
        elif dim != RAG_EMBED_DIM:
            print(f"[RAG] Index dimension {dim} != expected {RAG_EMBED_DIM} (embedding model changed) — rebuilding")
            _rag_reindex("startup-dimension-change")
        elif meta_model != RAG_EMBED_MODEL_NAME:
            # Same dimension, different (or unrecorded) embedder — vectors
            # aren't comparable across models even at equal size.
            print(f"[RAG] Index built by {meta_model!r}, expected {RAG_EMBED_MODEL_NAME!r} — rebuilding")
            _rag_reindex("startup-model-change")
        else:
            print(f"[RAG] Existing index found ({count} chunks, {dim}d, {meta_model}) — hourly refresh scheduled")
        while True:
            time.sleep(RAG_REINDEX_INTERVAL_SECONDS)
            _rag_reindex("hourly")

    t = threading.Thread(target=loop, daemon=True)
    t.start()


def _rag_named_products(message: str) -> set[str]:
    low = (message or "").lower()
    return {key for term, key in _RAG_NAMED_PRODUCT_TERMS if term in low}


def _rag_retrieve(message: str) -> str:
    """Point A retrieval: keyword gate → embed query → top-K chunks →
    [RETRIEVED CONTEXT] block. Returns "" (a strict no-op for the caller)
    when the gate misses, the embedding model/index is unavailable, or
    anything throws. Never raises."""
    if _rag_embedder is None or not message:
        return ""
    try:
        if not _RAG_TRIGGER_RE.search(message):
            return ""
        t0 = time.time()
        q = _rag_embed([message])
        if q is None:
            return ""
        import numpy as np
        qv = np.asarray(q[0], dtype=np.float32)

        conn, vec_loaded = _rag_db()
        candidates: list[tuple[str, str, str, float]] = []  # source, title, content, score
        try:
            if vec_loaded:
                rows = conn.execute(
                    "SELECT rowid, distance FROM rag_vec WHERE embedding MATCH ? AND k = ?",
                    (qv.tobytes(), RAG_CANDIDATES),
                ).fetchall()
                for rowid, dist in rows:
                    r = conn.execute(
                        "SELECT source, title, content FROM rag_chunks WHERE id = ?",
                        (rowid,),
                    ).fetchone()
                    if r:
                        candidates.append((r[0], r[1], r[2], -float(dist)))
            else:
                rows = conn.execute(
                    "SELECT source, title, content, embedding FROM rag_chunks"
                ).fetchall()
                scored = [
                    (s, t, c, float(np.dot(qv, np.frombuffer(b, dtype=np.float32))))
                    for s, t, c, b in rows
                ]
                scored.sort(key=lambda x: x[3], reverse=True)
                candidates = scored[:RAG_CANDIDATES]
        finally:
            conn.close()

        if not candidates:
            return ""

        # Wrong-SKU guard (audit 5.3): a named product restricts
        # product-sourced chunks to that product; policy chunks pass.
        named = _rag_named_products(message)
        if named:
            candidates = [
                c for c in candidates
                if c[0].startswith("policy:") or any(n in c[0] for n in named)
            ]

        picked, total = [], 0
        for source, title, content, _score in candidates:
            if len(picked) >= RAG_TOP_K:
                break
            if total + len(content) > RAG_MAX_CONTEXT_CHARS:
                continue
            picked.append(f"• ({title}) {content}")
            total += len(content)

        if not picked:
            return ""

        elapsed_ms = int((time.time() - t0) * 1000)
        print(f"[RAG] Retrieved {len(picked)} chunk(s) in {elapsed_ms}ms")
        return (
            "[RETRIEVED CONTEXT]\n"
            "The following is supplementary factual information about products "
            "or policies. It does not override any classification, escalation, "
            "or Never-list rule above.\n\n"
            + "\n\n".join(picked)
        )
    except Exception as e:
        print(f"[RAG] Retrieval failed (falling back to brain-only): {type(e).__name__}: {e}")
        return ""


# RAG layer startup — runs at import (same pattern as _init_db above, but
# placed here because it needs the definitions in this section). Model
# load is synchronous and up-front (audit 5.5: no lazy first-request
# latency spike); indexing runs on a daemon thread so a slow Shopify
# fetch never delays boot. Both degrade gracefully: model unavailable →
# retrieval disabled, brain-only behavior.
_rag_load_model()
_start_rag_reindex_loop()

# Warm the live-policy cache at startup so the first webhook doesn't pay
# the two page fetches. Failure is fine — get_live_policies retries on
# the next call and returns "" (no-op) until a fetch succeeds.
get_live_policies()


# ===== Deterministic escalation pre-filter =====
#
# Explicit, unambiguous high-risk phrases must hit ESCALATE every time —
# not "usually, depending on how the LLM reads it this call". This filter
# runs alongside the LLM classification and can only UPGRADE the result to
# ESCALATE; it never downgrades an LLM decision. The phrase list is
# deliberately short and founder-confirmed — do not extend it with soft
# signals (tone, sentiment, non-Hindi anger); those stay with the LLM.
#
# Rollback: set ESCALATION_PREFILTER_DISABLED=1 in the environment and
# restart. Do not edit brain.md to compensate for filter behavior.
#
# Word boundaries matter: \bpolice\b must not fire on "policy" — a return
# policy question is one of the most common AUTO messages the twin sees.
_ESCALATION_PREFILTER_PATTERNS = re.compile(
    r"\b("
    r"lawyer"
    r"|consumer\s+court"
    r"|legal\s+notice"
    r"|police"
    r"|refund\s+karo"        # imperative refund demand (Hinglish). Variants pending Udit's confirmed list — do not add unconfirmed spellings.
    r"|post\s+(?:this\s+|it\s+)?on\s+social\s+media"
    r"|post\s+(?:this|it)\s+online"
    r")\b",
    re.IGNORECASE,
)


def _escalation_prefilter_hit(message: str) -> str | None:
    """Return the matched high-risk phrase, or None.

    Returns None unconditionally when ESCALATION_PREFILTER_DISABLED is
    set (rollback switch — read per call so a Render env change takes
    effect on restart without code edits).
    """
    if (os.environ.get("ESCALATION_PREFILTER_DISABLED") or "").strip().lower() in ("1", "true", "yes"):
        return None
    m = _ESCALATION_PREFILTER_PATTERNS.search(message or "")
    return m.group(0) if m else None


def _bulk_commit_prefilter_hit(message: str) -> int | None:
    """Return the tray quantity if the message is a deterministic bulk-commit
    that must ESCALATE per resolve_pricing_action(), else None.

    Rate-only asks never fire here — pricing_rules resolves those to AUTO
    (brain.md v1.10: the Hard Money Threshold covers commitments only, so an
    implied quote value >₹1,500 is not an escalation ground). Shares the
    ESCALATION_PREFILTER_DISABLED rollback switch with the phrase filter.
    """
    if (os.environ.get("ESCALATION_PREFILTER_DISABLED") or "").strip().lower() in ("1", "true", "yes"):
        return None
    qty = detect_bulk_commit_quantity(message)
    if qty is None:
        return None
    # Implied amount is only known for bulk quantities (tray rate applies);
    # below 20 the product mix is unknown, so pass None rather than guess.
    amount = qty * BULK_RATE_INR if qty >= BULK_MIN_TRAYS else None
    action = resolve_pricing_action(quantity=qty, amount=amount, intent=INTENT_COMMIT)
    return qty if action == ACTION_ESCALATE else None


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
    # get_live_inventory() doc for details. The 5-minute cache there means
    # the system prompt's content changes at most ~once per 5 minutes.
    # (DeepSeek does its own automatic server-side context caching; there's
    # no client-side cache directive to manage.)
    live_stock = get_live_inventory()
    if live_stock:
        brain = live_stock + "\n\n" + brain

    # Live published policy pages (Phase 0 quick win) — appended AFTER
    # brain.md so classification/escalation rules keep prompt priority;
    # the block is factual reference only. Empty string (never fetched
    # successfully) degrades to the pre-Phase-0 prompt unchanged.
    live_policies = get_live_policies()
    if live_policies:
        brain = brain + "\n\n" + live_policies

    # RAG retrieval (Point A) — supplementary product/policy chunks,
    # appended LAST so brain rules and policies keep priority. Empty
    # retrieval (gate not hit, no model, no index, any error) is a no-op.
    retrieved = _rag_retrieve(message)
    if retrieved:
        brain = brain + "\n\n" + retrieved

    # Deterministic pre-filter — evaluated on the raw customer message
    # before the LLM call so the outcome can't depend on the model's
    # judgment. The LLM still runs (we want its drafted reply for the
    # Telegram notification); the filter only pins the classification.
    prefilter_phrase = _escalation_prefilter_hit(message)
    bulk_commit_qty = _bulk_commit_prefilter_hit(message)

    raw = ask_claude(brain, message, order_context, history=history, source=source)

    classification = ""
    reply = ""
    try:
        parsed = json.loads(raw)
        classification = (parsed.get("classification") or "").strip()
        reply = (parsed.get("reply") or "").strip()
    except json.JSONDecodeError:
        print("[TWIN] Claude's response wasn't valid JSON — leaving classification/reply empty")


    # Upgrade-only override: a matched high-risk phrase forces ESCALATE
    # regardless of what the LLM decided. Never the other direction.
    if prefilter_phrase and classification != "ESCALATE":
        print(
            f"[PREFILTER] Forcing ESCALATE (was {classification or 'unparsed'!r}) — "
            f"matched high-risk phrase {prefilter_phrase!r}"
        )
        classification = "ESCALATE"

    if bulk_commit_qty is not None and classification != "ESCALATE":
        print(
            f"[PREFILTER] Forcing ESCALATE (was {classification or 'unparsed'!r}) — "
            f"bulk commit signal for {bulk_commit_qty} trays "
            f"(Rule 3b-i / Hard Money Threshold, resolve_pricing_action)"
        )
        classification = "ESCALATE"

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
    message — see ask_claude().

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
    # Vision stays on Claude (claude_client) — DeepSeek's deepseek-chat is
    # text-only. Small payload: image + short prompt, no brain, 512 tokens.
    raw = ""  # so it's defined for the except branch below
    try:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        message = claude_client.messages.create(
            model=CLAUDE_MODEL,
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
        usage = message.usage
        print(
            f"[VISION] Claude extraction done "
            f"(input: {usage.input_tokens}, output: {usage.output_tokens} tokens)"
        )
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
        print(f"[VISION] Vision error: {type(e).__name__}: {e}")
        return None


def ask_claude(
    brain: str,
    customer_message: str,
    order_context: str,
    history: list[dict] | None = None,
    source: str = "WhatsApp",
) -> str:
    """Call the DeepSeek chat-completions API (OpenAI-compatible SDK).

    The brain content is sent as the first `system` message. DeepSeek
    applies its own automatic server-side context caching (no client-side
    cache directive needed), so a repeated identical brain prefix is billed
    at the cheaper cache-hit rate. The customer message and order context
    go in the user prompt.

    `history`, when provided, is a list of {ts, msg_text, reply_text} dicts
    representing prior exchanges with the same customer (oldest first).
    Each entry becomes a user/assistant pair preceding the current
    user message, so the model treats the request as a real multi-turn
    conversation rather than a one-shot question.

    `source` labels the channel ("WhatsApp" by default, "Instagram DM"
    for IG webhook calls). Surfaced in the per-request user prompt
    header; doesn't affect the system prompt.
    """
    user_text = build_user_message(customer_message, order_context, source=source)
    print(
        f"[LLM] Calling {DEEPSEEK_MODEL} "
        f"(brain: {len(brain)} chars, user: {len(user_text)} chars, "
        f"history: {len(history) if history else 0} turns, source: {source})"
    )

    # Build the messages list. The brain is the first system message; when
    # history is non-empty, prior exchanges are interleaved as alternating
    # user/assistant turns BEFORE the current task-shaped user message.
    # Empty / None history → single-turn (system + one user message).
    all_messages: list[dict] = [{"role": "system", "content": brain}]
    if history:
        for turn in history:
            all_messages.append({"role": "user", "content": turn["msg_text"]})
            all_messages.append({"role": "assistant", "content": turn["reply_text"]})
    all_messages.append({"role": "user", "content": user_text})

    message = deepseek_client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        max_tokens=MAX_TOKENS,
        messages=all_messages,
    )

    raw = (message.choices[0].message.content or "").strip()

    usage = message.usage
    print(
        f"[LLM] Got {len(raw)} chars back. "
        f"Tokens — input: {usage.prompt_tokens}, "
        f"output: {usage.completion_tokens}"
    )

    preview = raw[:300] + ("..." if len(raw) > 300 else "")
    print(f"[LLM] Raw response preview:\n        {preview}")

    cleaned, was_fenced = strip_markdown_fences(raw)
    if was_fenced:
        print("[LLM] NOTE: markdown code fences were detected and stripped")
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
        "model": DEEPSEEK_MODEL,
        "vision_model": CLAUDE_MODEL,
        "deepseek_api_key_set": bool(os.environ.get("DEEPSEEK_API_KEY", "")),
        "claude_vision_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "")),
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

        # SUPPLEMENTARY SAFETY NET (WATI API scan) — the two checks above
        # rely on local state: the in-memory pause register and a HUMAN_UDIT
        # row written by the outbound webhook. Both miss the case where Udit
        # replied from the WATI dashboard but WATI's outbound webhook never
        # reached us (no HUMAN_UDIT row) and the worker has since restarted
        # (empty pause register). That's the May-13 failure. So before
        # spending a Claude call, ask WATI directly whether a human has
        # replied more recently than the bot. Only reached when NOT paused
        # (gate above already returned), so the ~0.5-1s GET is paid at most
        # once per customer per 4h window — on a hit we write the durable
        # HUMAN_UDIT row + pause so every later inbound short-circuits cheaply.
        if _check_recent_human_reply(wa_id):
            print(f"[HUMAN_UDIT] WATI scan found a manual reply for {wa_id} — pausing + skipping auto-reply")
            _log_message(
                wa_id, sender_name, text_body or "[image]",
                status="HUMAN_UDIT",
                reply_text="(human reply detected via WATI getMessages scan)",
            )
            _pause_number(wa_id)
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
        # Best-effort — failures inside _load_wati_history return [] and we
        # fall through to a single-turn call.
        history = _load_wati_history(wa_id)

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
            # Same false-success guard as the Instagram AUTO branch: WATI
            # failures (including result=false on HTTP 200) must not log
            # "Replied".
            sent, send_err = send_whatsapp_reply(wa_id, reply)
            if sent:
                print(f"[AUTO] Replied to {wa_id}")
            else:
                print(f"[AUTO] Send FAILED to {wa_id}: {send_err}")
            elapsed_ms = int((time.time() - t_start) * 1000)
            _log_message(
                wa_id, sender_name, text_body,
                status="AUTO", reply_text=reply, latency_ms=elapsed_ms,
            )
        elif classification == "DRAFT+APPROVE":
            # New buttoned approval flow: Telegram message with
            # ✅ Send as-is / ✏️ Edit / ⛔ Skip inline buttons. State is
            # registered in the pending_drafts table; the actual customer reply
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
            # before (this uses the same paused_senders register), and
            # the HUMAN_UDIT safety net is a separate, additive check
            # that also short-circuits inbound when Udit replies via WATI.
            _pause_number(wa_id)
            print(f"[ESCALATE] Auto-paused {wa_id} for 4h after holding reply sent")
            # Udit is about to take this conversation over by hand in WATI,
            # which will reassign the ticket to him and kill automation. Pin
            # it to the Bot now so the webhook stays alive for this customer;
            # the 4h pause above is what keeps the twin quiet meanwhile.
            _reassign_to_bot(wa_id)
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
        sent, send_err = send_whatsapp_reply(customer_number, message)
        _scheduled_reviews.pop(order_id, None)
        if sent:
            print(f"[REVIEW] Sent review request for order {order_number} to {customer_number}")
        else:
            print(f"[REVIEW] Send FAILED for order {order_number} to {customer_number}: {send_err}")
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
            sent, send_err = send_whatsapp_reply(wa_id, tracking_message)
            _mark_shipping_sent(order_id, "tracking", phone=wa_id, order_number=order_number)
            if sent:
                print(f"[SHIPPING] Sent tracking link for order #{order_number} to {wa_id}")
            else:
                print(f"[SHIPPING] Tracking link send FAILED for order #{order_number} to {wa_id}: {send_err}")

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


@app.route("/shopify-product-webhook", methods=["POST"])
def shopify_product_webhook():
    """Receive Shopify products/create, products/update, products/delete
    webhooks and refresh the RAG corpus so product-description chunks
    track Shopify edits within seconds instead of the hourly fallback.

    Auth: same SHOPIFY_WEBHOOK_SECRET / HMAC scheme as /shopify-webhook.
    The reindex runs on a daemon thread — Shopify gets its 200 back
    immediately (it retries and eventually disables slow webhooks).
    Payload body is ignored: any product change rebuilds the whole
    ~40-chunk corpus, which is cheaper than diffing.
    """
    raw_body = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    topic = (request.headers.get("X-Shopify-Topic") or "").strip().lower()

    if not _verify_shopify_hmac(raw_body, hmac_header):
        print("[RAG] Product webhook: invalid HMAC signature")
        return jsonify({"error": "invalid signature"}), 401

    if topic.startswith("products/"):
        _rag_reindex_async(f"webhook:{topic}")
        print(f"[RAG] Product webhook {topic} — reindex scheduled")
    else:
        print(f"[RAG] Product webhook ignored — unexpected topic {topic!r}")
    return jsonify({"status": "ok"}), 200


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
        text = (message.get("text") or "").strip()

        # ===== ORDER MATTERS — see below =====
        #
        # 1) PAGE-ID CHECK FIRST (HUMAN_UDIT_IG detection).
        #
        # Messages sent BY the page (sender.id == PAGE_ID) arrive as echoes
        # (is_echo == true) for BOTH:
        #   (a) the bot's own Send API replies, AND
        #   (b) Udit's manual replies typed in the IG app / Business Suite.
        # Meta provides no structural field to tell (a) from (b), so we match
        # the echoed text against recently-sent bot replies (registered in
        # _send_instagram_reply via _record_bot_outbound).
        #
        # This MUST run BEFORE the generic is_echo drop below — otherwise
        # Udit's manual replies (which are also echoes) would be swallowed by
        # the is_echo return and HUMAN_UDIT_IG would never fire. That was the
        # ordering bug this block fixes.
        if INSTAGRAM_PAGE_ID and sender_id == INSTAGRAM_PAGE_ID:
            if _is_bot_outbound(text):
                # The bot's own reply echoing back — already handled on send.
                # Skipping here is what KEEPS the IG AUTO flow working: without
                # this match, every bot reply's echo would be tagged
                # HUMAN_UDIT and auto-pause the customer for 4h.
                return
            # Not a recent bot send → Udit replied manually from the IG app.
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

        # 2) ECHO CHECK SECOND — any other page echo (e.g. PAGE_ID unset, or
        # an echo whose sender we couldn't match) is dropped silently. The
        # bot's own sends from the page-id branch above already returned;
        # this is the fallback for echoes not covered there.
        if message.get("is_echo"):
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

        # Pause gate — same paused_senders register the WATI flow uses.
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

        if not classification or not reply:
            print(
                f"[INSTAGRAM] Twin returned empty result "
                f"(classification={classification!r}, reply_len={len(reply)}); not sending"
            )
            return

        ig_sender_info = f"Instagram DM — sender {sender_id}"

        # Classification gate — mirrors the WATI /webhook branch. Only
        # AUTO ships a customer-facing reply immediately; DRAFT+APPROVE
        # waits for a Telegram button tap; ESCALATE sends nothing and
        # hands the thread to the founder.
        if classification == "AUTO":
            # Only claim success when the Graph API actually accepted the
            # send — a 400 (e.g. expired token) used to still log
            # "Replied", which hid the July 8 token outage for ~9h.
            sent, send_err = _send_instagram_reply(sender_id, reply)
            _log_instagram(sender_id, text, reply, timestamp)
            if sent:
                print(f"[INSTAGRAM-AUTO] Replied to {sender_id}")
            else:
                print(f"[INSTAGRAM-AUTO] Send FAILED to {sender_id}: {send_err}")

        elif classification == "DRAFT+APPROVE":
            # Same buttoned approval flow WhatsApp uses, keyed on the IG
            # sender_id via the shared pending_drafts table. The customer
            # reply ships from /telegram-callback when Udit taps Send (or
            # completes an Edit) — nothing is sent here. Falls back to a
            # plain-text notification if the buttoned send fails so Udit
            # always gets *some* heads-up about the pending draft.
            sent_with_buttons = send_draft_for_approval(
                customer_number=sender_id,
                customer_name="",
                customer_message=text,
                reply_text=reply,
                channel="Instagram",
                ig_timestamp=timestamp,
            )
            if not sent_with_buttons:
                try:
                    send_telegram_notification(
                        classification, text, reply,
                        sender_info=ig_sender_info, channel="Instagram",
                    )
                except Exception as tg_err:
                    print(
                        f"[INSTAGRAM-TG] Fallback notification failed: "
                        f"{type(tg_err).__name__}: {tg_err}"
                    )
            # Log the pending draft with a NULL reply: _load_instagram_history
            # filters on reply_text IS NOT NULL, so an un-approved draft never
            # appears in conversation context as if the customer received it.
            # The delivered exchange is logged (DRAFT_SENT_IG) on approval.
            _log_instagram(sender_id, text, None, timestamp, source="DRAFT_PENDING_IG")
            print(f"[INSTAGRAM-DRAFT] Notified founder for {sender_id} (buttons={sent_with_buttons})")

        elif classification == "ESCALATE":
            # No customer-facing reply — same as WhatsApp. The founder
            # takes over directly; the notification carries the 🛑 Stop
            # button (customer_id) and the 4h auto-pause keeps the twin
            # quiet on this thread meanwhile. Wrapped in its own try so a
            # Telegram outage doesn't take down the IG flow.
            try:
                send_telegram_notification(
                    classification, text, reply,
                    sender_info=ig_sender_info,
                    channel="Instagram",
                    customer_id=sender_id,
                )
                print(f"[INSTAGRAM-ESCALATE] Notified founder for {sender_id}")
            except Exception as tg_err:
                print(
                    f"[INSTAGRAM-TG] Notification failed: "
                    f"{type(tg_err).__name__}: {tg_err}"
                )
            _pause_number(sender_id)
            # NULL reply keeps this row out of _load_instagram_history —
            # the customer never received anything for this message.
            _log_instagram(sender_id, text, None, timestamp, source="ESCALATE_IG")
            print(f"[ESCALATE] Auto-paused {sender_id} for 4h — no reply sent")

        else:
            print(f"[INSTAGRAM] Unknown classification {classification!r} — no dispatch")
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
    print(f"  Text model:   {DEEPSEEK_MODEL}")
    print(f"  Vision model: {CLAUDE_MODEL}")
    print(f"  Open this in your browser: http://{host}:{port}")
    print("  Press CTRL+C in this terminal to stop the server.")
    print("=" * 60)
    app.run(host=host, port=port, debug=True)
