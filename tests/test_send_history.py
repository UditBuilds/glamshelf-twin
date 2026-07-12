"""Tests for delivery-truthful history records (security-review Issue 2).

Before this fix, every dispatch site logged the reply as delivered
(AUTO / DRAFT_SENT) even when the send failed, so the twin would later
"remember" telling the customer something they never received, and the
Telegram approval flow reported "✅ Sent" on failures.

Covered here, per channel and per path:
  AUTO success        -> status AUTO / untagged IG row, present in history
  AUTO failure        -> AUTO_FAILED / AUTO_FAILED_IG, absent from history
  approve-send (both) -> DRAFT_SENT[_IG] vs DRAFT_SEND_FAILED[_IG],
                         Telegram status line says ✅ Sent vs ⚠️ Send FAILED
  edit-completion     -> same split as approve-send
  sender-level alerts -> a real failed send fires _alert_send_failure

Run:  python -m unittest discover tests
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same import-isolation preamble as test_webhook_auth (whichever test
# module loads first wins the DB_PATH; always query glam.DB_PATH, never
# the env var, so both orders work under `unittest discover`).
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-send-history-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""
    os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("APP_PASSWORD", "test-app-password")
    os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam

TEST_WATI_TOKEN = "test-wati-path-token"


def _uid() -> str:
    """Unique id fragment — the dedup cache file survives across runs."""
    return uuid.uuid4().hex[:12]


def _wati_rows(wa_id: str) -> list[tuple]:
    conn = sqlite3.connect(glam.DB_PATH)
    rows = conn.execute(
        "SELECT status, reply_text, error FROM message_logs WHERE wa_id = ?",
        (wa_id,),
    ).fetchall()
    conn.close()
    return rows


def _ig_rows(sender_id: str) -> list[tuple]:
    conn = sqlite3.connect(glam.DB_PATH)
    rows = conn.execute(
        "SELECT source, reply_text FROM instagram_logs WHERE sender_id = ?",
        (sender_id,),
    ).fetchall()
    conn.close()
    return rows


class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._body


class SenderAlerts(unittest.TestCase):
    """A confirmed send failure inside the real senders fires the
    Telegram alert (PR #29 behavior the site-level tests rely on)."""

    def test_wati_rejection_alerts(self):
        alert = Mock()
        with patch.object(glam, "WATI_API_KEY", "k"), \
             patch.object(glam, "WATI_ENDPOINT", "https://fake-wati.test"), \
             patch.object(glam, "_alert_send_failure", alert), \
             patch.object(glam.requests, "post", return_value=_FakeResponse(
                 200, {"result": False, "info": "boom"})):
            ok, err = glam.send_whatsapp_reply("919999000001", "hello")
        self.assertFalse(ok)
        self.assertIn("boom", err)
        alert.assert_called_once()

    def test_instagram_http_failure_alerts(self):
        alert = Mock()
        with patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", "tok"), \
             patch.object(glam, "_alert_send_failure", alert), \
             patch.object(glam.requests, "post", return_value=_FakeResponse(
                 400, {"error": {"message": "token expired"}})):
            ok, err = glam._send_instagram_reply("1789", "hello")
        self.assertFalse(ok)
        alert.assert_called_once()


class WatiAutoHistory(unittest.TestCase):
    """POST /webhook AUTO branch: history record must match delivery."""

    def _post(self, wa_id: str, send_result):
        payload = {
            "type": "text",
            "waId": wa_id,
            "senderName": "Tester",
            "text": "do you have lashes?",
            "id": f"mid-{_uid()}",
        }
        with patch.object(glam, "WATI_WEBHOOK_TOKEN", TEST_WATI_TOKEN), \
             patch.object(glam, "_udit_replied_recently", lambda *a, **k: False), \
             patch.object(glam, "_check_recent_human_reply", lambda *a, **k: False), \
             patch.object(glam, "draft_reply_logic",
                          Mock(return_value=("AUTO", "test reply", "raw"))), \
             patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=send_result)):
            resp = glam.app.test_client().post(
                f"/webhook/{TEST_WATI_TOKEN}", json=payload
            )
        self.assertEqual(resp.status_code, 200)

    def test_send_success_logged_auto_and_in_history(self):
        wa_id = f"9198{_uid()[:8]}"
        self._post(wa_id, (True, ""))
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["AUTO"])
        history = glam._load_wati_history(wa_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reply_text"], "test reply")

    def test_send_failure_logged_failed_and_absent_from_history(self):
        wa_id = f"9197{_uid()[:8]}"
        self._post(wa_id, (False, "WATI rejected: boom"))
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["AUTO_FAILED"])
        self.assertEqual(rows[0][1], "test reply")     # attempted text kept
        self.assertIn("boom", rows[0][2])              # error recorded
        self.assertEqual(glam._load_wati_history(wa_id), [])


class InstagramAutoHistory(unittest.TestCase):
    """_process_instagram_event AUTO branch: same delivery-truth rule."""

    def _event(self, sender_id: str, send_result):
        event = {
            "sender": {"id": sender_id},
            "recipient": {"id": "17840000000000000"},
            "timestamp": "1720000000",
            "message": {"mid": f"igmid-{_uid()}", "text": "hi there"},
        }
        with patch.object(glam, "INSTAGRAM_PAGE_ID", "17840000000000000"), \
             patch.object(glam, "draft_reply_logic",
                          Mock(return_value=("AUTO", "ig reply", "raw"))), \
             patch.object(glam, "_send_instagram_reply",
                          Mock(return_value=send_result)):
            glam._process_instagram_event(event)

    def test_send_success_untagged_and_in_history(self):
        sender = f"igok{_uid()}"
        self._event(sender, (True, ""))
        rows = _ig_rows(sender)
        self.assertEqual([r[0] for r in rows], [None])
        history = glam._load_instagram_history(sender)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reply_text"], "ig reply")

    def test_send_failure_tagged_and_absent_from_history(self):
        sender = f"igko{_uid()}"
        self._event(sender, (False, "token expired"))
        rows = _ig_rows(sender)
        self.assertEqual([r[0] for r in rows], ["AUTO_FAILED_IG"])
        self.assertEqual(rows[0][1], "ig reply")       # attempted text kept
        self.assertEqual(glam._load_instagram_history(sender), [])


class DraftApproveSend(unittest.TestCase):
    """Telegram ✅ Send-as-is: log status, history, and the status line
    Udit sees must all reflect the real send outcome."""

    CHAT_ID = 777001

    def _tap_send(self, draft: dict, send_result):
        draft_id = f"d{_uid()[:8]}"
        self.assertTrue(glam._draft_register(draft_id, draft))
        cb = {
            "id": "cb1",
            "data": f"action:send|num:{draft['customer_number']}|id:{draft_id}",
            "message": {"chat": {"id": self.CHAT_ID}, "message_id": 5,
                        "text": "draft message"},
        }
        telegram = Mock()
        sender_name = ("_send_instagram_reply"
                       if draft.get("channel") == "Instagram"
                       else "send_whatsapp_reply")
        with patch.object(glam, "TELEGRAM_CHAT_ID", str(self.CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             patch.object(glam, "_reassign_to_bot", Mock()), \
             patch.object(glam, sender_name, Mock(return_value=send_result)):
            glam._handle_telegram_callback(cb)
        return telegram

    def _telegram_texts(self, telegram: Mock) -> str:
        return " | ".join(
            str(call.args[1].get("text", ""))
            for call in telegram.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        )

    def _wati_draft(self, wa_id: str) -> dict:
        return {
            "customer_number": wa_id,
            "customer_name": "Cust",
            "customer_message": "how much?",
            "reply_text": "approved reply",
            "original_text": "orig",
        }

    def test_wati_approve_success(self):
        wa_id = f"9196{_uid()[:8]}"
        telegram = self._tap_send(self._wati_draft(wa_id), (True, ""))
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SENT"])
        self.assertEqual(len(glam._load_wati_history(wa_id)), 1)
        self.assertIn("✅ Sent to", self._telegram_texts(telegram))

    def test_wati_approve_failure(self):
        wa_id = f"9195{_uid()[:8]}"
        telegram = self._tap_send(
            self._wati_draft(wa_id), (False, "WATI rejected: boom")
        )
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SEND_FAILED"])
        self.assertIn("boom", rows[0][2])
        self.assertEqual(glam._load_wati_history(wa_id), [])
        texts = self._telegram_texts(telegram)
        self.assertIn("⚠️ Send FAILED", texts)
        self.assertNotIn("✅ Sent to", texts)

    def _ig_draft(self, sender_id: str) -> dict:
        return {
            "customer_number": sender_id,
            "customer_name": "",
            "customer_message": "price?",
            "reply_text": "ig approved reply",
            "original_text": "orig",
            "channel": "Instagram",
            "ig_timestamp": "1720000000",
        }

    def test_ig_approve_success(self):
        sender = f"igds{_uid()}"
        telegram = self._tap_send(self._ig_draft(sender), (True, ""))
        rows = _ig_rows(sender)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SENT_IG"])
        self.assertEqual(len(glam._load_instagram_history(sender)), 1)
        self.assertIn("✅ Sent to", self._telegram_texts(telegram))

    def test_ig_approve_failure(self):
        sender = f"igdf{_uid()}"
        telegram = self._tap_send(
            self._ig_draft(sender), (False, "token expired")
        )
        rows = _ig_rows(sender)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SEND_FAILED_IG"])
        self.assertEqual(glam._load_instagram_history(sender), [])
        self.assertIn("⚠️ Send FAILED", self._telegram_texts(telegram))


class EditCompletionSend(unittest.TestCase):
    """Telegram ✏️ Edit completion: same delivery-truth split."""

    CHAT_ID = 777002

    def _complete_edit(self, wa_id: str, send_result):
        draft_id = f"e{_uid()[:8]}"
        draft = {
            "customer_number": wa_id,
            "customer_name": "Cust",
            "customer_message": "original question",
            "reply_text": "pre-edit draft",
            "original_text": "orig",
            "awaiting_edit": True,
            "edit_started_at": time.time(),
            "telegram_chat_id": self.CHAT_ID,
            "telegram_message_id": 9,
        }
        self.assertTrue(glam._draft_register(draft_id, draft))
        msg = {"chat": {"id": self.CHAT_ID}, "text": "edited reply text"}
        telegram = Mock()
        with patch.object(glam, "TELEGRAM_CHAT_ID", str(self.CHAT_ID)), \
             patch.object(glam, "_telegram_api", telegram), \
             patch.object(glam, "_reassign_to_bot", Mock()), \
             patch.object(glam, "send_whatsapp_reply",
                          Mock(return_value=send_result)):
            glam._handle_telegram_message(msg)
        return telegram

    def _telegram_texts(self, telegram: Mock) -> str:
        return " | ".join(
            str(call.args[1].get("text", ""))
            for call in telegram.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        )

    def test_edit_send_success(self):
        wa_id = f"9194{_uid()[:8]}"
        telegram = self._complete_edit(wa_id, (True, ""))
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SENT"])
        self.assertEqual(rows[0][1], "edited reply text")
        self.assertEqual(len(glam._load_wati_history(wa_id)), 1)
        self.assertIn("✅ Sent your edit", self._telegram_texts(telegram))

    def test_edit_send_failure(self):
        wa_id = f"9193{_uid()[:8]}"
        telegram = self._complete_edit(wa_id, (False, "Network error: Timeout"))
        rows = _wati_rows(wa_id)
        self.assertEqual([r[0] for r in rows], ["DRAFT_SEND_FAILED"])
        self.assertEqual(glam._load_wati_history(wa_id), [])
        texts = self._telegram_texts(telegram)
        self.assertIn("⚠️ Send FAILED", texts)
        self.assertNotIn("✅ Sent your edit", texts)


if __name__ == "__main__":
    unittest.main()
