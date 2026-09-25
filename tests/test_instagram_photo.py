"""Instagram photos (audit T1-9).

Before: every Instagram message without text — photos included — was
dropped silently: no reply, no log, no alert, while brain.md told the
model images were being processed.

Now:
  - a photo gets "I can't view photos here yet — ..." plus a Telegram
    notice with the sender ID, at most once per sender per 10 minutes
    (extra photos in a burst are logged, not answered);
  - it passes the same dedup / pause / takeover gates as text and never
    reaches the model or the LLM rate limiter;
  - reactions, stickers, shares and other non-text events are still
    ignored, but logged;
  - the exchange is logged as "[sent a photo]" so the next turn's history
    tells the model a photo arrived.

No live API call anywhere here: sends, Telegram and the model are stubbed.

Run:  python -m unittest tests.test_instagram_photo
"""
import io, os, sqlite3, sys, tempfile, unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ig-photo-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000321"
PHOTO = {"type": "image", "payload": {"url": "https://example.invalid/p.jpg"}}


def event(message=None, **extra):
    ev = {"sender": {"id": SENDER}, "recipient": {"id": "page"}, "timestamp": 1}
    if message is not None:
        ev["message"] = message
    ev.update(extra)
    return ev


def db_rows(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class InstagramPhotoTestCase(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "llm_usage"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        self.sends, self.notices, self.llm_calls = [], [], []
        self.send_result = (True, "")
        for p in (
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "TELEGRAM_CHAT_ID", "123"),
            patch.object(glam, "_telegram_api", lambda method, payload: self.notices.append(payload["text"])),
            patch.object(glam, "ask_claude", lambda *a, **k: self.llm_calls.append(a)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, sender_id, text):
        self.sends.append((sender_id, text))
        return self.send_result

    def process(self, ev):
        buf = io.StringIO()
        with redirect_stdout(buf):
            glam._process_instagram_event(ev)
        return buf.getvalue()


class PhotoReply(InstagramPhotoTestCase):
    def test_photo_gets_honest_reply_and_founder_notice(self):
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertEqual(self.sends, [(SENDER, glam.INSTAGRAM_PHOTO_REPLY)])
        (notice,) = self.notices
        self.assertIn(SENDER, notice)
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(db_rows("SELECT COUNT(*) FROM llm_usage"), [(0,)])
        self.assertEqual(
            db_rows("SELECT message_text, reply_text, source FROM instagram_logs"),
            [("[sent a photo]", glam.INSTAGRAM_PHOTO_REPLY, "PHOTO_IG")],
        )

    def test_burst_of_photos_answered_once(self):
        for _ in range(3):
            self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(
            db_rows("SELECT source, reply_text FROM instagram_logs ORDER BY id"),
            [("PHOTO_IG", glam.INSTAGRAM_PHOTO_REPLY),
             ("PHOTO_IG_REPEAT", None), ("PHOTO_IG_REPEAT", None)],
        )

    def test_answered_again_after_the_window(self):
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("UPDATE instagram_logs SET logged_at = datetime('now', '-11 minutes')")
        conn.commit()
        conn.close()
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertEqual(len(self.sends), 2)

    def test_failed_reply_is_retried_on_next_photo_and_kept_out_of_history(self):
        self.send_result = (False, "HTTP 400: token expired")
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertIn("FAILED", self.notices[0])
        self.assertEqual(glam._load_instagram_history(SENDER), [])
        self.send_result = (True, "")
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertEqual(len(self.sends), 2)

    def test_history_tells_the_model_a_photo_arrived(self):
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        (turn,) = glam._load_instagram_history(SENDER)
        self.assertEqual(turn["msg_text"], "[sent a photo]")
        self.assertEqual(turn["reply_text"], glam.INSTAGRAM_PHOTO_REPLY)

    def test_paused_sender_gets_nothing(self):
        glam._pause_number(SENDER)
        self.process(event({"mid": "", "attachments": [PHOTO]}))
        self.assertEqual(self.sends, [])
        self.assertEqual(self.notices, [])

    def test_duplicate_delivery_answered_once(self):
        mid = "photo-dedup-test-mid"
        self.addCleanup(glam._seen_ids.discard, mid)
        with patch.object(glam, "_persist_seen_id", lambda m: None):
            self.process(event({"mid": mid, "attachments": [PHOTO]}))
            self.process(event({"mid": mid, "attachments": [PHOTO]}))
        self.assertEqual(len(self.sends), 1)


class OtherNonTextEvents(InstagramPhotoTestCase):
    def assert_ignored_and_logged(self, ev, kind):
        out = self.process(ev)
        self.assertEqual(self.sends, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.llm_calls, [])
        self.assertIn("Ignored non-text event", out)
        self.assertIn(kind, out)

    def test_sticker(self):
        sticker = {"type": "image", "payload": {"url": "https://x/s.png", "sticker_id": 369239263222822}}
        self.assert_ignored_and_logged(event({"mid": "", "attachments": [sticker]}), "attachments=image")

    def test_shared_post(self):
        share = {"type": "share", "payload": {"url": "https://instagram.com/p/x"}}
        self.assert_ignored_and_logged(event({"mid": "", "attachments": [share]}), "attachments=share")

    def test_reaction(self):
        self.assert_ignored_and_logged(
            event(reaction={"mid": "m", "action": "react", "reaction": "love"}), "reaction"
        )

    def test_read_receipt(self):
        self.assert_ignored_and_logged(event(read={"mid": "m"}), "read")


if __name__ == "__main__":
    unittest.main()
