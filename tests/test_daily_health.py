"""Daily health message on Telegram.

From about 6 to 22 Sep real customers got no replies (expired Instagram
token) and nobody noticed. Now every night at DAILY_HEALTH_HOUR_IST the
Telegram chat gets one short 🟢 / 🟡 / 🔴 message, and "/health" sends it on
demand. Checked here:
  - it's timer-driven: a day with zero traffic still sends (🟡);
  - once per IST day: a second tick, and a fresh process on the same DB (a
    restart), don't resend; a send Telegram refused is retried; nothing
    before the hour; kill switch DAILY_HEALTH_DISABLED; a crashing tick
    doesn't kill the scheduler thread;
  - the 🔴 / 🟡 / 🟢 rules — holding lines aren't replies, drafts and
    escalations aren't missed replies (a draft-only day isn't red), a
    DeepSeek 402 is red;
  - DeepSeek balance ok / low / critical / not available / HTTP error /
    network error / CNY only — never a crash, never the key;
  - TEST_SENDER_IDS are left out of every number; no customer id or text
    in the message; the brand name comes from the brand file;
  - /health only from the authorised chat, and never sent to a customer as
    an edited draft.

No network: requests.get (Meta, DeepSeek) and Telegram are stubbed; the
subprocesses stub them too.

Run:  python -m unittest tests.test_daily_health
"""
import hashlib, hmac, io, json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-daily-health-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
for _name in ("BRAND_CONFIG_PATH", "DAILY_HEALTH_DISABLED", "TEST_SENDER_IDS"):
    os.environ.pop(_name, None)
import app as glam
import requests

IST = timezone(timedelta(hours=5, minutes=30))
NIGHT = datetime(2026, 9, 28, 22, 10, tzinfo=IST).timestamp()   # Mon 28 Sep, 22:10 IST
DAY = 86400
CUSTOMER_IG = "17800000000000123"
CUSTOMER_WA = "919800000001"
TEST_IG = "17800000000000999"
TEST_WA = "919000000777"
SECRET_TEXT = "my order 4471 for Priya Sharma"   # must never reach the message
DEEPSEEK_KEY = "sk-test-deepseek-key"
MARK = "@@RESULT@@"


def resp(status=200, body=None):
    r = Mock(ok=200 <= status < 300, status_code=status, text=json.dumps(body))
    r.json.return_value = body if body is not None else {}
    return r


def usd(amount, available=True):
    return {"is_available": available, "balance_infos": [
        {"currency": "USD", "total_balance": amount, "granted_balance": "0.00",
         "topped_up_balance": amount}]}


def ig_row(source, ago=3600, sender=CUSTOMER_IG, msg=SECRET_TEXT, reply="ok"):
    conn = sqlite3.connect(glam.DB_PATH)
    conn.execute(
        "INSERT INTO instagram_logs (sender_id, message_text, reply_text, timestamp, source, logged_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sender, msg, reply, "", source, glam._sqlite_utc(NIGHT - ago)),
    )
    conn.commit(); conn.close()


def wa_row(status, ago=3600, wa=CUSTOMER_WA, msg=SECRET_TEXT, reply="ok", error=None):
    conn = sqlite3.connect(glam.DB_PATH)
    conn.execute(
        "INSERT INTO message_logs (ts, wa_id, sender_name, msg_text, status, reply_text, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (NIGHT - ago, wa, "Priya Sharma", msg, status, reply, error),
    )
    conn.commit(); conn.close()


def pending_draft(draft_id, customer, channel, ago=600, **extra):
    conn = sqlite3.connect(glam.DB_PATH)
    data = {"customer_number": customer, "channel": channel, "reply_text": "draft text",
            "customer_message": SECRET_TEXT, "created_at": NIGHT - ago, **extra}
    conn.execute("INSERT INTO pending_drafts (draft_id, data, created_at) VALUES (?, ?, ?)",
                 (draft_id, json.dumps(data), NIGHT - ago))
    conn.commit(); conn.close()


def run_python(code: str, **env) -> subprocess.CompletedProcess:
    """`code` in a fresh interpreter from the project folder (offline shim
    and all of this environment included) — a restart, as far as the
    module-level state goes."""
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


def result_json(result: subprocess.CompletedProcess):
    for line in result.stdout.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise AssertionError(f"no result line:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")


class HealthTestCase(unittest.TestCase):
    """A green day's defaults: Instagram connected, WATI not, token with 40
    days left, $12.40 on DeepSeek, an Instagram webhook event 10 min ago."""

    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "message_logs", "pending_drafts", "health_marks", "scheduled_jobs"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        glam._webhook_last_seen.clear()
        self.sent = []           # every Telegram sendMessage payload
        self.telegram_ok = True
        self.balance = resp(200, usd("12.40"))
        self.meta = resp(200, {"id": "1"})
        self.balance_headers = []

        def telegram(method, payload):
            self.sent.append(payload)
            return {"ok": True} if self.telegram_ok else None

        def get(url, params=None, headers=None, timeout=None, **kw):
            if url == glam.DEEPSEEK_BALANCE_URL:
                self.balance_headers.append(headers)
                if isinstance(self.balance, Exception):
                    raise self.balance
                return self.balance
            return self.meta

        for p in (
            patch.object(glam, "TELEGRAM_BOT_TOKEN", "123:abc"),
            patch.object(glam, "TELEGRAM_CHAT_ID", "5"),
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", "IGAAtest-token"),
            patch.object(glam, "INSTAGRAM_APP_ID", ""),
            patch.object(glam, "INSTAGRAM_APP_SECRET", ""),
            patch.object(glam, "_ig_token_expires_at", NIGHT + 40 * DAY),
            patch.object(glam, "WATI_API_KEY", ""),
            patch.object(glam, "WATI_ENDPOINT", ""),
            patch.object(glam, "DAILY_HEALTH_HOUR_IST", 22),
            patch.object(glam, "DEEPSEEK_LOW_BALANCE_USD", 2.0),
            patch.object(glam, "_telegram_api", telegram),
            patch.object(glam.requests, "get", get),
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": DEEPSEEK_KEY}),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DAILY_HEALTH_DISABLED", None)
        os.environ.pop("TEST_SENDER_IDS", None)
        glam._health_mark("webhook:instagram", NIGHT - 600)

    def report(self, now=NIGHT):
        with redirect_stdout(io.StringIO()):
            return glam._build_health_report(now)

    def tick(self, now=NIGHT):
        with redirect_stdout(io.StringIO()):
            return glam._daily_health_if_due(now)

    def with_wati(self):
        for p in (patch.object(glam, "WATI_API_KEY", "k"), patch.object(glam, "WATI_ENDPOINT", "https://wati.example")):
            p.start()
            self.addCleanup(p.stop)
        glam._health_mark("webhook:whatsapp", NIGHT - 600)


class ScheduleTest(HealthTestCase):
    def test_zero_traffic_day_still_sends(self):
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM health_marks")
        conn.commit(); conn.close()
        self.assertTrue(self.tick())
        (msg,) = self.sent
        self.assertEqual(msg["chat_id"], "5")
        self.assertTrue(msg["text"].startswith("🟡 Warning — The Glam Shelf — Mon 28 Sep 2026"))
        self.assertIn("• No Instagram webhook event of any kind in 24h", msg["text"])
        self.assertIn("Instagram, last 24h: 0 messages in · 0 auto replies", msg["text"])
        self.assertIn("Last webhook event of any kind: Instagram none recorded yet", msg["text"])

    def test_nothing_before_the_hour_then_once(self):
        before = datetime(2026, 9, 28, 21, 59, tzinfo=IST).timestamp()
        self.assertFalse(self.tick(before))
        self.assertEqual(self.sent, [])
        at_hour = datetime(2026, 9, 28, 22, 0, tzinfo=IST).timestamp()
        self.assertTrue(self.tick(at_hour))
        self.assertFalse(self.tick(at_hour + 300))
        self.assertFalse(self.tick(datetime(2026, 9, 28, 23, 59, tzinfo=IST).timestamp()))
        self.assertEqual(len(self.sent), 1)

    def test_next_day_sends_again(self):
        self.assertTrue(self.tick(NIGHT))
        self.assertFalse(self.tick(NIGHT + 6 * 3600))            # 04:10 IST next day, before the hour
        self.assertTrue(self.tick(NIGHT + DAY))
        self.assertEqual(len(self.sent), 2)

    def test_other_send_hour(self):
        with patch.object(glam, "DAILY_HEALTH_HOUR_IST", 9):
            self.assertTrue(self.tick(datetime(2026, 9, 28, 9, 5, tzinfo=IST).timestamp()))

    def test_refused_send_is_retried(self):
        self.telegram_ok = False
        self.assertFalse(self.tick(NIGHT))
        conn = sqlite3.connect(glam.DB_PATH)
        self.assertIsNone(conn.execute(
            "SELECT last_run FROM scheduled_jobs WHERE job = ?", (glam.DAILY_HEALTH_JOB,)).fetchone())
        conn.close()
        self.telegram_ok = True
        self.assertTrue(self.tick(NIGHT + 300))
        self.assertFalse(self.tick(NIGHT + 600))
        self.assertEqual(len(self.sent), 2)                         # one refused, one delivered

    def test_refused_send_keeps_yesterdays_mark(self):
        self.assertTrue(self.tick(NIGHT - DAY))
        self.telegram_ok = False
        self.assertFalse(self.tick(NIGHT))
        conn = sqlite3.connect(glam.DB_PATH)
        (last,) = conn.execute(
            "SELECT last_run FROM scheduled_jobs WHERE job = ?", (glam.DAILY_HEALTH_JOB,)).fetchone()
        conn.close()
        self.assertEqual(last, NIGHT - DAY)

    def test_once_a_day_survives_a_restart(self):
        """The 'sent today' mark lives in the DB: a fresh process (a restart)
        on the same DB_PATH doesn't send again, and sends the next day."""
        db = os.path.join(tempfile.mkdtemp(prefix="glamshelf-daily-health-restart-"), "test.db")
        code = (
            "import io, json, contextlib, sys\n"
            "from unittest.mock import patch, Mock\n"
            "with contextlib.redirect_stdout(io.StringIO()):\n"
            "    import app\n"
            "sent = []\n"
            "bal = Mock(ok=True, status_code=200)\n"
            "bal.json.return_value = {'is_available': True, 'balance_infos': "
            "[{'currency': 'USD', 'total_balance': '9.00'}]}\n"
            "with patch.object(app, '_telegram_api', lambda m, p: (sent.append(p['text']), {'ok': True})[1]), \\\n"
            "     patch.object(app.requests, 'get', return_value=bal), contextlib.redirect_stdout(io.StringIO()):\n"
            "    result = app._daily_health_if_due(float(sys.argv[1]))\n"
            f"print({MARK!r} + json.dumps({{'result': result, 'sent': len(sent)}}))\n"
        )
        env = dict(DB_PATH=db, GITHUB_TOKEN="", GITHUB_REPO="", TELEGRAM_BOT_TOKEN="123:abc",
                   TELEGRAM_CHAT_ID="5", DEEPSEEK_API_KEY="k", SECRET_KEY="t", APP_PASSWORD="t",
                   DASHBOARD_KEY="t", DAILY_HEALTH_HOUR_IST="22")

        def run(now):
            result = subprocess.run(
                [sys.executable, "-c", code, str(now)], cwd=ROOT, env={**os.environ, **env},
                capture_output=True, text=True, encoding="utf-8", timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            return result_json(result)

        self.assertEqual(run(NIGHT), {"result": True, "sent": 1})
        self.assertEqual(run(NIGHT + 900), {"result": False, "sent": 0})     # restarted, same night
        self.assertEqual(run(NIGHT + DAY), {"result": True, "sent": 1})      # next night

    def test_kill_switch(self):
        with patch.dict(os.environ, {"DAILY_HEALTH_DISABLED": "1"}), \
             patch.object(glam.threading, "Thread") as thread, redirect_stdout(io.StringIO()) as buf:
            self.assertFalse(glam._daily_health_if_due(NIGHT))
            glam._start_daily_health_loop()
        thread.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertIn("off (DAILY_HEALTH_DISABLED)", buf.getvalue())

    def test_loop_starts_when_telegram_is_set(self):
        with patch.object(glam.threading, "Thread") as thread, redirect_stdout(io.StringIO()) as buf:
            glam._start_daily_health_loop()
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["target"], glam._daily_health_loop)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertIn("22:00 IST", buf.getvalue())

    def test_no_telegram_no_loop(self):
        with patch.object(glam, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(glam.threading, "Thread") as thread, redirect_stdout(io.StringIO()):
            glam._start_daily_health_loop()
            self.assertFalse(glam._daily_health_if_due(NIGHT))
        thread.assert_not_called()

    def test_a_crashing_tick_does_not_kill_the_loop(self):
        class Stop(BaseException):
            pass
        real_sleep, sleeps, ticks = time.sleep, [], []

        def fake_sleep(seconds):
            if threading.current_thread() is not threading.main_thread():
                return real_sleep(seconds)        # another test-process thread
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise Stop

        def boom(*a, **k):
            ticks.append(1)
            raise RuntimeError("boom")

        with patch.object(glam.time, "sleep", fake_sleep), \
             patch.object(glam, "_daily_health_if_due", boom), redirect_stdout(io.StringIO()):
            with self.assertRaises(Stop):
                glam._daily_health_loop()
        self.assertEqual(len(ticks), 2)
        self.assertEqual(sleeps, [glam.DAILY_HEALTH_TICK_SECONDS] * 3)

    def test_bad_env_values_fall_back(self):
        for name, raw, default in (("DAILY_HEALTH_HOUR_IST", "25", 22), ("DAILY_HEALTH_HOUR_IST", "ten", 22),
                                   ("DEEPSEEK_LOW_BALANCE_USD", "-1", 2.0), ("DEEPSEEK_LOW_BALANCE_USD", "abc", 2.0)):
            with self.subTest(name=name, raw=raw), patch.dict(os.environ, {name: raw}), \
                 redirect_stdout(io.StringIO()) as buf:
                cast = int if name == "DAILY_HEALTH_HOUR_IST" else float
                self.assertEqual(glam._health_env_number(name, default, cast, lambda v: 0 <= v <= 23), default)
                self.assertIn("isn't valid", buf.getvalue())
        with patch.dict(os.environ, {"DAILY_HEALTH_HOUR_IST": " 7 "}):
            self.assertEqual(glam._health_env_number("DAILY_HEALTH_HOUR_IST", 22, int, lambda h: 0 <= h <= 23), 7)


class ColourRulesTest(HealthTestCase):
    def test_green_day(self):
        for _ in range(3):
            ig_row(None)
        level, text = self.report()
        self.assertEqual(level, "green")
        self.assertTrue(text.startswith("🟢 All good — The Glam Shelf — Mon 28 Sep 2026\n\n"))
        self.assertNotIn("•", text)
        self.assertIn("Instagram, last 24h: 3 messages in · 3 auto replies · 0 drafts waiting · "
                      "0 drafts approved · 0 escalations · 0 holding lines · 0 send failures", text)
        self.assertIn("Last reply delivered to a customer: 1 h ago (Instagram)", text)
        self.assertIn("Last webhook event of any kind: Instagram 10 min ago", text)
        self.assertIn("Instagram token: 40 days left", text)
        self.assertIn("DeepSeek balance: $12.40", text)

    def test_messages_in_but_nothing_delivered_is_red(self):
        ig_row("AUTO_FAILED_IG")
        ig_row("AUTO_FAILED_IG")
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("• Instagram: 2 messages needed a reply — none delivered", text)
        self.assertIn("• Instagram: 2 send failures", text)

    def test_holding_lines_are_not_replies(self):
        """A day of model failures that only sent holding lines is red."""
        ig_row("PIPELINE_HOLDING_IG", reply=glam.BRAIN_HOLDING_LINE)
        ig_row("PIPELINE_HOLDING_IG", reply=glam.BRAIN_HOLDING_LINE)
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("• Instagram: 2 messages needed a reply — none delivered", text)
        self.assertIn("• Instagram: 2 reply errors (model trouble)", text)
        self.assertIn("2 holding lines", text)
        self.assertIn("Last reply delivered to a customer: none on record", text)

    def test_draft_only_day_is_not_red(self):
        for i in range(3):
            ig_row("DRAFT_PENDING_IG", reply=None)
            pending_draft(f"d{i}", CUSTOMER_IG, "Instagram")
        ig_row("DRAFT_HANDOFF_IG", msg="", reply=glam.BRAIN_HOLDING_LINE)
        level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("3 messages in · 0 auto replies · 3 drafts waiting · 0 drafts approved", text)
        self.assertIn("1 holding line", text)

    def test_approved_drafts_count_as_replies(self):
        ig_row("DRAFT_PENDING_IG", reply=None)
        ig_row("DRAFT_SENT_IG")
        level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("1 draft approved", text)
        self.assertIn("Last reply delivered to a customer: 1 h ago (Instagram)", text)

    def test_escalation_only_day_is_not_red(self):
        ig_row("ESCALATE_IG", reply=None)
        level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("1 message in · 0 auto replies", text)
        self.assertIn("1 escalation", text)

    def test_any_send_failure_is_red(self):
        for _ in range(5):
            ig_row(None)
        ig_row("PHOTO_IG_FAILED")
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("• Instagram: 1 send failure", text)
        self.assertNotIn("needed a reply", text)

    def test_deepseek_402_is_red(self):
        class Http402(Exception):
            status_code = 402
        create = Mock(side_effect=Http402("Error code: 402 - Insufficient Balance"))
        with patch.object(glam.deepseek_client.chat.completions, "create", create), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(Http402):
                glam.ask_claude("brain", "hi", "")
        mark = glam._health_mark_get("deepseek_402")
        self.assertIsNotNone(mark)
        level, text = self.report(now=mark + 60)
        self.assertEqual(level, "red")
        self.assertIn('• DeepSeek "402 Insufficient Balance" in the last 24h', text)
        # A day later it's history, not a problem.
        self.assertNotIn("402", self.report(now=mark + DAY + 60)[1])

    def test_other_model_errors_are_not_recorded_as_402(self):
        class Http500(Exception):
            status_code = 500
        with patch.object(glam.deepseek_client.chat.completions, "create", Mock(side_effect=Http500())), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(Http500):
                glam.ask_claude("brain", "hi", "")
        self.assertIsNone(glam._health_mark_get("deepseek_402"))

    def test_whatsapp_402_error_row_is_red(self):
        self.with_wati()
        wa_row("AUTO")
        wa_row("ERROR", reply=None,
               error="Error code: 402 - {'error': {'message': 'Insufficient Balance', 'type': 'unknown_error'}}")
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn('• DeepSeek "402 Insufficient Balance" in the last 24h', text)
        self.assertIn("• WhatsApp: 1 reply error (model trouble)", text)

    def test_token_check_failed_is_red_without_the_token_alert(self):
        self.meta = resp(400, {"error": {"message": "Session has expired", "type": "OAuthException", "code": 190}})
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("• Instagram token check FAILED", text)
        self.assertIn("Instagram token: expiry unknown (basic check FAILED)", text)
        self.assertEqual(self.sent, [])          # the probe itself alerts nobody
        self.assertNotIn("IGAAtest-token", text)

    def test_token_expiry_unknown_basic_check_ok(self):
        with patch.object(glam, "_ig_token_expires_at", 0.0):
            level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("Instagram token: expiry unknown (basic check OK)", text)

    def test_token_under_15_days_is_yellow(self):
        with patch.object(glam, "_ig_token_expires_at", NIGHT + 10 * DAY):
            level, text = self.report()
        self.assertEqual(level, "yellow")
        self.assertIn("• Instagram token has under 15 days left", text)
        self.assertIn("Instagram token: 10 days left", text)

    def test_debug_token_invalid_is_red(self):
        for p in (patch.object(glam, "INSTAGRAM_APP_ID", "123"), patch.object(glam, "INSTAGRAM_APP_SECRET", "shh")):
            p.start()
            self.addCleanup(p.stop)
        self.meta = resp(200, {"data": {"is_valid": False, "error": {"message": "expired"}}})
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("Instagram token: INVALID", text)
        self.assertEqual(self.sent, [])

    def test_debug_token_reads_the_expiry(self):
        for p in (patch.object(glam, "INSTAGRAM_APP_ID", "123"), patch.object(glam, "INSTAGRAM_APP_SECRET", "shh")):
            p.start()
            self.addCleanup(p.stop)
        self.meta = resp(200, {"data": {"is_valid": True, "expires_at": int(NIGHT + 50 * DAY)}})
        level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("Instagram token: 50 days left", text)

    def test_token_check_network_trouble_is_not_red(self):
        def get(url, params=None, headers=None, timeout=None, **kw):
            if url == glam.DEEPSEEK_BALANCE_URL:
                return self.balance
            raise requests.ConnectionError("down")
        with patch.object(glam.requests, "get", get):
            level, text = self.report()
        self.assertEqual(level, "green")
        self.assertIn("Instagram token: couldn't check", text)

    def test_no_webhook_event_is_yellow_per_channel(self):
        self.with_wati()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM health_marks WHERE key = 'webhook:whatsapp'")
        conn.commit(); conn.close()
        glam._health_mark("webhook:whatsapp", NIGHT - 30 * 3600)
        level, text = self.report()
        self.assertEqual(level, "yellow")
        self.assertIn("• No WhatsApp webhook event of any kind in 24h", text)
        self.assertNotIn("No Instagram webhook", text)
        self.assertIn("Last webhook event of any kind: Instagram 10 min ago · WhatsApp 30 h ago", text)

    def test_whatsapp_line_only_when_wati_is_configured(self):
        self.assertNotIn("WhatsApp", self.report()[1])
        self.with_wati()
        wa_row("AUTO")
        wa_row("DRAFT_SENT")
        wa_row("ESCALATE", reply=glam.ESCALATE_FALLBACK_HOLDING_REPLY)
        wa_row("AUTO_FAILED")
        wa_row("HUMAN_UDIT", msg="thanks!", reply="thanks!")         # the founder's own reply: not a message in
        wa_row("HUMAN_UDIT", reply="(human reply detected via WATI getMessages scan)")   # a skipped customer message
        level, text = self.report()
        self.assertIn("WhatsApp, last 24h: 4 messages in · 1 auto reply · 0 drafts waiting · 1 draft approved · "
                      "1 escalation · 1 holding line · 1 send failure", text)
        self.assertEqual(level, "red")
        self.assertIn("• WhatsApp: 1 send failure", text)


class BalanceTest(HealthTestCase):
    def check(self, balance, level, line):
        self.balance = balance
        got_level, text = self.report()
        self.assertEqual(got_level, level, text)
        self.assertIn(line, text)
        self.assertNotIn(DEEPSEEK_KEY, text)
        return text

    def test_ok(self):
        self.check(resp(200, usd("12.40")), "green", "DeepSeek balance: $12.40")
        self.assertEqual(self.balance_headers[-1]["Authorization"], f"Bearer {DEEPSEEK_KEY}")

    def test_low(self):
        text = self.check(resp(200, usd("1.50")), "yellow", "DeepSeek balance: $1.50")
        self.assertIn("• DeepSeek balance under $2.00", text)

    def test_low_threshold_from_env_setting(self):
        with patch.object(glam, "DEEPSEEK_LOW_BALANCE_USD", 20.0):
            self.check(resp(200, usd("12.40")), "yellow", "DeepSeek balance: $12.40")

    def test_critical(self):
        text = self.check(resp(200, usd("0.30")), "red", "DeepSeek balance: $0.30")
        self.assertIn("• DeepSeek balance under $0.50", text)

    def test_not_available(self):
        self.check(resp(200, usd("0.90", available=False)), "red",
                   "DeepSeek balance: $0.90 — DeepSeek says it's too low for API calls")

    def test_api_error(self):
        self.check(resp(401, {"error": {"message": "Authentication Fails"}}), "green",
                   "DeepSeek balance: check failed (HTTP 401)")

    def test_network_error(self):
        self.check(requests.ConnectionError("down"), "green",
                   "DeepSeek balance: check failed (network error: ConnectionError)")

    def test_unexpected_answer(self):
        self.check(resp(200, ["not", "a", "dict"]), "green", "DeepSeek balance: check failed (unexpected answer)")

    def test_cny_only(self):
        self.check(resp(200, {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "110.00"}]}), "green",
            "DeepSeek balance: 110.00 CNY (no USD balance to compare)")

    def test_no_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            self.check(self.balance, "green", "DeepSeek balance: check failed (DEEPSEEK_API_KEY not set)")
        self.assertEqual(self.balance_headers, [])


class PrivacyAndTestSendersTest(HealthTestCase):
    def test_test_senders_are_left_out(self):
        self.with_wati()
        os.environ["TEST_SENDER_IDS"] = f" {TEST_IG} , +{TEST_WA} "
        ig_row("AUTO_FAILED_IG", sender=TEST_IG)
        ig_row(None, sender=TEST_IG)
        ig_row("ESCALATE_IG", sender=TEST_IG, reply=None)
        pending_draft("t1", TEST_IG, "Instagram")
        wa_row("ERROR", wa=TEST_WA, reply=None, error="Error code: 402 - Insufficient Balance")
        wa_row("AUTO", wa=TEST_WA)
        pending_draft("t2", TEST_WA, "WhatsApp")
        level, text = self.report()
        self.assertEqual(level, "green", text)
        self.assertIn("Instagram, last 24h: 0 messages in · 0 auto replies · 0 drafts waiting", text)
        self.assertIn("WhatsApp, last 24h: 0 messages in · 0 auto replies · 0 drafts waiting", text)
        self.assertIn("Last reply delivered to a customer: none on record", text)
        # Without the setting they're customers again.
        os.environ.pop("TEST_SENDER_IDS")
        level, text = self.report()
        self.assertEqual(level, "red")
        self.assertIn("Instagram, last 24h: 3 messages in · 1 auto reply · 1 draft waiting", text)

    def test_no_ids_names_or_message_text(self):
        self.with_wati()
        ig_row(None)
        ig_row("AUTO_FAILED_IG")
        wa_row("AUTO")
        pending_draft("p1", CUSTOMER_WA, "WhatsApp")
        with patch.object(glam, "_telegram_api", lambda m, p: self.sent.append(p) or {"ok": True}):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(glam._send_health_report())
        text = self.sent[-1]["text"]
        for secret in (CUSTOMER_IG, CUSTOMER_WA, "9800000001", SECRET_TEXT, "Priya", "4471",
                       "IGAAtest-token", DEEPSEEK_KEY):
            self.assertNotIn(secret, text)

    def test_brand_name_comes_from_the_brand_file(self):
        with tempfile.TemporaryDirectory(prefix="daily-health-brand-") as tmp:
            result = run_python(
                "import io, json, contextlib\n"
                "from unittest.mock import patch, Mock\n"
                "with contextlib.redirect_stdout(io.StringIO()):\n"
                "    import app\n"
                "bal = Mock(ok=True, status_code=200)\n"
                "bal.json.return_value = {'is_available': True, 'balance_infos': "
                "[{'currency': 'USD', 'total_balance': '9.00'}]}\n"
                "with patch.object(app.requests, 'get', return_value=bal), contextlib.redirect_stdout(io.StringIO()):\n"
                "    level, text = app._build_health_report()\n"
                f"print({MARK!r} + json.dumps({{'text': text}}))\n",
                BRAND_CONFIG_PATH="brands/example.json",
                DB_PATH=os.path.join(tmp, "test.db"), GITHUB_TOKEN="", GITHUB_REPO="",
                DEEPSEEK_API_KEY="k", SECRET_KEY="t", APP_PASSWORD="t", DASHBOARD_KEY="t",
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        text = result_json(result)["text"]
        self.assertIn(" — Example Candle Co — ", text.splitlines()[0])
        self.assertNotIn("glam", text.lower())


class WebhookMarkTest(HealthTestCase):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM health_marks")
        conn.commit(); conn.close()
        self.client = glam.app.test_client()

    def test_wati_events_are_recorded_only_after_the_token_check(self):
        with patch.object(glam, "WATI_WEBHOOK_TOKEN", "right"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.client.post("/webhook/wrong", json={"type": "status"}).status_code, 401)
            self.assertIsNone(glam._last_webhook_event("whatsapp"))
            self.assertEqual(self.client.post("/webhook/right", json={"type": "status"}).status_code, 200)
        self.assertIsNotNone(glam._health_mark_get("webhook:whatsapp"))

    def test_instagram_events_are_recorded_only_after_the_signature_check(self):
        body = json.dumps({"object": "instagram", "entry": []}).encode()
        good = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        with patch.object(glam, "INSTAGRAM_APP_SECRET", "shh"), redirect_stdout(io.StringIO()):
            bad = self.client.post("/instagram-webhook", data=body, content_type="application/json",
                                   headers={"X-Hub-Signature-256": "sha256=00"})
            self.assertEqual(bad.status_code, 401)
            self.assertIsNone(glam._last_webhook_event("instagram"))
            ok = self.client.post("/instagram-webhook", data=body, content_type="application/json",
                                  headers={"X-Hub-Signature-256": good})
            self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(glam._health_mark_get("webhook:instagram"))

    def test_db_write_at_most_once_a_minute(self):
        with patch.object(glam, "_health_mark") as mark:
            glam._note_webhook_event("instagram")
            glam._note_webhook_event("instagram")
            glam._note_webhook_event("whatsapp")
        self.assertEqual([c.args[0] for c in mark.call_args_list], ["webhook:instagram", "webhook:whatsapp"])
        self.assertIsNotNone(glam._last_webhook_event("instagram"))   # the in-memory one still counts


class HealthCommandTest(HealthTestCase):
    """/health builds the report for the real 'now', so this class's
    green-day marks are set relative to the real clock."""

    def setUp(self):
        super().setUp()
        glam._health_mark("webhook:instagram", time.time() - 60)
        p = patch.object(glam, "_ig_token_expires_at", time.time() + 40 * DAY)
        p.start()
        self.addCleanup(p.stop)

    def command(self, text, chat_id=5):
        """Run /health synchronously (the handler starts a thread)."""
        started = []

        class SyncThread:
            def __init__(self, target, args=(), daemon=None, **kw):
                started.append(target)
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with patch.object(glam.threading, "Thread", SyncThread), redirect_stdout(io.StringIO()):
            glam._handle_telegram_message({"chat": {"id": chat_id}, "text": text})
        return started

    def test_health_sends_the_report_to_the_chat(self):
        for text in ("/health", "/health@GlamTwinBot", "#health", "/HEALTH "):
            with self.subTest(text=text):
                self.sent.clear()
                self.assertEqual(self.command(text), [glam._send_health_report])
                (msg,) = self.sent
                self.assertEqual(msg["chat_id"], 5)
                self.assertTrue(msg["text"].startswith("🟢 All good — The Glam Shelf — "))

    def test_ignored_from_another_chat(self):
        self.assertEqual(self.command("/health", chat_id=6), [])
        self.assertEqual(self.sent, [])

    def test_works_with_the_kill_switch_on(self):
        with patch.dict(os.environ, {"DAILY_HEALTH_DISABLED": "1"}):
            self.command("/health")
        self.assertEqual(len(self.sent), 1)

    def test_not_forwarded_as_an_edited_draft(self):
        pending_draft("edit1", CUSTOMER_WA, "WhatsApp", awaiting_edit=True, telegram_chat_id=5,
                      edit_started_at=NIGHT - 60)
        with patch.object(glam, "send_whatsapp_reply") as wa, patch.object(glam, "_send_instagram_reply") as ig:
            self.command("/health")
        wa.assert_not_called()
        ig.assert_not_called()
        self.assertIsNotNone(glam._draft_get("edit1"))            # still waiting for the real edit
        self.assertEqual(len(self.sent), 1)
        self.assertIn("The Glam Shelf", self.sent[0]["text"])

    def test_a_crash_while_building_still_sends_a_red_note(self):
        with patch.object(glam, "_build_health_report", side_effect=RuntimeError("boom")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertTrue(glam._send_health_report())
        (msg,) = self.sent
        self.assertTrue(msg["text"].startswith("🔴 Problem — The Glam Shelf"))
        self.assertIn("health check itself crashed (RuntimeError)", msg["text"])


if __name__ == "__main__":
    unittest.main()
