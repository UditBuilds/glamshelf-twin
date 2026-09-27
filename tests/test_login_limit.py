"""Login and key hardening (audit T2-1, part of T2-2; PR E item 6).

  - APP_PASSWORD and DASHBOARD_KEY are compared with hmac.compare_digest
    (via _secret_equals), non-ASCII input included.
  - /login: at most 5 failed attempts per IP per 15 minutes, then 429 —
    even for the right password. The IP is the right-most X-Forwarded-For
    entry (Render's proxy), so a spoofed left-most entry doesn't help.
  - The dashboard key still travels as ?key= (the dashboard depends on it).

No live API call: Flask's test client only.

Run:  python -m unittest tests.test_login_limit
"""
import io, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-login-limit-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

PASSWORD = "right-password"
KEY = "dash-key-123"


class LoginLimitTest(unittest.TestCase):
    def setUp(self):
        glam._login_failures.clear()
        for p in (patch.object(glam, "APP_PASSWORD", PASSWORD),
                  patch.object(glam, "DASHBOARD_KEY", KEY)):
            p.start()
            self.addCleanup(p.stop)
        self.client = glam.app.test_client()

    def login(self, password, ip="203.0.113.7"):
        with redirect_stdout(io.StringIO()):
            return self.client.post("/login", data={"password": password},
                                    headers={"X-Forwarded-For": ip})

    def test_right_password_logs_in(self):
        self.assertEqual(self.login(PASSWORD).status_code, 302)

    def test_sixth_attempt_is_rejected_with_429_even_with_the_right_password(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong").status_code, 200)
        resp = self.login(PASSWORD)
        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many failed attempts", resp.data)

    def test_limit_is_per_ip(self):
        for _ in range(5):
            self.login("wrong", ip="203.0.113.7")
        self.assertEqual(self.login(PASSWORD, ip="198.51.100.9").status_code, 302)

    def test_spoofed_left_most_forwarded_entry_is_ignored(self):
        for i in range(5):
            self.login("wrong", ip=f"10.0.0.{i}, 203.0.113.7")
        self.assertEqual(self.login(PASSWORD, ip="10.9.9.9, 203.0.113.7").status_code, 429)

    def test_failures_expire_after_15_minutes(self):
        for _ in range(5):
            self.login("wrong")
        later = glam.time.time() + glam.LOGIN_FAILURE_WINDOW_SECONDS + 1
        with patch.object(glam.time, "time", return_value=later):
            self.assertEqual(self.login(PASSWORD).status_code, 302)

    def test_non_ascii_password_is_a_plain_failure(self):
        self.assertEqual(self.login("pässwörd").status_code, 200)


class SecretCompareTest(unittest.TestCase):
    def test_secret_equals(self):
        self.assertTrue(glam._secret_equals("abc", "abc"))
        self.assertFalse(glam._secret_equals("abd", "abc"))
        self.assertFalse(glam._secret_equals("", "abc"))
        self.assertFalse(glam._secret_equals(None, "abc"))
        self.assertFalse(glam._secret_equals("abc", ""))
        self.assertFalse(glam._secret_equals("ключ", "abc"))

    def test_login_uses_compare_digest(self):
        with patch.object(glam.hmac, "compare_digest", wraps=glam.hmac.compare_digest) as cd, \
             patch.object(glam, "APP_PASSWORD", PASSWORD):
            glam._login_failures.clear()
            with redirect_stdout(io.StringIO()):
                glam.app.test_client().post("/login", data={"password": PASSWORD})
        cd.assert_called()


class DashboardKeyParamTest(unittest.TestCase):
    """The ?key= param still works; comparison is constant-time."""

    def setUp(self):
        p = patch.object(glam, "DASHBOARD_KEY", KEY)
        p.start()
        self.addCleanup(p.stop)
        self.client = glam.app.test_client()

    def get(self, path):
        with redirect_stdout(io.StringIO()):
            return self.client.get(path)

    def test_dashboard_accepts_the_url_key(self):
        self.assertEqual(self.get(f"/dashboard?key={KEY}").status_code, 200)

    def test_dashboard_rejects_a_wrong_missing_or_non_ascii_key(self):
        for path in ("/dashboard?key=nope", "/dashboard", "/dashboard?key=%D0%BA%D0%BB"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, 401)

    def test_json_endpoints_use_the_same_check(self):
        with patch.object(glam.hmac, "compare_digest", wraps=glam.hmac.compare_digest) as cd:
            self.assertEqual(self.get("/dashboard-data?key=nope").status_code, 401)
        cd.assert_called()


if __name__ == "__main__":
    unittest.main()
