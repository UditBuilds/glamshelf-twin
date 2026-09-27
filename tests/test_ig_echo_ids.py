"""Instagram echo detection by message id (audit T2-11, PR E item 3).

Meta echoes every message the page sends (sender.id == INSTAGRAM_PAGE_ID,
is_echo). Twin used to recognise its own echoes only by exact text in a
5-minute in-memory cache, so after a restart its own reply was logged as
"Udit replied" and the customer was paused for 4 hours. Every Instagram
send now stores the message_id Meta returns in SQLite (ig_sent_mids), and
an echo with a stored mid is Twin's own. The text match is the fallback.

No live API call: requests.post is stubbed.

Run:  python -m unittest tests.test_ig_echo_ids
"""
import io, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-ig-echo-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

PAGE_ID = "17840000000000999"
CUSTOMER = "17800000000000311"


def _ok_response(body):
    resp = Mock(ok=True, status_code=200)
    resp.json.return_value = body
    return resp


class EchoByMessageIdTest(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("ig_sent_mids", "instagram_logs", "paused_senders"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        glam._bot_recent_replies.clear()
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ID", PAGE_ID),
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", "fake-token"),
            patch.object(glam, "_alert_send_failure", Mock()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def send(self, text, body):
        with patch.object(glam.requests, "post", Mock(return_value=_ok_response(body))), \
             redirect_stdout(io.StringIO()):
            ok, err = glam._send_instagram_message(CUSTOMER, text)
        self.assertTrue(ok, err)

    def echo(self, mid, text):
        with redirect_stdout(io.StringIO()) as buf:
            glam._process_instagram_event({
                "sender": {"id": PAGE_ID}, "recipient": {"id": CUSTOMER},
                "timestamp": str(int(time.time() * 1000)),
                "message": {"mid": mid, "text": text, "is_echo": True},
            })
        return buf.getvalue()

    def human_udit_rows(self):
        conn = sqlite3.connect(glam.DB_PATH)
        try:
            return conn.execute(
                "SELECT reply_text FROM instagram_logs WHERE sender_id = ? "
                "AND source = 'HUMAN_UDIT_INSTAGRAM'", (CUSTOMER,)
            ).fetchall()
        finally:
            conn.close()

    def test_send_stores_the_message_id(self):
        self.send("GS1 is ₹849 🤍", {"recipient_id": CUSTOMER, "message_id": "mid.sent-1"})
        self.assertTrue(glam._is_ig_sent_mid("mid.sent-1"))
        self.assertFalse(glam._is_ig_sent_mid("mid.other"))

    def test_own_echo_after_a_restart_does_not_pause(self):
        self.send("GS1 is ₹849 🤍", {"recipient_id": CUSTOMER, "message_id": "mid.sent-2"})
        glam._bot_recent_replies.clear()   # simulated restart: the in-memory text cache is gone
        out = self.echo("mid.sent-2", "GS1 is ₹849 🤍")
        self.assertIn("matched by message id", out)
        self.assertFalse(glam._is_paused(CUSTOMER))
        self.assertEqual(self.human_udit_rows(), [])

    def test_genuine_manual_reply_still_pauses(self):
        self.send("GS1 is ₹849 🤍", {"recipient_id": CUSTOMER, "message_id": "mid.sent-3"})
        out = self.echo("mid.udit-typed", "Hi! Udit here, I'll sort this out for you")
        self.assertIn("[HUMAN_UDIT_IG]", out)
        self.assertTrue(glam._is_paused(CUSTOMER))
        self.assertEqual(len(self.human_udit_rows()), 1)

    def test_text_match_is_still_the_fallback(self):
        # No message_id in the response: the echo is recognised by text.
        self.send("Kawaii is ₹299 🤍", {"recipient_id": CUSTOMER})
        out = self.echo("mid.unknown", "Kawaii is ₹299 🤍")
        self.assertIn("matched by text", out)
        self.assertFalse(glam._is_paused(CUSTOMER))

    def test_non_string_message_id_is_ignored(self):
        resp = Mock(ok=True, status_code=200)   # resp.json() returns a Mock
        with patch.object(glam.requests, "post", Mock(return_value=resp)), \
             redirect_stdout(io.StringIO()):
            ok, _ = glam._send_instagram_message(CUSTOMER, "hello")
        self.assertTrue(ok)

    def test_old_ids_are_pruned(self):
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("INSERT INTO ig_sent_mids (mid, sent_at) VALUES (?, ?)",
                     ("mid.ancient", time.time() - glam.IG_SENT_MID_RETENTION_SECONDS - 60))
        conn.commit(); conn.close()
        self.send("hello", {"message_id": "mid.new"})
        self.assertFalse(glam._is_ig_sent_mid("mid.ancient"))
        self.assertTrue(glam._is_ig_sent_mid("mid.new"))


if __name__ == "__main__":
    unittest.main()
