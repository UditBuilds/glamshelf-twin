"""Tokens never reach the logs or a Telegram alert (audit T2-8, PR E item 5).

A requests connection error names the URL it was fetching. For Instagram
that URL carries ?access_token=<token>; for Telegram the bot token sits in
the /bot<token>/ path segment. _redact_secrets masks both, and it runs on
the network-error log lines of the Instagram and Telegram senders and on
every Telegram alert text.

Fake tokens only; requests.post is stubbed — no live API call.

Run:  python -m unittest tests.test_log_redaction
"""
import io, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-log-redaction-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

FAKE_IG_TOKEN = "IGAAFakeToken0123456789abcdefXYZ"
FAKE_BOT_TOKEN = "123456789:AAFakeBotToken_abc-XYZ0123456789"

# The shape requests really produces (host and path printed separately).
IG_CONN_ERROR = (
    "HTTPSConnectionPool(host='graph.instagram.com', port=443): Max retries exceeded "
    f"with url: /v22.0/me/messages?access_token={FAKE_IG_TOKEN} "
    "(Caused by NameResolutionError(\"Failed to resolve 'graph.instagram.com'\"))"
)
TG_CONN_ERROR = (
    "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
    f"with url: /bot{FAKE_BOT_TOKEN}/sendMessage (Caused by ConnectTimeoutError())"
)


class RedactSecretsTest(unittest.TestCase):
    def test_access_token_value_is_masked(self):
        out = glam._redact_secrets(IG_CONN_ERROR)
        self.assertNotIn(FAKE_IG_TOKEN, out)
        self.assertIn("access_token=<redacted>", out)
        self.assertIn("graph.instagram.com", out)   # the rest stays readable

    def test_telegram_bot_token_is_masked(self):
        for text in (TG_CONN_ERROR, f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getMe"):
            with self.subTest(text=text):
                out = glam._redact_secrets(text)
                self.assertNotIn(FAKE_BOT_TOKEN, out)
                self.assertNotIn("AAFakeBotToken", out)
                self.assertIn("/bot<redacted>/", out)

    def test_plain_text_is_unchanged(self):
        text = "Network error: ConnectionError — the robot / bot said hi"
        self.assertEqual(glam._redact_secrets(text), text)


class SenderLogsTest(unittest.TestCase):
    def setUp(self):
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", FAKE_IG_TOKEN),
            patch.object(glam, "TELEGRAM_BOT_TOKEN", FAKE_BOT_TOKEN),
            patch.object(glam, "TELEGRAM_CHAT_ID", "42"),
        ):
            p.start()
            self.addCleanup(p.stop)
        glam._send_failure_last_alert.clear()

    def assertClean(self, text):
        self.assertNotIn(FAKE_IG_TOKEN, text)
        self.assertNotIn(FAKE_BOT_TOKEN, text)
        self.assertNotIn("AAFakeBotToken", text)

    def test_instagram_connection_error_log_and_alert(self):
        alerts = []

        def post(url, **kwargs):
            if "api.telegram.org" in url:
                alerts.append(kwargs["json"]["text"])
                return Mock(ok=True, json=Mock(return_value={"ok": True}))
            raise glam.requests.ConnectionError(IG_CONN_ERROR)

        with patch.object(glam.requests, "post", post), redirect_stdout(io.StringIO()) as buf:
            ok, err = glam._send_instagram_message("1789", "hello")
        self.assertFalse(ok)
        self.assertIn("access_token=<redacted>", buf.getvalue())
        self.assertClean(buf.getvalue())
        self.assertClean(err)
        self.assertEqual(len(alerts), 1)
        self.assertClean(alerts[0])

    def test_telegram_api_connection_error_log(self):
        with patch.object(glam.requests, "post",
                          Mock(side_effect=glam.requests.ConnectionError(TG_CONN_ERROR))), \
             redirect_stdout(io.StringIO()) as buf:
            self.assertIsNone(glam._telegram_api("sendMessage", {"chat_id": "42", "text": "x"}))
        self.assertIn("/bot<redacted>/", buf.getvalue())
        self.assertClean(buf.getvalue())

    def test_telegram_notification_connection_error_log(self):
        with patch.object(glam.requests, "post",
                          Mock(side_effect=glam.requests.ConnectionError(TG_CONN_ERROR))), \
             redirect_stdout(io.StringIO()) as buf:
            glam.send_telegram_notification("ESCALATE", "help", "draft")
        self.assertIn("/bot<redacted>/", buf.getvalue())
        self.assertClean(buf.getvalue())

    def test_alert_text_is_redacted(self):
        sent = []

        def post(url, json=None, timeout=None):
            sent.append(json["text"])
            return Mock(ok=True, json=Mock(return_value={"ok": True}))

        with patch.object(glam.requests, "post", post), redirect_stdout(io.StringIO()):
            glam._alert_send_failure("Instagram", f"boom: {IG_CONN_ERROR} / {TG_CONN_ERROR}",
                                     "1789", kind="pipeline", holding_sent=True)
        self.assertEqual(len(sent), 1)
        self.assertClean(sent[0])
        self.assertIn("<redacted>", sent[0])


if __name__ == "__main__":
    unittest.main()
