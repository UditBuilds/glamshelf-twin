"""Regression: ESCALATE auto-pause must silence rapid follow-ups.
Modeled on tests/test_escalation_fallback_wati.py — same /webhook route,
same env kill switches, same recorder stubs."""
import io, os, sys, tempfile, unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="glamshelf-pause-test-"), "test.db")
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

WA_ID = "919812345678"

def named(calls, name): return [c for c in calls if c[0] == name]

class PauseGateTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)
        glam._init_db()

    def _post_escalation(self, calls):
        def recorder(name, ret=None):
            def f(*a, **k): calls.append((name, a, k)); return ret
            return f
        patches = [
            patch.object(glam, "_is_outbound_event", recorder("_is_outbound_event", False)),
            patch.object(glam, "_handle_pause_directive", recorder("_handle_pause_directive", None)),
            patch.object(glam, "_udit_replied_recently", recorder("_udit_replied_recently", False)),
            patch.object(glam, "_check_recent_human_reply", recorder("_check_recent_human_reply", False)),
            patch.object(glam, "_load_wati_history", recorder("_load_wati_history", [])),
            patch.object(glam, "_lookup_recent_order", recorder("_lookup_recent_order", "")),
            patch.object(glam, "_load_brain_cached", recorder("_load_brain_cached", "BRAIN v-test")),
            patch.object(glam, "get_live_inventory", recorder("get_live_inventory", "")),
            patch.object(glam, "get_live_policies", recorder("get_live_policies", "")),
            patch.object(glam, "_rag_retrieve", recorder("_rag_retrieve", "")),
            patch.object(glam, "ask_claude", recorder("ask_claude",
                '{"classification": "ESCALATE", "reply": "holding reply"}')),
            patch.object(glam, "send_whatsapp_reply", recorder("send_whatsapp_reply", (True, ""))),
            patch.object(glam, "send_telegram_notification", recorder("send_telegram_notification")),
            patch.object(glam, "_reassign_to_bot", recorder("_reassign_to_bot")),
        ]
        payload = {"type": "text", "waId": WA_ID, "senderName": "Test",
                   "text": "I am going to the consumer court", "id": ""}
        with ExitStack() as stack:
            for p in patches: stack.enter_context(p)
            resp = glam.app.test_client().post("/webhook", json=payload)
        self.assertEqual(resp.status_code, 200)

    def test_escalation_pause_then_rapid_followups_are_silent(self):
        # One shared recorder across all four posts: the follow-ups must add
        # nothing to it. (Separate lists would make the counts vacuous.)
        calls = []
        self._post_escalation(calls)
        self.assertTrue(glam._is_paused(WA_ID))
        for i in range(3):
            self._post_escalation(calls)
        self.assertEqual(len(named(calls, "ask_claude")), 1)
        self.assertEqual(len(named(calls, "send_telegram_notification")), 1)
        # A normal ESCALATE ships nothing to the customer — the holding reply
        # is fallback-only (app.py ESCALATE branch). Silence is the contract.
        self.assertEqual(len(named(calls, "send_whatsapp_reply")), 0)
        import sqlite3
        conn = sqlite3.connect(glam.DB_PATH)
        paused = [r for r in conn.execute(
            "SELECT status, COUNT(*) FROM message_logs WHERE wa_id=? GROUP BY status", (WA_ID,))]
        conn.close()
        self.assertIn(("PAUSED", 3), paused)

    def test_pause_write_failure_is_loud(self):
        # assertIs(False), not assertFalse: the old signature returned None,
        # which is falsy — this must fail against the pre-fix code.
        buf = io.StringIO()
        with patch.object(glam.sqlite3, "connect", side_effect=RuntimeError("db gone")):
            with redirect_stdout(buf):
                result = glam._pause_number(WA_ID)
        self.assertIs(result, False)
        self.assertIn("WRITE FAILED", buf.getvalue())
        self.assertIn("pause gate may miss", buf.getvalue())

if __name__ == "__main__":
    unittest.main()
