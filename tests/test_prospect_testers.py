"""Prospects testing Twin get their question answered (audit finding #4).

Small-brand owners are told to test Twin by DMing @glamshelfstore. Twin used
to answer "udit asked me to test this dm bot. how do u handle refunds?" with
only "Thanks for checking it out! Udit will message you personally 🤍" (2 of
3 audit runs). brain.md now tells the model to answer a tester's question
like any customer's, to invite a tester who asked nothing to ask one, and
to keep the LEAD line for someone who wants this assistant for their brand.
Whether the model does that is checked with the audit runner, not here.

These tests cover the deterministic parts: the LEAD backstop catches the
messages prospects actually send, a tester's answer reaches them unchanged,
a tester whose answer waits for approval still gets the founder a LEAD
notice, nobody here is paused, and brain.md keeps the approved lines.

No live API call anywhere here: every send and Telegram call is stubbed.

Run:  python -m unittest tests.test_prospect_testers
"""
import io, json, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-prospect-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import output_guard

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
INVITE_LINE = "Go ahead — ask me anything a customer would, like prices, delivery or which lashes suit you 🤍"
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}
SENDER = "17800000000000477"
HANDOFF = glam.BRAIN_HOLDING_LINE

F7 = "hey, udit asked me to test this dm bot. how do u handle refunds?"
PROSPECT_OPENERS = [
    F7,
    "hey udit sent me here, whats the price of GS1?",
    "testing ur bot lol. do u deliver to bangalore?",
    "udit asked me to test this",
    "I run a lash brand, want this bot for my store",
    "udit told me to try this",
]


class LeadBackstop(unittest.TestCase):
    """_LEAD_RE flags a prospect even when the model leaves the tag empty."""

    def setUp(self):
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)

    def test_every_prospect_opener_gets_the_lead_notice_untagged(self):
        for msg in PROSPECT_OPENERS:
            with self.subTest(msg=msg):
                self.assertTrue(glam._ig_is_lead(msg, "AUTO", "an answer", ""))

    def test_customers_are_not_prospects(self):
        for msg in ("can u send the price list?", "whats ur return policy?",
                    "Udit told me I'd get a refund",
                    "udit, you sent me the wrong pair",
                    "udit sent me the wrong lashes",
                    "my order was sent to me late",
                    "is this lash good for my brand shoot?"):
            with self.subTest(msg=msg):
                self.assertFalse(glam._ig_is_lead(msg, "AUTO", "an answer", ""))

    def test_backstop_still_never_turns_an_escalation_into_a_lead(self):
        self.assertFalse(glam._ig_is_lead(
            "hey udit sent me here, whats the price of GS1?", "ESCALATE", "x", ""))


class ProspectHandler(unittest.TestCase):
    """_process_instagram_event end to end, model and sends stubbed."""

    def setUp(self):
        glam._init_db()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED"):
            os.environ.pop(var, None)
        self.reset()
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "send_draft_for_approval", self._draft),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
            patch.object(glam, "_load_instagram_history", lambda *a, **k: []),
            patch.object(glam, "draft_reply_logic", self._model),
            patch.dict(glam._inventory_cache, {"prices": set(LIVE_PRICES)}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def reset(self):
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        self.sends, self.drafts, self.notices = [], [], []
        self.model = ("AUTO", "", "")

    def _send(self, sender_id, text):
        self.sends.append(text)
        return True, ""

    def _draft(self, **kwargs):
        self.drafts.append(kwargs)
        return True

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.notices.append({"classification": classification, "message": customer_message,
                             "reply": reply, **kwargs})

    def _model(self, text, order_line, history=None, source=""):
        classification, reply, tag = self.model
        return classification, reply, json.dumps(
            {"classification": classification, "reply": reply, "tag": tag})

    def dm(self, text):
        with redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"}, "timestamp": 1,
                "message": {"mid": f"m-{time.time_ns()}", "text": text},
            })

    def lead_notices(self):
        return [n for n in self.notices if n["classification"] == "LEAD"]

    def log_sources(self):
        conn = sqlite3.connect(glam.DB_PATH)
        try:
            return [r[0] for r in conn.execute(
                "SELECT source FROM instagram_logs WHERE sender_id = ? ORDER BY id", (SENDER,))]
        finally:
            conn.close()

    def test_tester_gets_the_answer_itself_and_the_founder_a_lead_notice(self):
        msg = "hey udit sent me here, whats the price of GS1?"
        answer = "GS1 is ₹849 for a tray of 10 pairs — soft and natural for everyday wear 🤍"
        for tag in ("LEAD", ""):   # the model's tag, or the backstop without it
            with self.subTest(tag=tag):
                self.reset()
                self.model = ("AUTO", answer, tag)
                self.dm(msg)
                self.assertEqual(self.sends, [answer])      # never swapped for a canned line
                (notice,) = self.lead_notices()
                self.assertEqual(notice["message"], msg)
                self.assertEqual(notice["reply"], answer)
                self.assertIn(SENDER, notice["sender_info"])
                self.assertEqual(self.log_sources(), ["LEAD_IG"])
                self.assertFalse(glam._is_paused(SENDER))

    def test_tester_answer_held_by_the_guard_still_notifies(self):
        # Output guard rule 2 holds any AUTO reply saying "refund" (audit
        # finding #7, a separate fix): the tester gets the handoff line and
        # the draft waits for approval — and the founder still gets the LEAD
        # notice.
        self.model = ("AUTO", "Refunds go back to your original payment method 🤍", "LEAD")
        self.dm(F7)
        self.assertEqual(self.sends, [HANDOFF])
        (draft,) = self.drafts
        self.assertIn("rule 2", draft["guard_note"])
        (notice,) = self.lead_notices()
        self.assertEqual(notice["message"], F7)
        self.assertEqual(notice["reply"], HANDOFF)          # what the tester actually got
        self.assertIn(SENDER, notice["sender_info"])
        self.assertFalse(glam._is_paused(SENDER))

    def test_tester_question_the_model_drafts_still_notifies(self):
        msg = "udit asked me to test this. can i return a tray i didnt like?"
        self.model = ("DRAFT+APPROVE", "We accept returns within 14 days of delivery 🤍", "LEAD")
        self.dm(msg)
        self.assertEqual(self.sends, [HANDOFF])
        self.assertEqual(len(self.drafts), 1)
        (notice,) = self.lead_notices()
        self.assertEqual(notice["message"], msg)
        self.assertFalse(glam._is_paused(SENDER))

    def test_second_draft_notice_says_nothing_new_was_sent(self):
        self.model = ("DRAFT+APPROVE", "We accept returns within 14 days of delivery 🤍", "LEAD")
        self.dm("udit asked me to test this. can i return a tray i didnt like?")
        self.dm("and if it arrives damaged?")
        # Handoff line once per window; the second draft gets the one-time
        # acknowledgement instead (audit finding 1). Known gap — the lead path
        # was out of scope there: this notice doesn't mention it yet.
        self.assertEqual(self.sends, [HANDOFF, glam.IG_DRAFT_ACK_LINE])
        first, second = self.lead_notices()
        self.assertEqual(first["reply"], HANDOFF)
        self.assertIn("nothing new", second["reply"])

    def test_notice_says_so_when_the_handoff_line_failed_to_send(self):
        self.model = ("DRAFT+APPROVE", "We accept returns within 14 days of delivery 🤍", "LEAD")
        with patch.object(glam, "_send_instagram_reply", lambda s, t: (False, "HTTP 400: token expired")):
            self.dm("udit asked me to test this. can i return a tray i didnt like?")
        self.assertEqual(len(self.drafts), 1)              # the draft still waits for approval
        (notice,) = self.lead_notices()
        self.assertIn("failed to send", notice["reply"])
        self.assertNotIn("recently", notice["reply"])
        self.assertFalse(glam._is_paused(SENDER))

    def test_ordinary_customer_draft_gets_no_lead_notice(self):
        self.model = ("DRAFT+APPROVE", "So sorry about the mix-up 🤍", "")
        self.dm("i ordered GS1 but got GS2 in the box")
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.lead_notices(), [])

    def test_health_signal_on_a_drafted_tester_message_gets_no_lead_notice(self):
        self.model = ("DRAFT+APPROVE", "I'm sorry to hear that 🤍", "LEAD")
        self.dm("testing your bot — my eyes got itchy after GS1")
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.lead_notices(), [])


class BrainTesterRule(unittest.TestCase):
    def test_invite_and_lead_lines_are_in_brain_and_pass_the_output_guard(self):
        for line in (INVITE_LINE, glam.LEAD_REPLY):
            with self.subTest(line=line):
                self.assertIn(line, BRAIN)
                # Sent only with a LEAD notice (rule 7's founder_notice).
                self.assertEqual(output_guard.check_reply(line, LIVE_PRICES, founder_notice=True), [])
        self.assertNotIn("Udit", INVITE_LINE)

    def test_testers_are_no_longer_short_circuited_to_the_lead_line(self):
        self.assertNotIn("answer it briefly first, then add that line", BRAIN)
        self.assertNotIn('→ 🟢 AUTO with tag "LEAD"', BRAIN)
        self.assertIn("exactly as you would for any customer", BRAIN)


if __name__ == "__main__":
    unittest.main()
