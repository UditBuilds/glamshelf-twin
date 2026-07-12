"""Tests for webhook origin verification on the three non-Shopify inbound
endpoints: /webhook + /wati-outbound (WATI path token), /instagram-webhook
(Meta X-Hub-Signature-256), /telegram-callback (Telegram secret-token
header).

Every provider gets the same four checks:
  valid credential   -> 200 (request reaches the handler)
  wrong credential   -> 401
  missing credential -> 401
  kill switch set    -> 200 even without a credential (rollback path)
plus the fail-closed case: secret not configured -> 401 for everything.

Payloads are deliberately benign no-op events (unsupported message type,
empty entry list, empty update) so a passing request exits the handler
before any LLM call, WhatsApp send, or Telegram notification.

Run:  python -m unittest discover tests
"""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing app runs its startup block (DB restore/init, backup loop).
# Point DB_PATH at a throwaway temp file and blank the GitHub backup vars
# BEFORE the import so tests never touch a real database or repo.
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-webhook-auth-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
# app.py refuses to start without these (see _require_env). Dummies are
# fine for auth tests; setdefault keeps any real local .env values intact.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-app-password")
os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam

TEST_INSTAGRAM_SECRET = "test-instagram-app-secret"
TEST_TELEGRAM_SECRET = "test-telegram-secret-token"
TEST_WATI_TOKEN = "test-wati-path-token"

# Kill-switch env vars must not leak in from the host environment.
_KILL_SWITCHES = (
    "WATI_WEBHOOK_VERIFY_DISABLED",
    "INSTAGRAM_WEBHOOK_VERIFY_DISABLED",
    "TELEGRAM_WEBHOOK_VERIFY_DISABLED",
)


def _meta_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


class WebhookAuthBase(unittest.TestCase):
    """Shared setup: test client + secrets configured on the app module."""

    def setUp(self):
        self.client = glam.app.test_client()
        for var in _KILL_SWITCHES:
            os.environ.pop(var, None)
        patches = [
            patch.object(glam, "INSTAGRAM_APP_SECRET", TEST_INSTAGRAM_SECRET),
            patch.object(glam, "TELEGRAM_WEBHOOK_SECRET", TEST_TELEGRAM_SECRET),
            patch.object(glam, "WATI_WEBHOOK_TOKEN", TEST_WATI_TOKEN),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class WatiInboundAuth(WebhookAuthBase):
    """Path-token gate on POST /webhook (inbound WhatsApp)."""

    # Unsupported message type -> handler no-ops immediately after auth.
    PAYLOAD = {"type": "status", "waId": "919999999999"}

    def test_valid_token_passes(self):
        resp = self.client.post(
            f"/webhook/{TEST_WATI_TOKEN}", json=self.PAYLOAD
        )
        self.assertEqual(resp.status_code, 200)

    def test_wrong_token_401(self):
        resp = self.client.post("/webhook/wrong-token", json=self.PAYLOAD)
        self.assertEqual(resp.status_code, 401)

    def test_missing_token_401(self):
        resp = self.client.post("/webhook", json=self.PAYLOAD)
        self.assertEqual(resp.status_code, 401)

    def test_unconfigured_token_fails_closed(self):
        with patch.object(glam, "WATI_WEBHOOK_TOKEN", ""):
            resp = self.client.post(
                f"/webhook/{TEST_WATI_TOKEN}", json=self.PAYLOAD
            )
        self.assertEqual(resp.status_code, 401)

    def test_kill_switch_bypasses(self):
        with patch.dict(os.environ, {"WATI_WEBHOOK_VERIFY_DISABLED": "1"}):
            resp = self.client.post("/webhook", json=self.PAYLOAD)
        self.assertEqual(resp.status_code, 200)

    def test_get_ping_stays_open(self):
        # WATI's URL test sends a GET; it carries no event data and must
        # keep working on both the bare and tokenized paths.
        self.assertEqual(self.client.get("/webhook").status_code, 200)
        self.assertEqual(
            self.client.get(f"/webhook/{TEST_WATI_TOKEN}").status_code, 200
        )


class WatiOutboundAuth(WebhookAuthBase):
    """Path-token gate on POST /wati-outbound."""

    # Empty payload -> _process_wati_outbound skips (no waId/text).
    PAYLOAD = {}

    def test_valid_token_passes(self):
        resp = self.client.post(
            f"/wati-outbound/{TEST_WATI_TOKEN}", json=self.PAYLOAD
        )
        self.assertEqual(resp.status_code, 200)

    def test_wrong_token_401(self):
        resp = self.client.post(
            "/wati-outbound/wrong-token", json=self.PAYLOAD
        )
        self.assertEqual(resp.status_code, 401)

    def test_missing_token_401(self):
        resp = self.client.post("/wati-outbound", json=self.PAYLOAD)
        self.assertEqual(resp.status_code, 401)

    def test_kill_switch_bypasses(self):
        with patch.dict(os.environ, {"WATI_WEBHOOK_VERIFY_DISABLED": "1"}):
            resp = self.client.post("/wati-outbound", json=self.PAYLOAD)
        self.assertEqual(resp.status_code, 200)


class InstagramWebhookAuth(WebhookAuthBase):
    """X-Hub-Signature-256 gate on POST /instagram-webhook."""

    # Empty entry list -> handler loops over nothing and returns.
    BODY = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")

    def _post(self, body: bytes, signature: str | None):
        headers = {}
        if signature is not None:
            headers["X-Hub-Signature-256"] = signature
        return self.client.post(
            "/instagram-webhook",
            data=body,
            content_type="application/json",
            headers=headers,
        )

    def test_valid_signature_passes(self):
        sig = _meta_signature(TEST_INSTAGRAM_SECRET, self.BODY)
        self.assertEqual(self._post(self.BODY, sig).status_code, 200)

    def test_wrong_signature_401(self):
        sig = _meta_signature("some-other-secret", self.BODY)
        self.assertEqual(self._post(self.BODY, sig).status_code, 401)

    def test_tampered_body_401(self):
        sig = _meta_signature(TEST_INSTAGRAM_SECRET, self.BODY)
        tampered = self.BODY + b" "
        self.assertEqual(self._post(tampered, sig).status_code, 401)

    def test_missing_signature_401(self):
        self.assertEqual(self._post(self.BODY, None).status_code, 401)

    def test_unconfigured_secret_fails_closed(self):
        sig = _meta_signature(TEST_INSTAGRAM_SECRET, self.BODY)
        with patch.object(glam, "INSTAGRAM_APP_SECRET", ""):
            resp = self._post(self.BODY, sig)
        self.assertEqual(resp.status_code, 401)

    def test_kill_switch_bypasses(self):
        with patch.dict(
            os.environ, {"INSTAGRAM_WEBHOOK_VERIFY_DISABLED": "1"}
        ):
            resp = self._post(self.BODY, None)
        self.assertEqual(resp.status_code, 200)


class TelegramCallbackAuth(WebhookAuthBase):
    """X-Telegram-Bot-Api-Secret-Token gate on POST /telegram-callback."""

    # Update with neither callback_query nor message -> handler ignores.
    PAYLOAD = {"update_id": 1}

    def _post(self, secret: str | None):
        headers = {}
        if secret is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        return self.client.post(
            "/telegram-callback", json=self.PAYLOAD, headers=headers
        )

    def test_valid_secret_passes(self):
        self.assertEqual(self._post(TEST_TELEGRAM_SECRET).status_code, 200)

    def test_wrong_secret_401(self):
        self.assertEqual(self._post("wrong-secret").status_code, 401)

    def test_missing_secret_401(self):
        self.assertEqual(self._post(None).status_code, 401)

    def test_unconfigured_secret_fails_closed(self):
        with patch.object(glam, "TELEGRAM_WEBHOOK_SECRET", ""):
            resp = self._post(TEST_TELEGRAM_SECRET)
        self.assertEqual(resp.status_code, 401)

    def test_kill_switch_bypasses(self):
        with patch.dict(
            os.environ, {"TELEGRAM_WEBHOOK_VERIFY_DISABLED": "1"}
        ):
            resp = self._post(None)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
