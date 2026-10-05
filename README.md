# Glam Shelf Twin

AI customer support agent for **The Glam Shelf**, a live Indian D2C false-eyelash brand. It handles real customer conversations on Instagram — replies automatically where it can and flags the founder when it can't, answering from per-SKU product data.

**Channel status:** Instagram is live. The WhatsApp channel (via WATI) is built but **currently paused**.

---

## What it does

Every text message that reaches the model is classified into one of three paths:

**AUTO** — Routine questions the twin can answer confidently: pricing, shipping timelines, product recommendations, payment methods. The reply goes out immediately. On Instagram, order or tracking questions, restock requests and 20+ tray rate questions also send the founder a heads-up on Telegram. Nothing waits for approval.

**DRAFT+APPROVE** — The twin drafts a reply but isn't confident enough to send alone, e.g. return and exchange requests, damaged or wrong items, payment taken with no order. The founder gets it on Telegram with one-tap buttons: ✅ Send as-is, ✏️ Edit, or ⛔ Skip. The draft itself goes out only when the founder acts; meanwhile, on Instagram, the customer gets a short handoff line ("I've passed this to the team — they'll reply to you here 🤍").

**ESCALATE** — Situations that need human judgment: refund complaints, legal threats, allergic reactions, press enquiries, angry complaints (even a first one), and a customer placing a 20+ tray order or pushing below the price floor. The founder is paged on Telegram and the twin pauses itself for that customer for 4 hours (lift it early from Telegram with ▶️ Resume bot or `#resume <id>`). On Instagram the customer gets the same handoff line — or, for an allergic reaction, advice to stop using the product and see a doctor — except legal threats and press enquiries, which get no automated reply. On WhatsApp a normal escalation sends nothing; a stock holding reply goes out only when the model's output was unusable.

People testing the assistant get their question answered like any customer's, or a one-line invite to ask one; anyone asking to get it for their own brand gets a friendly one-liner. Either way the founder gets a LEAD notice on Telegram, even when the answer waits for approval — and there's no pause.

On Instagram, media never reaches the model: photos get a fixed reply asking for the customer's eye shape or the occasion, voice notes and shared posts or reels a reply asking "could you type your question?", story mentions, videos and files no reply — and the founder gets a Telegram notice.

---

## How it stays accurate

The twin uses a **RAG retrieval layer** built on `fastembed` with ONNX embeddings, stored in the app's SQLite database. Search uses the `sqlite-vec` extension where it loads; on Render it doesn't, so a plain numpy scan over the stored embeddings does the search, which is fast at a few dozen chunks. When a message mentions a product or policy topic, it retrieves the two closest chunks from the storefront's product descriptions and its returns, shipping and terms pages, and adds them to the prompt. Retrieved product text is marketing copy, so the prompt allows it for specs (pairs, length, style) only; `brain.md` stays the authority on everything else.

Stock status and product descriptions come from the live Shopify storefront feed (cached for up to 5 minutes) and are injected into every call. Prices are quoted from `brain.md`; the live storefront prices feed the output guard instead, so a ₹ amount that doesn't match the store is held for the founder rather than sent.

---

## Safety rails

- **Output guard** — before an Instagram AUTO reply is sent, a plain-Python check holds it for founder approval (the customer gets the handoff line) if it mentions a ₹ amount that isn't a current Shopify price, a fixed policy amount, or an order total of up to ₹1,500 built from those prices; offers a code, discount, refund or freebie (each sentence is judged on its own, and brain.md's policy statements — free shipping above ₹799, no coupon codes, the refund timeline — are sent); links anywhere other than the brand's own site and Instagram; promises a replacement, reshipment or exchange (only the store policy's own sentences pass); suggests adding products to reach free shipping; promises a follow-up ("the team will update you", "we'll get back to you"), except the handoff line's "they'll reply to you here" and the LEAD line's "Udit will message you personally" when a founder notice goes out with the reply; or talks about its own instructions.
- **Rate limits** — at most 15 messages per sender per 10 minutes and 40 per day reach the model, plus a daily cap on model calls overall. Over a limit, the sender gets one short notice per window. On Instagram every message over the limit is forwarded to the founder on Telegram; on WhatsApp the founder gets one alert per sender per day.
- **Fixed escalation checks** — legal threats (lawyer, court, consumer forum, legal notice, police, FIR, and Hinglish forms such as "case kar dunga"), threats to post on social media, "refund karo", angry complaints and a commitment to a 20+ tray order always escalate, whatever the model decides.

---

## How it's evaluated

[`eval/`](eval/README.md) re-runs real customer questions through the live reply logic (`draft_reply_logic`) and scores each fresh answer against the founder's own written ideal answer with an LLM judge — Pass / Partial / Fail plus a reason, overall and by category. It never sends anything to a real customer. See [`eval/README.md`](eval/README.md) for how the ground truth was built and its known limits.

---

## Where it runs

| Layer | Technology |
|-------|-----------|
| App | Python (Flask + gunicorn) |
| Hosting | Render |
| Text replies | DeepSeek (`deepseek-chat`) |
| Image understanding | Claude Sonnet on WhatsApp (reads order screenshots, eye photos). On Instagram Twin can't read photos yet: it asks for the customer's eye shape or the occasion instead and alerts the founder |
| WhatsApp | WATI Business API (channel currently paused) |
| Instagram | Meta Instagram Graph API |
| Founder UI | Telegram Bot (inline buttons) |
| Commerce | Shopify webhooks + live storefront feed |
| Search | fastembed (ONNX) embeddings over product descriptions and the returns, shipping and terms pages; numpy search on Render (sqlite-vec doesn't load there) |
| Database | SQLite with hourly GitHub backups |

---

## In production

The twin answers real customers of a live D2C store on Instagram. Drafts, output-guard holds and escalations go to the founder on Telegram; the other replies go out automatically, with product facts, live Shopify stock, and the conversation history injected into every call.

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
