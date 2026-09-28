# Launching Twin for a new brand

Each brand gets its **own copy** of Twin: its own Render service, its own
Meta (Instagram) app, its own Telegram group and its own settings. The code
is the same for every brand. Only the settings differ, and those never go in
this repo, because the repo is public.

The Glam Shelf's copy needs none of this. With none of the new settings
set, Twin behaves exactly as before.

Allow about two hours, plus Meta's review time if the app still needs
Advanced Access.

---

## What you need from the brand

- [ ] Brand name, Instagram handle, contact email, website domain
- [ ] Their price rules: free-shipping limit, the order value above which
      the founder decides (the "Hard Money Threshold"), bulk price and bulk
      minimum
- [ ] Their Shopify store domain, if they have one
- [ ] The founder's Telegram account (they approve replies there)
- [ ] Admin access to their Instagram professional account and a Meta
      developer account

---

## Step 1 — Write the brand's brain

The brain is the rulebook Twin answers from.

1. Copy `brain/brain.md` as a starting point and rewrite it for the brand:
   products, prices, voice, policies and escalation rules.
2. Keep the Section 2 product table in the same shape
   (`| PRODUCT NAME | ₹849 | ... |`, between `## SECTION 2` and
   `### Generic Price Inquiry Handling`). If the Shopify feed is down,
   the output guard reads its allowed prices from this table.
3. Keep the "tester / brand owner" LEAD rule's reply the same as
   `messages.lead_reply` in the settings file (Step 2).
4. Save it as **`<brand>.brain.md`** (for example `acme.brain.md`). That
   name is git-ignored.

> ⚠️ Never commit a client's brain. `.gitignore` blocks everything in
> `brain/` except Glam Shelf's file, and any `*.brain.md`. Don't force it
> with `git add -f`.

---

## Step 2 — Fill in the brand settings file

1. Copy `brands/example.json` (a fake candle brand) to **`<brand>.brand.json`**.
   That name is git-ignored too.
2. Fill in every field. Twin refuses to start if a field is missing, has a
   typo or has the wrong type, and the boot log names the field. It never
   falls back to Glam Shelf's values.

| Field | What it is | Example |
|---|---|---|
| `brand_name` | Shown on the login page and in the photo reader | `"Acme Lashes"` |
| `brand_short_name` | Control-panel title | `"Acme"` |
| `instagram_handle` | Without the @ | `"acmelashes"` |
| `email` | Contact email replies may give out | `"hello@acme.in"` |
| `website_domain` | Without https:// | `"acme.in"` |
| `dashboard_api_base` | Leave `null` (the control panel calls its own server) | `null` |
| `allowed_links.domains / instagram_handles / emails` | **Other** links Twin may mention. The brand's own domain, handle and email are always allowed. Anything else in an automatic reply sends it to you for approval. | `[]` |
| `vision.*` | Words for the photo reader (WhatsApp photos only) | `"an Indian lash brand"`, `"lash"`, `"lashes"`, `"Classic Tray"` |
| `shopify.products_url` | `https://<store>/products.json`, or `null` if there's no Shopify store | `"https://acme.in/products.json"` |
| `shopify.prompt_policy_pages` | Policy pages Twin reads live (title + URL), or `[]` | refund, shipping |
| `shopify.rag_policy_pages` | Policy pages for product/policy lookups, or `[]` | refund, shipping, terms |
| `pricing.free_shipping_threshold_inr` | Free shipping above this | `999` |
| `pricing.hard_money_threshold_inr` | Committed orders above this always go to the founder | `2000` |
| `pricing.bulk_rate_inr`, `bulk_floor_inr`, `bulk_min_units` | Bulk price, lowest bulk price, bulk minimum. A "I'll take N" message with N at or above the minimum goes to the founder. | `699`, `649`, `20` |
| `pricing.extra_allowed_inr` | Other fixed ₹ amounts the brain quotes (not product prices) | `[99]` |
| `messages.review_request` | WhatsApp review ask, 10 days after delivery. `{first_name}` placeholder. `null` = off. | `null` |
| `messages.lead_reply` | Reply to testers and people asking about the AI. Say who will follow up; the LEAD alert goes to **this brand's** Telegram group. | |
| `messages.allergy_holding_line` | Sentence after the fixed "stop using it and see a doctor" advice | |
| `messages.escalate_fallback_holding_reply` | WhatsApp holding reply when an escalation has no usable draft | |
| `messages.instagram_photo_reply` | Reply to a photo on Instagram (Twin can't see photos there) | |
| `messages.shipped_intro`, `out_for_delivery`, `delivered` | WhatsApp shipping updates. `{first_name}` and `{order_number}` placeholders. | |
| `shipping.tracking_url_prefix` | Courier tracking link without the number | `"https://shiprocket.in/tracking/"` |
| `shipping.default_carrier` | Carrier name when Shopify has none | `"Shiprocket"` |
| `shipping.wati_template_name` | Approved WATI template for "shipped" | |
| `bot_text_signatures` | Unique text from Twin's own automatic WhatsApp messages (e.g. the review link), so Twin doesn't mistake them for a human reply. 8+ characters each. | `["acme.in/pages/reviews"]` |
| `storage_prefix` | Names this copy's files (`<prefix>_logs.db`, the backup file). Lower-case, unique per brand. | `"acme"` |

Keys starting with `_` are notes and are ignored.

To check the file before uploading it:

```
python -c "import brand_config; print(brand_config.load_brand_config('acme.brand.json')['brand_name'])"
```

---

## Step 3 — Create the Render service

1. Render → **New → Web Service** → this repo, branch `main`. Render uses
   the `Procfile` (`gunicorn app:app --timeout 60`) and `runtime.txt`.
2. **Add a persistent disk** (Settings → Disks), mounted at **`/var/data`**.
   This needs a paid plan. Without a disk, customer history, pauses and
   pending drafts reset on every deploy, and token auto-refresh stays off.
3. **Secret Files** (Environment → Secret Files): upload `acme.brain.md`
   and `acme.brand.json`. Render mounts them at `/etc/secrets/<filename>`.
   It also copies them into the service's root folder, which is why they
   need unique names that no repo file uses.
4. Set the environment variables below (the table at the end has them all).
   Start with `DRAFT_ONLY_MODE=1`.

Minimum for an Instagram-only brand:

```
SECRET_KEY=<long random string>
APP_PASSWORD=<password for the drafter page>
DASHBOARD_KEY=<long random string>
DEEPSEEK_API_KEY=<key>
BRAND_CONFIG_PATH=/etc/secrets/acme.brand.json
BRAIN_FILE_PATH=/etc/secrets/acme.brain.md
DB_PATH=/var/data/acme.db
INSTAGRAM_VERIFY_TOKEN=<random string you choose>
INSTAGRAM_PAGE_ACCESS_TOKEN=<from Step 5>
INSTAGRAM_PAGE_ID=<from Step 5>
INSTAGRAM_APP_SECRET=<from Step 5>
INSTAGRAM_APP_ID=<from Step 5>
TELEGRAM_BOT_TOKEN=<from Step 4>
TELEGRAM_CHAT_ID=<from Step 4>
TELEGRAM_WEBHOOK_SECRET=<random string you choose>
DRAFT_ONLY_MODE=1
```

Always set `BRAND_CONFIG_PATH` and `BRAIN_FILE_PATH` **together**. If only
one is set, the boot log shows a `[BRAND] WARNING`, because the copy would
be answering with Glam Shelf's brain or Glam Shelf's settings.

---

## Step 4 — Telegram bot and group

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. `/setprivacy` → choose the bot → **Disable**. The bot has to read plain
   messages in the group: your edited replies and `#resume <id>`.
3. Create a group with the founder, add the bot, and send any message.
4. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `chat.id`. Group ids are negative, e.g. `-1001234567890`. That goes in
   `TELEGRAM_CHAT_ID`. Twin ignores button taps from any other chat.
5. After the Render service is up, point Telegram at it:

```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<service>.onrender.com/telegram-callback&secret_token=<TELEGRAM_WEBHOOK_SECRET>&allowed_updates=["callback_query","message"]
```

(`getUpdates` stops working once a webhook is set. That's expected.)

---

## Step 5 — Meta app (Instagram API with Instagram Login)

Use a **separate Meta app per brand**. Twin also skips any event that
isn't for its own `INSTAGRAM_PAGE_ID`, so a shared app can't make one
brand answer another's customers. Separate apps still keep tokens, reviews
and limits apart.

1. developers.facebook.com → **Create app** (Business) → add the
   **Instagram** use case → **API setup with Instagram login**.
2. Add the brand's Instagram professional account and **generate an access
   token**. It needs `instagram_business_basic` and
   `instagram_business_manage_messages`. That token goes in
   `INSTAGRAM_PAGE_ACCESS_TOKEN`.
3. Find the account's id: open
   `https://graph.instagram.com/v22.0/me?fields=user_id,username&access_token=<TOKEN>`
   → `user_id` (starts with 1784…) goes in `INSTAGRAM_PAGE_ID`.
4. App settings → Basic: **App Secret** goes in `INSTAGRAM_APP_SECRET`
   (Twin rejects unsigned webhooks without it), and **App ID** goes in
   `INSTAGRAM_APP_ID`.
5. Webhooks: callback URL `https://<service>.onrender.com/instagram-webhook`,
   verify token = your `INSTAGRAM_VERIFY_TOKEN`. Subscribe to **`messages`**
   and **`message_echoes`**. Echoes are how Twin notices the founder
   replying by hand and pauses itself.
6. Turn the subscription on for the account:
   `POST https://graph.instagram.com/v22.0/me/subscribed_apps?subscribed_fields=messages,message_echoes&access_token=<TOKEN>`
7. Meta only sends webhooks when the app is **Live** and has **Advanced
   Access** for the messaging permission. That may need App Review.

**Token renewal:** Instagram tokens last 60 days. Twin checks the token
daily and, with 15 days or fewer left, refreshes it on its own. The new
token is saved on the disk, next to the DB, and Telegram gets a ✅ message.
This needs `INSTAGRAM_APP_ID` + `INSTAGRAM_APP_SECRET` (to read the expiry
date) and the `/var/data` disk. If a refresh fails, Telegram gets a ⚠️
message and Twin retries the next day. Pasting a new
`INSTAGRAM_PAGE_ACCESS_TOKEN` on Render always takes over from the saved
one.

---

## Step 6 — First two days: draft-only mode

With `DRAFT_ONLY_MODE=1`, **nothing Twin writes goes out without the
founder's tap**. Every reply that would normally go out on its own arrives
in the Telegram group as a draft with **✅ Send as-is / ✏️ Edit / ⛔ Skip**.

What the customer sees meanwhile:

- **Instagram:** straight away, *"I've passed this to the team — they'll
  reply to you here 🤍"*, at most once per 30 minutes per customer. Then
  the approved reply when the founder taps Send. Instagram only allows
  replies within **24 hours** of the customer's last message, so approve
  before then.
- **WhatsApp:** nothing until the founder approves.

Fixed texts (from the code or the settings file, not written by Twin)
still go out without approval, as they always have:

- **Instagram:** the holding line or allergy safety line on escalations
  (silence for legal threats and press), the photo reply, the "team will
  reply shortly" rate-limit notice, the holding line when Twin itself
  fails, and the fixed tester/LEAD line.
- **WhatsApp:** the escalation holding reply when Twin's own draft was
  unusable, the rate-limit notice, the photo reader's two fallback
  questions, and shipping updates / review requests.

After two days of good drafts, **delete** `DRAFT_ONLY_MODE` on Render.
Render restarts the service and replies go out automatically from then on.

---

## Step 7 — Check it works

- [ ] Boot log (Render → Logs) shows `[BRAND] <brand name> — settings from /etc/secrets/…`
      and `[BRAIN] Brain file: /etc/secrets/…`, with **no** `WARNING` and
      no `NOT FOUND`.
- [ ] `curl -H "X-Dashboard-Key: <DASHBOARD_KEY>" https://<service>.onrender.com/healthz`
      shows the right `brand`, `brain_present: true`, `draft_only_mode: true`,
      and `instagram_token_auto_refresh: "on"`.
- [ ] Send a DM to the brand's account from a personal account. A draft
      should appear in the Telegram group within a few seconds, and your
      account should get the handoff line.
- [ ] The log must **not** show `[IG-FILTER] Skipped …` for that DM. If it
      does, `INSTAGRAM_PAGE_ID` is wrong (see Step 5.3).
- [ ] Tap ✅ Send as-is: the reply arrives in the DM.
- [ ] Log in at `https://<service>.onrender.com/` (APP_PASSWORD) and at
      `/dashboard?key=<DASHBOARD_KEY>`. Both pages should show the brand's name.

---

## All environment variables

**Required** means the brand's copy doesn't work properly without it.
Leave every kill switch unset unless you're rolling something back.

| Variable | Needed? | Example / note |
|---|---|---|
| `SECRET_KEY` | Required (won't start) | long random string |
| `APP_PASSWORD` | Required (won't start) | drafter page password |
| `DASHBOARD_KEY` | Required (won't start) | long random string |
| `DEEPSEEK_API_KEY` | Required | writes every reply |
| `BRAND_CONFIG_PATH` | Required for a new brand | `/etc/secrets/acme.brand.json` (unset = Glam Shelf) |
| `BRAIN_FILE_PATH` | Required for a new brand | `/etc/secrets/acme.brain.md` (unset = `brain/brain.md`) |
| `DB_PATH` | Required (persistent disk) | `/var/data/acme.db` |
| `INSTAGRAM_PAGE_ACCESS_TOKEN` | Required for Instagram | `IGAA…` |
| `INSTAGRAM_PAGE_ID` | Required for Instagram | `1784…` (other accounts' events are skipped) |
| `INSTAGRAM_APP_SECRET` | Required for Instagram | signs webhooks |
| `INSTAGRAM_VERIFY_TOKEN` | Required for Instagram | any random string |
| `INSTAGRAM_APP_ID` | Recommended | expiry date → auto-refresh + 7-day warning |
| `TELEGRAM_BOT_TOKEN` | Required | from @BotFather |
| `TELEGRAM_CHAT_ID` | Required | group id, e.g. `-1001234567890` |
| `TELEGRAM_WEBHOOK_SECRET` | Required | any random string, also in setWebhook |
| `DRAFT_ONLY_MODE` | First 2 days | `1` = every automatic reply waits for approval |
| `ANTHROPIC_API_KEY` | Optional | WhatsApp photo reading |
| `GITHUB_TOKEN`, `GITHUB_REPO` | Optional | hourly DB backup to a **private** repo; off unless both are set |
| `GITHUB_BACKUP_PATH` | Optional | default `<storage_prefix>_logs.db` |
| `WATI_API_KEY`, `WATI_ENDPOINT`, `WATI_WEBHOOK_TOKEN` | WhatsApp only | WATI webhook: `/webhook/<WATI_WEBHOOK_TOKEN>` |
| `BUSINESS_NUMBER`, `OWNER_NUMBER` | WhatsApp only | numbers Twin never replies to |
| `WATI_BOT_OPERATOR_EMAIL` | WhatsApp only | default `Bot` |
| `SHOPIFY_WEBHOOK_SECRET` | Optional | Shopify order / shipping webhooks |
| `LLM_DAILY_CAP` | Optional | default 500 model calls a day |
| `INSTAGRAM_API_BASE` | Leave unset | |
| `DASHBOARD_DB_PATH` | Leave unset | old name for `DB_PATH` |
| `OUTPUT_GUARD_DISABLED` | Kill switch | skips the automatic-reply checks (draft-only still holds) |
| `ESCALATION_PREFILTER_DISABLED` | Kill switch | legal-phrase / bulk-order escalation |
| `LLM_RATE_LIMIT_DISABLED` | Kill switch | per-customer and daily limits |
| `SHIPPING_RETRY_DISABLED` | Kill switch | shipping-message retries |
| `INSTAGRAM_WEBHOOK_VERIFY_DISABLED`, `WATI_WEBHOOK_VERIFY_DISABLED`, `TELEGRAM_WEBHOOK_VERIFY_DISABLED` | Kill switch | webhook verification |
| `INSTAGRAM_ACCOUNT_FILTER_DISABLED` | Kill switch | process other accounts' events again |
| `TOKEN_AUTO_REFRESH_DISABLED` | Kill switch | stop refreshing the Instagram token (a token already saved from the current env token stays in use; paste a new env token to replace it) |

---

## If something's wrong

| You see | Why | Fix |
|---|---|---|
| Service won't start: `brand settings file …` | Settings file missing or a field is wrong. The message names the field. | Fix the file and re-upload it |
| `[BRAND] WARNING: settings are for …` | Brain path not set | Set `BRAIN_FILE_PATH` |
| `[BRAIN] … NOT FOUND` | Wrong path or file name | Check the Secret File name |
| `[IG-FILTER] Skipped …` for the brand's own DMs | `INSTAGRAM_PAGE_ID` doesn't match the account | Use `user_id` from Step 5.3; set `INSTAGRAM_ACCOUNT_FILTER_DISABLED=1` meanwhile |
| No drafts in Telegram | Bot token / chat id / webhook | Re-run setWebhook; check `TELEGRAM_CHAT_ID` is the group id |
| Edited replies are ignored | Bot privacy mode is on | @BotFather → `/setprivacy` → Disable |
| `[TOKEN-REFRESH] Skipped: expiry date unknown` | No `INSTAGRAM_APP_ID` | Set it (App settings → Basic) |
| `[TOKEN-REFRESH] Not refreshing: … doesn't exist` | No persistent disk | Add the disk at `/var/data` and set `DB_PATH` on it |
