"""Tests for shipping-notification delivery integrity (security-review
Issue 3).

Before this fix, all three shipping dispatch sites wrote the
(order_id, message_type) dedup key unconditionally after calling
send_whatsapp_reply, so one transient WATI failure permanently
suppressed that customer's shipping update — no retry ever.

Covered here:
  success           -> marked sent (plus also_mark keys), no retry timer
  transient failure -> NOT marked, bounded retry scheduled with backoff
  retry success     -> marked on the later attempt
  retries exhausted -> NOT marked (manual resend still possible),
                       dedicated Telegram exhaustion alert fired
  dedup race        -> a retry attempt skips silently if another path
                       already delivered
  kill switch       -> SHIPPING_RETRY_DISABLED=1 restores legacy
                       send-once / mark-always behavior
  site wiring       -> _process_shipping_event marks only on success and
                       only schedules the review request after a
                       confirmed 'delivered' send

Run:  python -m unittest discover tests
"""

import os
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-shipping-retry-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("APP_PASSWORD", "test-app-password")
    os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam


class FakeTimer:
    """Stands in for threading.Timer — records instead of scheduling, so
    tests can assert on delays/args and fire callbacks synchronously."""

    instances: list = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True
        FakeTimer.instances.append(self)

    def fire(self):
        self.function(*self.args)


class ShippingRetryBase(unittest.TestCase):
    def setUp(self):
        FakeTimer.instances = []
        os.environ.pop("SHIPPING_RETRY_DISABLED", None)
        timer_patch = patch.object(threading, "Timer", FakeTimer)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)
        self.addCleanup(
            lambda: os.environ.pop("SHIPPING_RETRY_DISABLED", None)
        )

    @staticmethod
    def _order():
        return f"oid-{uuid.uuid4().hex[:12]}"


class RetryHelper(ShippingRetryBase):
    def test_success_marks_and_schedules_nothing(self):
        oid = self._order()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(True, ""))):
            ok = glam._send_shipping_with_retry(
                "919000000001", "msg", oid, "tracking", "1001"
            )
        self.assertTrue(ok)
        self.assertTrue(glam._was_shipping_sent(oid, "tracking"))
        self.assertEqual(FakeTimer.instances, [])

    def test_success_marks_also_mark_keys(self):
        oid = self._order()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(True, ""))):
            ok = glam._send_shipping_with_retry(
                "919000000002", "msg", oid, "shipped", "1002",
                also_mark=("tracking",),
            )
        self.assertTrue(ok)
        self.assertTrue(glam._was_shipping_sent(oid, "shipped"))
        self.assertTrue(glam._was_shipping_sent(oid, "tracking"))

    def test_failure_not_marked_and_retry_scheduled(self):
        oid = self._order()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(False, "WATI rejected: boom"))):
            ok = glam._send_shipping_with_retry(
                "919000000003", "msg", oid, "tracking", "1003"
            )
        self.assertFalse(ok)
        self.assertFalse(glam._was_shipping_sent(oid, "tracking"))
        self.assertEqual(len(FakeTimer.instances), 1)
        timer = FakeTimer.instances[0]
        self.assertEqual(timer.interval, glam.SHIPPING_SEND_RETRY_DELAYS[0])
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)
        self.assertEqual(timer.args[5], 2)  # next attempt number

    def test_retry_success_marks_sent(self):
        oid = self._order()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(False, "timeout"))):
            glam._send_shipping_with_retry(
                "919000000004", "msg", oid, "shipped", "1004",
                also_mark=("tracking",),
            )
        self.assertEqual(len(FakeTimer.instances), 1)
        # Fire the captured retry with the sender now healthy.
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(True, ""))):
            FakeTimer.instances[0].fire()
        self.assertTrue(glam._was_shipping_sent(oid, "shipped"))
        self.assertTrue(glam._was_shipping_sent(oid, "tracking"))
        self.assertEqual(len(FakeTimer.instances), 1)  # no further retry

    def test_exhaustion_alerts_and_leaves_unmarked(self):
        oid = self._order()
        telegram = Mock()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(False, "still down"))), \
             patch.object(glam, "_telegram_api", telegram), \
             patch.object(glam, "TELEGRAM_CHAT_ID", "777"):
            ok = glam._send_shipping_with_retry(
                "919000000005", "msg", oid, "tracking", "1005",
                attempt=glam.SHIPPING_SEND_MAX_ATTEMPTS,
            )
        self.assertFalse(ok)
        self.assertFalse(glam._was_shipping_sent(oid, "tracking"))
        self.assertEqual(FakeTimer.instances, [])  # no timer past the cap
        telegram.assert_called_once()
        method, payload = telegram.call_args.args
        self.assertEqual(method, "sendMessage")
        self.assertIn("Shipping update FAILED", payload["text"])
        self.assertIn("1005", payload["text"])

    def test_retry_skips_if_already_delivered_elsewhere(self):
        oid = self._order()
        glam._mark_shipping_sent(oid, "tracking", phone="919000000006",
                                 order_number="1006")
        sender = Mock(return_value=(True, ""))
        with patch.object(glam, "send_whatsapp_reply", sender):
            ok = glam._send_shipping_with_retry(
                "919000000006", "msg", oid, "tracking", "1006", attempt=2
            )
        self.assertTrue(ok)
        sender.assert_not_called()  # no duplicate customer message

    def test_kill_switch_restores_legacy_behavior(self):
        oid = self._order()
        with patch.dict(os.environ, {"SHIPPING_RETRY_DISABLED": "1"}), \
             patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(False, "boom"))):
            ok = glam._send_shipping_with_retry(
                "919000000007", "msg", oid, "shipped", "1007",
                also_mark=("tracking",),
            )
        self.assertTrue(ok)  # legacy: reported as handled regardless
        self.assertTrue(glam._was_shipping_sent(oid, "shipped"))
        self.assertTrue(glam._was_shipping_sent(oid, "tracking"))
        self.assertEqual(FakeTimer.instances, [])  # no retries in legacy mode


class ShippingEventWiring(ShippingRetryBase):
    """_process_shipping_event end-to-end: mark-on-success only, and the
    review request fires only after a confirmed 'delivered' send."""

    def _fulfillment(self, oid: str) -> dict:
        return {
            "order_id": oid,
            "name": "#1042.1",
            "shipment_status": "delivered",
            "destination": {"phone": "9876543210", "first_name": "Asha"},
        }

    def test_delivered_send_failure_not_marked_review_not_scheduled(self):
        oid = self._order()
        review = Mock()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(False, "boom"))), \
             patch.object(glam, "_schedule_review_request", review):
            glam._process_shipping_event(
                "fulfillments/update", self._fulfillment(oid)
            )
        self.assertFalse(glam._was_shipping_sent(oid, "delivered"))
        self.assertEqual(len(FakeTimer.instances), 1)  # retry pending
        review.assert_not_called()

    def test_delivered_send_success_marked_and_review_scheduled(self):
        oid = self._order()
        review = Mock()
        with patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=(True, ""))), \
             patch.object(glam, "_schedule_review_request", review):
            glam._process_shipping_event(
                "fulfillments/update", self._fulfillment(oid)
            )
        self.assertTrue(glam._was_shipping_sent(oid, "delivered"))
        self.assertEqual(FakeTimer.instances, [])
        review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
