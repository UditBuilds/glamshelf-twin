"""Ignore other Instagram accounts' events (multi-brand task 3).

A Meta app connected to several Instagram accounts delivers all of their
events to every webhook. Each brand's copy now processes an event only
when INSTAGRAM_PAGE_ID is its recipient.id (a customer's DM to us) or its
sender.id (our own echo) — the same test the existing echo check relies
on. Anything else is skipped and logged once per other account, ids only,
never the message. No INSTAGRAM_PAGE_ID -> everything is processed, as
before (fail open). Kill switch: INSTAGRAM_ACCOUNT_FILTER_DISABLED=1.

Payloads follow Meta's Instagram webhook shapes (object "instagram",
entry[].id = the account, messaging[] with sender / recipient / timestamp
and message / read / reaction / message_edit), and are signed and POSTed
to the real /instagram-webhook route. Twin's pipeline and all sends are
stubbed — no network.

Run:  python -m unittest tests.test_ig_account_filter
"""
import hashlib, hmac, io, json, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-ig-filter-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SECRET = "test-app-secret"
GLAM_ID = "17841400000000001"     # this copy's INSTAGRAM_PAGE_ID
OTHER_ID = "17841400000000002"    # another brand's account on the same Meta app
CUSTOMER = "1780000000000311"     # an Instagram-scoped customer id
CUSTOMER_2 = "1780000000000312"


def ts() -> int:
    return int(time.time() * 1000)


def dm(account, customer, text, mid):
    """A customer's DM to `account`."""
    return {"sender": {"id": customer}, "recipient": {"id": account}, "timestamp": ts(),
            "message": {"mid": mid, "text": text}}


def echo(account, customer, text, mid):
    """`account`'s own message to `customer`, echoed back."""
    return {"sender": {"id": account}, "recipient": {"id": customer}, "timestamp": ts(),
            "message": {"mid": mid, "text": text, "is_echo": True}}


def read(account, customer):
    return {"sender": {"id": customer}, "recipient": {"id": account}, "timestamp": ts(),
            "read": {"mid": "m-read"}}


def reaction(account, customer):
    return {"sender": {"id": customer}, "recipient": {"id": account}, "timestamp": ts(),
            "reaction": {"mid": "m-react", "action": "react", "reaction": "love", "emoji": "❤️"}}


def edit(account, customer):
    return {"sender": {"id": customer}, "recipient": {"id": account}, "timestamp": ts(),
            "message_edit": {"mid": "m-edit", "text": "edited", "num_edit": 1}}


class AccountFilterTest(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "ig_sent_mids"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        glam._bot_recent_replies.clear()
        glam._ig_other_accounts_logged.clear()
        self.draft = Mock(return_value=("AUTO", "Hi! GS1 is our lightest tray 🤍", ""))
        self.sent = []
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ID", GLAM_ID),
            patch.object(glam, "INSTAGRAM_APP_SECRET", SECRET),
            patch.object(glam, "draft_reply_logic", self.draft),
            patch.object(glam, "_send_instagram_reply",
                         lambda sid, text: (self.sent.append((sid, text)), (True, ""))[1]),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda s: ""),
            patch.object(glam, "_alert_send_failure", Mock()),
            patch.object(glam, "send_telegram_notification", Mock()),
            patch.object(glam, "_telegram_api", Mock()),
            patch.dict(os.environ, {"INSTAGRAM_ACCOUNT_FILTER_DISABLED": ""}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def post(self, *events, entry_id=None) -> str:
        body = json.dumps({"object": "instagram", "entry": [{
            "time": ts(), "id": entry_id or GLAM_ID, "messaging": list(events),
        }]}).encode("utf-8")
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with redirect_stdout(io.StringIO()) as buf:
            resp = glam.app.test_client().post(
                "/instagram-webhook", data=body,
                headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        return buf.getvalue()

    def logs(self, sender):
        conn = sqlite3.connect(glam.DB_PATH)
        try:
            return conn.execute(
                "SELECT source, message_text, reply_text FROM instagram_logs WHERE sender_id = ?",
                (sender,),
            ).fetchall()
        finally:
            conn.close()

    # ---- Glam Shelf's own events are processed exactly as before ----

    def test_glamshelf_dm_is_answered(self):
        out = self.post(dm(GLAM_ID, CUSTOMER, "which tray is lightest?", "m-glam-1"))
        self.draft.assert_called_once()
        self.assertEqual(self.sent, [(CUSTOMER, "Hi! GS1 is our lightest tray 🤍")])
        self.assertNotIn("[IG-FILTER]", out)

    def test_glamshelf_bot_echo_is_recognised(self):
        glam._record_bot_outbound("Hi! GS1 is our lightest tray 🤍")
        out = self.post(echo(GLAM_ID, CUSTOMER, "Hi! GS1 is our lightest tray 🤍", "m-echo-1"))
        self.assertIn("[ECHO-IG]", out)
        self.assertFalse(glam._is_paused(CUSTOMER))

    def test_glamshelf_human_echo_still_pauses(self):
        out = self.post(echo(GLAM_ID, CUSTOMER, "Udit here, sending it today!", "m-human-1"))
        self.assertIn("[HUMAN_UDIT_IG]", out)
        self.assertTrue(glam._is_paused(CUSTOMER))
        self.assertEqual(self.logs(CUSTOMER)[0][0], "HUMAN_UDIT_INSTAGRAM")

    def test_glamshelf_non_text_events_reach_the_existing_handling(self):
        for event in (read(GLAM_ID, CUSTOMER), reaction(GLAM_ID, CUSTOMER), edit(GLAM_ID, CUSTOMER)):
            with self.subTest(kind=[k for k in event if k not in ("sender", "recipient", "timestamp")]):
                out = self.post(event)
                self.assertIn("Ignored non-text event", out)
                self.assertNotIn("[IG-FILTER]", out)
        self.draft.assert_not_called()

    # ---- another account's events are skipped ----

    def test_other_account_dm_is_skipped(self):
        out = self.post(dm(OTHER_ID, CUSTOMER, "my secret order question", "m-other-1"),
                        entry_id=OTHER_ID)
        self.draft.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.logs(CUSTOMER), [])
        self.assertIn("[IG-FILTER]", out)
        self.assertIn(OTHER_ID, out)
        self.assertNotIn("my secret order question", out)   # no content in the log
        self.assertNotIn("m-other-1", glam._seen_ids)       # not even deduped as ours

    def test_other_account_echo_does_not_pause_or_log(self):
        out = self.post(echo(OTHER_ID, CUSTOMER, "their founder typed this", "m-other-echo"),
                        entry_id=OTHER_ID)
        self.assertFalse(glam._is_paused(CUSTOMER))
        self.assertEqual(self.logs(CUSTOMER), [])
        self.assertNotIn("[HUMAN_UDIT_IG]", out)
        self.assertNotIn("their founder typed this", out)

    def test_other_account_non_text_events_are_skipped(self):
        out = self.post(read(OTHER_ID, CUSTOMER), reaction(OTHER_ID, CUSTOMER), edit(OTHER_ID, CUSTOMER),
                        entry_id=OTHER_ID)
        self.assertNotIn("Ignored non-text event", out)

    def test_logged_once_per_other_account(self):
        out = self.post(dm(OTHER_ID, CUSTOMER, "one", "m-o-1"), dm(OTHER_ID, CUSTOMER_2, "two", "m-o-2"),
                        echo(OTHER_ID, CUSTOMER, "three", "m-o-3"), entry_id=OTHER_ID)
        self.assertEqual(out.count("[IG-FILTER]"), 1)

    def test_mixed_batch_only_answers_our_customer(self):
        self.post(dm(OTHER_ID, CUSTOMER_2, "other brand", "m-mix-1"),
                  dm(GLAM_ID, CUSTOMER, "our customer", "m-mix-2"))
        self.assertEqual([c.args[0] for c in self.draft.call_args_list], ["our customer"])
        self.assertEqual([sid for sid, _ in self.sent], [CUSTOMER])

    def test_missing_ids_are_skipped(self):
        out = self.post({"timestamp": ts(), "message": {"mid": "m-noid", "text": "hello"}})
        self.draft.assert_not_called()
        self.assertIn("[IG-FILTER]", out)

    # ---- fail open / kill switch ----

    def test_no_page_id_processes_everything(self):
        with patch.object(glam, "INSTAGRAM_PAGE_ID", ""):
            out = self.post(dm(OTHER_ID, CUSTOMER, "no page id set", "m-open-1"), entry_id=OTHER_ID)
        self.draft.assert_called_once()
        self.assertNotIn("[IG-FILTER]", out)

    def test_kill_switch_processes_everything(self):
        for value in ("1", "true", "yes"):
            with self.subTest(value=value), \
                 patch.dict(os.environ, {"INSTAGRAM_ACCOUNT_FILTER_DISABLED": value}):
                self.draft.reset_mock()
                out = self.post(dm(OTHER_ID, CUSTOMER, "kill switch", f"m-kill-{value}"),
                                entry_id=OTHER_ID)
                self.draft.assert_called_once()
                self.assertNotIn("[IG-FILTER]", out)

    def test_rule_matches_the_echo_check(self):
        # The filter's notion of "ours" is exactly the two ids the echo
        # check and a normal DM use.
        self.assertTrue(glam._ig_event_is_ours(CUSTOMER, GLAM_ID))    # DM to us
        self.assertTrue(glam._ig_event_is_ours(GLAM_ID, CUSTOMER))    # our echo
        self.assertFalse(glam._ig_event_is_ours(CUSTOMER, OTHER_ID))
        self.assertFalse(glam._ig_event_is_ours(OTHER_ID, CUSTOMER))
        self.assertFalse(glam._ig_event_is_ours("", ""))


if __name__ == "__main__":
    unittest.main()
