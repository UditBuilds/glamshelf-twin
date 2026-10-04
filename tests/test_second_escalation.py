"""A second escalation inside the handoff window doesn't resend the holding
line (audit finding 8).

A customer whose message went to a draft got "I've passed this to the team —
they'll reply to you here"; if their next message escalated minutes later,
they got the same line again. Now the founder is paged and the customer is
paused as before, but nothing new is sent inside the 30-minute window. The
safety line (health advice) always goes out.

No live API call anywhere here: sends, Telegram and the model are stubbed.

Run:  python -m unittest tests.test_second_escalation
"""
import io, json, os, sqlite3, sys, tempfile, unittest, uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-second-escalation-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000871"
HANDOFF = glam.BRAIN_HOLDING_LINE


def log_rows():
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute("SELECT message_text, reply_text, source FROM instagram_logs "
                            "WHERE sender_id = ? ORDER BY id", (SENDER,)).fetchall()
    finally:
        conn.close()


class SecondEscalation(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED"):
            os.environ.pop(var, None)
        self.sends, self.notices = [], []
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "send_draft_for_approval", lambda **k: True),
            patch.object(glam, "_telegram_api", lambda method, payload: {"ok": True}),
            patch.object(glam, "_persist_seen_id", lambda mid: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, sender_id, text):
        self.sends.append(text)
        return True, ""

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.notices.append({"classification": classification, **kwargs})

    def dm(self, text, classification, reply, tag=""):
        raw = json.dumps({"classification": classification, "reply": reply, "tag": tag})
        with patch.object(glam, "draft_reply_logic", lambda *a, **k: (classification, reply, raw)), \
             redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"}, "timestamp": 1,
                "message": {"mid": f"second.{uuid.uuid4().hex}", "text": text},
            })

    def escalate(self, text="THIS IS RIDICULOUS, NO ONE REPLIES"):
        self.dm(text, "ESCALATE", "Really sorry for the back and forth 🤍")

    def test_a_draft_then_an_escalation_sends_the_holding_line_once(self):
        self.dm("my tray arrived damaged", "DRAFT+APPROVE", "So sorry — please email the photos 🤍")
        self.escalate()
        self.assertEqual(self.sends, [HANDOFF])
        (notice,) = [n for n in self.notices if n["classification"] == "ESCALATE"]
        self.assertEqual(notice["customer_line"],
                         "Nothing new sent — they got the holding line in the last 30 minutes.")
        self.assertFalse(notice["holding_reply_sent"])
        self.assertTrue(glam._is_paused(SENDER))
        self.assertEqual(log_rows()[-1], ("THIS IS RIDICULOUS, NO ONE REPLIES", None, "ESCALATE_REPEAT_IG"))

    def test_the_repeat_row_stays_out_of_history(self):
        self.dm("my tray arrived damaged", "DRAFT+APPROVE", "So sorry 🤍")
        self.escalate()
        glam._unpause_number(SENDER)
        history = [(h["msg_text"], h["reply_text"]) for h in glam._load_instagram_history(SENDER)]
        self.assertEqual(history, [("my tray arrived damaged", HANDOFF)])

    def test_after_the_window_the_holding_line_goes_out_again(self):
        self.escalate()
        glam._resume_sender(SENDER)
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("UPDATE instagram_logs SET logged_at = datetime('now', '-31 minutes')")
        conn.commit()
        conn.close()
        self.escalate("STILL NO REPLY, THIS IS RIDICULOUS")
        self.assertEqual(self.sends, [HANDOFF, HANDOFF])

    def test_the_safety_line_always_goes_out(self):
        self.escalate()
        glam._resume_sender(SENDER)
        self.dm("my eye is swollen after wearing them", "ESCALATE", "x", tag="SAFETY")
        self.assertEqual(self.sends, [HANDOFF, glam.ALLERGY_HOLDING_REPLY])

    def test_legal_escalations_stay_silent_as_before(self):
        self.dm("my tray arrived damaged", "DRAFT+APPROVE", "So sorry 🤍")
        self.dm("I will send you a legal notice", "ESCALATE", "x", tag="LEGAL")
        (notice,) = [n for n in self.notices if n["classification"] == "ESCALATE"]
        self.assertIn("legal escalations get no automated reply", notice["customer_line"])
        self.assertEqual(log_rows()[-1][2], "ESCALATE_IG")


if __name__ == "__main__":
    unittest.main()
