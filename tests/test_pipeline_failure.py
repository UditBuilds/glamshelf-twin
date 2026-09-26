"""No more silent DeepSeek failures (audit T1-8).

  - the DeepSeek client fails fast (timeout 20s, 1 retry) and gunicorn's
    worker timeout is raised to 60s, so a slow call ends in our own
    failure handling instead of a killed worker;
  - an Instagram pipeline failure sends brain.md's holding line and a
    rate-limited Telegram alert (the parity cases live in
    test_graph_parity.py);
  - the WhatsApp error path alerts too;
  - a handler error AFTER the pipeline alerts without re-sending anything.

No live API call anywhere here: every send is stubbed.

Run:  python -m unittest tests.test_pipeline_failure
"""
import json, os, sys, tempfile, unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-pipeline-failure-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

WA_ID = "919812345670"
IG_SENDER = "17800000000000456"
CANNED_BRAIN = "== BRAIN v-test =="


def named(calls, name):
    return [c for c in calls if c[0] == name]


def recorder(calls, name, ret=None, exc=None):
    def f(*a, **k):
        calls.append((name, a, k))
        if exc is not None:
            raise exc
        return ret
    return f


class DeepSeekCallBudget(unittest.TestCase):
    def test_client_fails_fast(self):
        self.assertEqual(glam.deepseek_client.timeout, 20)
        self.assertEqual(glam.deepseek_client.max_retries, 1)

    def test_procfile_raises_gunicorn_worker_timeout(self):
        procfile = (REPO / "Procfile").read_text(encoding="utf-8")
        self.assertIn("--timeout 60", procfile)


class PipelineAlertWording(unittest.TestCase):
    def setUp(self):
        glam._send_failure_last_alert.clear()
        self.sent = []
        for p in (
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", lambda method, payload: self.sent.append(payload["text"])),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_outcome_line_matches_what_the_customer_got(self):
        glam._alert_send_failure("Instagram", "APITimeoutError: a", IG_SENDER, kind="pipeline", holding_sent=True)
        glam._alert_send_failure("Instagram", "APITimeoutError: b", IG_SENDER, kind="pipeline", holding_sent=False)
        glam._alert_send_failure("Instagram", "APITimeoutError: c", IG_SENDER, kind="pipeline", holding_sent=None)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("reply pipeline FAILED", self.sent[0])
        self.assertIn("got only the holding line", self.sent[0])
        self.assertIn("got NO reply", self.sent[1])
        self.assertIn("may not have got a reply", self.sent[2])

    def test_send_alert_wording_unchanged(self):
        glam._alert_send_failure("WhatsApp", "HTTP 400: boom", WA_ID)
        (text,) = self.sent
        self.assertTrue(text.startswith("⚠️ WhatsApp send FAILED\n"))
        self.assertIn(f"Customer {WA_ID} did not get a reply.", text)

    def test_repeats_of_the_same_pipeline_error_are_muted(self):
        for _ in range(3):
            glam._alert_send_failure("Instagram", "APITimeoutError: x", IG_SENDER, kind="pipeline", holding_sent=True)
        self.assertEqual(len(self.sent), 1)

    def test_pipeline_and_send_alerts_do_not_mute_each_other(self):
        glam._alert_send_failure("Instagram", "same error", IG_SENDER)
        glam._alert_send_failure("Instagram", "same error", IG_SENDER, kind="pipeline", holding_sent=False)
        self.assertEqual(len(self.sent), 2)


class WhatsAppErrorPathAlerts(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)

    def test_llm_failure_alerts_and_sends_nothing(self):
        calls = []
        r = lambda name, ret=None, exc=None: recorder(calls, name, ret, exc)
        patches = [
            patch.object(glam, "_is_outbound_event", r("_is_outbound_event", False)),
            patch.object(glam, "_handle_pause_directive", r("_handle_pause_directive", None)),
            patch.object(glam, "_is_paused", r("_is_paused", False)),
            patch.object(glam, "_udit_replied_recently", r("_udit_replied_recently", False)),
            patch.object(glam, "_check_recent_human_reply", r("_check_recent_human_reply", False)),
            patch.object(glam, "_load_wati_history", r("_load_wati_history", [])),
            patch.object(glam, "_lookup_recent_order", r("_lookup_recent_order", "")),
            patch.object(glam, "_load_brain_cached", r("_load_brain_cached", CANNED_BRAIN)),
            patch.object(glam, "get_live_inventory", r("get_live_inventory", "")),
            patch.object(glam, "get_live_policies", r("get_live_policies", "")),
            patch.object(glam, "_rag_retrieve", r("_rag_retrieve", "")),
            patch.object(glam, "ask_claude", r("ask_claude", exc=RuntimeError("DeepSeek unavailable"))),
            patch.object(glam, "send_whatsapp_reply", r("send_whatsapp_reply", (True, ""))),
            patch.object(glam, "send_telegram_notification", r("send_telegram_notification")),
            patch.object(glam, "_log_message", r("_log_message")),
            patch.object(glam, "_alert_send_failure", r("_alert_send_failure")),
        ]
        payload = {"type": "text", "waId": WA_ID, "senderName": "T", "text": "price?", "id": ""}
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            resp = glam.app.test_client().post("/webhook", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(named(calls, "send_whatsapp_reply"), [])
        (alert,) = named(calls, "_alert_send_failure")
        self.assertEqual(alert[1], ("WhatsApp", "RuntimeError: DeepSeek unavailable", WA_ID))
        self.assertEqual(alert[2], {"kind": "pipeline", "holding_sent": False})
        (log,) = named(calls, "_log_message")
        self.assertEqual(log[2]["status"], "ERROR")


class InstagramHandlerErrorAlerts(unittest.TestCase):
    def test_error_after_the_reply_alerts_without_resending(self):
        calls = []
        r = lambda name, ret=None, exc=None: recorder(calls, name, ret, exc)
        auto = json.dumps({"classification": "AUTO", "reply": "GS1 is ₹849 🤍"})
        patches = [
            patch.object(glam, "_is_paused", r("_is_paused", False)),
            patch.object(glam, "_udit_replied_recently_ig", r("_udit_replied_recently_ig", False)),
            patch.object(glam, "_lookup_recent_order", r("_lookup_recent_order", "")),
            patch.object(glam, "_load_instagram_history", r("_load_instagram_history", [])),
            patch.object(glam, "_load_brain_cached", r("_load_brain_cached", CANNED_BRAIN)),
            patch.object(glam, "get_live_inventory", r("get_live_inventory", "")),
            patch.object(glam, "get_live_policies", r("get_live_policies", "")),
            patch.object(glam, "_rag_retrieve", r("_rag_retrieve", "")),
            patch.object(glam, "ask_claude", r("ask_claude", auto)),
            patch.object(glam, "_send_instagram_reply", r("_send_instagram_reply", (True, ""))),
            # A bug after the reply went out:
            patch.object(glam, "_log_instagram", r("_log_instagram", exc=RuntimeError("disk full"))),
            patch.object(glam, "_alert_send_failure", r("_alert_send_failure")),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            glam._process_instagram_event({
                "sender": {"id": IG_SENDER}, "recipient": {"id": "page"},
                "timestamp": 1, "message": {"text": "price of GS1?"},
            })
        (send,) = named(calls, "_send_instagram_reply")      # the AUTO reply only
        self.assertEqual(send[1][1], "GS1 is ₹849 🤍")
        (alert,) = named(calls, "_alert_send_failure")
        self.assertEqual(alert[1], ("Instagram", "RuntimeError: disk full", IG_SENDER))
        self.assertEqual(alert[2], {"kind": "pipeline", "holding_sent": None})


class HistoryExcludesUndeliveredHoldingLine(unittest.TestCase):
    def test_only_the_delivered_holding_line_is_history(self):
        glam._init_db()
        sender = "17800000000000789"
        glam._log_instagram(sender, "q1", glam.BRAIN_HOLDING_LINE, "1", source="PIPELINE_HOLDING_IG")
        glam._log_instagram(sender, "q2", glam.BRAIN_HOLDING_LINE, "2", source="PIPELINE_HOLDING_FAILED_IG")
        history = glam._load_instagram_history(sender)
        self.assertEqual([h["msg_text"] for h in history], ["q1"])


if __name__ == "__main__":
    unittest.main()
