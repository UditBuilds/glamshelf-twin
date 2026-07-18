"""Regression tests for the July 17 escalation-gap fix on the WhatsApp
(WATI) path — the sibling of the Instagram tests in test_graph_parity.py.

The rule under test: an escalation verdict must survive regardless of
what happens downstream. A high-risk message whose LLM response fails to
parse as JSON (or whose LLM call raises) must dispatch the fallback
escalation — stock holding reply to the customer + Telegram page — never
be silently dropped by the empty-reply gate.

Exercised through the real /webhook Flask route (kill switch bypasses
the path-token check, same rollback mechanism test_webhook_auth covers)
with all network/DB side effects recorded by stubs.

Run:  python -m unittest tests.test_escalation_fallback_wati
"""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-wati-fallback-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-app-password")
os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam

WA_ID = "919812345678"
MSG = "I am going to the consumer court"
CANNED_BRAIN = "== BRAIN v-test =="


def named(calls, name):
    return [c for c in calls if c[0] == name]


class WatiEscalationFallbackTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)
        # Bypass the path-token check via the documented kill switch so
        # this test doesn't depend on token configuration.
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)

    def _post(self, *, llm_response=None, llm_exception=None, send_ok=True):
        """POST a high-risk text message to /webhook with every side
        effect stubbed; return the recorded (fn, args, kwargs) calls."""
        calls = []

        def recorder(name, ret=None, exc=None):
            def f(*a, **k):
                calls.append((name, a, k))
                if exc is not None:
                    raise exc
                return ret
            return f

        send_result = (True, "") if send_ok else (False, "HTTP 400: boom")
        patches = [
            patch.object(glam, "_is_outbound_event", recorder("_is_outbound_event", False)),
            patch.object(glam, "_handle_pause_directive", recorder("_handle_pause_directive", None)),
            patch.object(glam, "_is_paused", recorder("_is_paused", False)),
            patch.object(glam, "_udit_replied_recently", recorder("_udit_replied_recently", False)),
            patch.object(glam, "_check_recent_human_reply", recorder("_check_recent_human_reply", False)),
            patch.object(glam, "_load_wati_history", recorder("_load_wati_history", [])),
            patch.object(glam, "_lookup_recent_order", recorder("_lookup_recent_order", "")),
            patch.object(glam, "_load_brain_cached", recorder("_load_brain_cached", CANNED_BRAIN)),
            patch.object(glam, "get_live_inventory", recorder("get_live_inventory", "")),
            patch.object(glam, "get_live_policies", recorder("get_live_policies", "")),
            patch.object(glam, "_rag_retrieve", recorder("_rag_retrieve", "")),
            patch.object(glam, "ask_claude", recorder("ask_claude", llm_response, exc=llm_exception)),
            patch.object(glam, "send_whatsapp_reply", recorder("send_whatsapp_reply", send_result)),
            patch.object(glam, "send_telegram_notification", recorder("send_telegram_notification")),
            patch.object(glam, "_pause_number", recorder("_pause_number")),
            patch.object(glam, "_reassign_to_bot", recorder("_reassign_to_bot")),
            patch.object(glam, "_log_message", recorder("_log_message")),
        ]
        payload = {
            "type": "text",
            "waId": WA_ID,
            "senderName": "Parity Test",
            "text": MSG,
            "id": "",
        }
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = glam.app.test_client()
            resp = client.post("/webhook", json=payload)
        self.assertEqual(resp.status_code, 200)
        return calls

    def _assert_fallback_dispatched(self, calls, *, holding_delivered):
        (send,) = named(calls, "send_whatsapp_reply")
        self.assertEqual(send[1], (WA_ID, glam.ESCALATE_FALLBACK_HOLDING_REPLY))
        (tg,) = named(calls, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertEqual(tg[2]["customer_id"], WA_ID)
        self.assertEqual(tg[2]["holding_reply_sent"], holding_delivered)
        self.assertEqual(len(named(calls, "_pause_number")), 1)
        self.assertEqual(len(named(calls, "_reassign_to_bot")), 1)

    def test_prefilter_escalation_survives_unparseable_llm_output(self):
        calls = self._post(llm_response="sorry, no json today")
        self._assert_fallback_dispatched(calls, holding_delivered=True)
        # Delivered holding reply logs under ESCALATE — inside
        # _load_wati_history's delivered-only allowlist, and this time
        # the customer really did receive the text.
        (log,) = [c for c in named(calls, "_log_message")
                  if c[2].get("status", "").startswith("ESCALATE")]
        self.assertEqual(log[2]["status"], "ESCALATE")
        self.assertEqual(log[2]["reply_text"], glam.ESCALATE_FALLBACK_HOLDING_REPLY)

    def test_prefilter_escalation_survives_llm_exception(self):
        calls = self._post(llm_exception=RuntimeError("DeepSeek unavailable"))
        self._assert_fallback_dispatched(calls, holding_delivered=True)

    def test_holding_send_failure_logs_outside_history_allowlist(self):
        calls = self._post(llm_response="not json", send_ok=False)
        self._assert_fallback_dispatched(calls, holding_delivered=False)
        (log,) = [c for c in named(calls, "_log_message")
                  if c[2].get("status", "").startswith("ESCALATE")]
        self.assertEqual(log[2]["status"], "ESCALATE_HOLDING_FAILED")
        self.assertEqual(log[2]["error"], "HTTP 400: boom")


if __name__ == "__main__":
    unittest.main()
