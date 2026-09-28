# Glam Shelf Twin

AI customer support agent for **The Glam Shelf**, a live Indian D2C false-eyelash brand. It handles real customer conversations on Instagram — replies automatically where it can and flags the founder when it can't, answering from per-SKU product data.

**Channel status:** Instagram is live. The WhatsApp channel (via WATI) is built but **currently paused**.

---

## What it does

Every incoming message on WhatsApp or Instagram is classified into one of three paths:

**AUTO** — Routine questions the twin can answer confidently: pricing, shipping timelines, product recommendations, return policies, payment methods. The reply goes out immediately. No founder involvement.

**DRAFT+APPROVE** — The twin drafts a reply but isn't confident enough to send alone. The founder gets it on Telegram with one-tap buttons: ✅ Send, ✏️ Edit, or ⛔ Skip. The draft itself goes out only when the founder acts; meanwhile, on Instagram, the customer gets a short handoff line ("I've passed this to the team — they'll reply to you here 🤍").

**ESCALATE** — Situations that need human judgment: refund disputes, damage claims, angry customers, legal threats, bulk negotiations. The founder is paged on Telegram and the twin pauses itself for that customer for 4 hours (lift it early from Telegram with ▶️ Resume bot or `#resume <id>`). On Instagram the customer gets the same handoff line — or, for an allergic reaction, advice to stop using the product and see a doctor — except legal threats and press enquiries, which get no automated reply. On WhatsApp a normal escalation sends nothing; a stock holding reply goes out only when the model's output was unusable.

People testing the assistant, or asking about it, get a friendly one-liner and the founder a LEAD notice on Telegram — no pause.

---

## How it stays accurate

The twin uses a **RAG retrieval layer** built on `fastembed` with ONNX embeddings. Behind every reply, it searches a per-SKU product knowledge base — materials, band types, lash lengths, care instructions — and injects relevant facts into the prompt before generating a response. This means the twin doesn't guess about product details. If a customer asks "are GS1 lashes suitable for hooded eyes?", it retrieves the actual GS1 specs and answers from real data, not training memory.

Prices and stock come from the live Shopify storefront feed (cached for up to 5 minutes) and are injected into every call — they are not hard-coded in the prompt.

---

## Safety rails

- **Output guard** — before an Instagram AUTO reply is sent, a plain-Python check holds it for founder approval (the customer gets the handoff line) if it mentions a ₹ amount that isn't a current Shopify price, a fixed policy amount, or an order total of up to ₹1,500 built from those prices; offers a code, discount, refund or freebie; links anywhere other than the brand's own site and Instagram; or talks about its own instructions.
- **Rate limits** — at most 8 messages per sender per 10 minutes and 40 per day reach the model, plus a daily cap on model calls overall. Over a limit, the sender gets one short notice and the founder a Telegram alert.
- **Fixed escalation words** — legal threats (lawyer, court, consumer forum, legal notice, police, FIR, and Hinglish forms such as "case kar dunga") always escalate, whatever the model decides.

---

## Where it runs

| Layer | Technology |
|-------|-----------|
| App | Python (Flask + gunicorn) |
| Hosting | Render |
| Text replies | DeepSeek v3 |
| Image understanding | Claude Sonnet on WhatsApp (reads order screenshots, eye photos). On Instagram Twin can't read photos yet: it asks for the customer's eye shape or the occasion instead and alerts the founder |
| WhatsApp | WATI Business API (channel currently paused) |
| Instagram | Meta Instagram Graph API |
| Founder UI | Telegram Bot (inline buttons) |
| Commerce | Shopify webhooks + live storefront feed |
| Search | fastembed + ONNX embeddings over per-SKU data |
| Database | SQLite with hourly GitHub backups |

---

## In production

The twin answers real customers of a live D2C store on Instagram. Drafts, output-guard holds and escalations go to the founder on Telegram; the other replies go out automatically, with product facts, live Shopify prices and stock, and the conversation history injected into every call.

---

## Running it for another brand

Each brand runs its own copy: its own Render service, Meta app and Telegram group. Everything brand-specific (links, prices, store feed, canned messages) lives in one settings file: `brands/glamshelf.json` for The Glam Shelf, or the file `BRAND_CONFIG_PATH` points to. `BRAIN_FILE_PATH` points to that brand's brain. With neither set, Twin runs as The Glam Shelf, exactly as before. Setup checklist: [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md).

---

## Development setup

The project virtualenv (`venv/`) is the canonical local environment and must match `requirements.txt`. After pulling changes that touch `requirements.txt`, re-sync it:

```
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the test suite with the venv interpreter:

```
venv\Scripts\python.exe -m unittest discover tests
```

`start.bat` launches the local dev server using this venv. Don't rely on the system Python — it can drift from `requirements.txt` and isn't what Render deploys.
