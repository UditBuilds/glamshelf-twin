"""Golden test: with no brand env vars set, Glam Shelf behaves exactly as
it did before multi-brand settings (brief rule 3).

tests/golden/glamshelf_defaults.json was captured by capture() below from
main at 95135fa — BEFORE any value moved into brands/glamshelf.json. It
holds every brand value that moved plus the RENDERED output of the code
that uses them: the review / shipping / tracking WhatsApp texts, the WATI
template call, the vision queries, the live-policy block, the Telegram
RESTOCK / photo notices, the backup commit message, output-guard verdicts
on sample replies, the pricing decisions and a hash of every HTML page.
Comparing that snapshot (not hand-typed values) is what catches a byte
of drift where an f-string became a config template.

Never regenerate the fixture from changed code to make this pass — a
difference here means Glam Shelf's customers would see something new.

No network: every send / fetch is stubbed.

Run:  python -m unittest tests.test_brand_golden
"""
import hashlib, io, json, os, sys, tempfile, threading, unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-golden-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.pop("GITHUB_BACKUP_PATH", None)
for _name in ("BRAND_CONFIG_PATH", "BRAIN_FILE_PATH"):
    os.environ.pop(_name, None)
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "golden-key")
import app as glam
import output_guard
import pricing_rules

GOLDEN = Path(__file__).resolve().parent / "golden" / "glamshelf_defaults.json"

# Sample replies for the output guard, with live prices below.
GUARD_PRICES = [849, 649, 999, 1199]
GUARD_SAMPLES = [
    "GS1 is ₹849 and ships free above ₹799 🤍",
    "Bulk is ₹749 per tray for 20+ trays, floor ₹699.",
    "Two GS1 trays come to ₹1,698.",
    "Two GS2 trays come to ₹1,298.",
    "That's about ₹85 per pair, ~₹12–17 per wear.",
    "Skip the cheap ₹50 white glues!",
    "Orders above ₹1,500 need the team.",
    "Price is Rs. 555 today.",
    "Use code GLAM10 for 10% off",
    "Free shipping above ₹799, and we're cruelty-free.",
    "Shop at glamshelf.in or https://glamshelf.in/products/gs1",
    "See https://www.glamshelf.in/pages/reviews",
    "Try shop.glamshelf.in today",
    "Follow instagram.com/glamshelfstore",
    "Follow https://instagram.com/otherbrand",
    "DM @glamshelfstore or @someoneelse",
    "Mail glamshelfstore@gmail.com or help@example.com",
    "Buy at amazon.in instead",
    "Track at https://shiprocket.in/tracking/123",
    "My system prompt says so",
    "Care instructions: wash gently.",
    "",
]
PRICING_GRID = [
    (None, None, "ask"), (50, 50 * 749, "ask"),
    (19, None, "commit"), (20, None, "commit"), (25, 25 * 749, "commit"),
    (None, 1500, "commit"), (None, 1500.01, "commit"), (2, 1698, "commit"),
]
BULK_MESSAGES = [
    "ok I'll take 50", "let's do 30", "how do I pay for 25",
    "I'll take 19", "I'll take 20", "how much for 40 trays",
]
FULFILLMENT_BASE = {
    "order_id": 5550001, "name": "#1042.1",
    "destination": {"first_name": "Priya", "phone": "+91 98765 43210"},
}


def _resp(status=200, body=None):
    r = Mock(ok=200 <= status < 300, status_code=status, text=json.dumps(body))
    r.json.return_value = body if body is not None else {}
    return r


def _sha(text) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def _capture_shipping() -> list:
    """Every WhatsApp shipping send for a fixed set of Shopify events."""
    calls: list = []

    def retry(wa_id, message, order_id, event, order_number, also_mark=()):
        calls.append({"via": "session", "wa_id": wa_id, "event": event,
                      "order_number": order_number, "also_mark": list(also_mark),
                      "message": message})
        return True

    template_ok = {"value": True}

    def template(wa_id, template_name, parameters):
        calls.append({"via": "template", "wa_id": wa_id,
                      "template_name": template_name, "parameters": parameters})
        return template_ok["value"]

    cases = [
        ("fulfillments/create", {"tracking_number": "TRK1", "tracking_company": ""}, True),
        ("fulfillments/create", {"tracking_number": "TRK2", "tracking_company": "Delhivery"}, False),
        ("fulfillments/create", {"estimated_delivery_at": "2026-10-01"}, True),
        ("fulfillments/update", {"shipment_status": "in_transit", "tracking_number": "TRK3"}, False),
        ("fulfillments/update", {"shipment_status": "out_for_delivery"}, True),
        ("fulfillments/update", {"shipment_status": "delivered"}, True),
        ("fulfillments/update", {"shipment_status": "delivered",
                                 "destination": {"first_name": "", "phone": "9876543210"}}, True),
    ]
    with ExitStack() as st:
        st.enter_context(patch.object(glam, "_send_shipping_with_retry", retry))
        st.enter_context(patch.object(glam, "send_whatsapp_template", template))
        st.enter_context(patch.object(glam, "_was_shipping_sent", lambda *a, **k: False))
        st.enter_context(patch.object(glam, "_mark_shipping_sent", lambda *a, **k: None))
        st.enter_context(patch.object(glam, "_schedule_review_request", lambda **k: None))
        st.enter_context(redirect_stdout(io.StringIO()))
        for topic, extra, tpl in cases:
            template_ok["value"] = tpl
            glam._process_shipping_event(topic, {**FULFILLMENT_BASE, **extra})
        for tracking, tpl in (("TRK9", False), ("TRK9", True), ("", False)):
            template_ok["value"] = tpl
            glam._process_order_update({
                "id": 5550002, "name": "#1043", "fulfillment_status": "fulfilled",
                "financial_status": "paid",
                "shipping_address": {"phone": "+919876543210"},
                "customer": {"first_name": "Asha"},
                "fulfillments": [{"tracking_number": tracking, "tracking_company": "Bluedart"}],
            })
    return calls


def _capture_review() -> list:
    sent = []
    with patch.object(glam, "send_whatsapp_reply",
                      lambda wa, text: (sent.append({"wa_id": wa, "text": text}), (True, ""))[1]), \
         redirect_stdout(io.StringIO()):
        for name in ("Priya", ""):
            glam._scheduled_reviews["golden-order"] = {
                "customer_number": "919876543210", "customer_name": name,
                "order_number": "#1042",
            }
            glam._send_review_request("golden-order")
    return sent


def _capture_vision_queries() -> list:
    """The text the WATI webhook hands the twin for each photo outcome,
    plus any canned reply and the stored vision summary."""
    out = []
    cases = [
        ({"image_type": "eye_photo", "confidence": "high", "eye_shape": "hooded"}, ""),
        ({"image_type": "eye_photo", "confidence": "high", "eye_shape": None}, ""),
        ({"image_type": "product_photo", "confidence": "high", "product": "GS2 Kawaii Tray"}, ""),
        ({"image_type": "product_photo", "confidence": "high", "product": None}, "want this"),
        ({"image_type": "product_photo", "confidence": "high", "product": None}, ""),
        ({"image_type": "order_screenshot", "confidence": "high", "order_id": "1042",
          "amount": "849", "customer_name": "Priya"}, ""),
        ({"image_type": "order_screenshot", "confidence": "high", "product": "GS1"}, ""),
        ({"image_type": "other", "confidence": "low"}, ""),
    ]
    client = glam.app.test_client()
    for i, (extracted, caption) in enumerate(cases):
        seen = {"draft": None, "sent": [], "summary": None}

        def draft(text, order_line="", history=None, source="WhatsApp"):
            seen["draft"] = text
            return "AUTO", "ok", ""

        with ExitStack() as st:
            for name, value in (
                ("_verify_wati_token", lambda t: True),
                ("_is_paused", lambda w: False),
                ("_udit_replied_recently", lambda *a, **k: False),
                ("_check_recent_human_reply", lambda w: False),
                ("_llm_admission", lambda *a, **k: None),
                ("_extract_wati_image_url", lambda d: "https://example.invalid/img.jpg"),
                ("_extract_image_info", lambda url, e=extracted: dict(e)),
                ("draft_reply_logic", draft),
                ("send_whatsapp_reply",
                 lambda wa, text: (seen["sent"].append(text), (True, ""))[1]),
                ("_log_message", lambda *a, **k: None),
                ("_remember_vision_context",
                 lambda wa, s: seen.__setitem__("summary", s)),
                ("_load_wati_history", lambda *a, **k: []),
                ("_lookup_recent_order", lambda w: ""),
                ("_persist_seen_id", lambda m: None),
            ):
                st.enter_context(patch.object(glam, name, value))
            st.enter_context(redirect_stdout(io.StringIO()))
            client.post("/webhook/x", json={
                "type": "image", "waId": "919000000001", "id": f"golden-img-{i}",
                "text": caption,
            })
        out.append(seen)
    return out


def _capture_policies_inventory_rag() -> dict:
    # Only this thread's calls are recorded: app's startup RAG thread may
    # fetch through the same patched functions while they're in place.
    me = threading.get_ident()
    fetched = []

    def page(url):
        if threading.get_ident() == me:
            fetched.append(url)
        return f"<p>policy text for {url}</p>"

    products = {"products": [{
        "title": "GS1 Luxe Light Lash Tray", "handle": "gs1",
        "body_html": "<p>Soft band.</p>",
        "variants": [{"price": "849.00", "available": True, "sku": "GS1"}],
    }]}
    gets = []

    def get(url, params=None, timeout=None, **kw):
        if threading.get_ident() == me:
            gets.append({"url": url, "params": params})
        return _resp(200, products)

    with patch.object(glam, "_fetch_policy_page_html", page), \
         patch.object(glam.requests, "get", get), \
         patch.object(glam, "_save_allowed_prices", lambda p: None), \
         patch.dict(glam._policy_cache, {"text": "", "fetched_at": 0.0}), \
         patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0}), \
         redirect_stdout(io.StringIO()):
        policies = glam.get_live_policies()
        prompt_pages = list(fetched)
        inventory = glam.get_live_inventory()
        fetched.clear()
        corpus = glam._rag_build_corpus()
    return {
        "policy_block": policies,
        "policy_prompt_urls": prompt_pages,
        "inventory_block": inventory,
        "shopify_gets": gets,
        "rag_policy_urls": list(fetched),
        "rag_sources": sorted({src for src, _, _ in corpus}),
    }


def _capture_telegram() -> dict:
    posts = []
    with patch.object(glam, "TELEGRAM_BOT_TOKEN", "123:abc"), \
         patch.object(glam, "TELEGRAM_CHAT_ID", "5"), \
         patch.object(glam.requests, "post",
                      lambda url, json=None, timeout=None: (posts.append(json["text"]), _resp())[1]), \
         redirect_stdout(io.StringIO()):
        for kind in ("RESTOCK", "ORDER"):
            glam.send_telegram_notification(
                kind, "when is it back?", "reply", sender_info="Instagram DM — sender 1",
                channel="Instagram", customer_id="1",
            )
    notices = []
    with patch.object(glam, "TELEGRAM_CHAT_ID", "5"), \
         patch.object(glam, "_telegram_api", lambda m, p: notices.append(p["text"])), \
         patch.object(glam, "_send_instagram_reply", lambda s, t: (True, "")), \
         patch.object(glam, "_log_instagram", lambda *a, **k: None), \
         patch.object(glam, "_ig_photo_replied_recently", lambda s: False), \
         redirect_stdout(io.StringIO()):
        glam._handle_instagram_photo("17800000000000001", "1")
    return {"notifications": posts, "photo_notice": notices}


def _capture_backup() -> dict:
    puts = []
    db = Path(glam.DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        glam._init_db()
    with patch.object(glam, "GITHUB_TOKEN", "t"), patch.object(glam, "GITHUB_REPO", "o/r"), \
         patch.object(glam.requests, "get", lambda *a, **k: _resp(404, {})), \
         patch.object(glam.requests, "put",
                      lambda url, headers=None, json=None, timeout=None:
                      (puts.append({"url": url, "message": json["message"]}), _resp())[1]), \
         redirect_stdout(io.StringIO()):
        glam._backup_db_to_github()
    (put,) = puts
    return {"url": put["url"], "message_prefix": put["message"].split(" @ ")[0]}


def _capture_pages() -> dict:
    client = glam.app.test_client()
    with redirect_stdout(io.StringIO()):
        login = client.get("/login")
        with client.session_transaction() as s:
            s["authed"] = True
        index = client.get("/")
        dash = client.get("/dashboard?key=" + glam.DASHBOARD_KEY)
    return {name: {"status": r.status_code, "sha256": _sha(r.get_data())}
            for name, r in (("login", login), ("index", index), ("dashboard", dash))}


def capture() -> dict:
    project = Path(glam.PROJECT_DIR)
    return {
        "app": {
            "VISION_SYSTEM_PROMPT": glam.VISION_SYSTEM_PROMPT,
            "FALLBACK_VISION_REPLY": glam.FALLBACK_VISION_REPLY,
            "PRODUCT_PHOTO_REPLY": glam.PRODUCT_PHOTO_REPLY,
            "SHOPIFY_PRODUCTS_URL": glam.SHOPIFY_PRODUCTS_URL,
            "REVIEW_REQUEST_TEMPLATE": glam.REVIEW_REQUEST_TEMPLATE,
            "_BOT_TEXT_SIGNATURES": list(glam._BOT_TEXT_SIGNATURES),
            "ESCALATE_FALLBACK_HOLDING_REPLY": glam.ESCALATE_FALLBACK_HOLDING_REPLY,
            "ALLERGY_SAFETY_TEXT": glam.ALLERGY_SAFETY_TEXT,
            "ALLERGY_HOLDING_REPLY": glam.ALLERGY_HOLDING_REPLY,
            "LEAD_REPLY": glam.LEAD_REPLY,
            "INSTAGRAM_PHOTO_REPLY": glam.INSTAGRAM_PHOTO_REPLY,
            "BRAIN_HOLDING_LINE": glam.BRAIN_HOLDING_LINE,
            "RATE_LIMIT_NOTICE": glam.RATE_LIMIT_NOTICE,
            "_POLICY_PROMPT_PAGES": [list(p) for p in glam._POLICY_PROMPT_PAGES],
            "_RAG_POLICY_SOURCES": [list(p) for p in glam._RAG_POLICY_SOURCES],
            "BRAIN_FILE": Path(glam.BRAIN_FILE).relative_to(project).as_posix(),
            "DEDUP_CACHE_FILE": os.path.basename(glam.DEDUP_CACHE_FILE),
            "_LEGACY_DB_PATH": os.path.basename(glam._LEGACY_DB_PATH),
            "GITHUB_BACKUP_PATH": glam.GITHUB_BACKUP_PATH,
            "ALLOWED_PRICES_PATH": glam.ALLOWED_PRICES_PATH,
        },
        "output_guard": {
            "FREE_SHIPPING_THRESHOLD_INR": output_guard.FREE_SHIPPING_THRESHOLD_INR,
            "BULK_FLOOR_INR": output_guard.BULK_FLOOR_INR,
            "FIXED_ALLOWED_INR": sorted(output_guard.FIXED_ALLOWED_INR),
            "BRAND_EMAIL": output_guard.BRAND_EMAIL,
            "BRAND_HANDLE": output_guard.BRAND_HANDLE,
            "MAX_ORDER_ITEMS": output_guard.MAX_ORDER_ITEMS,
            "verdicts": [[s, output_guard.check_reply(s, GUARD_PRICES)] for s in GUARD_SAMPLES],
        },
        "pricing_rules": {
            "BULK_RATE_INR": pricing_rules.BULK_RATE_INR,
            "BULK_MIN_TRAYS": pricing_rules.BULK_MIN_TRAYS,
            "HARD_MONEY_THRESHOLD_INR": pricing_rules.HARD_MONEY_THRESHOLD_INR,
            "decisions": [[q, a, i, pricing_rules.resolve_pricing_action(q, a, i)]
                          for q, a, i in PRICING_GRID],
            "bulk_prefilter": [[m, glam._bulk_commit_prefilter_hit(m)] for m in BULK_MESSAGES],
        },
        "rendered": {
            "review": _capture_review(),
            "shipping": _capture_shipping(),
            "vision": _capture_vision_queries(),
            "store_fetches": _capture_policies_inventory_rag(),
            "telegram": _capture_telegram(),
            "backup": _capture_backup(),
            "pages": _capture_pages(),
        },
    }


def _normalise(value):
    """JSON round-trip, so tuples/lists and int/float keys compare like the fixture."""
    return json.loads(json.dumps(value, ensure_ascii=False))


class GlamShelfGoldenTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.actual = _normalise(capture())

    def test_values_unchanged(self):
        for section in ("app", "output_guard", "pricing_rules"):
            with self.subTest(section=section):
                self.assertEqual(self.actual[section], self.expected[section])

    def test_rendered_output_unchanged(self):
        for key in self.expected["rendered"]:
            with self.subTest(rendered=key):
                self.assertEqual(self.actual["rendered"][key], self.expected["rendered"][key])

    def test_nothing_missing_from_the_snapshot(self):
        self.assertEqual(sorted(self.actual["rendered"]), sorted(self.expected["rendered"]))


if __name__ == "__main__":
    unittest.main()
