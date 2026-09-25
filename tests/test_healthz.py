"""/healthz must not leak anything publicly (audit T1-7).

The service URL is published in this public repo, and the old public
/healthz response included the owner's personal phone number
(protected_numbers), row counts and raw DB error text. Now:

  - no X-Dashboard-Key header (or a wrong one): {"status": "ok"}, or 503
    {"status": "error"} when the DB can't be read — nothing else;
  - correct X-Dashboard-Key header: the full diagnostic payload;
  - a ?key= URL param is ignored (keys in URLs end up in logs).

Run:  python -m unittest tests.test_healthz
"""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-healthz-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

FAKE_BUSINESS = "919876543210"
FAKE_OWNER = "919876543211"


class HealthzTestCase(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        self.client = glam.app.test_client()
        for p in (
            patch.object(glam, "BUSINESS_NUMBER", FAKE_BUSINESS),
            patch.object(glam, "OWNER_NUMBER", FAKE_OWNER),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _broken_db(self):
        # A directory can't be opened as a SQLite file -> OperationalError.
        return patch.object(glam, "DB_PATH", tempfile.mkdtemp(prefix="glamshelf-healthz-nodb-"))


class PublicView(HealthzTestCase):
    def test_public_answer_is_status_only(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        self.assertNotIn(b"98765", resp.data)

    def test_key_in_url_param_is_ignored(self):
        resp = self.client.get("/healthz", query_string={"key": glam.DASHBOARD_KEY})
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_wrong_header_key_gets_public_view(self):
        resp = self.client.get("/healthz", headers={"X-Dashboard-Key": "not-the-key"})
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_non_ascii_header_key_does_not_crash(self):
        resp = self.client.get(
            "/healthz", headers={"X-Dashboard-Key": "clé-ü".encode("utf-8").decode("latin-1")}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_db_unreachable_is_503_without_error_text(self):
        with self._broken_db():
            resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {"status": "error"})


class KeyedView(HealthzTestCase):
    def _get(self):
        return self.client.get("/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY})

    def test_correct_header_key_gets_full_diagnostics(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["db"], "ok")
        self.assertEqual(data["protected_numbers"], [FAKE_BUSINESS, FAKE_OWNER])
        for field in ("total_logged", "total_orders", "total_instagram",
                      "seen_ids_cached", "sqlite_vec", "brain_present"):
            self.assertIn(field, data)

    def test_db_unreachable_keyed_view_shows_the_error(self):
        with self._broken_db():
            resp = self._get()
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertEqual(data["status"], "error")
        self.assertTrue(data["db"].startswith("error: "))


if __name__ == "__main__":
    unittest.main()
