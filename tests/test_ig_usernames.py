"""Instagram usernames in the founder's notices (PR 5b).

The LEAD, DRAFT, ESCALATE, paused / rate-limit forward, ORDER / RESTOCK,
photo and media notices show
"https://instagram.com/username (sender 1784…)" instead of the bare
numeric id — a link to the Instagram profile, never "@username", which
Telegram would link to a Telegram account of that name. The username
comes from Meta's User Profile API:

  - looked up only when a notice is built, which is always after the
    customer's reply went out, so it can't delay a reply;
  - at most 3s (less when the reply budget is nearly spent), cached for a
    week (an hour after a failure), and the notice falls back to the id;
  - only the `username` field, shown only when it looks like a username;
  - no token or IG_USERNAME_LOOKUP_DISABLED=1 means no lookup at all.

No live API call anywhere here: Meta, the model and Telegram are stubbed.

Run:  python -m unittest tests.test_ig_usernames
"""
import io, json, os, sqlite3, sys, tempfile, unittest, uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ig-usernames-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import requests
import app as glam

REAL_SEND_DRAFT = glam.send_draft_for_approval

SENDER = "17841400000000777"
TOKEN = "TEST-TOKEN-NOT-REAL-0123456789"


def ok_response(payload):
    return SimpleNamespace(ok=True, status_code=200, text=json.dumps(payload), json=lambda: payload)


class Base(unittest.TestCase):
    def setUp(self):
        with redirect_stdout(io.StringIO()):
            glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts",
                      "rate_limit_events", "ig_usernames"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED",
                    "LLM_RATE_LIMIT_DISABLED", "IG_USERNAME_LOOKUP_DISABLED"):
            os.environ.pop(var, None)
        self.events = []         # ("send", text) / ("lookup", url) / ("notice", ...) in order
        self.lookup_result = ok_response({"username": "glam.tester_1", "id": SENDER})
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", TOKEN),
            patch.object(glam.requests, "get", self._get),
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", self._tg),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "send_draft_for_approval", self._draft),
            patch.object(glam, "_persist_seen_id", lambda mid: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _get(self, url, params=None, timeout=None, **kw):
        self.events.append(("lookup", url, dict(params or {}), timeout))
        if isinstance(self.lookup_result, Exception):
            raise self.lookup_result
        return self.lookup_result

    def _send(self, sender_id, text):
        self.events.append(("send", text))
        return True, ""

    def _tg(self, method, payload):
        self.events.append(("telegram", payload["text"]))
        return {"ok": True}

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.events.append(("notice", classification, kwargs.get("sender_info")))

    def _draft(self, **kwargs):
        self.events.append(("draft", kwargs.get("customer_name"), kwargs.get("customer_number")))
        return True

    def kinds(self):
        return [e[0] for e in self.events]

    def lookups(self):
        return [e for e in self.events if e[0] == "lookup"]

    def dm(self, text, classification="AUTO", reply="ok 🤍", tag=""):
        raw = json.dumps({"classification": classification, "reply": reply, "tag": tag})
        with patch.object(glam, "draft_reply_logic", lambda *a, **k: (classification, reply, raw)), \
                redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"},
                "timestamp": 1, "message": {"mid": f"u.{uuid.uuid4().hex}", "text": text},
            })

    def media(self, att_type, payload=None):
        att = {"type": att_type, "payload": payload or {"url": "https://lookaside.example/x"}}
        with redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"},
                "timestamp": 1, "message": {"mid": f"u.{uuid.uuid4().hex}", "attachments": [att]},
            })


PROFILE = "https://instagram.com/glam.tester_1"
LABEL = f"{PROFILE} (sender {SENDER})"


class Lookup(Base):
    def call(self):
        with redirect_stdout(io.StringIO()) as out:
            name = glam._ig_username(SENDER)
        return name, out.getvalue()

    def test_asks_only_for_the_username_and_caches_it(self):
        self.assertEqual(self.call()[0], "glam.tester_1")
        self.assertEqual(self.call()[0], "glam.tester_1")
        (lookup,) = self.lookups()
        _, url, params, timeout = lookup
        self.assertEqual(url, f"{glam.INSTAGRAM_API_BASE}/{SENDER}")
        self.assertEqual(params, {"fields": "username", "access_token": TOKEN})
        self.assertEqual(timeout, 3)
        self.assertEqual(glam._ig_sender_label(SENDER), LABEL)

    def test_cache_lasts_a_week(self):
        now = [50_000.0]
        with patch.object(glam.time, "time", lambda: now[0]):
            self.call()
            now[0] += 6 * 86400
            self.call()
            now[0] += 2 * 86400
            self.call()
        self.assertEqual(len(self.lookups()), 2)

    def test_a_failed_lookup_shows_the_id_and_retries_after_an_hour(self):
        self.lookup_result = requests.Timeout(f"read timed out: /x?access_token={TOKEN}")
        now = [50_000.0]
        with patch.object(glam.time, "time", lambda: now[0]):
            name, out = self.call()
            self.assertEqual(name, "")
            self.assertNotIn(TOKEN, out)                       # redacted
            self.assertEqual(glam._ig_sender_label(SENDER), f"sender {SENDER}")
            self.assertEqual(len(self.lookups()), 1)           # cached failure
            now[0] += 3601
            self.lookup_result = ok_response({"username": "back_again"})
            self.assertEqual(self.call()[0], "back_again")
        self.assertEqual(len(self.lookups()), 2)

    def test_http_errors_and_odd_values_show_the_id(self):
        for result in (
            SimpleNamespace(ok=False, status_code=400, text=f"bad token {TOKEN}", json=lambda: {}),
            ok_response({"username": "<b>evil</b>"}),
            ok_response({"username": "x" * 31}),
            ok_response({"name": "No Username"}),
        ):
            with self.subTest(result=result):
                with redirect_stdout(io.StringIO()):
                    sqlite3.connect(glam.DB_PATH).execute("DELETE FROM ig_usernames").connection.commit()
                self.lookup_result = result
                name, out = self.call()
                self.assertEqual(name, "")
                self.assertNotIn(TOKEN, out)

    def test_no_token_kill_switch_or_odd_id_means_no_lookup(self):
        with patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", ""):
            self.assertEqual(self.call()[0], "")
        with patch.dict(os.environ, {"IG_USERNAME_LOOKUP_DISABLED": "1"}):
            self.assertEqual(self.call()[0], "")
        self.assertEqual(glam._ig_username("me/messages"), "")
        self.assertEqual(self.lookups(), [])

    def test_inside_the_reply_budget(self):
        clock = [100.0]
        with patch.object(glam, "_clock", lambda: clock[0]):
            glam._reply_budget.deadline = clock[0] + 0.5        # under 1s left: no lookup
            try:
                self.assertEqual(self.call()[0], "")
                glam._reply_budget.deadline = clock[0] + 2      # 2s left: a 2s lookup
                self.assertEqual(self.call()[0], "glam.tester_1")
            finally:
                glam._reply_budget.deadline = None
        (lookup,) = self.lookups()
        self.assertEqual(lookup[3], 2)


class NoticesNameTheSender(Base):
    """In every path the customer's reply goes out before the lookup."""

    def test_escalate(self):
        self.dm("I want to speak to the owner", "ESCALATE", "Sorry 🤍")
        self.assertEqual(self.kinds(), ["send", "lookup", "notice"])
        self.assertEqual(self.events[0][1], glam.BRAIN_HOLDING_LINE)
        self.assertEqual(self.events[2], ("notice", "ESCALATE", f"Instagram DM — {LABEL}"))

    def test_lead(self):
        self.dm("hey, udit asked me to test this bot. do u ship to pune?", "AUTO",
                "Yes, we ship across India 🤍", tag="LEAD")
        self.assertEqual(self.kinds(), ["send", "lookup", "notice"])
        self.assertEqual(self.events[2], ("notice", "LEAD", f"Instagram DM — {LABEL}"))

    def test_draft(self):
        self.dm("my GS2 tray arrived damaged", "DRAFT+APPROVE", "So sorry — could you email us…")
        self.assertEqual(self.kinds()[:3], ["send", "lookup", "draft"])
        self.assertEqual(self.events[0][1], glam.BRAIN_HOLDING_LINE)       # the handoff line
        self.assertEqual(self.events[2], ("draft", PROFILE, SENDER))

    def test_draft_fallback_notice_when_the_buttons_fail(self):
        with patch.object(glam, "send_draft_for_approval", lambda **kw: False):
            self.dm("my GS2 tray arrived damaged", "DRAFT+APPROVE", "So sorry 🤍")
        self.assertIn(("notice", "DRAFT+APPROVE", f"Instagram DM — {LABEL}"), self.events)

    def test_paused_forward(self):
        self.dm("I want to speak to the owner", "ESCALATE", "Sorry 🤍")
        self.events.clear()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM ig_usernames")              # make the forward look it up
        conn.commit()
        conn.close()
        self.dm("hello?? can u just send me the payment link")
        self.assertEqual(self.kinds(), ["send", "lookup", "telegram"])
        self.assertEqual(self.events[0][1], glam.IG_DRAFT_ACK_LINE)
        self.assertIn(f"From: Instagram DM — {LABEL}", self.events[2][1])

    def test_a_failing_lookup_never_stops_the_reply(self):
        self.lookup_result = requests.ConnectionError("graph.instagram.com unreachable")
        self.dm("I want to speak to the owner", "ESCALATE", "Sorry 🤍")
        self.assertEqual(self.kinds(), ["send", "lookup", "notice"])
        self.assertEqual(self.events[2], ("notice", "ESCALATE", f"Instagram DM — sender {SENDER}"))

    def test_no_notice_uses_a_bare_at_handle(self):
        # "@handle" in Telegram links to a Telegram account of that name.
        self.dm("I want to speak to the owner", "ESCALATE", "Sorry 🤍")
        self.dm("hello?? can u just send me the payment link")
        texts = [str(e) for e in self.events if e[0] in ("notice", "telegram", "draft")]
        self.assertEqual(len(texts), 2)
        for text in texts:
            self.assertIn(PROFILE, text)
            self.assertNotIn("@glam.tester_1", text)

    def test_the_draft_card_shows_the_link_and_the_id(self):
        captured = []

        def tg(method, payload):
            captured.append(payload)
            return {"ok": True, "result": {"message_id": 1, "chat": {"id": 123}}}

        with patch.object(glam, "send_draft_for_approval", REAL_SEND_DRAFT), \
                patch.object(glam, "TELEGRAM_BOT_TOKEN", "123:TEST"), \
                patch.object(glam, "_telegram_api", tg):
            self.dm("my GS2 tray arrived damaged", "DRAFT+APPROVE", "So sorry 🤍")
        (card,) = [p["text"] for p in captured if p["text"].startswith("🟡")]
        self.assertIn(f"From: {PROFILE} ({SENDER})", card)
        self.assertNotIn("@glam.tester_1", card)

    # ORDER / RESTOCK heads-ups, photo and media notices (4 Oct 2026 brief;
    # until then the ORDER heads-up showed the id only).
    def test_order_heads_up(self):
        self.dm("where is my order #1234?", "AUTO", "I've passed this to the team 🤍", tag="ORDER")
        self.assertEqual(self.kinds(), ["send", "lookup", "notice"])
        self.assertEqual(self.events[2], ("notice", "ORDER", f"Instagram DM — {LABEL}"))

    def test_restock_heads_up(self):
        self.dm("notify me when kawaii is back", "AUTO", "Kawaii is sold out right now 🤍", tag="RESTOCK")
        self.assertEqual(self.kinds(), ["send", "lookup", "notice"])
        self.assertEqual(self.events[2], ("notice", "RESTOCK", f"Instagram DM — {LABEL}"))

    def test_photo(self):
        self.media("image")
        self.assertEqual(self.kinds(), ["send", "lookup", "telegram"])
        self.assertEqual(self.events[0][1], glam.INSTAGRAM_PHOTO_REPLY)
        self.assertTrue(self.events[2][1].startswith(f"📷 Instagram photo from {LABEL}\n"), self.events[2][1])

    def test_voice_note(self):
        self.media("audio")
        self.assertEqual(self.kinds(), ["send", "lookup", "telegram"])
        self.assertEqual(self.events[0][1], glam.INSTAGRAM_MEDIA_REPLY)
        self.assertTrue(self.events[2][1].startswith(f"🎤 Instagram voice note from {LABEL}\n"), self.events[2][1])

    def test_story_mention_gets_no_reply_and_a_linked_notice(self):
        self.media("story_mention")
        self.assertEqual(self.kinds(), ["lookup", "telegram"])
        self.assertTrue(self.events[1][1].startswith(f"📣 Instagram story mention from {LABEL}\n"), self.events[1][1])

    def test_a_failed_lookup_leaves_these_notices_as_they_were(self):
        self.lookup_result = requests.ConnectionError("graph.instagram.com unreachable")
        self.dm("where is my order #1234?", "AUTO", "I've passed this to the team 🤍", tag="ORDER")
        self.media("image")
        self.assertIn(("notice", "ORDER", f"Instagram DM — sender {SENDER}"), self.events)
        (photo,) = [e[1] for e in self.events if e[0] == "telegram"]
        self.assertTrue(photo.startswith(f"📷 Instagram photo from sender {SENDER}\n"), photo)


if __name__ == "__main__":
    unittest.main()
