"""Login and key hardening (audit T2-1, part of T2-2; PR E item 6).

  - APP_PASSWORD and DASHBOARD_KEY are compared with hmac.compare_digest
    (via _secret_equals), non-ASCII input included.
  - /login: at most 5 failed attempts per IP per 15 minutes, then 429 —
    even for the right password. The IP comes from _client_ip (PR F):
    CF-Connecting-IP, True-Client-IP, the first X-Forwarded-For entry,
    then remote_addr, skipping malformed values. (The right-most
    X-Forwarded-For entry was a Cloudflare edge shared by many visitors.)
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

    def test_visitors_behind_one_cloudflare_edge_have_separate_limits(self):
        # The live finding: every visitor arrived via the same edge address.
        edge = "172.68.174.232"
        for _ in range(5):
            self.login("wrong", ip=f"203.0.113.7, {edge}")
        self.assertEqual(self.login(PASSWORD, ip=f"203.0.113.7, {edge}").status_code, 429)
        self.assertEqual(self.login(PASSWORD, ip=f"198.51.100.9, {edge}").status_code, 302)

    def test_failed_login_log_names_the_ip_and_its_source(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.client.post("/login", data={"password": "wrong"},
                             headers={"CF-Connecting-IP": "1.2.3.4"})
        self.assertIn("[AUTH] Login failed from 1.2.3.4 (via CF-Connecting-IP)", buf.getvalue())

    def test_failures_expire_after_15_minutes(self):
        for _ in range(5):
            self.login("wrong")
        later = glam.time.time() + glam.LOGIN_FAILURE_WINDOW_SECONDS + 1
        with patch.object(glam.time, "time", return_value=later):
            self.assertEqual(self.login(PASSWORD).status_code, 302)

    def test_non_ascii_password_is_a_plain_failure(self):
        self.assertEqual(self.login("pässwörd").status_code, 200)


class ClientIpTest(unittest.TestCase):
    """_client_ip: header order, validation and the remote_addr fallback."""

    def ip(self, headers=None, remote="192.0.2.50"):
        with glam.app.test_request_context(
            "/login", headers=headers or {}, environ_base={"REMOTE_ADDR": remote}
        ):
            return glam._client_ip()

    def test_cf_connecting_ip_wins_over_forwarded_for(self):
        self.assertEqual(
            self.ip({"CF-Connecting-IP": "1.2.3.4", "True-Client-IP": "5.6.7.8",
                     "X-Forwarded-For": "9.9.9.9, 172.68.174.232"}),
            ("1.2.3.4", "CF-Connecting-IP"),
        )

    def test_true_client_ip_is_second(self):
        self.assertEqual(
            self.ip({"True-Client-IP": "5.6.7.8", "X-Forwarded-For": "9.9.9.9"}),
            ("5.6.7.8", "True-Client-IP"),
        )

    def test_forwarded_for_first_hop_without_cf_header(self):
        self.assertEqual(
            self.ip({"X-Forwarded-For": " 9.9.9.9 , 172.68.174.232"}),
            ("9.9.9.9", "X-Forwarded-For"),
        )

    def test_ipv6_is_accepted(self):
        self.assertEqual(self.ip({"CF-Connecting-IP": "2001:db8::1"}),
                         ("2001:db8::1", "CF-Connecting-IP"))

    def test_malformed_headers_are_skipped(self):
        self.assertEqual(
            self.ip({"CF-Connecting-IP": "not-an-ip", "True-Client-IP": "999.1.1.1",
                     "X-Forwarded-For": "9.9.9.9"}),
            ("9.9.9.9", "X-Forwarded-For"),
        )

    def test_falls_back_to_remote_addr(self):
        self.assertEqual(self.ip(), ("192.0.2.50", "remote_addr"))
        self.assertEqual(self.ip({"X-Forwarded-For": "garbage, 9.9.9.9"}),
                         ("192.0.2.50", "remote_addr"))


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
