"""Instagram DRAFT+APPROVE no longer means silence (audit T2-14).

Before: an Instagram message routed to DRAFT+APPROVE sent the customer
nothing until the founder tapped Send in Telegram — and Instagram only
accepts a reply within 24h of the customer's last message, so a late
approval failed with Meta's raw error (muted after the first one).

Now:
  - the customer immediately gets the brain's Default Handoff Line, at
    most once per sender per 30 minutes (the same line from the
    pipeline-failure or escalation path counts);
  - the Telegram draft says "Approve within 24h — Instagram blocks
    replies after that." (Instagram only);
  - an approval (Send as-is or Edit) refused because the 24h window
    closed gets a clear, unmuted Telegram alert;
  - WhatsApp drafts are unchanged.

No live API call anywhere here: sends, Telegram and the model are stubbed.

Run:  python -m unittest tests.test_instagram_draft_handoff
"""
import io, json, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ig-draft-handoff-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000987"
HANDOFF = "I've passed this to the team — they'll reply to you here 🤍"
DRAFT_REPLY = "So sorry about that — here's what we can do..."
WINDOW_CLOSED = "HTTP 400: (#10) This message is sent outside of allowed window."
CHAT_ID = 777003


def db_rows(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_exec(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def telegram_texts(telegram: Mock) -> str:
    return " | ".join(
        str(call.args[1].get("text", ""))
        for call in telegram.call_args_list
        if len(call.args) > 1 and isinstance(call.args[1], dict)
    )


class ResetDb(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts"):
            db_exec(f"DELETE FROM {table}")


class DraftHandoffTest(ResetDb):
    """The DRAFT+APPROVE branch of _process_instagram_event, with the
    model, sends and Telegram stubbed and real log rows in a temp DB."""

    def setUp(self):
        super().setUp()
        self.sends, self.drafts = [], []
        self.send_result = (True, "")
        self.mid = 0
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "send_draft_for_approval", self._draft),
            patch.object(glam, "send_telegram_notification", Mock()),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
            patch.object(glam, "_load_instagram_history", lambda *a, **k: []),
            patch.object(glam, "draft_reply_logic", self._model),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.classification = "DRAFT+APPROVE"

    def _send(self, sender_id, text):
        self.sends.append((sender_id, text))
        return self.send_result

    def _draft(self, **kwargs):
        self.drafts.append(kwargs)
        return True

    def _model(self, text, order_line, history=None, source=""):
        return self.classification, DRAFT_REPLY, json.dumps(
            {"classification": self.classification, "reply": DRAFT_REPLY, "tag": ""}
        )

    def dm(self, text="I want to return my order"):
        self.mid += 1
        with redirect_stdout(io.StringIO()) as buf:
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"},
                "timestamp": 1720000000,
                "message": {"mid": f"m-{time.time_ns()}-{self.mid}", "text": text},
            })
        return buf.getvalue()

    def sources(self):
        return [r[0] for r in db_rows(
            "SELECT source FROM instagram_logs WHERE sender_id = ? ORDER BY id", (SENDER,)
        )]

    def test_draft_sends_the_handoff_line_and_still_asks_the_founder(self):
        self.dm()
        self.assertEqual(self.sends, [(SENDER, HANDOFF)])
        self.assertEqual(glam.BRAIN_HOLDING_LINE, HANDOFF)
        (draft,) = self.drafts
        self.assertEqual(draft["reply_text"], DRAFT_REPLY)
        self.assertEqual(draft["channel"], "Instagram")
        self.assertEqual(self.sources(), ["DRAFT_HANDOFF_IG", "DRAFT_PENDING_IG"])
        # The audit row has no customer text, so it never enters history.
        (row,) = db_rows(
            "SELECT message_text, reply_text FROM instagram_logs WHERE source = 'DRAFT_HANDOFF_IG'"
        )
        self.assertEqual(row, ("", HANDOFF))

    def test_second_draft_inside_30_minutes_does_not_repeat_it(self):
        self.dm()
        out = self.dm("also can I exchange it?")
        self.assertEqual(self.sends, [(SENDER, HANDOFF)])
        self.assertEqual(len(self.drafts), 2)
        self.assertIn("not repeating", out)

    def test_after_30_minutes_it_is_sent_again(self):
        self.dm()
        db_exec(
            "UPDATE instagram_logs SET logged_at = datetime('now', '-31 minutes') "
            "WHERE source = 'DRAFT_HANDOFF_IG'"
        )
        self.dm("any update?")
        self.assertEqual(self.sends, [(SENDER, HANDOFF), (SENDER, HANDOFF)])

    def test_same_line_from_the_pipeline_failure_path_counts(self):
        db_exec(
            "INSERT INTO instagram_logs (sender_id, message_text, reply_text, timestamp, source) "
            "VALUES (?, 'hi', ?, '1', 'PIPELINE_HOLDING_IG')",
            (SENDER, HANDOFF),
        )
        self.dm()
        self.assertEqual(self.sends, [])
        self.assertEqual(len(self.drafts), 1)

    def test_other_senders_are_not_affected(self):
        db_exec(
            "INSERT INTO instagram_logs (sender_id, message_text, reply_text, timestamp, source) "
            "VALUES ('someone-else', '', ?, '1', 'DRAFT_HANDOFF_IG')",
            (HANDOFF,),
        )
        self.dm()
        self.assertEqual(self.sends, [(SENDER, HANDOFF)])

    def test_failed_handoff_is_logged_and_retried_on_the_next_draft(self):
        self.send_result = (False, "HTTP 500: boom")
        self.dm()
        self.assertEqual(self.sources(), ["DRAFT_HANDOFF_FAILED_IG", "DRAFT_PENDING_IG"])
        self.send_result = (True, "")
        self.dm("hello?")
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.sources()[-2:], ["DRAFT_HANDOFF_IG", "DRAFT_PENDING_IG"])

    def test_auto_reply_gets_no_handoff_line(self):
        self.classification = "AUTO"
        self.dm("what's the price of GS1?")
        self.assertEqual(self.sends, [(SENDER, DRAFT_REPLY)])


class DraftMessageDeadlineTest(ResetDb):
    def _draft_text(self, channel):
        telegram = Mock(return_value={"ok": True, "result": {"message_id": 1, "chat": {"id": CHAT_ID}}})
        with patch.object(glam, "TELEGRAM_BOT_TOKEN", "x"), \
             patch.object(glam, "TELEGRAM_CHAT_ID", str(CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(glam.send_draft_for_approval(
                customer_number="c1", customer_name="", customer_message="hi",
                reply_text="draft", channel=channel,
            ))
        return telegram.call_args_list[0].args[1]["text"]

    def test_instagram_draft_carries_the_24h_line(self):
        self.assertIn(
            "Approve within 24h — Instagram blocks replies after that.",
            self._draft_text("Instagram"),
        )

    def test_whatsapp_draft_is_unchanged(self):
        self.assertNotIn("24h", self._draft_text("WhatsApp"))


class ApprovalWindowClosedTest(ResetDb):
    def _draft(self, channel="Instagram", **extra):
        draft = {
            "customer_number": SENDER if channel == "Instagram" else "919800000001",
            "customer_name": "",
            "customer_message": "price?",
            "reply_text": "approved reply",
            "original_text": "orig",
            "channel": channel,
            "ig_timestamp": "1720000000",
            "telegram_chat_id": CHAT_ID,
            "telegram_message_id": 9,
        }
        draft.update(extra)
        return draft

    def tap_send(self, draft, send_result):
        draft_id = f"w{time.time_ns() % 10**7}"
        self.assertTrue(glam._draft_register(draft_id, draft))
        cb = {
            "id": "cb1",
            "data": f"action:send|num:{draft['customer_number']}|id:{draft_id}",
            "message": {"chat": {"id": CHAT_ID}, "message_id": 9, "text": "orig"},
        }
        sender = ("_send_instagram_reply" if draft["channel"] == "Instagram"
                  else "send_whatsapp_reply")
        telegram = Mock()
        with patch.object(glam, "TELEGRAM_CHAT_ID", str(CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             patch.object(glam, "_reassign_to_bot", Mock()), \
             patch.object(glam, "_log_message", Mock()), \
             patch.object(glam, sender, Mock(return_value=send_result)), \
             redirect_stdout(io.StringIO()):
            glam._handle_telegram_callback(cb)
        return telegram_texts(telegram)

    def complete_edit(self, draft, send_result):
        draft_id = f"e{time.time_ns() % 10**7}"
        draft.update(awaiting_edit=True, edit_started_at=time.time())
        self.assertTrue(glam._draft_register(draft_id, draft))
        telegram = Mock()
        with patch.object(glam, "TELEGRAM_CHAT_ID", str(CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             patch.object(glam, "_reassign_to_bot", Mock()), \
             patch.object(glam, "_send_instagram_reply", Mock(return_value=send_result)), \
             redirect_stdout(io.StringIO()):
            glam._handle_telegram_message({"chat": {"id": CHAT_ID}, "text": "edited reply"})
        return telegram_texts(telegram)

    def test_send_as_is_after_the_window_gets_a_clear_alert(self):
        texts = self.tap_send(self._draft(), (False, WINDOW_CLOSED))
        self.assertIn("Instagram reply NOT sent", texts)
        self.assertIn("24h window has closed", texts)
        self.assertIn("NOT sent to", texts)
        self.assertNotIn("✅ Sent to", texts)
        self.assertEqual(
            [r[0] for r in db_rows("SELECT source FROM instagram_logs WHERE sender_id = ?", (SENDER,))],
            ["DRAFT_SEND_FAILED_IG"],
        )

    def test_other_instagram_failures_keep_the_generic_line(self):
        texts = self.tap_send(self._draft(), (False, "HTTP 400: token expired"))
        self.assertIn("⚠️ Send FAILED", texts)
        self.assertNotIn("24h window", texts)

    def test_edit_sent_after_the_window_gets_the_alert_not_a_sent_note(self):
        texts = self.complete_edit(self._draft(), (False, WINDOW_CLOSED))
        self.assertIn("24h window has closed", texts)
        self.assertIn("Edited but NOT sent", texts)
        self.assertNotIn("Edited and sent", texts)

    def test_edit_sent_in_time_is_unchanged(self):
        texts = self.complete_edit(self._draft(), (True, ""))
        self.assertIn("✅ Sent your edit", texts)
        self.assertIn("Edited and sent", texts)

    def tap_missing_draft(self, message_text, age_seconds, action="send"):
        """Tap a button whose draft row is gone (pruned after 24h, or
        already handled). The callback still carries the Telegram
        message's text and send time."""
        cb = {
            "id": "cb2",
            "data": f"action:{action}|num:{SENDER}|id:gone1234",
            "message": {"chat": {"id": CHAT_ID}, "message_id": 9,
                        "date": int(time.time() - age_seconds), "text": message_text},
        }
        telegram = Mock()
        with patch.object(glam, "TELEGRAM_CHAT_ID", str(CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             redirect_stdout(io.StringIO()):
            glam._handle_telegram_callback(cb)
        return telegram_texts(telegram)

    def test_tap_on_an_expired_instagram_draft_alerts_instead_of_already_handled(self):
        ig_draft = "🟡 DRAFT + APPROVE\n...\n\n" + glam.IG_APPROVAL_DEADLINE_LINE
        for action in ("send", "edit"):
            with self.subTest(action=action):
                texts = self.tap_missing_draft(ig_draft, 25 * 3600, action)
                self.assertIn("Expired — Instagram's 24h window closed", texts)
                self.assertIn("24h window has closed", texts)
                self.assertNotIn("Already handled", texts)

    def test_quick_double_tap_on_a_fresh_instagram_draft_is_still_already_handled(self):
        ig_draft = "🟡 DRAFT + APPROVE\n...\n\n" + glam.IG_APPROVAL_DEADLINE_LINE
        texts = self.tap_missing_draft(ig_draft, 5)
        self.assertIn("Already handled", texts)
        self.assertNotIn("24h window", texts)

    def test_old_whatsapp_draft_tap_is_unchanged(self):
        texts = self.tap_missing_draft("🟡 DRAFT + APPROVE\n...", 25 * 3600)
        self.assertIn("Already handled", texts)
        self.assertNotIn("24h window", texts)

    def test_whatsapp_approval_failure_is_unchanged(self):
        texts = self.tap_send(self._draft(channel="WhatsApp"), (False, WINDOW_CLOSED))
        self.assertIn("⚠️ Send FAILED", texts)
        self.assertNotIn("24h window", texts)


if __name__ == "__main__":
    unittest.main()
