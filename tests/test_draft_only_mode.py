"""Draft-only mode (multi-brand task 4).

DRAFT_ONLY_MODE=1 sends every AUTO reply through the existing DRAFT+APPROVE
Telegram flow instead of to the customer — for a new brand's first days.
  - Instagram: held by app._ig_output_guard (the output-guard downgrade
    path graph.py shares; parity is in tests/test_graph_parity.py). The
    customer gets brain.md's handoff line, at most once per 30 minutes,
    and the approved reply when the founder taps Send.
  - WhatsApp (no output guard): the WATI webhook flips AUTO to
    DRAFT+APPROVE. The customer gets nothing until approval.
  - Off by default; 1/true/yes turn it on; OUTPUT_GUARD_DISABLED doesn't
    turn it off; DRAFT+APPROVE and ESCALATE are untouched.

No network: sends, Telegram and the model are stubbed.

Run:  python -m unittest tests.test_draft_only_mode
"""
import io, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-draft-only-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

REPLY = "GS1 is our lightest tray 🤍"


def env(**values):
    base = {"DRAFT_ONLY_MODE": "", "OUTPUT_GUARD_DISABLED": ""}
    base.update(values)
    return patch.dict(os.environ, base)


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()) as buf:
        result = fn(*a, **k)
    return result, buf.getvalue()


class GuardHelperTest(unittest.TestCase):
    def test_off_by_default(self):
        with env():
            self.assertFalse(glam._draft_only_mode())
            self.assertEqual(quiet(glam._ig_output_guard, "s1", "AUTO", REPLY)[0], "")

    def test_switch_values(self):
        for value, on in (("1", True), ("true", True), ("YES", True), (" 1 ", True),
                          ("0", False), ("no", False), ("", False)):
            with self.subTest(value=value), env(DRAFT_ONLY_MODE=value):
                self.assertIs(glam._draft_only_mode(), on)

    def test_holds_every_auto_reply(self):
        with env(DRAFT_ONLY_MODE="1"):
            note, out = quiet(glam._ig_output_guard, "s1", "AUTO", REPLY)
        self.assertEqual(note, glam.DRAFT_ONLY_NOTE)
        self.assertIn("Held AUTO reply to s1", out)

    def test_guard_reasons_are_added(self):
        with env(DRAFT_ONLY_MODE="1"):
            note, _ = quiet(glam._ig_output_guard, "s1", "AUTO", "Use code GLAM10 🤍")
        self.assertTrue(note.startswith(glam.DRAFT_ONLY_NOTE))
        self.assertIn("rule 2", note)

    def test_guard_kill_switch_does_not_turn_it_off(self):
        with env(DRAFT_ONLY_MODE="1", OUTPUT_GUARD_DISABLED="1"):
            note, _ = quiet(glam._ig_output_guard, "s1", "AUTO", "Use code GLAM10 🤍")
        self.assertEqual(note, glam.DRAFT_ONLY_NOTE)     # guard itself skipped

    def test_non_auto_and_empty_replies_untouched(self):
        with env(DRAFT_ONLY_MODE="1"):
            for classification, reply in (("DRAFT+APPROVE", REPLY), ("ESCALATE", REPLY), ("AUTO", "")):
                with self.subTest(classification=classification):
                    self.assertEqual(quiet(glam._ig_output_guard, "s1", classification, reply)[0], "")


class InstagramCustomerTest(unittest.TestCase):
    """What an Instagram customer actually receives while replies wait."""

    CUSTOMER = "1780000000000901"

    def setUp(self):
        glam._init_db()
        conn = sqlite3.connect(glam.DB_PATH)
        for table in ("instagram_logs", "paused_senders", "pending_drafts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit(); conn.close()
        self.sent, self.drafts = [], []
        for p in (
            patch.object(glam, "INSTAGRAM_PAGE_ID", ""),
            patch.object(glam, "draft_reply_logic", Mock(return_value=("AUTO", REPLY, ""))),
            patch.object(glam, "_send_instagram_reply",
                         lambda sid, text: (self.sent.append(text), (True, ""))[1]),
            patch.object(glam, "send_draft_for_approval",
                         lambda **k: (self.drafts.append(k), True)[1]),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda s: ""),
            patch.object(glam, "_persist_seen_id", lambda m: None),
            patch.object(glam, "send_telegram_notification", Mock()),
            patch.object(glam, "_alert_send_failure", Mock()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def dm(self, text, mid):
        quiet(glam._process_instagram_event, {
            "sender": {"id": self.CUSTOMER}, "recipient": {"id": "page"},
            "timestamp": int(time.time() * 1000), "message": {"mid": mid, "text": text},
        })

    def test_customer_gets_the_handoff_line_once_then_waits(self):
        stamp = time.time()
        with env(DRAFT_ONLY_MODE="1"):
            self.dm("which tray is lightest?", f"m-do-1-{stamp}")
            self.dm("and the price?", f"m-do-2-{stamp}")
        # One handoff line for the burst; the drafted reply never went out.
        self.assertEqual(self.sent, [glam.BRAIN_HOLDING_LINE])
        self.assertEqual([d["reply_text"] for d in self.drafts], [REPLY, REPLY])
        self.assertTrue(all(d["channel"] == "Instagram" for d in self.drafts))
        self.assertTrue(all("DRAFT_ONLY_MODE" in d["guard_note"] for d in self.drafts))

    def test_off_sends_the_reply(self):
        with env():
            self.dm("which tray is lightest?", f"m-do-off-{time.time()}")
        self.assertEqual(self.sent, [REPLY])
        self.assertEqual(self.drafts, [])


class WhatsAppTest(unittest.TestCase):
    WA_ID = "919000000777"

    def setUp(self):
        self.sent, self.drafts, self.logged = [], [], []
        self.classification = "AUTO"
        for p in (
            patch.object(glam, "_verify_wati_token", lambda t: True),
            patch.object(glam, "_is_paused", lambda w: False),
            patch.object(glam, "_udit_replied_recently", lambda *a, **k: False),
            patch.object(glam, "_check_recent_human_reply", lambda w: False),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "draft_reply_logic",
                         lambda *a, **k: (self.classification, REPLY, "")),
            patch.object(glam, "send_whatsapp_reply",
                         lambda wa, text: (self.sent.append(text), (True, ""))[1]),
            patch.object(glam, "send_draft_for_approval",
                         lambda **k: (self.drafts.append(k), True)[1]),
            patch.object(glam, "send_telegram_notification", Mock()),
            patch.object(glam, "_log_message",
                         lambda *a, **k: self.logged.append(k.get("status"))),
            patch.object(glam, "_load_wati_history", lambda *a, **k: []),
            patch.object(glam, "_lookup_recent_order", lambda w: ""),
            patch.object(glam, "_persist_seen_id", lambda m: None),
            patch.object(glam, "_pause_number", lambda *a, **k: True),
            patch.object(glam, "_reassign_to_bot", lambda w: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def post(self):
        with redirect_stdout(io.StringIO()) as buf:
            glam.app.test_client().post("/webhook/x", json={
                "type": "text", "waId": self.WA_ID, "id": f"wa-do-{time.time()}",
                "text": "which tray is lightest?",
            })
        return buf.getvalue()

    def test_auto_waits_for_approval(self):
        with env(DRAFT_ONLY_MODE="1"):
            out = self.post()
        self.assertEqual(self.sent, [])                    # customer gets nothing yet
        (draft,) = self.drafts
        self.assertEqual(draft["reply_text"], REPLY)
        self.assertEqual(draft["customer_number"], self.WA_ID)
        self.assertEqual(draft["guard_note"], glam.DRAFT_ONLY_NOTE)
        self.assertEqual(self.logged, ["DRAFT"])
        self.assertIn("[DRAFT-ONLY]", out)

    def test_off_sends_as_before(self):
        with env():
            self.post()
        self.assertEqual(self.sent, [REPLY])
        self.assertEqual(self.drafts, [])
        self.assertEqual(self.logged, ["AUTO"])

    def test_draft_and_escalate_untouched(self):
        with env(DRAFT_ONLY_MODE="1"):
            self.classification = "DRAFT+APPROVE"
            self.post()
            self.assertEqual(self.drafts[-1]["guard_note"], "")   # a real draft, no draft-only note
            self.classification = "ESCALATE"
            self.post()
        self.assertEqual(self.logged, ["DRAFT", "ESCALATE"])
        self.assertEqual(len(self.drafts), 1)


class HealthzTest(unittest.TestCase):
    def test_keyed_healthz_reports_the_mode(self):
        client = glam.app.test_client()
        for value, expected in (("1", True), ("", False)):
            with self.subTest(value=value), env(DRAFT_ONLY_MODE=value):
                body = client.get("/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY}).get_json()
                self.assertIs(body["draft_only_mode"], expected)


if __name__ == "__main__":
    unittest.main()
