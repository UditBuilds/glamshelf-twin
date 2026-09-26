"""Daily Instagram token check.

The Instagram token expired silently on Sep 6 and Twin was dead on
Instagram for ~16 days. Now, once per 24h (on the hourly loop, gated in
scheduled_jobs so a redeploy doesn't re-run it early):
  - with INSTAGRAM_APP_ID + INSTAGRAM_APP_SECRET: Meta's debug_token
    reads the expiry; a Telegram alert when it's <= 7 days away or the
    token is invalid; if debug_token errors, the basic check runs instead;
  - otherwise: one cheap authenticated call; an alert on an auth/expiry
    error (code 190 / OAuthException / HTTP 401);
  - alerts repeat daily, are never muted, and never contain the token;
  - a network failure is not recorded, so the next hour retries.

No live API call anywhere here: requests.get and Telegram are stubbed.

Run:  python -m unittest tests.test_ig_token_check
"""
import io, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-token-check-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import requests

TOKEN = "IGAAtest-secret-token-value"
DAY = 86400
NOW = 1_790_000_000.0


def resp(status=200, body=None):
    r = Mock(ok=200 <= status < 300, status_code=status, text=str(body))
    r.json.return_value = body if body is not None else {}
    return r


class TokenCheckTest(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM scheduled_jobs")
        conn.commit(); conn.close()
        self.alerts = []
        self.get = Mock()
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", TOKEN),
            patch.object(glam, "INSTAGRAM_APP_ID", ""),
            patch.object(glam, "INSTAGRAM_APP_SECRET", ""),
            patch.object(glam, "TELEGRAM_CHAT_ID", "5"),
            patch.object(glam, "_telegram_api", lambda m, p: self.alerts.append(p["text"])),
            patch.object(glam.requests, "get", self.get),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_check(self, now=NOW):
        with redirect_stdout(io.StringIO()) as buf:
            glam._ig_token_check_if_due(now)
        out = buf.getvalue()
        self.assertNotIn(TOKEN, out)                   # never print the token
        for a in self.alerts:
            self.assertNotIn(TOKEN, a)
        return out

    def with_app_id(self):
        for p in (patch.object(glam, "INSTAGRAM_APP_ID", "123"),
                  patch.object(glam, "INSTAGRAM_APP_SECRET", "shh")):
            p.start()
            self.addCleanup(p.stop)

    # ---- basic check (no app ID) ----

    def test_basic_ok_sends_nothing(self):
        self.get.return_value = resp(200, {"id": "1"})
        self.run_check()
        self.assertEqual(self.alerts, [])
        url = self.get.call_args.args[0]
        self.assertTrue(url.endswith("/me"))

    def test_basic_expired_token_alerts(self):
        self.get.return_value = resp(400, {"error": {
            "message": "Error validating access token: Session has expired",
            "type": "OAuthException", "code": 190}})
        self.run_check()
        (alert,) = self.alerts
        self.assertIn("Instagram token REJECTED", alert)
        self.assertIn("Session has expired", alert)
        self.assertIn("INSTAGRAM_PAGE_ACCESS_TOKEN", alert)

    def test_basic_non_auth_error_does_not_alert(self):
        self.get.return_value = resp(500, {"error": {"message": "temporary", "code": 2}})
        self.run_check()
        self.assertEqual(self.alerts, [])

    # ---- once a day ----

    def test_runs_once_per_24h_and_repeats_daily(self):
        self.get.return_value = resp(400, {"error": {"code": 190, "message": "expired"}})
        self.run_check(NOW)
        self.run_check(NOW + 3600)            # next hourly tick: not due
        self.run_check(NOW + 23 * 3600)       # still not due
        self.assertEqual(len(self.alerts), 1)
        self.run_check(NOW + DAY)             # a day later: alerts again
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(self.get.call_count, 2)

    def test_network_failure_is_retried_next_hour(self):
        self.get.side_effect = requests.ConnectionError("down")
        self.run_check(NOW)
        self.get.side_effect = None
        self.get.return_value = resp(400, {"error": {"code": 190, "message": "expired"}})
        self.run_check(NOW + 3600)
        self.assertEqual(len(self.alerts), 1)

    def test_no_token_configured_skips(self):
        with patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", ""):
            self.run_check()
        self.get.assert_not_called()

    # ---- debug_token (app ID + secret) ----

    def test_debug_token_far_from_expiry_sends_nothing(self):
        self.with_app_id()
        self.get.return_value = resp(200, {"data": {"is_valid": True, "expires_at": NOW + 30 * DAY}})
        out = self.run_check()
        self.assertEqual(self.alerts, [])
        self.assertIn("debug_token", self.get.call_args.args[0])
        self.assertEqual(self.get.call_args.kwargs["params"]["access_token"], "123|shh")
        self.assertIn("expires", out)

    def test_debug_token_within_7_days_warns_daily(self):
        self.with_app_id()
        self.get.return_value = resp(200, {"data": {"is_valid": True, "expires_at": NOW + 6 * DAY + 3600}})
        self.run_check(NOW)
        (alert,) = self.alerts
        self.assertIn("Instagram token expires in 6 day(s)", alert)
        self.run_check(NOW + DAY)
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("5 day(s)", self.alerts[1])

    def test_debug_token_exactly_7_days_warns(self):
        self.with_app_id()
        self.get.return_value = resp(200, {"data": {"is_valid": True, "expires_at": NOW + 7 * DAY}})
        self.run_check()
        self.assertEqual(len(self.alerts), 1)

    def test_debug_token_invalid_alerts(self):
        self.with_app_id()
        self.get.return_value = resp(200, {"data": {"is_valid": False, "error": {"message": "Session expired"}}})
        self.run_check()
        (alert,) = self.alerts
        self.assertIn("Instagram token INVALID", alert)

    def test_debug_token_error_falls_back_to_the_basic_check(self):
        self.with_app_id()
        self.get.side_effect = [
            resp(400, {"error": {"message": "Invalid OAuth access token", "code": 190}}),
            resp(400, {"error": {"message": "Session has expired", "code": 190}}),
        ]
        self.run_check()
        self.assertEqual(self.get.call_count, 2)
        self.assertTrue(self.get.call_args_list[1].args[0].endswith("/me"))
        (alert,) = self.alerts
        self.assertIn("REJECTED", alert)


class SchedulerHookTest(unittest.TestCase):
    def test_hourly_loop_calls_the_token_check(self):
        import inspect
        src = inspect.getsource(glam._start_rag_reindex_loop)
        self.assertIn("_ig_token_check_if_due()", src)


if __name__ == "__main__":
    unittest.main()
