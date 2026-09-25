"""Parity tests: the LangGraph pipeline (graph.py) vs the production
Instagram handler (_process_instagram_event in app.py).

Method: run BOTH implementations on the same scenario with every
network/DB side effect monkeypatched by a recorder, then assert the two
recorded effect sequences are identical — same functions, same order,
same arguments. Each test also asserts the OLD path's expected shape
first, so a test can't pass because both sides are wrong the same way.

Recorded effects: ask_claude (LLM call incl. the fully assembled system
prompt), _send_instagram_reply, send_draft_for_approval,
send_telegram_notification, _pause_number, _log_instagram,
_persist_seen_id, _alert_send_failure.

The escalation prefilters (_escalation_prefilter_hit,
_bulk_commit_prefilter_hit) are deliberately NOT stubbed — they are pure
functions and part of the behavior under test.

Run:  python -m unittest tests.test_graph_parity
"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same import preamble as test_webhook_auth.py: point the DB at a temp
# file and disable GitHub backup BEFORE app's startup block runs.
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-graph-parity-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-app-password")
os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam
import graph as twin

SENDER = "17841400000000001"
TIMESTAMP = 1752700000000
MSG = "Do GS1 lashes suit hooded eyes?"

CANNED_BRAIN = "== BRAIN v-test =="
CANNED_INVENTORY = "[LIVE INVENTORY]\nGS1: 12 in stock"
CANNED_POLICIES = "[LIVE POLICIES]\nReturns within 7 days"
CANNED_RAG = "[RETRIEVED CONTEXT]\n• (GS1) 3D faux mink, flexible band"
ORDER_LINE = "Recent order: #1234 — GS1 x2, delivered"
HISTORY = [{"msg_text": "hi, do you ship to Pune?", "reply_text": "Yes ma'am, we do 🤍"}]

AUTO_JSON = json.dumps({"classification": "AUTO", "reply": "Yes ma'am, GS1 works beautifully for hooded eyes 🤍"})
DRAFT_JSON = json.dumps({"classification": "DRAFT+APPROVE", "reply": "So sorry about the mix-up — here's what we can do..."})
ESCALATE_JSON = json.dumps({"classification": "ESCALATE", "reply": "I'm looping in the founder — they'll get back to you shortly 🤍"})

# The side-effectful calls whose sequence must match between old and new.
EFFECT_FNS = {
    "ask_claude",
    "_send_instagram_reply",
    "send_draft_for_approval",
    "send_telegram_notification",
    "_pause_number",
    "_log_instagram",
    "_persist_seen_id",
    "_alert_send_failure",
    "_ig_rate_limited",
}


def effects(calls):
    return [c for c in calls if c[0] in EFFECT_FNS]


def named(calls, name):
    return [c for c in calls if c[0] == name]


class GraphParityTestCase(unittest.TestCase):
    def setUp(self):
        # The prefilters read this per call — a leaked host value would
        # silently disable the exact behavior under test.
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)

    def _run(
        self,
        target,
        *,
        llm_response,
        message=MSG,
        sender=SENDER,
        mid="",
        send_ok=True,
        buttons_ok=True,
        paused=False,
        udit_recent=False,
        llm_exception=None,
        limited=None,
    ):
        """Run one implementation ("old" or "new") fully stubbed; return
        the recorded call list of (fn_name, args, kwargs) tuples.
        `llm_exception`, when set, makes the ask_claude stub raise after
        recording the call (simulates a DeepSeek outage)."""
        calls = []

        def recorder(name, ret=None, exc=None):
            def f(*a, **k):
                calls.append((name, a, k))
                if exc is not None:
                    raise exc
                return ret
            return f

        send_result = (True, "") if send_ok else (False, "HTTP 400: token expired")
        patches = [
            patch.object(glam, "_is_paused", recorder("_is_paused", paused)),
            patch.object(glam, "_udit_replied_recently_ig", recorder("_udit_replied_recently_ig", udit_recent)),
            # Rate limiter stubbed: None = admitted (its own tests live in
            # test_rate_limit.py); `limited` simulates a refusal.
            patch.object(glam, "_llm_admission", recorder("_llm_admission", limited)),
            patch.object(glam, "_ig_rate_limited", recorder("_ig_rate_limited")),
            patch.object(glam, "_persist_seen_id", recorder("_persist_seen_id")),
            patch.object(glam, "_lookup_recent_order", recorder("_lookup_recent_order", ORDER_LINE)),
            patch.object(glam, "_load_instagram_history", recorder("_load_instagram_history", list(HISTORY))),
            patch.object(glam, "_load_brain_cached", recorder("_load_brain_cached", CANNED_BRAIN)),
            patch.object(glam, "get_live_inventory", recorder("get_live_inventory", CANNED_INVENTORY)),
            patch.object(glam, "get_live_policies", recorder("get_live_policies", CANNED_POLICIES)),
            patch.object(glam, "_rag_retrieve", recorder("_rag_retrieve", CANNED_RAG)),
            patch.object(glam, "ask_claude", recorder("ask_claude", llm_response, exc=llm_exception)),
            patch.object(glam, "_send_instagram_reply", recorder("_send_instagram_reply", send_result)),
            patch.object(glam, "send_draft_for_approval", recorder("send_draft_for_approval", buttons_ok)),
            patch.object(glam, "send_telegram_notification", recorder("send_telegram_notification")),
            patch.object(glam, "_pause_number", recorder("_pause_number")),
            patch.object(glam, "_log_instagram", recorder("_log_instagram")),
            patch.object(glam, "_alert_send_failure", recorder("_alert_send_failure")),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if target == "old":
                glam._process_instagram_event({
                    "sender": {"id": sender},
                    "recipient": {"id": "recipient-page"},
                    "timestamp": TIMESTAMP,
                    "message": {"mid": mid, "text": message},
                })
            else:
                twin.handle_instagram_message(
                    sender, message, msg_id=mid, timestamp=str(TIMESTAMP)
                )
        return calls

    def _assert_parity(self, old_calls, new_calls):
        self.assertEqual(effects(old_calls), effects(new_calls))

    # ---- classification paths ----

    def test_auto_reply(self):
        old = self._run("old", llm_response=AUTO_JSON)
        # Old-path shape: one IG send with the drafted reply, one
        # delivered-exchange log row (no source tag), founder not paged.
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, json.loads(AUTO_JSON)["reply"]))
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[1], (SENDER, MSG, json.loads(AUTO_JSON)["reply"], str(TIMESTAMP)))
        self.assertEqual(log[2], {})
        self.assertEqual(named(old, "send_telegram_notification"), [])

        new = self._run("new", llm_response=AUTO_JSON)
        self._assert_parity(old, new)

    def test_auto_send_failure_logged_distinctly(self):
        old = self._run("old", llm_response=AUTO_JSON, send_ok=False)
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[2], {"source": "AUTO_FAILED_IG"})

        new = self._run("new", llm_response=AUTO_JSON, send_ok=False)
        self._assert_parity(old, new)

    def test_draft_approve_buttons(self):
        old = self._run("old", llm_response=DRAFT_JSON)
        (draft,) = named(old, "send_draft_for_approval")
        self.assertEqual(draft[2]["customer_number"], SENDER)
        self.assertEqual(draft[2]["channel"], "Instagram")
        self.assertEqual(draft[2]["ig_timestamp"], str(TIMESTAMP))
        # Buttons succeeded -> no plain-text fallback, pending log row.
        self.assertEqual(named(old, "send_telegram_notification"), [])
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[1], (SENDER, MSG, None, str(TIMESTAMP)))
        self.assertEqual(log[2], {"source": "DRAFT_PENDING_IG"})

        new = self._run("new", llm_response=DRAFT_JSON)
        self._assert_parity(old, new)

    def test_draft_buttons_fail_falls_back_to_plain_notification(self):
        old = self._run("old", llm_response=DRAFT_JSON, buttons_ok=False)
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "DRAFT+APPROVE")

        new = self._run("new", llm_response=DRAFT_JSON, buttons_ok=False)
        self._assert_parity(old, new)

    def test_escalate(self):
        # Audit T1-5: an ordinary escalation no longer means silence. The
        # customer gets brain.md's holding line (never the model's own
        # draft, which only goes to the founder), then page + 4h pause.
        old = self._run("old", llm_response=ESCALATE_JSON)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.BRAIN_HOLDING_LINE))
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertEqual(tg[1][2], json.loads(ESCALATE_JSON)["reply"])  # draft shown, not sent
        self.assertEqual(tg[2]["customer_id"], SENDER)  # Stop / Resume buttons
        self.assertTrue(tg[2]["holding_reply_sent"])
        self.assertIn(glam.BRAIN_HOLDING_LINE, tg[2]["customer_line"])
        (pause,) = named(old, "_pause_number")
        self.assertEqual(pause[1], (SENDER,))
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[1], (SENDER, MSG, glam.BRAIN_HOLDING_LINE, str(TIMESTAMP)))
        self.assertEqual(log[2], {"source": "ESCALATE_HOLDING_IG"})

        new = self._run("new", llm_response=ESCALATE_JSON)
        self._assert_parity(old, new)

    # ---- deterministic triage floors ----

    def test_prefilter_phrase_upgrades_auto_to_escalate(self):
        msg = "This is unacceptable, I will contact my lawyer about this order"
        old = self._run("old", llm_response=AUTO_JSON, message=msg)
        # LLM said AUTO; the phrase filter must force the ESCALATE path —
        # and a legal threat stays silent (founder decision).
        self.assertEqual(named(old, "_send_instagram_reply"), [])
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertIn("legal escalations get no automated reply", tg[2]["customer_line"])
        self.assertEqual(len(named(old, "_pause_number")), 1)

        new = self._run("new", llm_response=AUTO_JSON, message=msg)
        self._assert_parity(old, new)

    def test_bulk_commit_upgrades_auto_to_escalate(self):
        msg = "ok I'll take 50 trays"
        old = self._run("old", llm_response=AUTO_JSON, message=msg)
        # Not legal/press: the customer gets the holding line, not the
        # model's AUTO answer.
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.BRAIN_HOLDING_LINE))
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")

        new = self._run("new", llm_response=AUTO_JSON, message=msg)
        self._assert_parity(old, new)

    # ---- degenerate LLM output ----

    def test_unparseable_llm_output_retries_then_escalates(self):
        """Used to drop silently — this test asserted that, because that was
        the behaviour. An unparseable 200 left an ordinary customer with no
        reply at all, no fallback and no page to the founder.

        Now the call is retried once, and if the retry is also unusable the
        thread escalates: brain.md's holding line ships to the customer (the
        July 17 "I hear you…" text is retired on Instagram) and the founder
        is notified. Both implementations must agree.
        """
        garbage = "sorry, I can't produce JSON today"
        old = self._run("old", llm_response=garbage)
        self.assertEqual(len(named(old, "ask_claude")), 2, "original call + one retry")
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.BRAIN_HOLDING_LINE))
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")

        new = self._run("new", llm_response=garbage)
        self._assert_parity(old, new)

    def test_prefilter_escalation_survives_unparseable_llm_output(self):
        # REGRESSION TEST for the July 17 escalation-gap fix: a prefilter
        # escalation whose LLM output fails to parse must still page the
        # founder and pause — never be dropped by the empty-reply gate.
        # What the CUSTOMER gets changed with audit T1-5: a legal threat
        # stays silent on every path, fallback included (founder decision).
        msg = "I am going to the consumer court"
        old = self._run("old", llm_response="not json", message=msg)
        self.assertEqual(named(old, "_send_instagram_reply"), [])
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertEqual(tg[2]["customer_id"], SENDER)
        self.assertFalse(tg[2]["holding_reply_sent"])
        self.assertIn("(Twin's own reply was unusable.)", tg[2]["customer_line"])
        self.assertEqual(len(named(old, "_pause_number")), 1)
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[1], (SENDER, msg, None, str(TIMESTAMP)))
        self.assertEqual(log[2], {"source": "ESCALATE_IG"})

        new = self._run("new", llm_response="not json", message=msg)
        self._assert_parity(old, new)

    def test_prefilter_escalation_survives_llm_exception(self):
        # Same rule, harder failure: the LLM call itself raises on a
        # prefilter-hit message. Page + pause still happen; a legal threat
        # still gets no automated reply.
        msg = "This is unacceptable, I will contact my lawyer about this order"
        old = self._run(
            "old", llm_response=None, message=msg,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        self.assertEqual(named(old, "_send_instagram_reply"), [])
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertFalse(tg[2]["holding_reply_sent"])
        self.assertEqual(len(named(old, "_pause_number")), 1)

        new = self._run(
            "new", llm_response=None, message=msg,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        self._assert_parity(old, new)

    def test_llm_exception_without_prefilter_sends_holding_line_and_alerts(self):
        # Used to drop silently: no reply, no page (audit T1-8). Now the
        # customer gets brain.md's holding line and the founder a
        # rate-limited pipeline alert. Not an escalation: no pause.
        old = self._run(
            "old", llm_response=None,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.BRAIN_HOLDING_LINE))
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[1], (SENDER, MSG, glam.BRAIN_HOLDING_LINE, str(TIMESTAMP)))
        self.assertEqual(log[2], {"source": "PIPELINE_HOLDING_IG"})
        (alert,) = named(old, "_alert_send_failure")
        self.assertEqual(alert[1], ("Instagram", "RuntimeError: DeepSeek unavailable", SENDER))
        self.assertEqual(alert[2], {"kind": "pipeline", "holding_sent": True})
        self.assertEqual(named(old, "send_telegram_notification"), [])
        self.assertEqual(named(old, "_pause_number"), [])

        new = self._run(
            "new", llm_response=None,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        self._assert_parity(old, new)

    def test_holding_line_send_failure_logged_distinctly(self):
        old = self._run(
            "old", llm_response=None, send_ok=False,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[2], {"source": "PIPELINE_HOLDING_FAILED_IG"})
        (alert,) = named(old, "_alert_send_failure")
        self.assertEqual(alert[2], {"kind": "pipeline", "holding_sent": False})

        new = self._run(
            "new", llm_response=None, send_ok=False,
            llm_exception=RuntimeError("DeepSeek unavailable"),
        )
        self._assert_parity(old, new)

    def test_unknown_classification_sends_holding_line_and_alerts(self):
        # Used to drop silently with a log line only.
        maybe = json.dumps({"classification": "MAYBE", "reply": "hmm"})
        old = self._run("old", llm_response=maybe)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.BRAIN_HOLDING_LINE))
        (alert,) = named(old, "_alert_send_failure")
        self.assertEqual(
            alert[1], ("Instagram", "unusable model output (classification='MAYBE')", SENDER)
        )

        new = self._run("new", llm_response=maybe)
        self._assert_parity(old, new)

    def test_deliberately_empty_auto_reply_stays_silent(self):
        # brain.md's "stay silent after a holding message" rule returns
        # AUTO with reply "" — that is NOT a failure: nothing is sent and
        # nobody is paged.
        silent = json.dumps({"classification": "AUTO", "reply": ""})
        old = self._run("old", llm_response=silent)
        self.assertEqual([c for c in effects(old) if c[0] != "ask_claude"], [])

        new = self._run("new", llm_response=silent)
        self._assert_parity(old, new)

    def test_fallback_holding_send_failure_still_pages(self):
        # If the holding line can't be delivered, the page must still fire
        # (holding_reply_sent=False, and the page says the send FAILED) and
        # the attempted text is logged under the history-excluded
        # ESCALATE_HOLDING_FAILED_IG source.
        old = self._run("old", llm_response="not json", send_ok=False)
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertFalse(tg[2]["holding_reply_sent"])
        self.assertIn("FAILED", tg[2]["customer_line"])
        self.assertEqual(len(named(old, "_pause_number")), 1)
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[2], {"source": "ESCALATE_HOLDING_FAILED_IG"})

        new = self._run("new", llm_response="not json", send_ok=False)
        self._assert_parity(old, new)

    # ---- escalation kinds and LEADs (audit T1-5) ----

    def _escalate_tagged(self, tag, reply="drafted holding reply"):
        return json.dumps({"classification": "ESCALATE", "reply": reply, "tag": tag})

    def test_safety_escalation_sends_the_allergy_line(self):
        msg = "My eyelids got swollen and red after wearing GS2"
        old = self._run("old", llm_response=self._escalate_tagged("SAFETY"), message=msg)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.ALLERGY_HOLDING_REPLY))
        self.assertEqual(len(named(old, "_pause_number")), 1)

        new = self._run("new", llm_response=self._escalate_tagged("SAFETY"), message=msg)
        self._assert_parity(old, new)

    def test_legal_plus_symptoms_sends_the_safety_text_only(self):
        # Eval row 10 — founder decision: health advice, no holding promise.
        msg = "Mene aapke eyelashes khareeda the, rashes ho gaye, ab court me jaaunga"
        old = self._run("old", llm_response=self._escalate_tagged("LEGAL"), message=msg)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.ALLERGY_SAFETY_TEXT))

        new = self._run("new", llm_response=self._escalate_tagged("LEGAL"), message=msg)
        self._assert_parity(old, new)

    def test_press_escalation_stays_silent(self):
        msg = "Hi, I'm a journalist writing about Indian lash brands"
        old = self._run("old", llm_response=self._escalate_tagged("PRESS"), message=msg)
        self.assertEqual(named(old, "_send_instagram_reply"), [])
        self.assertEqual(len(named(old, "_pause_number")), 1)

        new = self._run("new", llm_response=self._escalate_tagged("PRESS"), message=msg)
        self._assert_parity(old, new)

    def test_lead_tag_on_auto_reply_notifies_and_never_pauses(self):
        msg = "Hi! Udit asked me to test your assistant"
        lead = json.dumps({
            "classification": "AUTO",
            "reply": "Thanks for checking it out! Udit will message you personally 🤍",
            "tag": "LEAD",
        })
        old = self._run("old", llm_response=lead, message=msg)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, json.loads(lead)["reply"]))
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "LEAD")
        self.assertEqual(named(old, "_pause_number"), [])
        (log,) = named(old, "_log_instagram")
        self.assertEqual(log[2], {"source": "LEAD_IG"})

        new = self._run("new", llm_response=lead, message=msg)
        self._assert_parity(old, new)

    def test_lead_tag_on_escalate_becomes_a_lead_with_the_fixed_line(self):
        # The model escalated a tester for naming the founder, but tagged
        # it LEAD: no lockout, and the fixed LEAD line — not the draft.
        msg = "Udit told me to try out your chatbot, can I speak to him?"
        old = self._run("old", llm_response=self._escalate_tagged("LEAD"), message=msg)
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, glam.LEAD_REPLY))
        self.assertEqual(named(old, "_pause_number"), [])

        new = self._run("new", llm_response=self._escalate_tagged("LEAD"), message=msg)
        self._assert_parity(old, new)

    def test_lead_regex_backstop_adds_notice_to_untagged_auto_reply(self):
        msg = "I'm a brand owner, just testing your AI assistant"
        old = self._run("old", llm_response=AUTO_JSON, message=msg)
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "LEAD")
        (send,) = named(old, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, json.loads(AUTO_JSON)["reply"]))

        new = self._run("new", llm_response=AUTO_JSON, message=msg)
        self._assert_parity(old, new)

    def test_legal_threat_outranks_a_lead_tag(self):
        msg = "Udit asked me to test this. I'll also call the police on you."
        lead = json.dumps({"classification": "AUTO", "reply": "hi", "tag": "LEAD"})
        old = self._run("old", llm_response=lead, message=msg)
        self.assertEqual(named(old, "_send_instagram_reply"), [])  # legal: silent
        (tg,) = named(old, "send_telegram_notification")
        self.assertEqual(tg[1][0], "ESCALATE")
        self.assertEqual(len(named(old, "_pause_number")), 1)

        new = self._run("new", llm_response=lead, message=msg)
        self._assert_parity(old, new)

    # ---- intake gates ----

    def test_paused_sender_short_circuits_before_llm(self):
        old = self._run("old", llm_response=AUTO_JSON, paused=True)
        self.assertEqual(effects(old), [])

        new = self._run("new", llm_response=AUTO_JSON, paused=True)
        self._assert_parity(old, new)

    def test_rate_limited_sender_short_circuits_before_llm(self):
        old = self._run("old", llm_response=AUTO_JSON, limited=glam.LIMIT_SENDER_WINDOW)
        self.assertEqual(named(old, "ask_claude"), [])
        (rl,) = named(old, "_ig_rate_limited")
        self.assertEqual(rl[1], (SENDER, MSG, str(TIMESTAMP), glam.LIMIT_SENDER_WINDOW))

        new = self._run("new", llm_response=AUTO_JSON, limited=glam.LIMIT_SENDER_WINDOW)
        self._assert_parity(old, new)

    def test_human_handling_window_short_circuits_before_llm(self):
        old = self._run("old", llm_response=AUTO_JSON, udit_recent=True)
        self.assertEqual(effects(old), [])

        new = self._run("new", llm_response=AUTO_JSON, udit_recent=True)
        self._assert_parity(old, new)

    def test_duplicate_mid_processed_once(self):
        mid_old, mid_new = "parity-mid-old-1", "parity-mid-new-1"
        try:
            old_first = self._run("old", llm_response=AUTO_JSON, mid=mid_old)
            old_second = self._run("old", llm_response=AUTO_JSON, mid=mid_old)
            self.assertEqual(len(named(old_first, "_persist_seen_id")), 1)
            self.assertEqual(len(named(old_first, "_send_instagram_reply")), 1)
            self.assertEqual(effects(old_second), [])

            new_first = self._run("new", llm_response=AUTO_JSON, mid=mid_new)
            new_second = self._run("new", llm_response=AUTO_JSON, mid=mid_new)
            # The two sides must use DIFFERENT mids (they share app._seen_ids),
            # so normalize the mid before comparing the effect sequences.
            def norm(calls, mid):
                return [
                    (n, tuple("<MID>" if x == mid else x for x in a), k)
                    for n, a, k in effects(calls)
                ]
            self.assertEqual(norm(old_first, mid_old), norm(new_first, mid_new))
            self.assertEqual(norm(old_second, mid_old), norm(new_second, mid_new))
        finally:
            glam._seen_ids.discard(mid_old)
            glam._seen_ids.discard(mid_new)

    # ---- prompt assembly ----

    def test_system_prompt_assembled_identically(self):
        # Inventory PREPENDED, policies and RAG APPENDED — the layering
        # that keeps brain.md's classification rules at prompt priority.
        expected_brain = (
            CANNED_INVENTORY + "\n\n" + CANNED_BRAIN + "\n\n"
            + CANNED_POLICIES + "\n\n" + CANNED_RAG
        )
        old = self._run("old", llm_response=AUTO_JSON)
        new = self._run("new", llm_response=AUTO_JSON)
        (old_llm,) = named(old, "ask_claude")
        (new_llm,) = named(new, "ask_claude")
        self.assertEqual(old_llm[1][0], expected_brain)
        self.assertEqual(old_llm[1], new_llm[1])      # brain, message, order_context
        self.assertEqual(old_llm[2], new_llm[2])      # history=, source="Instagram DM"
        self.assertEqual(new_llm[2].get("source"), "Instagram DM")


if __name__ == "__main__":
    unittest.main()
