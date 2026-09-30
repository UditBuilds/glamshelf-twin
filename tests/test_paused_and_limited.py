"""No message vanishes without a trace (audit findings 8, 10 and 19).

Before:
  - a paused sender (after any escalation) got nothing for up to 4 hours,
    with no log row and no founder notice (P3: "hello?? can u just send me
    the payment link");
  - over the rate limit, the customer got one notice and then nothing, and
    the founder one alert per day;
  - voice notes, shared posts and reels got silence, and so did story
    mentions, with no founder notice (P6).

Now:
  - a paused or founder-handled sender's message is logged and forwarded to
    the founder on Telegram (no model call); after an escalation that sent
    a holding line, the customer gets the ack line at most once per pause;
  - (a second escalation within 30 minutes not resending the holding line
    is tested in tests/test_second_escalation.py);
  - every over-limit message is logged and forwarded to the founder;
  - voice notes, shared posts and reels get one line and a founder notice;
    story mentions a notice only.

No live API call anywhere here: sends, Telegram and the model are stubbed.

Run:  python -m unittest tests.test_paused_and_limited
"""
import io, json, os, sqlite3, sys, tempfile, time, unittest, uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-paused-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import graph as twin

SENDER = "17800000000000812"
HANDOFF = glam.BRAIN_HOLDING_LINE
ACK = glam.IG_DRAFT_ACK_LINE
MEDIA_LINE = glam.INSTAGRAM_MEDIA_REPLY


def db_rows(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def log_rows():
    return db_rows("SELECT message_text, reply_text, source FROM instagram_logs "
                   "WHERE sender_id = ? ORDER BY id", (SENDER,))


class Base(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts", "rate_limit_events"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED", "LLM_RATE_LIMIT_DISABLED"):
            os.environ.pop(var, None)
        self.sends, self.telegram, self.notices, self.drafts, self.llm_calls = [], [], [], [], []
        self.send_result = (True, "")
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", self._tg),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "send_draft_for_approval", self._draft),
            patch.object(glam, "_persist_seen_id", lambda mid: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, sender_id, text):
        self.sends.append(text)
        return self.send_result

    def _tg(self, method, payload):
        self.telegram.append(payload)
        return {"ok": True}

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.notices.append({"classification": classification, **kwargs})

    def _draft(self, **kwargs):
        self.drafts.append(kwargs)
        return True

    def dm(self, text="", classification="AUTO", reply="ok 🤍", tag="", attachments=None):
        message = {"mid": f"paused.{uuid.uuid4().hex}"}
        if text:
            message["text"] = text
        if attachments:
            message["attachments"] = attachments
        raw = json.dumps({"classification": classification, "reply": reply, "tag": tag})

        def model(*a, **k):
            self.llm_calls.append(a)
            return classification, reply, raw

        with patch.object(glam, "draft_reply_logic", model), redirect_stdout(io.StringIO()):
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"},
                "timestamp": 1, "message": message,
            })

    def escalate(self, text="THIS IS RIDICULOUS, NO ONE REPLIES"):
        self.dm(text, "ESCALATE", "Really sorry for the back and forth 🤍")

    def forwards(self):
        return [p for p in self.telegram if p["text"].startswith(("⏸️", "🚦"))]


class PausedSenders(Base):
    def test_after_an_escalation_the_next_message_gets_the_ack_once_and_is_forwarded(self):
        self.escalate()
        self.assertEqual(self.sends, [HANDOFF])
        self.assertTrue(glam._is_paused(SENDER))

        self.dm("hello?? can u just send me the payment link")      # audit P3
        self.dm("hello?")
        self.assertEqual(self.sends, [HANDOFF, ACK])                 # one ack, once
        self.assertEqual(self.llm_calls, [self.llm_calls[0]])        # no model call while paused
        first, second = self.forwards()
        self.assertIn("this customer is paused", first["text"])
        self.assertIn('"hello?? can u just send me the payment link"', first["text"])
        self.assertIn(f'They got the one-time line: "{ACK}"', first["text"])
        self.assertIn("they already got the one-time line during this pause", second["text"])
        self.assertEqual(first["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"action:resume|id:{SENDER}")
        self.assertEqual(log_rows()[1:], [
            ("hello?? can u just send me the payment link", ACK, "PAUSED_ACK_IG"),
            ("hello?", None, "PAUSED_IG"),
        ])

    def test_paused_messages_use_no_llm_budget(self):
        self.escalate()
        for i in range(3):
            self.dm(f"msg {i}")
        self.assertEqual(db_rows("SELECT COUNT(*) FROM llm_usage"), [(1,)])

    def test_the_ack_reaches_history_the_unanswered_messages_do_not(self):
        self.escalate()
        self.dm("any update?")
        self.dm("??")
        glam._unpause_number(SENDER)
        history = [(h["msg_text"], h["reply_text"]) for h in glam._load_instagram_history(SENDER)]
        self.assertEqual(history, [("THIS IS RIDICULOUS, NO ONE REPLIES", HANDOFF), ("any update?", ACK)])

    def test_legal_escalation_stays_silent_while_paused(self):
        self.dm("I will send a legal notice", "ESCALATE", "x", tag="LEGAL")
        self.assertEqual(self.sends, [])
        self.dm("hello?")
        self.assertEqual(self.sends, [])
        (forward,) = self.forwards()
        self.assertIn("legal / press", forward["text"])

    def test_founder_handling_the_chat_gets_no_ack(self):
        self.escalate()
        glam._log_instagram(SENDER, "", "I'm on it — Udit", "1", source="HUMAN_UDIT_INSTAGRAM")
        self.dm("thanks udit, when will it ship?")
        self.assertEqual(self.sends, [HANDOFF])
        (forward,) = self.forwards()
        self.assertIn("you're handling this chat", forward["text"])

    def test_human_handling_without_a_pause_row_is_logged_and_forwarded(self):
        glam._log_instagram(SENDER, "", "Hi, Udit here", "1", source="HUMAN_UDIT_INSTAGRAM")
        self.dm("ok great")
        self.assertEqual(self.sends, [])
        self.assertEqual(self.llm_calls, [])
        (forward,) = self.forwards()
        self.assertIn("you replied to this customer in the last 4 hours", forward["text"])
        self.assertEqual(log_rows()[-1], ("ok great", None, "HUMAN_HANDLING_IG"))

    def test_a_pause_without_a_holding_line_gets_no_ack(self):
        glam._pause_number(SENDER)        # e.g. the founder's 🛑 Stop bot, nothing sent
        self.dm("hello?")
        self.assertEqual(self.sends, [])
        self.assertIn("didn't get a holding line", self.forwards()[0]["text"])

    def test_a_failed_ack_is_retried_on_the_next_message(self):
        self.escalate()
        self.send_result = (False, "HTTP 500")
        self.dm("hello?")
        self.send_result = (True, "")
        self.dm("hello??")
        self.assertEqual(self.sends, [HANDOFF, ACK, ACK])       # the first ack failed
        self.assertIn("failed to send", self.forwards()[0]["text"])

    def test_extending_a_running_pause_keeps_the_one_ack(self):
        self.escalate()
        self.dm("hello?")
        glam._pause_number(SENDER)        # e.g. 🛑 Stop bot tapped later
        self.dm("hello??")
        self.assertEqual(self.sends.count(ACK), 1)

    def test_a_new_pause_after_the_old_one_expired_gets_a_new_ack(self):
        self.escalate()
        self.dm("hello?")
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("UPDATE paused_senders SET paused_until = ?", (time.time() - 1,))
        conn.commit()
        conn.close()
        glam._pause_number(SENDER)        # the old row is still there, expired
        self.dm("hello again?")
        self.assertEqual(self.sends.count(ACK), 2)

    def test_the_ack_stamp_survives_a_restart(self):
        self.escalate()
        self.dm("hello?")
        self.assertIsNotNone(db_rows("SELECT ack_sent_at FROM paused_senders WHERE sender_id = ?", (SENDER,))[0][0])

    def test_graph_intake_does_the_same(self):
        self.escalate()
        with redirect_stdout(io.StringIO()):
            twin.handle_instagram_message(SENDER, "hello from graph?", msg_id="", timestamp="1")
        self.assertEqual(self.sends, [HANDOFF, ACK])
        self.assertEqual(log_rows()[-1], ("hello from graph?", ACK, "PAUSED_ACK_IG"))


class AfterARepeatEscalation(Base):
    # The repeat itself is tested in tests/test_second_escalation.py.
    def test_a_paused_customer_after_a_repeat_escalation_still_gets_the_ack(self):
        self.dm("my tray arrived damaged", "DRAFT+APPROVE", "So sorry 🤍")
        self.escalate()
        self.dm("hello?")
        self.assertEqual(self.sends, [HANDOFF, ACK])


class RateLimitForwarding(Base):
    def fill(self, n):
        now = time.time()
        conn = sqlite3.connect(glam.DB_PATH)
        for i in range(n):
            conn.execute("INSERT INTO llm_usage (ts, channel, sender_id) VALUES (?, 'Instagram', ?)",
                         (now - 300 + i, SENDER))
        conn.commit()
        conn.close()

    def test_every_over_limit_message_is_forwarded(self):
        self.fill(glam.RATE_LIMIT_WINDOW_MAX)
        self.dm("price of GS1?")
        self.dm("hello??")
        self.assertEqual(self.sends, [glam.RATE_LIMIT_NOTICE])     # once per window, as before
        self.assertEqual(self.llm_calls, [])
        f1, f2 = self.forwards()
        self.assertIn("over the rate limit (8 messages in 10 minutes)", f1["text"])
        self.assertIn('"price of GS1?"', f1["text"])
        self.assertIn(f'They got "{glam.RATE_LIMIT_NOTICE}"', f1["text"])
        self.assertIn('"hello??"', f2["text"])
        self.assertIn("Nothing new sent to them", f2["text"])
        self.assertNotIn("reply_markup", f1)
        self.assertEqual([r[1:] for r in log_rows()],
                         [(glam.RATE_LIMIT_NOTICE, "RATE_LIMITED_IG"), (None, "RATE_LIMITED_IG")])

    def test_no_separate_daily_alert_for_a_sender(self):
        self.fill(glam.RATE_LIMIT_WINDOW_MAX)
        self.dm("one")
        self.assertEqual(len(self.telegram), 1)          # the forward, not forward + alert

    def test_the_global_cap_alert_stays_and_messages_are_forwarded(self):
        with patch.dict(os.environ, {"LLM_DAILY_CAP": "1"}):
            conn = sqlite3.connect(glam.DB_PATH)
            conn.execute("INSERT INTO llm_usage (ts, channel, sender_id) VALUES (?, 'Instagram', 'someone')",
                         (time.time(),))
            conn.commit()
            conn.close()
            self.dm("hi")
        texts = [p["text"] for p in self.telegram]
        self.assertTrue(any("Daily LLM cap reached" in t for t in texts))
        self.assertTrue(any(t.startswith("🚦 Twin didn't answer") and '"hi"' in t for t in texts))

    def test_the_limits_themselves_are_unchanged(self):
        self.assertEqual((glam.RATE_LIMIT_WINDOW_MAX, glam.RATE_LIMIT_DAILY_MAX, glam.LLM_DAILY_CAP_DEFAULT),
                         (8, 40, 500))


class VoiceSharesReelsAndStories(Base):
    VOICE = {"type": "audio", "payload": {"url": "https://example.invalid/v.mp4"}}
    SHARE = {"type": "share", "payload": {"url": "https://instagram.com/p/x"}}
    REEL = {"type": "ig_reel", "payload": {"url": "https://instagram.com/reel/x"}}
    STORY = {"type": "story_mention", "payload": {"url": "https://example.invalid/s"}}
    VIDEO = {"type": "video", "payload": {"url": "https://example.invalid/v.mov"}}

    def notices_text(self):
        return [p["text"] for p in self.telegram]

    def test_voice_note_gets_the_line_and_a_notice(self):
        self.dm(attachments=[self.VOICE])
        self.assertEqual(self.sends, [MEDIA_LINE])
        self.assertEqual(MEDIA_LINE, "I can't open voice notes or shared posts here yet — could you type your question? 🤍")
        (notice,) = self.notices_text()
        self.assertTrue(notice.startswith(f"🎤 Instagram voice note from sender {SENDER}"))
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(db_rows("SELECT COUNT(*) FROM llm_usage"), [(0,)])
        self.assertEqual(log_rows(), [("[sent a voice note]", MEDIA_LINE, "MEDIA_IG")])

    def test_shared_post_and_reel(self):
        self.dm(attachments=[self.SHARE])
        other = "17800000000000813"
        with patch.object(glam, "_send_instagram_reply", self._send), redirect_stdout(io.StringIO()):
            glam._process_instagram_event({"sender": {"id": other}, "recipient": {"id": "page"}, "timestamp": 1,
                                           "message": {"mid": f"p.{uuid.uuid4().hex}", "attachments": [self.REEL]}})
        self.assertEqual(self.sends, [MEDIA_LINE, MEDIA_LINE])
        self.assertIn("shared post", self.notices_text()[0])
        self.assertIn("shared reel", self.notices_text()[1])

    def test_a_burst_gets_one_line_but_every_one_is_notified(self):
        for att in (self.VOICE, self.VOICE, self.SHARE):
            self.dm(attachments=[att])
        self.assertEqual(self.sends, [MEDIA_LINE])
        self.assertEqual(len(self.notices_text()), 3)
        self.assertIn("nothing new was sent", self.notices_text()[1])

    def test_story_mention_gets_a_notice_only(self):
        self.dm(attachments=[self.STORY])
        self.assertEqual(self.sends, [])
        (notice,) = self.notices_text()
        self.assertIn("story mention", notice)
        self.assertIn("No reply sent", notice)
        self.assertEqual(log_rows(), [("[mentioned @glamshelfstore in their story]", None, "STORY_MENTION_IG")])

    def test_video_gets_a_notice_only(self):
        self.dm(attachments=[self.VIDEO])
        self.assertEqual(self.sends, [])
        self.assertIn("can't open a video", self.notices_text()[0])

    def test_history_shows_what_arrived(self):
        self.dm(attachments=[self.VOICE])
        (turn,) = glam._load_instagram_history(SENDER)
        self.assertEqual((turn["msg_text"], turn["reply_text"]), ("[sent a voice note]", MEDIA_LINE))

    def test_a_failed_line_stays_out_of_history_and_is_retried(self):
        self.send_result = (False, "HTTP 400")
        self.dm(attachments=[self.VOICE])
        self.assertEqual(glam._load_instagram_history(SENDER), [])
        self.assertIn("FAILED", self.notices_text()[0])
        self.send_result = (True, "")
        self.dm(attachments=[self.VOICE])
        self.assertEqual(self.sends, [MEDIA_LINE, MEDIA_LINE])

    def test_a_paused_sender_s_voice_note_is_forwarded_not_answered(self):
        self.escalate()
        self.dm(attachments=[self.VOICE])
        self.assertEqual(self.sends, [HANDOFF, ACK])
        self.assertIn('"[sent a voice note]"', self.forwards()[0]["text"])

    def test_stickers_and_reactions_are_still_ignored(self):
        sticker = {"type": "image", "payload": {"url": "https://x/s.png", "sticker_id": 1}}
        self.dm(attachments=[sticker])
        self.assertEqual((self.sends, self.telegram, log_rows()), ([], [], []))

    def test_brain_knows_what_the_system_does(self):
        brain = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
        self.assertIn("**On Instagram you never see voice notes, shared posts, reels, videos or story mentions.**", brain)
        self.assertIn(MEDIA_LINE, brain)


if __name__ == "__main__":
    unittest.main()
