"""LLM rate limits (audit T1-3).

  - per sender: 8 messages to the model per rolling 10 minutes, 40 per
    IST calendar day; all senders: LLM_DAILY_CAP per IST day (default 500)
  - counted in the llm_usage SQLite table, so limits survive restarts
  - over a limit: one "Thanks! The team will reply to you here shortly."
    per sender per window, then silence; one Telegram alert per sender per
    day, one per day for the global cap
  - max_tokens lowered to 400 and passed through to DeepSeek

No live API call anywhere here: the model, every send and Telegram are
stubbed.

Run:  python -m unittest tests.test_rate_limit
"""
import io, json, os, sqlite3, sys, tempfile, unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-rate-limit-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

# Noon IST on a fixed date: every "today" window below sits well inside one
# IST calendar day, whenever the suite runs.
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=glam.IST).timestamp()
S1, S2, S3, S4 = (f"178000000000001{i}" for i in range(4))
WA_ID = "919812345671"
CANNED_BRAIN = "== BRAIN v-test =="
AUTO_JSON = json.dumps({"classification": "AUTO", "reply": "GS1 is ₹849 🤍"})


def named(calls, name):
    return [c for c in calls if c[0] == name]


def recorder(calls, name, ret=None):
    def f(*a, **k):
        calls.append((name, a, k))
        return ret
    return f


def db_rows(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def seed_usage(sender, timestamps, channel="Instagram"):
    conn = sqlite3.connect(glam.DB_PATH)
    conn.executemany(
        "INSERT INTO llm_usage (ts, channel, sender_id) VALUES (?, ?, ?)",
        [(ts, channel, sender) for ts in timestamps],
    )
    conn.commit()
    conn.close()


class RateLimitTestCase(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("llm_usage", "rate_limit_events", "instagram_logs", "message_logs"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        for var in ("LLM_RATE_LIMIT_DISABLED", "LLM_DAILY_CAP"):
            os.environ.pop(var, None)
            self.addCleanup(os.environ.pop, var, None)


class Admission(RateLimitTestCase):
    def test_eight_per_ten_minutes_then_refused(self):
        verdicts = [glam._llm_admission("Instagram", S1, now=NOW + i) for i in range(9)]
        self.assertEqual(verdicts[:8], [None] * 8)
        self.assertEqual(verdicts[8], glam.LIMIT_SENDER_WINDOW)

    def test_refused_messages_are_not_counted(self):
        for i in range(12):
            glam._llm_admission("Instagram", S1, now=NOW + i)
        self.assertEqual(db_rows("SELECT COUNT(*) FROM llm_usage WHERE sender_id = ?", (S1,)), [(8,)])

    def test_window_rolls_off_after_ten_minutes(self):
        seed_usage(S1, [NOW - glam.RATE_LIMIT_WINDOW_SECONDS - 1 - i for i in range(8)])
        self.assertIsNone(glam._llm_admission("Instagram", S1, now=NOW))

    def test_forty_per_ist_day(self):
        # 40 earlier today, 11 minutes apart, so none is inside the 10-min window.
        seed_usage(S1, [NOW - 660 * (i + 1) for i in range(40)])
        self.assertEqual(glam._llm_admission("Instagram", S1, now=NOW), glam.LIMIT_SENDER_DAY)

    def test_yesterday_does_not_count_toward_today(self):
        day_start = glam._ist_day_start(NOW)
        seed_usage(S1, [day_start - 60 * (i + 1) for i in range(40)])
        self.assertIsNone(glam._llm_admission("Instagram", S1, now=NOW))

    def test_one_sender_does_not_block_another(self):
        for i in range(8):
            glam._llm_admission("Instagram", S1, now=NOW + i)
        self.assertEqual(glam._llm_admission("Instagram", S1, now=NOW + 8), glam.LIMIT_SENDER_WINDOW)
        self.assertIsNone(glam._llm_admission("Instagram", S2, now=NOW + 9))

    def test_global_daily_cap_from_env_counts_both_channels(self):
        os.environ["LLM_DAILY_CAP"] = "3"
        self.assertIsNone(glam._llm_admission("WhatsApp", WA_ID, now=NOW))
        self.assertIsNone(glam._llm_admission("Instagram", S1, now=NOW + 1))
        self.assertIsNone(glam._llm_admission("Instagram", S2, now=NOW + 2))
        self.assertEqual(glam._llm_admission("Instagram", S3, now=NOW + 3), glam.LIMIT_GLOBAL_DAY)

    def test_global_cap_default_and_bad_values(self):
        self.assertEqual(glam._llm_daily_cap(), 500)
        for bad in ("lots", "0", "-5"):
            os.environ["LLM_DAILY_CAP"] = bad
            self.assertEqual(glam._llm_daily_cap(), 500)
        os.environ["LLM_DAILY_CAP"] = "1200"
        self.assertEqual(glam._llm_daily_cap(), 1200)

    def test_kill_switch_admits_everything(self):
        os.environ["LLM_RATE_LIMIT_DISABLED"] = "1"
        seed_usage(S1, [NOW - i for i in range(1, 20)])
        self.assertIsNone(glam._llm_admission("Instagram", S1, now=NOW))

    def test_db_error_fails_open(self):
        with patch.object(glam, "DB_PATH", tempfile.mkdtemp(prefix="glamshelf-rl-nodb-")):
            self.assertIsNone(glam._llm_admission("Instagram", S1, now=NOW))

    def test_counts_live_in_sqlite_so_they_survive_restarts(self):
        glam._llm_admission("Instagram", S1, now=NOW)
        self.assertEqual(
            db_rows("SELECT channel, sender_id FROM llm_usage"), [("Instagram", S1)]
        )

    def test_old_rows_are_pruned(self):
        seed_usage(S1, [NOW - glam.RATE_LIMIT_RETENTION_SECONDS - 5])
        glam._llm_admission("Instagram", S2, now=NOW)
        self.assertEqual(db_rows("SELECT COUNT(*) FROM llm_usage WHERE sender_id = ?", (S1,)), [(0,)])


class OverLimitHandling(RateLimitTestCase):
    def setUp(self):
        super().setUp()
        self.sent, self.alerts = [], []
        for p in (
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", lambda method, payload: self.alerts.append(payload["text"])),
        ):
            p.start()
            self.addCleanup(p.stop)

    def send(self, number, text):
        self.sent.append((number, text))
        return (True, "")

    def handle(self, sender, limit, now, channel="Instagram"):
        return glam._handle_llm_limit(channel, sender, limit, "spam", self.send, now=now)

    def test_one_notice_per_ten_minute_window_then_quiet(self):
        self.assertTrue(self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW))
        self.assertFalse(self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW + 60))
        self.assertFalse(self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW + 120))
        self.assertEqual(self.sent, [(S1, glam.RATE_LIMIT_NOTICE)])
        # A new window gets one more notice.
        self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW + glam.RATE_LIMIT_WINDOW_SECONDS + 1)
        self.assertEqual(len(self.sent), 2)

    def test_daily_limit_notice_once_per_day(self):
        for i in range(3):
            self.handle(S1, glam.LIMIT_SENDER_DAY, NOW + i * 700)
        self.assertEqual(len(self.sent), 1)

    def test_one_alert_per_sender_per_day(self):
        self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW)
        self.handle(S1, glam.LIMIT_SENDER_WINDOW, NOW + 700)
        self.handle(S1, glam.LIMIT_SENDER_DAY, NOW + 800)
        self.handle(S2, glam.LIMIT_SENDER_WINDOW, NOW + 900)
        self.assertEqual(len(self.alerts), 2)
        self.assertIn(S1, self.alerts[0])
        self.assertIn(S2, self.alerts[1])

    def test_global_cap_one_notice_per_sender_and_one_alert(self):
        for s in (S1, S2, S1):
            self.handle(s, glam.LIMIT_GLOBAL_DAY, NOW, channel="WhatsApp")
        self.assertEqual([n for n, _ in self.sent], [S1, S2])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("LLM_DAILY_CAP", self.alerts[0])

    def test_failed_notice_is_not_retried_every_message(self):
        failing = lambda number, text: (self.sent.append((number, text)), (False, "HTTP 400"))[1]
        glam._handle_llm_limit("Instagram", S1, glam.LIMIT_SENDER_WINDOW, "x", failing, now=NOW)
        glam._handle_llm_limit("Instagram", S1, glam.LIMIT_SENDER_WINDOW, "x", failing, now=NOW + 5)
        self.assertEqual(len(self.sent), 1)


class EndToEnd(RateLimitTestCase):
    """Ten quick messages from one sender: eight reach the model, the
    ninth gets the notice, the tenth gets nothing."""

    def setUp(self):
        super().setUp()
        self.calls = []
        r = lambda name, ret=None: recorder(self.calls, name, ret)
        self.common = [
            patch.object(glam, "_load_brain_cached", r("_load_brain_cached", CANNED_BRAIN)),
            patch.object(glam, "get_live_inventory", r("get_live_inventory", "")),
            # The live prices a real inventory fetch would have cached —
            # without them the output guard holds "₹849" for approval.
            patch.dict(glam._inventory_cache, {"prices": {849.0}}),
            patch.object(glam, "get_live_policies", r("get_live_policies", "")),
            patch.object(glam, "_rag_retrieve", r("_rag_retrieve", "")),
            patch.object(glam, "ask_claude", r("ask_claude", AUTO_JSON)),
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", r("_telegram_api")),
        ]

    def test_instagram(self):
        r = lambda name, ret=None: recorder(self.calls, name, ret)
        patches = self.common + [
            patch.object(glam, "_is_paused", r("_is_paused", False)),
            patch.object(glam, "_udit_replied_recently_ig", r("_udit_replied_recently_ig", False)),
            patch.object(glam, "_lookup_recent_order", r("_lookup_recent_order", "")),
            patch.object(glam, "_load_instagram_history", r("_load_instagram_history", [])),
            patch.object(glam, "_send_instagram_reply", r("_send_instagram_reply", (True, ""))),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            for i in range(10):
                glam._process_instagram_event({
                    "sender": {"id": S1}, "recipient": {"id": "page"},
                    "timestamp": i, "message": {"mid": "", "text": f"question {i}"},
                })
        self.assertEqual(len(named(self.calls, "ask_claude")), 8)
        sends = named(self.calls, "_send_instagram_reply")
        self.assertEqual(len(sends), 9)
        self.assertEqual(sends[-1][1], (S1, glam.RATE_LIMIT_NOTICE))
        self.assertEqual(len(named(self.calls, "_telegram_api")), 1)
        limited_rows = db_rows(
            "SELECT message_text, reply_text FROM instagram_logs WHERE source = 'RATE_LIMITED_IG' ORDER BY id"
        )
        self.assertEqual(limited_rows, [("question 8", glam.RATE_LIMIT_NOTICE), ("question 9", None)])

    def test_whatsapp(self):
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)
        r = lambda name, ret=None: recorder(self.calls, name, ret)
        patches = self.common + [
            patch.object(glam, "_is_outbound_event", r("_is_outbound_event", False)),
            patch.object(glam, "_handle_pause_directive", r("_handle_pause_directive", None)),
            patch.object(glam, "_is_paused", r("_is_paused", False)),
            patch.object(glam, "_udit_replied_recently", r("_udit_replied_recently", False)),
            patch.object(glam, "_check_recent_human_reply", r("_check_recent_human_reply", False)),
            patch.object(glam, "_load_wati_history", r("_load_wati_history", [])),
            patch.object(glam, "_lookup_recent_order", r("_lookup_recent_order", "")),
            patch.object(glam, "send_whatsapp_reply", r("send_whatsapp_reply", (True, ""))),
        ]
        client = glam.app.test_client()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            for i in range(10):
                resp = client.post("/webhook", json={
                    "type": "text", "waId": WA_ID, "senderName": "T",
                    "text": f"question {i}", "id": "",
                })
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(named(self.calls, "ask_claude")), 8)
        sends = named(self.calls, "send_whatsapp_reply")
        self.assertEqual(len(sends), 9)
        self.assertEqual(sends[-1][1], (WA_ID, glam.RATE_LIMIT_NOTICE))
        self.assertEqual(
            db_rows("SELECT reply_text FROM message_logs WHERE status = 'RATE_LIMITED' ORDER BY id"),
            [(glam.RATE_LIMIT_NOTICE,), (None,)],
        )


class OutputBudget(unittest.TestCase):
    def test_max_tokens_is_400_and_truncation_is_logged(self):
        captured = {}
        fake = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"classification": "AUTO", "reply": "cut o'),
                finish_reason="length",
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=400),
        )

        def fake_create(**kwargs):
            captured.update(kwargs)
            return fake

        buf = io.StringIO()
        with patch.object(glam.deepseek_client.chat.completions, "create", fake_create), \
             redirect_stdout(buf):
            glam.ask_claude("brain", "hi", "")
        self.assertEqual(glam.MAX_TOKENS, 400)
        self.assertEqual(captured["max_tokens"], 400)
        self.assertIn("hit max_tokens=400", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
