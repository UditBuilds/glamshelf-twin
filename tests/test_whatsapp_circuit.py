"""Tests for the WhatsApp circuit breaker (WATI refusing the login).

In Oct 2026 the WATI account stopped working (every send: HTTP 401) and
Twin treated it like a short outage: each shipping webhook retried its
updates 3 times and alerted "send the update manually from WATI", so one
week of webhooks for five orders raised 34 Telegram alerts.

Covered here:
  401/403          -> circuit opens, no per-send alert; the shipping
                      helper schedules no retry, sends no exhaustion alert
                      and writes no dedup key
  circuit open     -> send_whatsapp_reply and send_whatsapp_template make
                      ZERO HTTP calls and fire no alert
  after 6h         -> the next send is a real probe: success closes the
                      circuit, another 401 re-opens it
  transient errors -> HTTP 500 and timeouts behave exactly as before
                      (retry Timers, exhaustion alert after 3)
  missing config   -> skipped by the shipping helper, circuit stays shut,
                      no alert
  daily alert      -> one Telegram message per 24h, even across a restart
  /healthz         -> keyed view shows the circuit
  storm replay     -> one order's create / update / out-for-delivery /
                      delivered / orders-updated webhooks with WATI
                      answering 401 (run_storm also runs on origin/main to
                      get the BEFORE numbers)
  kill switch      -> SHIPPING_RETRY_DISABLED=1 still sends once and marks

Only fake numbers (919999999999) and fake orders are used.

Run:  python -m unittest tests.test_whatsapp_circuit
"""

import base64
import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-whatsapp-circuit-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("APP_PASSWORD", "test-app-password")
    os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import requests

import app as glam

FAKE_ENDPOINT = "https://fake-wati.test"
FAKE_WA_ID = "919999999999"
FAKE_PHONE = "+91 99999 99999"  # becomes FAKE_WA_ID
STORM_SECRET = "storm-test-secret"
ALERT_JOB = "whatsapp_down_alert"  # literal, so run_storm also works on origin/main
T0 = 1_791_000_000.0
SIX_HOURS = 6 * 60 * 60
DAY = 24 * 60 * 60


class FakeResponse:
    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.ok = 200 <= status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class Clock:
    """Stands in for time.time so tests can step past the 6h window and
    the 24h alert interval."""

    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Wati:
    """Stands in for requests.post. Answers every WATI call with
    `self.reply` (a FakeResponse, or an exception to raise) and records
    the URLs; anything else is recorded in `other` and refused."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[str] = []
        self.other: list[str] = []

    def __call__(self, url, **_kwargs):
        if not str(url).startswith(FAKE_ENDPOINT):
            self.other.append(str(url))
            raise requests.ConnectionError("not WATI")
        self.calls.append(str(url))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def _timer_class(clock, pending):
    class FakeTimer:
        """Records instead of scheduling; drain() fires them at their due
        time on the fake clock."""

        def __init__(self, interval, function, args=()):
            self.interval = interval
            self.due = clock.now + interval
            self.function = function
            self.args = args
            self.daemon = False

        def start(self):
            pending.append(self)

    return FakeTimer


def drain(clock, pending):
    """Fire pending timers in due order, moving the clock forward, until
    none are left (a fired retry may schedule the next one)."""
    fired = 0
    while pending:
        pending.sort(key=lambda t: t.due)
        timer = pending.pop(0)
        clock.now = max(clock.now, timer.due)
        timer.function(*timer.args)
        fired += 1
    return fired


def clear_alert_stamp(app_module):
    app_module._init_db()
    conn = sqlite3.connect(app_module.DB_PATH)
    try:
        conn.execute("DELETE FROM scheduled_jobs WHERE job = ?", (ALERT_JOB,))
        conn.commit()
    finally:
        conn.close()


def shipping_rows(app_module, order_id):
    conn = sqlite3.connect(app_module.DB_PATH)
    try:
        return conn.execute(
            "SELECT message_type FROM shipping_notifications WHERE order_id = ?",
            (str(order_id),),
        ).fetchall()
    finally:
        conn.close()


def patches(app_module, clock, wati, telegram, timer_cls):
    """Everything a WATI test needs faked: config, Telegram, HTTP, Timers,
    the clock, and empty alert mutes / circuit state. No real network."""
    env = patch.dict(os.environ)
    found = [
        env,
        patch.object(app_module, "WATI_API_KEY", "test-wati-key"),
        patch.object(app_module, "WATI_ENDPOINT", FAKE_ENDPOINT),
        patch.object(app_module, "TELEGRAM_CHAT_ID", "777"),
        patch.object(app_module, "_telegram_api", telegram),
        patch.object(app_module, "SHOPIFY_WEBHOOK_SECRET", STORM_SECRET),
        patch.object(app_module, "BUSINESS_NUMBER", "910000000001"),
        patch.object(app_module, "OWNER_NUMBER", "910000000002"),
        patch.object(app_module.requests, "post", wati),
        patch.object(threading, "Timer", timer_cls),
        patch.object(time, "time", clock),
        patch.dict(app_module._send_failure_last_alert, {}, clear=True),
    ]
    if hasattr(app_module, "_whatsapp_circuit"):
        found.append(patch.dict(
            app_module._whatsapp_circuit, {"opened_at": None, "reason": None}
        ))
    return found


def telegram_texts(telegram):
    return [
        c.args[1].get("text", "")
        for c in telegram.call_args_list
        if c.args and c.args[0] == "sendMessage"
    ]


# ---- storm replay (also run against origin/main for the BEFORE numbers) ----

def storm_events(order_id):
    """One order's webhooks, as Shopify and Shiprocket send them: shipped
    (no tracking yet), in transit with tracking, out for delivery,
    delivered, then an orders/updated that the recovery route turns into
    another "shipped" (what happened to order 1157 on Oct 9)."""
    dest = {"phone": FAKE_PHONE, "first_name": "Test"}
    tracking = {"tracking_number": "TRK000000001", "tracking_company": "Delhivery"}
    fulfillment = {"order_id": order_id, "name": "#9001.1", "destination": dest}
    return [
        ("/shopify-fulfillment", "fulfillments/create", dict(fulfillment)),
        ("/shopify-fulfillment", "fulfillments/update",
         {**fulfillment, **tracking, "shipment_status": "in_transit"}),
        ("/shopify-fulfillment", "fulfillments/update",
         {**fulfillment, **tracking, "shipment_status": "out_for_delivery"}),
        ("/shopify-fulfillment", "fulfillments/update",
         {**fulfillment, **tracking, "shipment_status": "delivered"}),
        ("/shopify-order-update", "orders/updated", {
            "id": order_id, "name": "#9001",
            "fulfillment_status": "fulfilled", "financial_status": "paid",
            "shipping_address": {"phone": FAKE_PHONE},
            "customer": {"first_name": "Test"},
            "fulfillments": [dict(tracking)],
        }),
    ]


def post_signed(client, path, topic, payload):
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(STORM_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    resp = client.post(path, data=body, headers={
        "X-Shopify-Hmac-Sha256": base64.b64encode(digest).decode("ascii"),
        "X-Shopify-Topic": topic,
        "Content-Type": "application/json",
    })
    if resp.status_code != 200:
        raise AssertionError(f"{path} {topic} answered {resp.status_code}")


def run_storm(app_module, gap_seconds):
    """Replay storm_events through the real routes with every WATI call
    answering HTTP 401, firing each webhook's retry Timers before the next
    webhook arrives `gap_seconds` later. Returns the counts."""
    clock = Clock()
    pending: list = []
    wati = Wati(FakeResponse(401, text=""))
    telegram = Mock(return_value={"ok": True})
    order_id = f"storm-{uuid.uuid4().hex[:10]}"
    out = io.StringIO()
    with ExitStack() as stack:
        for p in patches(app_module, clock, wati, telegram, _timer_class(clock, pending)):
            stack.enter_context(p)
        os.environ.pop("SHIPPING_RETRY_DISABLED", None)
        clear_alert_stamp(app_module)
        client = app_module.app.test_client()
        with redirect_stdout(out):
            for i, (path, topic, payload) in enumerate(storm_events(order_id)):
                if i:
                    clock.now += gap_seconds
                post_signed(client, path, topic, payload)
                drain(clock, pending)
    texts = telegram_texts(telegram)
    return {
        "wati_calls": len(wati.calls),
        "telegram": len(texts),
        "down_alerts": sum("WhatsApp is down" in t for t in texts),
        "send_failed_alerts": sum("WhatsApp send FAILED" in t for t in texts),
        "exhaustion_alerts": sum("Shipping update FAILED" in t for t in texts),
        "skip_lines": out.getvalue().count("[SHIPPING-SKIP]"),
        "dedup_rows": len(shipping_rows(app_module, order_id)),
        "other_posts": len(wati.other),
        "phone_in_alerts": any("9999999999" in t for t in texts),
    }


class CircuitTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pending: list = []
        self.wati = Wati(FakeResponse(401, text=""))
        self.telegram = Mock(return_value={"ok": True})
        for p in patches(glam, self.clock, self.wati, self.telegram,
                         _timer_class(self.clock, self.pending)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("SHIPPING_RETRY_DISABLED", None)
        clear_alert_stamp(glam)
        self.addCleanup(clear_alert_stamp, glam)

    def ok_reply(self):
        return FakeResponse(200, {"result": True})

    @staticmethod
    def order():
        return f"oid-{uuid.uuid4().hex[:12]}"

    def texts(self):
        return telegram_texts(self.telegram)


class LoginRefused(CircuitTestCase):
    def test_401_opens_circuit_without_send_alert(self):
        alert = Mock()
        with patch.object(glam, "_alert_send_failure", alert):
            ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertFalse(ok)
        self.assertEqual(err, "HTTP 401: (empty body)")
        self.assertEqual(len(self.wati.calls), 1)
        alert.assert_not_called()
        status = glam._whatsapp_circuit_status()
        self.assertEqual(status["circuit"], "open")
        self.assertEqual(status["reason"], "HTTP 401: (empty body)")

    def test_403_with_json_body_counts_as_login_refused(self):
        # Without the status check first, {"result": false} would be read
        # as a one-off "WATI rejected" and treated as transient.
        self.wati.reply = FakeResponse(403, {"result": False, "info": "Forbidden"})
        alert = Mock()
        with patch.object(glam, "_alert_send_failure", alert):
            ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertFalse(ok)
        self.assertTrue(err.startswith("HTTP 403:"), err)
        alert.assert_not_called()
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "open")

    def test_shipping_401_no_retry_no_alert_no_dedup(self):
        oid = self.order()
        out = io.StringIO()
        with redirect_stdout(out):
            ok = glam._send_shipping_with_retry(
                FAKE_WA_ID, "msg", oid, "shipped", "9001", also_mark=("tracking",)
            )
        self.assertFalse(ok)
        self.assertEqual(self.pending, [])  # no retry Timer
        self.assertEqual(shipping_rows(glam, oid), [])  # dedup key NOT written
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "open")
        self.assertFalse(any("Shipping update FAILED" in t for t in self.texts()))
        self.assertFalse(any("WhatsApp send FAILED" in t for t in self.texts()))
        self.assertIn(
            "[SHIPPING-SKIP] order 9001 shipped: HTTP 401: (empty body)", out.getvalue()
        )


class CircuitOpen(CircuitTestCase):
    def setUp(self):
        super().setUp()
        glam.send_whatsapp_reply(FAKE_WA_ID, "trips the circuit")
        self.wati.calls.clear()
        self.telegram.reset_mock()

    def test_send_makes_zero_http_calls_and_no_alert(self):
        alert = Mock()
        with patch.object(glam, "_alert_send_failure", alert):
            ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertFalse(ok)
        self.assertEqual(err, "WhatsApp circuit open: HTTP 401: (empty body)")
        self.assertEqual(self.wati.calls, [])
        alert.assert_not_called()
        self.telegram.assert_not_called()

    def test_template_makes_zero_http_calls(self):
        ok = glam.send_whatsapp_template(FAKE_WA_ID, "shipping_notification_template", [])
        self.assertFalse(ok)
        self.assertEqual(self.wati.calls, [])

    def test_shipping_skips_with_no_call_no_timer_no_dedup(self):
        oid = self.order()
        ok = glam._send_shipping_with_retry(FAKE_WA_ID, "msg", oid, "delivered", "9001")
        self.assertFalse(ok)
        self.assertEqual(self.wati.calls, [])
        self.assertEqual(self.pending, [])
        self.assertEqual(shipping_rows(glam, oid), [])
        self.telegram.assert_not_called()

    def test_still_open_just_before_the_window_ends(self):
        self.clock.now = T0 + SIX_HOURS - 1
        ok, _ = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertFalse(ok)
        self.assertEqual(self.wati.calls, [])


class Probe(CircuitTestCase):
    def setUp(self):
        super().setUp()
        glam.send_whatsapp_reply(FAKE_WA_ID, "trips the circuit")
        self.wati.calls.clear()

    def test_success_after_window_closes_circuit(self):
        self.clock.now = T0 + SIX_HOURS + 1
        self.wati.reply = self.ok_reply()
        ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertTrue(ok, err)
        self.assertEqual(len(self.wati.calls), 1)  # the probe hit the network
        self.assertEqual(glam._whatsapp_circuit_status(),
                         {"circuit": "closed", "reason": None, "opened_at": None})
        ok, _ = glam.send_whatsapp_reply(FAKE_WA_ID, "again")
        self.assertTrue(ok)
        self.assertEqual(len(self.wati.calls), 2)

    def test_401_after_window_reopens_circuit(self):
        self.clock.now = T0 + SIX_HOURS + 1
        ok, _ = glam.send_whatsapp_reply(FAKE_WA_ID, "probe")
        self.assertFalse(ok)
        self.assertEqual(len(self.wati.calls), 1)
        self.assertEqual(
            glam._whatsapp_circuit_status()["opened_at"],
            datetime.fromtimestamp(T0 + SIX_HOURS + 1, timezone.utc).isoformat(timespec="seconds"),
        )
        self.clock.now += 60
        glam.send_whatsapp_reply(FAKE_WA_ID, "blocked again")
        self.assertEqual(len(self.wati.calls), 1)

    def test_template_success_after_window_closes_circuit(self):
        self.clock.now = T0 + SIX_HOURS + 1
        self.wati.reply = self.ok_reply()
        self.assertTrue(
            glam.send_whatsapp_template(FAKE_WA_ID, "shipping_notification_template", [])
        )
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "closed")


class TemplateLoginRefused(CircuitTestCase):
    def test_template_401_opens_circuit_for_session_sends_too(self):
        ok = glam.send_whatsapp_template(FAKE_WA_ID, "shipping_notification_template", [])
        self.assertFalse(ok)
        self.assertEqual(len(self.wati.calls), 1)
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "open")
        ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "fallback")
        self.assertFalse(ok)
        self.assertTrue(err.startswith("WhatsApp circuit open: HTTP 401"), err)
        self.assertEqual(len(self.wati.calls), 1)


class TransientUnchanged(CircuitTestCase):
    """HTTP 500 and timeouts keep today's behaviour: per-send alert,
    retries at 60s and 300s, exhaustion alert after 3, nothing marked."""

    def _assert_retried_and_exhausted(self, expected_error):
        oid = self.order()
        ok = glam._send_shipping_with_retry(FAKE_WA_ID, "msg", oid, "out_for_delivery", "9001")
        self.assertFalse(ok)
        self.assertEqual([t.interval for t in self.pending], [glam.SHIPPING_SEND_RETRY_DELAYS[0]])
        self.assertEqual(drain(self.clock, self.pending), 2)
        self.assertEqual(len(self.wati.calls), 3)
        self.assertEqual(shipping_rows(glam, oid), [])
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "closed")
        texts = self.texts()
        exhausted = [t for t in texts if "Shipping update FAILED" in t]
        self.assertEqual(len(exhausted), 1)
        self.assertIn("send the update manually from WATI", exhausted[0])
        self.assertIn(expected_error, exhausted[0])
        # The per-send alert fires once; the 30-min mute holds the repeats.
        self.assertEqual(sum("WhatsApp send FAILED" in t for t in texts), 1)
        self.assertEqual(sum("WhatsApp is down" in t for t in texts), 0)

    def test_http_500(self):
        self.wati.reply = FakeResponse(500, text="upstream down")
        self._assert_retried_and_exhausted("HTTP 500: upstream down")

    def test_timeout(self):
        self.wati.reply = requests.Timeout("read timed out")
        self._assert_retried_and_exhausted("Network error: Timeout")

    def test_wati_result_false_is_transient(self):
        self.wati.reply = FakeResponse(200, {"result": False, "info": "Ticket has been expired"})
        oid = self.order()
        glam._send_shipping_with_retry(FAKE_WA_ID, "msg", oid, "delivered", "9001")
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "closed")


class MissingConfig(CircuitTestCase):
    def setUp(self):
        super().setUp()
        p = patch.object(glam, "WATI_API_KEY", "")
        p.start()
        self.addCleanup(p.stop)

    def test_send_does_not_open_circuit_or_alert(self):
        ok, err = glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertFalse(ok)
        self.assertEqual(err, "WATI_API_KEY or WATI_ENDPOINT not set")
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "closed")
        self.assertEqual(self.wati.calls, [])
        self.telegram.assert_not_called()

    def test_shipping_skips_without_retry_alert_or_dedup(self):
        oid = self.order()
        out = io.StringIO()
        with redirect_stdout(out):
            ok = glam._send_shipping_with_retry(FAKE_WA_ID, "msg", oid, "shipped", "9001")
        self.assertFalse(ok)
        self.assertEqual(self.pending, [])
        self.assertEqual(shipping_rows(glam, oid), [])
        self.telegram.assert_not_called()
        self.assertIn(
            "[SHIPPING-SKIP] order 9001 shipped: WATI_API_KEY or WATI_ENDPOINT not set",
            out.getvalue(),
        )


class DailyAlert(CircuitTestCase):
    def test_first_open_sends_one_alert_without_customer_number(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        texts = self.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("WhatsApp is down — WATI refused the login (HTTP 401: (empty body))", texts[0])
        self.assertIn("At most one alert a day", texts[0])
        self.assertNotIn("9999999999", texts[0])

    def test_second_open_within_24h_sends_none_even_after_restart(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "first open")
        self.assertEqual(len(self.texts()), 1)
        # Simulated restart: every in-memory bit of state is gone, only the
        # scheduled_jobs stamp in the DB survives.
        glam._whatsapp_circuit.update({"opened_at": None, "reason": None})
        glam._send_failure_last_alert.clear()
        self.clock.now = T0 + DAY - 60
        glam.send_whatsapp_reply(FAKE_WA_ID, "second open")
        self.assertEqual(glam._whatsapp_circuit_status()["circuit"], "open")
        self.assertEqual(len(self.wati.calls), 2)
        self.assertEqual(len(self.texts()), 1)

    def test_next_day_alerts_again(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "first open")
        self.clock.now = T0 + DAY
        glam.send_whatsapp_reply(FAKE_WA_ID, "probe a day later")
        self.assertEqual(len(self.texts()), 2)


class Healthz(CircuitTestCase):
    def _keyed(self):
        resp = glam.app.test_client().get(
            "/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY}
        )
        return resp

    def test_closed_by_default(self):
        self.assertEqual(self._keyed().get_json()["whatsapp"],
                         {"circuit": "closed", "reason": None, "opened_at": None})

    def test_open_shows_reason_and_time_without_numbers(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        resp = self._keyed()
        self.assertEqual(resp.get_json()["whatsapp"], {
            "circuit": "open",
            "reason": "HTTP 401: (empty body)",
            "opened_at": datetime.fromtimestamp(T0, timezone.utc).isoformat(timespec="seconds"),
        })
        self.assertNotIn(b"9999999999", resp.data)

    def test_public_view_unchanged(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "hello")
        self.assertEqual(glam.app.test_client().get("/healthz").get_json(), {"status": "ok"})


class KillSwitch(CircuitTestCase):
    """SHIPPING_RETRY_DISABLED=1 keeps the legacy branch verbatim: one
    send, mark regardless, report handled (decision 7 over decision 3)."""

    def setUp(self):
        super().setUp()
        os.environ["SHIPPING_RETRY_DISABLED"] = "1"

    def test_401_sends_once_marks_and_schedules_nothing(self):
        oid = self.order()
        ok = glam._send_shipping_with_retry(
            FAKE_WA_ID, "msg", oid, "shipped", "9001", also_mark=("tracking",)
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.wati.calls), 1)
        self.assertEqual(sorted(r[0] for r in shipping_rows(glam, oid)), ["shipped", "tracking"])
        self.assertEqual(self.pending, [])
        self.assertFalse(any("Shipping update FAILED" in t for t in self.texts()))

    def test_open_circuit_still_marks(self):
        glam.send_whatsapp_reply(FAKE_WA_ID, "trips the circuit")
        self.wati.calls.clear()
        oid = self.order()
        ok = glam._send_shipping_with_retry(FAKE_WA_ID, "msg", oid, "delivered", "9001")
        self.assertTrue(ok)
        self.assertEqual(self.wati.calls, [])
        self.assertEqual(shipping_rows(glam, oid), [("delivered",)])


class StormReplay(unittest.TestCase):
    """The key test: one order's webhooks with WATI answering 401 to
    everything. BEFORE (origin/main) numbers are in the PR description."""

    def _check_common(self, result):
        self.assertEqual(result["other_posts"], 0)
        self.assertEqual(result["send_failed_alerts"], 0)
        self.assertEqual(result["exhaustion_alerts"], 0)
        self.assertEqual(result["dedup_rows"], 0)
        self.assertFalse(result["phone_in_alerts"])
        # shipped, tracking + shipped, tracking + out-for-delivery,
        # tracking + delivered, recovery shipped
        self.assertEqual(result["skip_lines"], 8)

    def test_burst_within_one_window(self):
        result = run_storm(glam, gap_seconds=10 * 60)
        self._check_common(result)
        self.assertEqual(result["wati_calls"], 1)
        self.assertEqual(result["telegram"], 1)
        self.assertEqual(result["down_alerts"], 1)

    def test_one_webhook_a_day(self):
        # Each day the first send is a probe (one 401), and each day gets
        # at most one alert.
        result = run_storm(glam, gap_seconds=DAY)
        self._check_common(result)
        self.assertEqual(result["wati_calls"], 5)
        self.assertEqual(result["telegram"], 5)
        self.assertEqual(result["down_alerts"], 5)


if __name__ == "__main__":
    unittest.main()
