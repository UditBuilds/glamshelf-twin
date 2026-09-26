"""Instagram escalations no longer mean silence; testers become LEADs;
pauses can be lifted from Telegram (audit T1-5).

The end-to-end dispatch cases (holding line, safety line, legal/press
silence, LEAD) run through both implementations in test_graph_parity.py.
This file covers the pieces underneath:

  - _ig_escalation_reply / _ig_is_lead / _parse_twin_tag decision tables,
    including the deterministic backstops and their deliberate blind spots
    ("press-on lashes", "social media");
  - the Telegram ESCALATE / LEAD messages and the ▶️ Resume bot button,
    with WhatsApp's messages unchanged;
  - lifting a pause early: ▶️ Resume bot, "#resume <id>", and the
    human-takeover hold that a resume must clear too.

No live API call anywhere here: Telegram and every send are stubbed.

Run:  python -m unittest tests.test_instagram_escalation
"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ig-escalation-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000654"
CHAT = "4242"


class EscalationReplyTable(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)

    def reply_for(self, message, tag=""):
        return glam._ig_escalation_reply(message, tag)

    def test_ordinary_escalation_gets_the_holding_line(self):
        self.assertEqual(self.reply_for("I want to speak to the owner"), ("other", glam.BRAIN_HOLDING_LINE))

    def test_legal_by_tag_or_by_prefilter_phrase_is_silent(self):
        self.assertEqual(self.reply_for("ab court me jaaunga", "LEGAL"), ("legal", ""))
        self.assertEqual(self.reply_for("I'll send you a legal notice"), ("legal", ""))
        # A later legal phrase counts even when another prefilter phrase comes first.
        self.assertEqual(self.reply_for("refund karo warna police bulaungi")[0], "legal")

    def test_non_legal_prefilter_phrases_still_get_the_holding_line(self):
        self.assertEqual(self.reply_for("refund karo abhi")[0], "other")
        self.assertEqual(self.reply_for("I will post this on social media")[0], "other")

    def test_press_by_tag_or_backstop_is_silent(self):
        self.assertEqual(self.reply_for("I write for a magazine"), ("press", ""))
        self.assertEqual(self.reply_for("feature request", "PRESS"), ("press", ""))

    def test_press_backstop_ignores_lash_talk(self):
        self.assertEqual(self.reply_for("do you sell press-on lashes?")[0], "other")
        self.assertEqual(self.reply_for("saw you on social media")[0], "other")

    def test_symptoms_get_the_allergy_line(self):
        self.assertEqual(self.reply_for("my eyes are itchy and red", ""), ("safety", glam.ALLERGY_HOLDING_REPLY))
        self.assertEqual(self.reply_for("something happened", "SAFETY"), ("safety", glam.ALLERGY_HOLDING_REPLY))

    def test_legal_or_press_plus_symptoms_get_the_safety_text_only(self):
        self.assertEqual(
            self.reply_for("rashes ho gaye, I'm calling my lawyer"),
            ("legal+safety", glam.ALLERGY_SAFETY_TEXT),
        )
        self.assertEqual(
            self.reply_for("journalist here — a reader had an allergic reaction"),
            ("press+safety", glam.ALLERGY_SAFETY_TEXT),
        )

    def test_safety_line_is_brain_md_allergy_holding_reply(self):
        brain = (Path(__file__).resolve().parent.parent / "brain" / "brain.md").read_text(encoding="utf-8")
        self.assertIn(glam.ALLERGY_HOLDING_REPLY, brain)


class LeadDecision(unittest.TestCase):
    def test_lead_tag(self):
        self.assertTrue(glam._ig_is_lead("Udit asked me to test this", "AUTO", "hi", "LEAD"))
        self.assertTrue(glam._ig_is_lead("Udit asked me to test this", "ESCALATE", "", "LEAD"))
        self.assertFalse(glam._ig_is_lead("testing your bot", "DRAFT+APPROVE", "hi", "LEAD"))

    def test_serious_signals_outrank_a_lead(self):
        self.assertFalse(glam._ig_is_lead("testing your bot, also calling my lawyer", "AUTO", "hi", "LEAD"))
        self.assertFalse(glam._ig_is_lead("testing your bot — my eyes are swollen", "AUTO", "hi", "LEAD"))
        self.assertFalse(glam._ig_is_lead("testing your bot, I'll take 50 trays", "AUTO", "hi", "LEAD"))

    def test_backstop_only_adds_a_notice_to_auto_replies(self):
        self.assertTrue(glam._ig_is_lead("Udit asked me to test this", "AUTO", "hi", ""))
        self.assertTrue(glam._ig_is_lead("can I get an AI assistant like this for my brand?", "AUTO", "hi", ""))
        self.assertFalse(glam._ig_is_lead("Udit asked me to test this", "ESCALATE", "hi", ""))
        self.assertFalse(glam._ig_is_lead("Udit told me I'd get a refund", "AUTO", "hi", ""))
        self.assertFalse(glam._ig_is_lead("price of GS1?", "AUTO", "hi", ""))

    def test_parse_tag(self):
        self.assertEqual(glam._parse_twin_tag(json.dumps({"tag": "lead"})), "LEAD")
        self.assertEqual(glam._parse_twin_tag(json.dumps({"tag": " Safety "})), "SAFETY")
        self.assertEqual(glam._parse_twin_tag(json.dumps({"tag": "VIP"})), "")
        self.assertEqual(glam._parse_twin_tag(json.dumps({"classification": "AUTO"})), "")
        self.assertEqual(glam._parse_twin_tag("not json"), "")
        self.assertEqual(glam._parse_twin_tag(""), "")


class TelegramMessages(unittest.TestCase):
    def setUp(self):
        self.posts = []

        class Ok:
            ok, status_code, text = True, 200, "{}"

        for p in (
            patch.object(glam, "TELEGRAM_BOT_TOKEN", "bot-token"),
            patch.object(glam, "TELEGRAM_CHAT_ID", CHAT),
            patch.object(glam.requests, "post", lambda url, json=None, timeout=None: self.posts.append(json) or Ok()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_instagram_escalation_says_what_the_customer_got(self):
        glam.send_telegram_notification(
            "ESCALATE", "where is my order??", "Model draft",
            sender_info="Instagram DM — sender X", channel="Instagram",
            customer_id=SENDER, holding_reply_sent=True,
            customer_line='Sent to the customer:\n"holding"',
        )
        (payload,) = self.posts
        self.assertIn('Sent to the customer:\n"holding"', payload["text"])
        self.assertIn('Twin\'s draft (not sent):\n"Model draft"', payload["text"])
        self.assertIn(f'#resume {SENDER}', payload["text"])
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons],
                         [f"action:pause_escalate|id:{SENDER}", f"action:resume|id:{SENDER}"])

    def test_whatsapp_escalation_message_is_unchanged(self):
        glam.send_telegram_notification(
            "ESCALATE", "msg", "holding", sender_info="Priya (919...)", customer_id="919812345670",
        )
        (payload,) = self.posts
        self.assertIn("Suggested holding reply:", payload["text"])
        self.assertIn("→ Do NOT send the reply. Handle this yourself.", payload["text"])
        self.assertEqual(len(payload["reply_markup"]["inline_keyboard"][0]), 1)

    def test_lead_notice(self):
        glam.send_telegram_notification(
            "LEAD", "Udit asked me to test this", glam.LEAD_REPLY,
            sender_info="Instagram DM — sender X", channel="Instagram", customer_id=SENDER,
        )
        (payload,) = self.posts
        self.assertTrue(payload["text"].startswith("🟢 LEAD"))
        self.assertIn(glam.LEAD_REPLY, payload["text"])
        self.assertNotIn("reply_markup", payload)


class ResumeFromTelegram(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        self.tg = []
        for p in (
            patch.object(glam, "TELEGRAM_CHAT_ID", CHAT),
            patch.object(glam, "_telegram_api", lambda method, payload: self.tg.append((method, payload))),
        ):
            p.start()
            self.addCleanup(p.stop)
        glam._unpause_number(SENDER)

    def held(self):
        return glam._is_paused(SENDER) or glam._udit_replied_recently_ig(SENDER)

    def test_resume_clears_the_pause_and_the_human_takeover_hold(self):
        glam._log_instagram(SENDER, "", "Udit's manual reply", "1", source="HUMAN_UDIT_INSTAGRAM")
        glam._pause_number(SENDER)
        self.assertTrue(glam._is_paused(SENDER))
        self.assertTrue(glam._udit_replied_recently_ig(SENDER))
        self.assertTrue(glam._resume_sender(SENDER))
        self.assertFalse(self.held())
        # A NEW manual reply after the resume holds the bot back again.
        glam._log_instagram(SENDER, "", "another manual reply", "2", source="HUMAN_UDIT_INSTAGRAM")
        self.assertTrue(glam._udit_replied_recently_ig(SENDER))
        # The resume marker never shows up as conversation history.
        self.assertEqual(glam._load_instagram_history(SENDER), [])

    def test_resume_button(self):
        glam._pause_number(SENDER)
        glam._handle_telegram_callback({
            "id": "cb1", "data": f"action:resume|id:{SENDER}",
            "message": {"chat": {"id": int(CHAT)}, "message_id": 7, "text": "🔴 ESCALATE ..."},
        })
        self.assertFalse(glam._is_paused(SENDER))
        self.assertIn("answerCallbackQuery", [m for m, _ in self.tg])

    def test_resume_command(self):
        glam._pause_number(SENDER)
        glam._handle_telegram_message({"chat": {"id": int(CHAT)}, "text": f"#resume {SENDER}"})
        self.assertFalse(glam._is_paused(SENDER))
        (method, payload), = self.tg
        self.assertIn(f"Bot resumed for {SENDER}", payload["text"])

    def test_resume_without_id_explains_usage(self):
        glam._handle_telegram_message({"chat": {"id": int(CHAT)}, "text": "#resume"})
        (method, payload), = self.tg
        self.assertIn("Usage: #resume <customer id>", payload["text"])

    def test_resume_command_is_never_sent_to_a_customer_as_an_edit(self):
        draft_id = "abcd1234"
        self.addCleanup(glam._draft_delete, draft_id)
        glam._draft_register(draft_id, {
            "reply_text": "draft", "customer_number": SENDER, "customer_name": "",
            "customer_message": "q", "original_text": "o", "telegram_chat_id": int(CHAT),
            "telegram_message_id": 1, "awaiting_edit": True, "edit_started_at": 1.0,
            "created_at": 1.0, "channel": "Instagram", "ig_timestamp": "",
        })
        sends = []
        with patch.object(glam, "_send_instagram_reply", lambda *a: sends.append(a) or (True, "")):
            glam._handle_telegram_message({"chat": {"id": int(CHAT)}, "text": f"#resume {SENDER}"})
        self.assertEqual(sends, [])
        self.assertIsNotNone(glam._draft_get(draft_id))  # edit flow untouched

    def test_unauthorized_chat_is_ignored(self):
        glam._pause_number(SENDER)
        glam._handle_telegram_message({"chat": {"id": 999}, "text": f"#resume {SENDER}"})
        self.assertTrue(glam._is_paused(SENDER))


if __name__ == "__main__":
    unittest.main()
