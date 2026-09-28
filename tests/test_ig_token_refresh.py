"""Instagram token auto-refresh (multi-brand task 5).

The daily token check now also refreshes the long-lived Instagram token
when it has 15 days or fewer left, via Meta's Instagram Login endpoint
GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token.
  - the new token is used at once and saved next to the DB on the
    persistent disk (never in the DB — it's backed up to GitHub hourly),
    owner-only, with a SHA-256 of the env token it came from;
  - at boot the saved token is used only if it came from the CURRENT env
    token — a freshly pasted INSTAGRAM_PAGE_ACCESS_TOKEN always wins;
  - no persistent storage or no known expiry date -> no refresh (logged);
  - Telegram alert on success and on failure; the token is never printed
    or alerted;
  - kill switch TOKEN_AUTO_REFRESH_DISABLED=1.

Meta and Telegram are stubbed (requests.get / _telegram_api) — no network.

Run:  python -m unittest tests.test_ig_token_refresh
"""
import io, json, os, sqlite3, stat, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-token-refresh-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import requests

ENV_TOKEN = "IGAAenv-token-secret-value-0001"
NEW_TOKEN = "IGAArefreshed-token-secret-0002"
NEWER_TOKEN = "IGAArefreshed-again-secret-0003"
ALL_TOKENS = (ENV_TOKEN, NEW_TOKEN, NEWER_TOKEN)
DAY = 86400
NOW = 1_790_000_000.0
SIXTY_DAYS = 5184000


def resp(status=200, body=None):
    r = Mock(ok=200 <= status < 300, status_code=status, text=json.dumps(body))
    r.json.return_value = body if body is not None else {}
    return r


class TokenRefreshTest(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM scheduled_jobs")
        conn.commit(); conn.close()
        self.store_dir = Path(tempfile.mkdtemp(prefix="token-store-"))
        self.store = self.store_dir / "instagram_token.json"
        self.alerts = []
        self.debug_expires = NOW + 10 * DAY
        self.refresh_response = resp(200, {"access_token": NEW_TOKEN, "token_type": "bearer",
                                           "expires_in": SIXTY_DAYS})
        self.refresh_calls = []
        for p in (
            patch.object(glam, "_IG_ENV_TOKEN", ENV_TOKEN),
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", ENV_TOKEN),
            patch.object(glam, "_ig_token_expires_at", 0.0),
            patch.object(glam, "IG_TOKEN_STORE_PATH", str(self.store)),
            patch.object(glam, "_ig_token_store_problem", lambda: ""),
            patch.object(glam, "INSTAGRAM_APP_ID", "123"),
            patch.object(glam, "INSTAGRAM_APP_SECRET", "shh"),
            patch.object(glam, "TELEGRAM_CHAT_ID", "5"),
            patch.object(glam, "_telegram_api", lambda m, p: self.alerts.append(p["text"])),
            patch.object(glam.requests, "get", self.fake_get),
            patch.dict(os.environ, {"TOKEN_AUTO_REFRESH_DISABLED": ""}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fake_get(self, url, params=None, timeout=None):
        if url == glam.IG_TOKEN_REFRESH_URL:
            self.refresh_calls.append(dict(params))
            if isinstance(self.refresh_response, Exception):
                raise self.refresh_response
            return self.refresh_response
        if "debug_token" in url:
            return resp(200, {"data": {"is_valid": True, "expires_at": self.debug_expires}})
        return resp(200, {"id": "1"})   # basic /me check

    def run_check(self, now=NOW):
        with redirect_stdout(io.StringIO()) as buf:
            glam._ig_token_check_if_due(now)
        out = buf.getvalue()
        for text in [out, *self.alerts]:
            for token in ALL_TOKENS:
                self.assertNotIn(token, text)       # never print or alert a token
        return out

    def saved(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    # ---- success ----

    def test_refreshes_within_15_days(self):
        out = self.run_check()
        (call,) = self.refresh_calls
        self.assertEqual(call, {"grant_type": "ig_refresh_token", "access_token": ENV_TOKEN})
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, NEW_TOKEN)
        self.assertEqual(glam._ig_token_expires_at, NOW + SIXTY_DAYS)
        data = self.saved()
        self.assertEqual(data["access_token"], NEW_TOKEN)
        self.assertEqual(data["expires_at"], NOW + SIXTY_DAYS)
        self.assertEqual(data["env_token_sha256"], glam._token_fingerprint(ENV_TOKEN))
        (alert,) = self.alerts
        self.assertIn("auto-refreshed", alert)
        self.assertIn("Nothing to do", alert)
        self.assertIn("Refreshed", out)

    def test_exactly_15_days_refreshes(self):
        self.debug_expires = NOW + 15 * DAY
        self.run_check()
        self.assertEqual(len(self.refresh_calls), 1)

    def test_not_due_far_from_expiry(self):
        self.debug_expires = NOW + 16 * DAY
        self.run_check()
        self.assertEqual(self.refresh_calls, [])
        self.assertEqual(self.alerts, [])
        self.assertFalse(self.store.exists())

    def test_refresh_runs_once_a_day_with_the_check(self):
        self.run_check(NOW)
        self.run_check(NOW + 3600)                  # hourly tick, check not due
        self.assertEqual(len(self.refresh_calls), 1)

    def test_second_refresh_keeps_the_env_fingerprint(self):
        self.run_check(NOW)
        self.debug_expires = NOW + 50 * DAY + 10 * DAY
        self.refresh_response = resp(200, {"access_token": NEWER_TOKEN, "expires_in": SIXTY_DAYS})
        self.run_check(NOW + 50 * DAY)
        self.assertEqual(self.refresh_calls[1]["access_token"], NEW_TOKEN)   # refreshes the token in use
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, NEWER_TOKEN)
        self.assertEqual(self.saved()["env_token_sha256"], glam._token_fingerprint(ENV_TOKEN))

    # ---- surviving a restart ----

    def test_saved_token_is_used_after_a_restart(self):
        self.run_check()
        with redirect_stdout(io.StringIO()) as buf:
            token, expires = glam._load_stored_ig_token(ENV_TOKEN)
        self.assertEqual((token, expires), (NEW_TOKEN, NOW + SIXTY_DAYS))
        self.assertIn("Using the auto-refreshed Instagram token", buf.getvalue())

    def test_a_new_env_token_wins(self):
        self.run_check()
        with redirect_stdout(io.StringIO()) as buf:
            token, expires = glam._load_stored_ig_token("IGAAfounder-pasted-a-new-one")
        self.assertEqual((token, expires), ("IGAAfounder-pasted-a-new-one", 0.0))
        self.assertIn("doesn't come from the current", buf.getvalue())

    def test_no_env_token_ignores_the_saved_one(self):
        self.run_check()
        self.assertEqual(glam._load_stored_ig_token(""), ("", 0.0))

    def test_unreadable_saved_file_falls_back(self):
        self.store.write_text("{not json", encoding="utf-8")
        with redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(glam._load_stored_ig_token(ENV_TOKEN), (ENV_TOKEN, 0.0))
        self.assertIn("unreadable", buf.getvalue())

    def test_saved_expiry_lets_the_basic_check_refresh(self):
        # No app ID -> no debug_token; the expiry saved by the last refresh is used.
        with patch.object(glam, "INSTAGRAM_APP_ID", ""), \
             patch.object(glam, "_ig_token_expires_at", NOW + 5 * DAY):
            self.run_check()
            self.assertEqual(len(self.refresh_calls), 1)

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX-only")
    def test_saved_file_is_owner_only(self):
        self.run_check()
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    # ---- failures ----

    def test_meta_rejects(self):
        self.refresh_response = resp(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
        self.run_check()
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, ENV_TOKEN)
        self.assertFalse(self.store.exists())
        (alert,) = self.alerts
        self.assertIn("auto-refresh FAILED", alert)
        self.assertIn("Invalid OAuth access token", alert)
        self.assertIn("INSTAGRAM_PAGE_ACCESS_TOKEN", alert)

    def test_network_error_never_leaks_the_token(self):
        self.refresh_response = requests.ConnectionError(
            f"Max retries exceeded with url: /refresh_access_token?grant_type=ig_refresh_token"
            f"&access_token={ENV_TOKEN}"
        )
        self.run_check()                            # run_check asserts no token anywhere
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, ENV_TOKEN)
        (alert,) = self.alerts
        self.assertIn("network error (ConnectionError)", alert)

    def test_answer_without_a_token_is_a_failure(self):
        self.refresh_response = resp(200, {"token_type": "bearer"})
        self.run_check()
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, ENV_TOKEN)
        self.assertIn("no new token", self.alerts[0])

    def test_save_failure_still_uses_the_new_token_and_says_so(self):
        with patch.object(glam.os, "replace", Mock(side_effect=OSError("disk full"))):
            self.run_check()
        self.assertEqual(glam.INSTAGRAM_PAGE_ACCESS_TOKEN, NEW_TOKEN)
        self.assertFalse(self.store.exists())
        self.assertFalse(Path(str(self.store) + ".tmp").exists())
        (alert,) = self.alerts
        self.assertIn("could NOT be saved", alert)

    # ---- switched off / not possible ----

    def test_kill_switch(self):
        self.debug_expires = NOW + 3 * DAY
        with patch.dict(os.environ, {"TOKEN_AUTO_REFRESH_DISABLED": "1"}):
            out = self.run_check()
        self.assertEqual(self.refresh_calls, [])
        self.assertIn("Off (TOKEN_AUTO_REFRESH_DISABLED)", out)
        # The existing expiry reminder still goes out.
        (alert,) = self.alerts
        self.assertIn("expires in 3 day(s)", alert)

    def test_no_persistent_storage_no_refresh(self):
        with patch.object(glam, "_ig_token_store_problem", lambda: "/var/data doesn't exist (no persistent disk?)"):
            out = self.run_check()
        self.assertEqual(self.refresh_calls, [])
        self.assertIn("Not refreshing: /var/data doesn't exist", out)

    def test_expiry_unknown_no_refresh(self):
        with patch.object(glam, "INSTAGRAM_APP_ID", ""):
            out = self.run_check()
        self.assertEqual(self.refresh_calls, [])
        self.assertIn("expiry date unknown", out)

    def test_rejected_token_is_not_refreshed(self):
        with patch.object(self, "debug_expires", NOW + 3 * DAY), \
             patch.object(glam.requests, "get", lambda url, params=None, timeout=None: (
                 self.fake_get(url, params, timeout) if url == glam.IG_TOKEN_REFRESH_URL
                 else resp(200, {"data": {"is_valid": False}}))):
            self.run_check()
        self.assertEqual(self.refresh_calls, [])

    # ---- storage check ----

    def test_store_problem_detection(self):
        with patch.object(glam, "_ig_token_store_problem", TokenRefreshTest.real_store_problem):
            with patch.object(glam, "IG_TOKEN_STORE_PATH", str(self.store_dir / "missing" / "t.json")):
                self.assertIn("doesn't exist", glam._ig_token_store_problem())
            with patch.object(glam, "IG_TOKEN_STORE_PATH", os.path.join(tempfile.gettempdir(), "t.json")):
                self.assertIn("temp folder", glam._ig_token_store_problem())
            # The same folder counts as persistent when it isn't under the temp dir.
            other_temp = tempfile.mkdtemp(prefix="pretend-temp-")
            with patch.object(glam.tempfile, "gettempdir", lambda: other_temp):
                self.assertEqual(glam._ig_token_store_problem(), "")

    # ---- redaction / healthz ----

    def test_redaction_covers_both_tokens_after_a_refresh(self):
        self.run_check()
        text = glam._redact_secrets(f"old {ENV_TOKEN} new {NEW_TOKEN}")
        self.assertNotIn(ENV_TOKEN, text)
        self.assertNotIn(NEW_TOKEN, text)

    def test_keyed_healthz_reports_the_refresh(self):
        self.run_check()
        body = glam.app.test_client().get(
            "/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY}).get_json()
        self.assertEqual(body["instagram_token_auto_refresh"], "on")
        self.assertTrue(body["instagram_token_expires_at"].startswith("2026-"))
        self.assertNotIn(NEW_TOKEN, json.dumps(body))


TokenRefreshTest.real_store_problem = staticmethod(glam._ig_token_store_problem)


class BootTest(unittest.TestCase):
    def test_boot_loads_the_matching_saved_token(self):
        # What app does at import: INSTAGRAM_PAGE_ACCESS_TOKEN comes from
        # _load_stored_ig_token(_IG_ENV_TOKEN).
        import inspect
        src = inspect.getsource(glam)
        self.assertIn(
            "INSTAGRAM_PAGE_ACCESS_TOKEN, _ig_token_expires_at = _load_stored_ig_token(_IG_ENV_TOKEN)",
            src,
        )

    def test_store_is_next_to_the_db_not_in_it(self):
        self.assertEqual(os.path.dirname(glam.IG_TOKEN_STORE_PATH),
                         os.path.dirname(os.path.abspath(glam.DB_PATH)))
        self.assertNotEqual(os.path.abspath(glam.IG_TOKEN_STORE_PATH), os.path.abspath(glam.DB_PATH))


if __name__ == "__main__":
    unittest.main()
