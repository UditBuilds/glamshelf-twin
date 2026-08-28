"""Regression: an unparseable model response must not drop the customer.

The bug: DeepSeek returns HTTP 200 with a body that isn't valid JSON.
draft_reply_logic handed back ("", ""), both webhook gates read
`if not classification or not reply:`, and unless the classification was
already ESCALATE the handler logged a line and returned 200 having sent
the customer nothing. No retry, no fallback, no founder notification —
silence, on ordinary non-escalation traffic.

The fix: retry the call once, and if the retry is also unusable, return
ESCALATE with an EMPTY reply so the EXISTING fallback path fires. The
empty reply is load-bearing — see
test_double_parse_failure_escalates_with_empty_reply below.

Modelled on tests/test_pause_gate.py: same /webhook route, same env kill
switches, same recorder/monkeypatch style. No live API call anywhere here.
"""
import json
import os
import sys
import uuid
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-parsefail-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t")
os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam  # noqa: E402

WA_ID = "919812345670"
IG_ID = "17841400000000009"

# What a broken 200 actually looks like: prose, not JSON.
GARBAGE = "I'm sorry, I can't help with that request."


def named(calls, name):
    return [c for c in calls if c[0] == name]


class ParseFailureFallbackTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)
        glam._init_db()

    # ------------------------------------------------------------------
    # draft_reply_logic contract
    # ------------------------------------------------------------------
    def _stub_context(self, stack, calls, llm_responses):
        """Stub everything around the LLM; ask_claude returns the queued
        responses in order so a double failure can be simulated exactly."""
        queue = list(llm_responses)

        def fake_ask(*_a, **_k):
            calls.append(("ask_claude", (), {}))
            return queue.pop(0) if queue else GARBAGE

        for name, ret in [
            ("_load_brain_cached", "BRAIN v-test"),
            ("get_live_inventory", ""),
            ("get_live_policies", ""),
            ("_rag_retrieve", ""),
        ]:
            stack.enter_context(patch.object(glam, name, lambda *a, _r=ret, **k: _r))
        stack.enter_context(patch.object(glam, "ask_claude", fake_ask))

    def test_first_parse_failure_is_retried(self):
        """One bad response, one good one: the customer gets the real reply."""
        calls = []
        good = json.dumps({"classification": "AUTO", "reply": "GS1 is ₹849."})
        with ExitStack() as stack:
            self._stub_context(stack, calls, [GARBAGE, good])
            classification, reply, _raw = glam.draft_reply_logic("what is the price")
        self.assertEqual(len(named(calls, "ask_claude")), 2, "should have retried once")
        self.assertEqual(classification, "AUTO")
        self.assertEqual(reply, "GS1 is ₹849.")

    def test_double_parse_failure_escalates_with_empty_reply(self):
        """Both responses garbage: ESCALATE, and reply stays EMPTY.

        Empty is not an oversight. The webhook gate is
        `if not classification or not reply:` and only inside that gate does
        the ESCALATE arm set fallback_escalation=True — the flag both
        handlers check before sending the holding reply. A non-empty reply
        here would skip the gate into a normal escalation, which sends the
        customer nothing at all.
        """
        calls = []
        with ExitStack() as stack:
            self._stub_context(stack, calls, [GARBAGE, GARBAGE])
            classification, reply, _raw = glam.draft_reply_logic("what is the price")
        self.assertEqual(len(named(calls, "ask_claude")), 2)
        self.assertEqual(classification, "ESCALATE")
        self.assertEqual(reply, "", "reply MUST stay empty to trigger the fallback path")

    def test_retry_api_error_still_falls_back(self):
        """If the retry call itself raises, escalate rather than propagate."""
        calls = []
        state = {"n": 0}

        def flaky(*_a, **_k):
            calls.append(("ask_claude", (), {}))
            state["n"] += 1
            if state["n"] == 1:
                return GARBAGE
            raise RuntimeError("connection reset")

        with ExitStack() as stack:
            self._stub_context(stack, calls, [])
            stack.enter_context(patch.object(glam, "ask_claude", flaky))
            classification, reply, _raw = glam.draft_reply_logic("what is the price")
        self.assertEqual(classification, "ESCALATE")
        self.assertEqual(reply, "")

    def test_good_response_is_not_retried(self):
        """A parseable response must cost exactly one call."""
        calls = []
        good = json.dumps({"classification": "AUTO", "reply": "Shipping is free above ₹799."})
        with ExitStack() as stack:
            self._stub_context(stack, calls, [good])
            classification, reply, _raw = glam.draft_reply_logic("shipping?")
        self.assertEqual(len(named(calls, "ask_claude")), 1, "must not retry a good response")
        self.assertEqual(classification, "AUTO")

    # ------------------------------------------------------------------
    # End to end: WhatsApp webhook
    # ------------------------------------------------------------------
    def test_wa_webhook_sends_holding_reply_and_notifies_founder(self):
        calls = []

        def recorder(name, ret=None):
            def f(*a, **k):
                calls.append((name, a, k))
                return ret
            return f

        with ExitStack() as stack:
            self._stub_context(stack, calls, [GARBAGE, GARBAGE])
            for name, ret in [
                ("_is_outbound_event", False),
                ("_handle_pause_directive", None),
                ("_is_paused", False),
                ("_udit_replied_recently", False),
                ("_check_recent_human_reply", False),
                ("_load_wati_history", []),
                ("_lookup_recent_order", ""),
                ("send_whatsapp_reply", (True, "")),
                ("send_telegram_notification", None),
                ("_reassign_to_bot", None),
                ("_pause_number", True),
            ]:
                stack.enter_context(patch.object(glam, name, recorder(name, ret)))
            resp = glam.app.test_client().post("/webhook", json={
                "type": "text", "waId": WA_ID, "senderName": "Test",
                "text": "what is the price of gs1", "id": "",
            })

        self.assertEqual(resp.status_code, 200)

        sends = named(calls, "send_whatsapp_reply")
        self.assertEqual(len(sends), 1, "customer must receive exactly one message, not silence")
        self.assertEqual(sends[0][1][0], WA_ID)
        self.assertEqual(sends[0][1][1], glam.ESCALATE_FALLBACK_HOLDING_REPLY)

        notifications = named(calls, "send_telegram_notification")
        self.assertEqual(len(notifications), 1, "founder must be notified")
        self.assertEqual(notifications[0][1][0], "ESCALATE")

    # ------------------------------------------------------------------
    # End to end: Instagram webhook
    # ------------------------------------------------------------------
    def test_ig_webhook_sends_holding_reply_and_notifies_founder(self):
        calls = []

        def recorder(name, ret=None):
            def f(*a, **k):
                calls.append((name, a, k))
                return ret
            return f

        with ExitStack() as stack:
            self._stub_context(stack, calls, [GARBAGE, GARBAGE])
            for name, ret in [
                ("_is_paused", False),
                ("_udit_replied_recently", False),
                ("_load_instagram_history", []),
                ("_lookup_recent_order", ""),
                ("_send_instagram_reply", (True, "")),
                ("send_telegram_notification", None),
                ("_pause_number", True),
                ("_log_instagram", None),
            ]:
                stack.enter_context(patch.object(glam, name, recorder(name, ret)))
            glam._process_instagram_event({
                "sender": {"id": IG_ID},
                "recipient": {"id": "page-1"},
                "timestamp": 1756000000000,
                # Unique per run: DEDUP_CACHE_FILE is a FIXED path in the
                # system temp dir shared by every process, so a hardcoded mid
                # passes once and is deduped away on every run after that.
                "message": {"mid": f"m-parsefail-{uuid.uuid4()}",
                            "text": "what is the price of gs1"},
            })

        sends = named(calls, "_send_instagram_reply")
        self.assertEqual(len(sends), 1, "customer must receive exactly one message, not silence")
        self.assertEqual(sends[0][1][1], glam.ESCALATE_FALLBACK_HOLDING_REPLY)

        notifications = named(calls, "send_telegram_notification")
        self.assertEqual(len(notifications), 1, "founder must be notified")
        self.assertEqual(notifications[0][1][0], "ESCALATE")


if __name__ == "__main__":
    unittest.main()
