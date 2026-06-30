# Glam Shelf Twin

AI customer-service twin for **The Glam Shelf** — an Indian D2C false-eyelash brand. Reads customer messages on WhatsApp and Instagram, replies in the brand's voice, and routes the hard ones to the founder via Telegram inline buttons.

## The problem it solves

A solo D2C founder runs marketing, fulfillment, and customer support — but customers expect replies within minutes. Missed DMs cost sales. Generic chatbots can't hold brand voice or distinguish "what's the price?" from "where is my refund?". This twin handles the routine 80% autonomously and escalates the 20% that need human judgment — without the founder ever leaving Telegram.

It also closes the loop on every order: automated shipping notifications, tracking links, delivery confirmations, and a 10-day post-delivery review request.

## Tech stack

- **Language / runtime** — Python 3.14
- **Web framework** — Flask, served by gunicorn on Render
- **LLM** — DeepSeek v3 for text replies + Anthropic Claude (Sonnet 4.6) for vision/image extraction
- **Database** — SQLite, with hourly GitHub-backed snapshots for redeploy persistence
- **WhatsApp** — WATI Business API (session messages within the 24h window, approved HSM template messages outside it)
- **Instagram** — Meta Instagram Graph API (Instagram Login flow)
- **Founder UI** — Telegram Bot API (inline keyboard callbacks for one-tap approvals)
- **Commerce** — Shopify webhooks (`orders/create`, `fulfillments/create`, `fulfillments/update`, `orders/updated`) + public storefront inventory feed
- **Hosting** — Render

## Key features

### Customer-facing

- **WhatsApp + Instagram auto-replies** in a defined brand voice — the 45 KB `brain/brain.md` is loaded as the system prompt on every call, with a local 5-minute in-memory cache to avoid re-reading from disk
- **Three-tier classification** per message: `AUTO` (send as-is), `DRAFT+APPROVE` (founder reviews on Telegram), `ESCALATE` (founder takes over directly)
- **Vision pipeline** — when a customer sends a screenshot, Claude reads it:
  - Order confirmation → extracts order ID + customer details → routes through normal reply flow
  - Eye photo → detects eye shape → recommends a suitable lash style
  - Other / unclear → neutral fallback message
- **Live Shopify inventory injection** — every Claude call sees current stock from the storefront feed; no manual brain updates when products restock or sell out
- **Multi-day conversation memory** — last 30 turns / 7 days. Lets the twin recognize ongoing refund/return flows instead of treating "any update?" as a fresh complaint and re-asking for the order ID

### Shipping + order lifecycle

- **Automated WhatsApp notifications** for: shipped (via WATI template — works outside the 24h session window), tracking link, out-for-delivery, delivered
- **Full Shiprocket status mapping** — handles `fulfillments/create` and `fulfillments/update` across all shipment_status variants (`in_transit`, `out_for_delivery`, `delivered`, `pickup_scheduled`, `pickup_failed`)
- **DB-persisted dedup** — composite primary key on `(order_id, message_type)` guarantees no duplicate "shipped" message even on webhook retries or Render redeploys
- **Archived-order recovery** — `orders/updated` webhook catches the case where a fulfillment was recorded while the order was archived; sends the shipping message when the order is unarchived
- **Post-delivery review request** — 10 days after delivery, an automated WhatsApp nudge for a review

### Founder controls

- **Telegram inline buttons** for `DRAFT+APPROVE` replies — ✅ Send as-is / ✏️ Edit / ⛔ Skip. One-tap approval without leaving Telegram. Edits captured via the next Telegram message; 10-minute timeout falls back to auto-skip.
- **🛑 Stop bot button** on every ESCALATE notification — one tap pauses the twin for that customer for 4 hours
- **Manual `#pause` / `#resume` directives** — type `#pause` outbound in any customer chat from the WATI app to take over; `#resume` releases
- **HUMAN_UDIT detection** on both channels — when the founder replies manually on WhatsApp or Instagram, the twin steps back automatically. Survives Render restarts via a DB-backed safety net.
- **Auto-pause after escalation** — the twin stops replying for 4h after sending a holding message, preventing the "customer gets the same robotic hold three times" failure
- **Live dashboard** — KPIs, conversation log, daily volume, error log, latency stats, all gated by a dashboard key

### Operational

- **Brand voice in markdown** — `brain/brain.md` is the single source of truth for tone, products, pricing, escalation rules, never-list. Version controlled, edits ship as commits.
- **Hourly SQLite → GitHub backup** — all conversations and dedup state survive Render redeploys
- **Atomic dedup primitives** — `dict.pop()` for Telegram callbacks (no double-tap double-send), `INSERT OR IGNORE` for shipping notifications, dedup by msg_id for WhatsApp inbound
- **Diagnostic endpoints** — `/healthz`, `/inventory-debug`, `/review-debug` for ops introspection
- **Fail-fast startup assertions** for auth-critical env vars; no insecure defaults in source

## Architecture

```
                         ┌──────────────────────┐
                         │   brain/brain.md     │  ← brand voice + rules
                         │  (system prompt)     │
                         └──────────┬───────────┘
                                    │
   ┌──────────┐    POST       ┌─────▼─────┐    text    ┌─────────────────┐
   │  WATI    ├──────────────►│           ├───────────►│  DeepSeek v3    │
   │ WhatsApp │               │           │            │  (text replies) │
   └──────────┘               │           │            └─────────────────┘
                              │           │
   ┌──────────┐               │           │   vision   ┌─────────────────┐
   │   Meta   ├──────────────►│   Flask   ├───────────►│  Claude Sonnet  │
   │ Instagram│               │   app     │            │  (image only)   │
   └──────────┘               │           │            └─────────────────┘
                              │           │
   ┌──────────┐               │  Routes:  │◄───────►┌───────────────────┐
   │ Shopify  ├──────────────►│ /webhook  │         │     SQLite        │
   │ webhooks │               │ /shopify-*│         │   logs, orders,   │
   └──────────┘               │ /telegram-│         │   ig_logs,        │
                              │  callback │         │   shipping_       │
   ┌──────────┐               │ /instagram│         │   notifications   │
   │Storefront│               │   etc.    │         └────────┬──────────┘
   │inventory ├──────────────►│           │                  │ hourly
   │  feed    │ (5 min cache) │           │                  ▼
   └──────────┘               └─────┬─────┘         ┌───────────────────┐
                                    │               │ GitHub backup     │
              ┌─────────────────────┼───────────────┤   repository      │
              │                     │               └───────────────────┘
              ▼                     ▼                         ▼
       ┌────────────┐        ┌────────────┐           ┌──────────────┐
       │   WATI     │        │  Meta IG   │           │   Telegram   │
       │  outbound  │        │  outbound  │           │  bot — alerts│
       │ (customer  │        │ (customer  │           │  + inline    │
       │  reply or  │        │  reply or  │           │  buttons for │
       │  template) │        │   holding) │           │  founder     │
       └────────────┘        └────────────┘           └──────────────┘
```

**Inbound message flow** (WhatsApp example):

1. Customer sends a WhatsApp message → WATI forwards to `/webhook`
2. Defense gates in order: message-id dedup → protected-number check → in-memory pause register → DB-backed HUMAN_UDIT check
3. Load conversation history (30 turns / 7 days), recent Shopify order context, live inventory block
4. Call DeepSeek with system prompt + history + current message. Vision calls (order screenshots, eye photos) route to Claude.
5. Parse classification: `AUTO` / `DRAFT+APPROVE` / `ESCALATE`
6. Dispatch:
   - `AUTO` → send to customer via WATI
   - `DRAFT+APPROVE` → Telegram inline-button card; customer waits for founder
   - `ESCALATE` → holding reply (Instagram only) + Telegram notification with 🛑 Stop bot button + auto-pause for 4h

**Image inbound flow** (vision):

1. Customer sends image → WATI webhook fires with `type=image`
2. Download media from WATI (auth header) → send to Claude Vision API with extraction schema
3. Branch on extracted `image_type` (text replies still use DeepSeek):
   - `order_screenshot` with high-confidence order ID → synthesize `"My order ID is #1042…"` → fall through to normal Claude pipeline
   - `eye_photo` with detected shape → synthesize `"…my eye shape looks hooded. Can you recommend a lash?"`
   - Otherwise → deterministic neutral fallback (no Claude call)

**Resilience layers**:

- Webhook idempotency (in-memory + file-cached `msg_id` set)
- DB-persisted shipping-notification dedup (survives restarts)
- HUMAN_UDIT detection on both channels — in-memory pause register (fast) + DB-backed safety net (restart-proof)
- Brain cache (5-minute TTL local, in-memory)
- All webhook handlers always return HTTP 200 to prevent provider retry storms
- HMAC verification on every Shopify webhook (`/shopify-webhook`, `/shopify-fulfillment`, `/shopify-order-update`)
- Founder-chat-id check on every Telegram callback before any state mutation

## Project structure

```
.
├── app.py                                  # Main Flask app — all routes + helpers (~4400 lines, single-file by design)
├── brain/
│   ├── brain.md                            # Production brand voice + decision rules
│   └── history/brain-v1.6.md               # Archived prior version
├── templates/
│   ├── index.html                          # Manual drafter UI
│   ├── login.html                          # Password gate
│   └── glamshelf-twin-control-panel.html   # Live dashboard
├── requirements.txt
├── Procfile                                # gunicorn config
├── runtime.txt                             # Python version pin
├── start.bat                               # Local Windows launcher
└── .gitignore
```

## Status

🟢 **In production**, deployed on Render. Handles real customer messages across WhatsApp and Instagram daily. The founder reviews a minority of replies via Telegram inline buttons; the rest go out autonomously, with vision, inventory, and order context injected per call.

## License

Proprietary — © The Glam Shelf.
