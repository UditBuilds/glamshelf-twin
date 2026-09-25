"""Twin only promises what's true (audit T1-6).

brain.md used to tell Twin to promise things nobody does: follow up with
the courier "within a few hours", pull up order details, notify a waitlist
that doesn't exist, send a follow-up after 3 days, handle support "over
WhatsApp only" while WhatsApp was down. These tests keep those phrases out
of the brain, keep the code's holding line in sync with it, and cover the
ORDER / RESTOCK heads-up that makes "I've passed this to the team" true on
an AUTO Instagram reply.

No live API call anywhere here.

Run:  python -m unittest tests.test_honest_replies
"""
import os, re, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-honest-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")


class BrainMakesNoFalsePromises(unittest.TestCase):
    FALSE_PROMISES = [
        "I'll personally notify you",
        "I'll personally follow up with the courier",
        "I'm pulling the proof of delivery",
        "I'll check within 10 minutes",
        "I'll pull up your order details",
        "I'm not finding an order under this number",
        "Your order is being packed",
        "I'll personally let you know",
        "support over WhatsApp only",
        "all support is WhatsApp text only",
        "You're on the notification list",
        "I promise",
        "Just checking in",
        "ONE soft follow-up",
        "respond with progress update",
        "I can see two orders",
        "main courier se personally follow up",
    ]

    def test_removed_phrases_stay_removed(self):
        still_there = [p for p in self.FALSE_PROMISES if p in BRAIN]
        self.assertEqual(still_there, [])

    def test_capability_rule_is_stated(self):
        self.assertIn("### What You Can and Can't Do", BRAIN)
        self.assertIn("On Instagram you cannot see orders, tracking, couriers", BRAIN)
        self.assertIn("You cannot message anyone later.", BRAIN)

    def test_delivery_charge_is_stated_without_an_amount(self):
        line = next(l for l in BRAIN.splitlines() if "Delivery charge on orders of ₹799 or less" in l)
        self.assertIn("shown at checkout", line)
        self.assertIsNone(re.search(r"₹\s?\d", line.replace("₹799", "")))

    def test_code_holding_line_matches_brain_default_handoff_line(self):
        section = BRAIN.split("### Default Handoff Line", 1)[1]
        quoted = re.search(r'> "(.+?)"', section).group(1)
        self.assertEqual(quoted, glam.BRAIN_HOLDING_LINE)

    def test_no_follow_up_messages(self):
        self.assertIn("### No Follow-up Messages", BRAIN)


class OrderAndRestockHeadsUp(unittest.TestCase):
    def test_topic_table(self):
        topic = glam._ig_fyi_topic
        self.assertEqual(topic("anything", "ORDER"), "ORDER")
        self.assertEqual(topic("anything", "RESTOCK"), "RESTOCK")
        self.assertEqual(topic("where is my order #1043", ""), "ORDER")
        self.assertEqual(topic("tracking link nahi aaya", ""), "ORDER")
        self.assertEqual(topic("order kab aayega", ""), "ORDER")
        self.assertEqual(topic("GS3 kab restock hoga?", ""), "RESTOCK")
        self.assertEqual(topic("when will the half lashes be back?", ""), "RESTOCK")
        self.assertEqual(topic("price of GS1?", ""), "")
        self.assertEqual(topic("which lash suits hooded eyes?", "LEAD"), "")

    def test_notice_text(self):
        posts = []

        class Ok:
            ok, status_code, text = True, 200, "{}"

        with patch.object(glam, "TELEGRAM_BOT_TOKEN", "bot"), \
             patch.object(glam, "TELEGRAM_CHAT_ID", "1"), \
             patch.object(glam.requests, "post", lambda url, json=None, timeout=None: posts.append(json) or Ok()):
            glam._ig_send_fyi("1780", "where is my order", "I've passed this to the team 🤍", "ORDER", True)
            glam._ig_send_fyi("1780", "notify me", "sold out 🤍", "RESTOCK", False)
        order, restock = (p["text"] for p in posts)
        self.assertTrue(order.startswith("📦 ORDER question"))
        self.assertIn("Reply with the real status from your Instagram DMs", order)
        self.assertTrue(restock.startswith("🔔 RESTOCK request"))
        self.assertIn("(send FAILED) sold out", restock)

    def test_prompt_offers_the_new_tags(self):
        self.assertIn('"ORDER" | "RESTOCK"', glam.build_user_message("hi", ""))


if __name__ == "__main__":
    unittest.main()
