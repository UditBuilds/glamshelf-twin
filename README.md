# Glam Shelf Twin

AI customer support agent for **The Glam Shelf**, a live Indian D2C false-eyelash brand. It handles real customer conversations on WhatsApp and Instagram — replies autonomously where it can, flags the founder when it can't, and learns from every product SKU to stay accurate.

---

## What it does

Every incoming message on WhatsApp or Instagram is classified into one of three paths:

**AUTO** — Routine questions the twin can answer confidently: pricing, shipping timelines, product recommendations, return policies, payment methods. The reply goes out immediately. No founder involvement.

**DRAFT+APPROVE** — The twin drafts a reply but isn't confident enough to send alone. The founder gets it on Telegram with one-tap buttons: ✅ Send, ✏️ Edit, or ⛔ Skip. The customer waits; nothing ships until the founder acts.

**ESCALATE** — Situations that need human judgment: refund disputes, damage claims, angry customers, legal threats, bulk negotiations. The twin sends a holding reply ("I'm looping in the founder — they'll get back to you shortly"), notifies the founder on Telegram, and pauses itself for that customer for 4 hours to avoid duplicate messages.

---

## How it stays accurate

The twin uses a **RAG retrieval layer** built on `fastembed` with ONNX embeddings. Behind every reply, it searches a per-SKU product knowledge base — materials, band types, lash lengths, care instructions — and injects relevant facts into the prompt before generating a response. This means the twin doesn't guess about product details. If a customer asks "are GS1 lashes suitable for hooded eyes?", it retrieves the actual GS1 specs and answers from real data, not training memory.

Live Shopify inventory is also injected per call, so stock counts and pricing are always current.

---

## Where it runs

| Layer | Technology |
|-------|-----------|
| App | Python (Flask + gunicorn) |
| Hosting | Render |
| Text replies | DeepSeek v3 |
| Image understanding | Claude Sonnet (reads order screenshots, eye photos) |
| WhatsApp | WATI Business API |
| Instagram | Meta Instagram Graph API |
| Founder UI | Telegram Bot (inline buttons) |
| Commerce | Shopify webhooks + live storefront feed |
| Search | fastembed + ONNX embeddings over per-SKU data |
| Database | SQLite with hourly GitHub backups |

---

## In production

The twin handles real customer traffic daily. It's not a demo or a prototype — it's the actual support layer behind a live D2C store. The founder reviews a small fraction of replies via Telegram; the rest go out autonomously with product facts, inventory context, and conversation history injected into every response.

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
