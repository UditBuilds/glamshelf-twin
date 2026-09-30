"""No follow-up promises nobody is told about; angry complaints escalate;
Hinglish in, Hinglish out; a decided buyer gets the link (audit findings 6,
11, 14 and 15, and brain.md's stale empty-reply note).

Twin can't message anyone later, so "we'll take it from there" on a plain
AUTO reply (audit D4) or "whenever you're ready, we'll be right here" (E3)
promised something nobody would do. Output guard rule 7 now holds a
follow-up promise; the handoff, ack and LEAD lines pass only when a founder
notice goes out with the reply.

No live API call anywhere here: every send and Telegram call is stubbed.

Run:  python -m unittest tests.test_promises_and_routing
"""
import io, json, os, re, sqlite3, sys, tempfile, unittest, uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-promises-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import output_guard

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}
SENDER = "17800000000000611"
HANDOFF = glam.BRAIN_HOLDING_LINE
ACK = glam.IG_DRAFT_ACK_LINE


def guard(text, founder_notice=False):
    return output_guard.check_reply(text, LIVE_PRICES, founder_notice=founder_notice)


def rule7(text, founder_notice=False):
    return [r for r in guard(text, founder_notice) if r.startswith("rule 7")]


class FollowUpPromisesAreHeld(unittest.TestCase):
    def test_the_audits_promises_are_held(self):
        for text in (
            # audit D4, brain.md:552's template, on an untagged AUTO reply
            "Our bulk rate is ₹749/tray for orders of 20+ — and shipping is free, since an order that "
            "size is well above ₹799. Please share your Instagram handle or business name and we'll "
            "take it from there 🤍",
            # audit E3
            "That sounds lovely — whenever you're ready, we'll be right here. You can order anytime at "
            "glamshelf.in 🤍",
            # the founder's examples
            "Refunds are initiated within 24–48 hours of approval, and the team will update you 🤍",
            "No problem, we'll remind you next week 🤍",
            "We'll be right here whenever you need us 🤍",
            # brain.md's old warm close
            "We'll be in touch soon 🤍",
            "GS3 is sold out — I'll let you know as soon as it's back 🤍",
            "You'll hear from us soon 🤍",
            "Team The Glam Shelf will personally look into this and get back to you shortly 🤍",
            "Our team will reach out to you shortly 🤍",
            "Got it — Team The Glam Shelf has your details and will reach out shortly 🤍",
            # Hinglish
            "Bilkul, next week remind kar denge 🤍",
            "Team aapko jaldi update karegi 🤍",
        ):
            with self.subTest(text=text):
                self.assertTrue(rule7(text), text)

    def test_founder_notice_does_not_excuse_other_promises(self):
        # An ORDER heads-up makes "they'll reply to you here" true — not
        # "the team will update you" or a reminder.
        self.assertTrue(rule7("I've passed this to the team and they'll update you shortly 🤍", True))
        self.assertTrue(rule7("We'll remind you next week 🤍", True))

    def test_the_handoff_ack_and_lead_lines_need_a_founder_notice(self):
        for text in (
            HANDOFF,
            ACK,
            glam.LEAD_REPLY,
            "Sorry it's taking longer than usual — could you share your order ID? I've passed this "
            "to the team and they'll reply to you here with an update 🤍",
            "Happy to help — could you share your order ID? I can't look orders up myself, so I'll "
            "pass it to the team and they'll reply to you here 🤍",
            "The team has your return request and will reply to you here 🤍",
        ):
            with self.subTest(text=text):
                self.assertEqual(rule7(text, founder_notice=True), [])
                self.assertTrue(rule7(text, founder_notice=False))

    def test_answers_that_promise_no_later_contact_pass(self):
        for text in (
            "Refunds are initiated within 24–48 hours of approval — from there it reflects in UPI/bank "
            "accounts in 5–7 working days and cards in 7–10 working days 🤍",
            "Refunds are initiated within 24–48 hours of approval, then take 5–7 working days to reflect "
            "for UPI/bank and 7–10 working days for cards. If you share your order ID, I'll pass it to "
            "the team so they can check yours 🤍",
            "I can't send reminders from here, but you can order anytime at glamshelf.in 🤍",
            "Orders usually dispatch within 2–5 business days, and delivery takes 7–10 business days "
            "anywhere in India. You'll get a tracking link by SMS once it ships 🤍",
            "Welcome back — let me know what you'd like to go ahead with and I'll help you through it 🤍",
            "Just email glamshelfstore@gmail.com with your order ID to get it started 🤍",
            "Which occasion is it for? I'll pick the right one.",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])

    def test_curly_apostrophes_are_caught(self):
        self.assertTrue(rule7("We’ll be right here 🤍"))


def template_after(heading: str) -> str:
    return re.search(r'^> "(.+?)"$', BRAIN.split(heading, 1)[1], re.M).group(1)


class BrainPromisesNoFollowUp(unittest.TestCase):
    def test_the_rule_is_stated(self):
        self.assertIn("never promise a reminder: if they ask you to remind them later, say plainly "
                      "you can't send reminders", BRAIN)
        rule = next(l for l in BRAIN.splitlines() if l.startswith("- **No other follow-up promise on an AUTO reply:**"))
        for phrase in ("the team will update you", "we'll remind you", "we'll be right here",
                       "we'll take it from there", "we'll get back to you", "🟡 DRAFT+APPROVE"):
            self.assertIn(phrase, rule)

    def test_bulk_template_no_longer_promises_to_take_it_from_there(self):
        t = template_after("**Bulk / MUA pricing — 20+ trays confirmed:**")
        self.assertNotIn("take it from there", t)
        self.assertIn("20+ trays", t)
        self.assertIn("shipping is free", t)        # brain.md: bulk rate ships with the free-shipping fact
        self.assertEqual(guard(t), [])

    def test_reminder_template(self):
        heading = "**Customer asks for a reminder"
        self.assertIn("🟢 AUTO", next(l for l in BRAIN.splitlines() if l.startswith(heading)))
        t = template_after(heading)
        self.assertEqual(t, "I can't send reminders from here, but you can order anytime at glamshelf.in 🤍")
        self.assertEqual(guard(t), [])

    def test_warm_close_is_not_a_promise(self):
        self.assertNotIn('`"We\'ll be in touch soon 🤍"` and Classify', BRAIN)
        self.assertIn('`"Happy to help 🤍"` (never a follow-up promise', BRAIN)
        self.assertEqual(guard("Happy to help 🤍"), [])

    def test_collab_replies_go_to_a_draft(self):
        self.assertIn("**Collab / ambassador:** 🟡 DRAFT+APPROVE", BRAIN)
        self.assertIn("| 10 | Collab / ambassador DM | 🟡 DRAFT+APPROVE", BRAIN)
        self.assertIn("sends their Instagram handle) → 🟡 DRAFT+APPROVE", BRAIN)
        # The collab drafts still promise a follow-up — fine in a draft the
        # founder approves, held if the model ever sent them AUTO.
        self.assertTrue(rule7("Got it — Team The Glam Shelf has your details and will reach out shortly 🤍"))


F4 = "THIS IS RIDICULOUS. ordered 12 days ago, emailed you twice, NO ONE REPLIES. worst brand ever"


class AngryComplaintsEscalate(unittest.TestCase):
    """Finding 11: the founder's rule — an angry complaint (all caps,
    "ridiculous", "no one replies") always escalates, legal threat or not."""

    def setUp(self):
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)

    def test_angry_messages_hit_the_prefilter(self):
        for message in (
            F4,
            "this is ridiculous, where is my order",
            "no one replies on whatsapp either",
            "nobody is responding to my emails",
            "worst service ever, still no parcel",
            "koi reply nahi aaya abhi tak",
            "WHY IS MY PARCEL STILL NOT HERE AFTER 10 DAYS",
            "pathetic. 2 weeks and nothing",
        ):
            with self.subTest(message=message):
                self.assertTrue(glam._escalation_prefilter_hit(message))

    def test_look_alikes_do_not(self):
        for message in (
            "these are ridiculously pretty!!",
            "PRICE OF GS1?",
            "HI I WANT TO ORDER GS1 TRAY",
            "OMG THESE ARE SO CUTE",
            "COD hai kya?",
            "is GS2 band thicker than GS1?",
            "what's the worst that can happen if I wear them daily?",
            "does anyone reply on sundays?",
        ):
            with self.subTest(message=message):
                self.assertIsNone(glam._escalation_prefilter_hit(message))

    def test_legal_threats_still_win_and_stay_silent(self):
        text = "THIS IS RIDICULOUS, I'll take you to consumer court"
        self.assertEqual(glam._escalation_prefilter_hit(text).lower(), "consumer court")
        self.assertEqual(glam._ig_escalation_reply(text, "")[0], "legal")

    def test_an_angry_complaint_gets_the_handoff_line(self):
        self.assertEqual(glam._ig_escalation_reply(F4, ""), ("other", HANDOFF))

    def test_kill_switch_turns_it_off(self):
        os.environ["ESCALATION_PREFILTER_DISABLED"] = "1"
        try:
            self.assertIsNone(glam._escalation_prefilter_hit(F4))
        finally:
            os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)

    def test_brain_says_escalate(self):
        self.assertIn('| 43 | Angry complaint — one gaali, a caps-lock rant, "ridiculous", "no one replies", '
                      '"worst brand" | 🔴 ESCALATE — always, legal threat or not', BRAIN)
        self.assertNotIn("| 43 | One gaali / caps lock rant | 🟡 DRAFT+APPROVE |", BRAIN)


class HinglishInHinglishOut(unittest.TestCase):
    """Finding 14: 0 of 7 Hinglish messages got a Hinglish reply. brain.md's
    only Hinglish example said "(only if customer is informal)"."""

    def test_the_rule_is_every_time_in_roman_script(self):
        rule = next(l for l in BRAIN.splitlines() if l.startswith("- **Language mirroring:**"))
        self.assertIn("reply in Hinglish, in Roman script, with exactly the same facts", rule)
        self.assertIn("every time, not only when the customer is informal", rule)
        self.assertIn("Keep policy sentences in English, word for word", rule)
        self.assertNotIn("Example Hinglish mirror (only if customer is informal)", BRAIN)

    def test_the_examples_pass_the_guard(self):
        notes = BRAIN.split("### Hinglish Mirroring", 1)[1].split("###", 1)[0]
        examples = re.findall(r'→ \*"(.+?)"\*', notes)
        self.assertEqual(len(examples), 3)
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])

    def test_english_policy_sentences_keep_a_hinglish_reply_sendable(self):
        for text in (
            "Refunds are initiated within 24–48 hours of approval, then reach UPI/bank accounts in "
            "5–7 working days. Return ke liye glamshelfstore@gmail.com pe order ID ke saath email "
            "kar dijiye 🤍",
            "Humare prices already MRP se kam hain. Our prices are already reduced from the original "
            "MRP — there's no additional discount available at the moment 🤍",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])

    def test_a_hinglish_policy_sentence_is_held(self):
        # Why brain.md keeps policy sentences in English: rule 2 only knows
        # the English statements, so this becomes a draft.
        self.assertIn("rule 2", " ".join(guard("₹799 se upar ke orders pe shipping free hai 🤍")))


class DecidedBuyerGetsTheLink(unittest.TestCase):
    """Finding 15: "just place the order for me - GS1 tray. ill pay later"
    got "checkout on glamshelf.in is the way to go" — no product link."""

    def rule(self):
        return BRAIN.split("**RULE: CUSTOMER ALREADY DECIDED — DON'T QUALIFY**", 1)[1].split("**RULE:", 1)[0]

    def test_place_the_order_for_me_is_a_decided_buyer(self):
        rule = self.rule()
        self.assertIn('"place the order for me — GS1 tray"', rule)
        self.assertIn('"ill pay later"', rule)
        self.assertIn("never send them to the homepage instead of the product link", rule)

    def test_the_template_has_the_link_and_passes_the_guard(self):
        quoted = re.findall(r'^> (.*)$', self.rule().split("If they ask you to place the order for them", 1)[1], re.M)
        text = "\n".join(q.strip('"') for q in quoted[:4])
        self.assertIn("I can't place orders from here", text)
        self.assertIn("glamshelf.in/products/gs1-luxe-light-lash-tray", text)
        self.assertIn("prepaid only", text)
        self.assertEqual(guard(text), [])


class FollowUpGuardInTheHandler(unittest.TestCase):
    """_process_instagram_event end to end, model and sends stubbed."""

    def setUp(self):
        glam._init_db()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED"):
            os.environ.pop(var, None)
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        self.sends, self.drafts, self.notices = [], [], []
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "send_draft_for_approval", self._draft),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
            patch.object(glam, "_load_instagram_history", lambda *a, **k: []),
            patch.object(glam, "_persist_seen_id", lambda mid: None),
            patch.dict(glam._inventory_cache, {"prices": set(LIVE_PRICES)}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, sender_id, text):
        self.sends.append(text)
        return True, ""

    def _draft(self, **kwargs):
        self.drafts.append(kwargs)
        return True

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.notices.append(classification)

    def dm(self, text, classification, reply, tag=""):
        raw = json.dumps({"classification": classification, "reply": reply, "tag": tag})
        with patch.object(glam, "draft_reply_logic", lambda *a, **k: (classification, reply, raw)), \
             redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "1"},
                "timestamp": 1, "message": {"mid": f"promises.{uuid.uuid4().hex}", "text": text},
            })

    def test_order_tagged_handoff_wording_is_sent_with_the_heads_up(self):
        reply = "Could you share your order ID? I've passed this to the team and they'll reply to you here 🤍"
        self.dm("my parcel still hasnt come", "AUTO", reply, tag="ORDER")
        self.assertEqual(self.sends, [reply])
        self.assertEqual(self.notices, ["ORDER"])
        self.assertEqual(self.drafts, [])

    def test_the_same_wording_on_a_plain_auto_reply_becomes_a_draft(self):
        reply = "Could you share your order ID? I've passed this to the team and they'll reply to you here 🤍"
        self.dm("hi, quick question about lashes", "AUTO", reply)
        self.assertEqual(self.sends, [HANDOFF])       # now true: the founder has the draft
        (draft,) = self.drafts
        self.assertIn("rule 7", draft["guard_note"])

    def test_bulk_template_promise_becomes_a_draft(self):
        reply = ("Our bulk rate is ₹749/tray for orders of 20+ — and shipping is free, since an order "
                 "that size is well above ₹799. Please share your Instagram handle or business name and "
                 "we'll take it from there 🤍")
        self.dm("whats ur bulk rate for 30 trays", "AUTO", reply)
        self.assertEqual(self.sends, [HANDOFF])
        self.assertIn("rule 7", self.drafts[0]["guard_note"])

    def test_f4_escalates_even_when_the_model_says_draft(self):
        # The audit's main run: the model said DRAFT+APPROVE, so no pause.
        # Only the model call is stubbed; draft_reply_logic's prefilter runs.
        raw = json.dumps({"classification": "DRAFT+APPROVE",
                          "reply": "Really sorry about the back and forth 🤍", "tag": ""})
        with patch.object(glam, "ask_claude", lambda *a, **k: raw), \
             patch.object(glam, "get_live_inventory", lambda: ""), \
             patch.object(glam, "get_live_policies", lambda: ""), \
             patch.object(glam, "_rag_retrieve", lambda m: ""), \
             redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "1"},
                "timestamp": 1, "message": {"mid": f"promises.{uuid.uuid4().hex}", "text": F4},
            })
        self.assertEqual(self.sends, [HANDOFF])
        self.assertEqual(self.notices, ["ESCALATE"])
        self.assertEqual(self.drafts, [])
        self.assertTrue(glam._is_paused(SENDER))

    def test_lead_line_is_sent_with_the_lead_notice(self):
        self.dm("I run a lash brand, want this bot for my store", "AUTO", glam.LEAD_REPLY, tag="LEAD")
        self.assertEqual(self.sends, [glam.LEAD_REPLY])
        self.assertEqual(self.notices, ["LEAD"])


if __name__ == "__main__":
    unittest.main()
