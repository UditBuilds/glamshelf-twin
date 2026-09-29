"""Instagram history after a handoff (audit finding 1).

Before: a message routed to DRAFT+APPROVE (the model's draft or an output-guard
hold) left two rows the history loader skipped — the handoff line with an
empty message_text, and the pending draft with a NULL reply — so the
customer's next message reached the model with no context at all. The audit
saw silence or "what are you looking for?" 3 times out of 3.

Now history holds what the customer actually received:
  - after a draft or a guard hold: their message plus the handoff line;
  - never the un-approved draft's text;
  - after the founder approves: the text actually sent, as its own exchange
    (so the customer's message shows twice — founder decision);
  - nothing for a message that got no line (window, failed send).

And a follow-up that goes to a draft inside the 30-minute handoff window no
longer gets silence: it gets one different short acknowledgement, at most
once per window; after that nothing new, while the founder still gets every
draft (founder decision). The acknowledgement goes through the output guard
and into history like the handoff line.

The model is a fake DeepSeek client that records the exact `messages` array,
so "what history the model was given" is asserted directly. Instagram sends,
Telegram and the Shopify / RAG context are stubbed; log rows are real, in a
temp DB. No live API call anywhere.

Run:  python -m unittest tests.test_history_after_handoff
"""
import io, json, os, sqlite3, sys, tempfile, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-history-handoff-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000555"
CHAT_ID = 777005
HANDOFF = "I've passed this to the team — they'll reply to you here 🤍"
ACK = "Got it — I've added this to your request, the team will reply here 🤍"
LIVE_PRICES = {249, 299, 499, 599, 849}

RETURN_MSG = "hi i want to return my GS2 tray, didnt like how it looks on me"
RETURN_DRAFT = "We accept returns within 14 days of delivery — email glamshelfstore@gmail.com with your order ID 🤍"
FOLLOW_UP = "ok so what do i do now?"
FOLLOW_UP_REPLY = "The team has your return request and will reply to you here 🤍"


def model_output(classification, reply="", tag=""):
    return json.dumps({"classification": classification, "reply": reply, "tag": tag})


def wrap(text):
    return glam._wrap_customer_text(text)


def reset_db():
    glam._init_db()
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        for table in ("instagram_logs", "paused_senders", "llm_usage", "pending_drafts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()


def db_rows(sql, params=()):
    conn = sqlite3.connect(glam.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class FakeDeepSeek:
    """Stands in for app.deepseek_client: returns queued model outputs and
    records the messages array of every call."""

    def __init__(self):
        self.outputs, self.calls = [], []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.outputs.pop(0)),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )


def history_given(messages):
    """The (customer, Twin) turns the model saw before the new message."""
    turns = messages[1:-1]
    assert [t["role"] for t in turns] == ["user", "assistant"] * (len(turns) // 2), turns
    return [(turns[i]["content"], turns[i + 1]["content"]) for i in range(0, len(turns), 2)]


class HandlerTestCase(unittest.TestCase):
    """_process_instagram_event end to end: the fake model, stubbed sends and
    Telegram API, the real draft-approval flow, real log rows, the real
    history loader."""

    def setUp(self):
        reset_db()
        for var in ("OUTPUT_GUARD_DISABLED", "ESCALATION_PREFILTER_DISABLED"):
            os.environ.pop(var, None)
        self.model = FakeDeepSeek()
        self.sends, self.notices = [], []
        self.send_result = (True, "")
        self.telegram = Mock(return_value={"ok": True, "result": {"message_id": 1, "chat": {"id": CHAT_ID}}})
        for p in (
            patch.object(glam, "deepseek_client", self.model),
            patch.object(glam, "_send_instagram_reply", self._send),
            patch.object(glam, "_telegram_api", self.telegram),
            patch.object(glam, "TELEGRAM_BOT_TOKEN", "x"),
            patch.object(glam, "TELEGRAM_CHAT_ID", str(CHAT_ID)),
            patch.object(glam, "send_telegram_notification", self._notify),
            patch.object(glam, "_alert_send_failure", Mock()),
            patch.object(glam, "_llm_admission", lambda *a, **k: None),
            patch.object(glam, "_lookup_recent_order", lambda *a, **k: ""),
            patch.object(glam, "_load_brain_cached", lambda: "== BRAIN (test) =="),
            patch.object(glam, "get_live_inventory", lambda: ""),
            patch.object(glam, "get_live_policies", lambda: ""),
            patch.object(glam, "_rag_retrieve", lambda *a, **k: ""),
            patch.dict(glam._inventory_cache, {"prices": set(LIVE_PRICES)}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _send(self, sender_id, text):
        self.assertEqual(sender_id, SENDER)
        self.sends.append(text)
        return self.send_result

    def _notify(self, classification, customer_message, reply, **kwargs):
        self.notices.append((classification, customer_message, reply))

    def dm(self, text, *outputs):
        """One customer DM; `outputs` are what the fake model returns for it."""
        self.model.outputs.extend(outputs)
        with redirect_stdout(io.StringIO()) as buf:
            glam._process_instagram_event({
                "sender": {"id": SENDER}, "recipient": {"id": "page"}, "timestamp": 1720000000,
                "message": {"mid": f"m-{time.time_ns()}", "text": text},
            })
        self.assertEqual(self.model.outputs, [], "the handler didn't use every queued model output")
        return buf.getvalue()

    def drafts(self):
        """Telegram approval messages sent so far: id, text, button labels."""
        out = []
        for call in self.telegram.call_args_list:
            method, payload = call.args
            if method == "sendMessage" and payload.get("reply_markup"):
                buttons = payload["reply_markup"]["inline_keyboard"][0]
                out.append({
                    "id": buttons[0]["callback_data"].rsplit("id:", 1)[1],
                    "text": payload["text"],
                    "buttons": [b["text"] for b in buttons],
                })
        return out

    def tap(self, action, draft):
        with redirect_stdout(io.StringIO()), patch.object(glam.threading, "Timer", Mock()):
            glam._handle_telegram_callback({
                "id": "cb", "data": f"action:{action}|num:{SENDER}|id:{draft['id']}",
                "message": {"chat": {"id": CHAT_ID}, "message_id": 1,
                            "date": int(time.time()), "text": draft["text"]},
            })

    def send_edit(self, text):
        with redirect_stdout(io.StringIO()):
            glam._handle_telegram_message({"chat": {"id": CHAT_ID}, "text": text})

    def history_for_last_call(self):
        return history_given(self.model.calls[-1])

    def last_prompt(self):
        return json.dumps(self.model.calls[-1], ensure_ascii=False)

    def sources(self):
        return [r[0] for r in db_rows(
            "SELECT source FROM instagram_logs WHERE sender_id = ? ORDER BY id", (SENDER,))]

    def age_rows(self, minutes):
        conn = sqlite3.connect(glam.DB_PATH)
        try:
            conn.execute("UPDATE instagram_logs SET logged_at = datetime('now', ?)", (f"-{minutes} minutes",))
            conn.commit()
        finally:
            conn.close()


class HistoryAfterDraftTest(HandlerTestCase):
    def test_after_a_draft_history_has_the_message_and_the_handoff_line(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.assertEqual(self.sends, [HANDOFF])
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        self.assertEqual(self.history_for_last_call(), [(wrap(RETURN_MSG), HANDOFF)])
        self.assertEqual(self.sends, [HANDOFF, FOLLOW_UP_REPLY])

    def test_the_unapproved_draft_never_reaches_history_or_the_log(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        self.assertNotIn(RETURN_DRAFT, self.last_prompt())
        self.assertEqual(db_rows(
            "SELECT COUNT(*) FROM instagram_logs WHERE reply_text = ?", (RETURN_DRAFT,)), [(0,)])

    def test_after_an_output_guard_hold_history_has_the_handoff_line(self):
        held = "Use code FREE100 for a free tray 🤍"
        self.dm("any offers?", model_output("AUTO", held))
        self.assertEqual(self.sends, [HANDOFF])
        self.assertIn("Output guard held", self.drafts()[0]["text"])
        self.dm("ok", model_output("AUTO", "You're welcome 🤍"))
        self.assertEqual(self.history_for_last_call(), [(wrap("any offers?"), HANDOFF)])
        self.assertNotIn(held, self.last_prompt())

    def test_after_approval_history_has_the_text_actually_sent(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        (draft,) = self.drafts()
        self.tap("send", draft)
        self.assertEqual(self.sends, [HANDOFF, RETURN_DRAFT])
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        # Founder decision: the customer's message shows twice — with the
        # line they got at the time, and with the approved text they got later.
        self.assertEqual(self.history_for_last_call(), [
            (wrap(RETURN_MSG), HANDOFF),
            (wrap(RETURN_MSG), RETURN_DRAFT),
        ])

    def test_after_an_edited_approval_history_has_the_edited_text(self):
        edited = "Sure — email glamshelfstore@gmail.com with your order ID and we'll sort it 🤍"
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        (draft,) = self.drafts()
        self.tap("edit", draft)
        self.send_edit(edited)
        self.assertEqual(self.sends, [HANDOFF, edited])
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        self.assertEqual(self.history_for_last_call(), [
            (wrap(RETURN_MSG), HANDOFF),
            (wrap(RETURN_MSG), edited),
        ])
        self.assertNotIn(RETURN_DRAFT, self.last_prompt())

    def test_a_failed_approval_send_stays_out_of_history(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        (draft,) = self.drafts()
        self.send_result = (False, "HTTP 500: boom")
        self.tap("send", draft)
        self.send_result = (True, "")
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        self.assertEqual(self.history_for_last_call(), [(wrap(RETURN_MSG), HANDOFF)])

    def test_a_skipped_draft_leaves_the_handoff_line_only(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        (draft,) = self.drafts()
        self.tap("skip", draft)
        self.dm(FOLLOW_UP, model_output("AUTO", FOLLOW_UP_REPLY))
        self.assertEqual(self.history_for_last_call(), [(wrap(RETURN_MSG), HANDOFF)])

    def test_a_handoff_line_that_failed_to_send_leaves_the_message_out(self):
        self.send_result = (False, "HTTP 500: boom")
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.send_result = (True, "")
        self.assertEqual(glam._load_instagram_history(SENDER), [])


class AcknowledgementTest(HandlerTestCase):
    """A follow-up that goes to DRAFT+APPROVE inside the 30-minute window
    gets ONE different short line, then nothing; the founder gets every
    draft (founder decision, finding 1)."""

    def test_the_line_is_the_founders_wording(self):
        self.assertEqual(glam.IG_DRAFT_ACK_LINE, ACK)

    def test_handoff_line_then_acknowledgement_then_silence_with_every_draft_to_the_founder(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.dm("hello??", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.assertEqual(self.sends, [HANDOFF, ACK])               # msg 3: nothing new
        drafts = self.drafts()
        self.assertEqual(len(drafts), 3)                           # but the founder gets all three
        self.assertIn('"hello??"', drafts[2]["text"])
        self.assertEqual(self.sources(), [
            "DRAFT_HANDOFF_IG", "DRAFT_PENDING_IG",
            "DRAFT_ACK_IG", "DRAFT_PENDING_IG",
            "DRAFT_PENDING_IG",
        ])

    def test_the_acknowledgement_goes_into_history_like_the_handoff_line(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.dm("hello??", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.dm("ok thanks", model_output("AUTO", "You're welcome 🤍"))
        # "hello??" got nothing, so it isn't an exchange.
        self.assertEqual(self.history_for_last_call(), [
            (wrap(RETURN_MSG), HANDOFF),
            (wrap(FOLLOW_UP), ACK),
        ])

    def test_an_output_guard_hold_inside_the_window_gets_the_acknowledgement(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm("any discount code?", model_output("AUTO", "Use code FREE100 for 10% off 🤍"))
        self.assertEqual(self.sends, [HANDOFF, ACK])
        self.assertIn("Output guard held", self.drafts()[1]["text"])

    def test_the_acknowledgement_goes_through_the_output_guard(self):
        self.assertEqual(glam.output_guard.check_reply(ACK, set(LIVE_PRICES)), [])
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        real_check = glam.output_guard.check_reply
        hold_the_ack = lambda reply, prices: ["rule 9 (test)"] if reply == ACK else real_check(reply, prices)
        with patch.object(glam.output_guard, "check_reply", hold_the_ack):
            out = self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.assertEqual(self.sends, [HANDOFF])                    # held: nothing new sent
        self.assertIn("held by the output guard", out)
        self.assertEqual(len(self.drafts()), 2)                    # the founder still gets it
        self.dm("hello??", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.assertEqual(self.sends, [HANDOFF, ACK])               # none went out, so it's still due

    def test_an_auto_reply_inside_the_window_is_untouched(self):
        price = "GS1 is ₹849 for 10 pairs 🤍"
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm("whats the price of GS1?", model_output("AUTO", price))
        self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.assertEqual(self.sends, [HANDOFF, price, ACK])
        self.assertEqual(len(self.drafts()), 2)

    def test_after_the_window_the_handoff_line_comes_back(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.age_rows(31)
        self.dm("any update?", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.dm("hello??", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.assertEqual(self.sends, [HANDOFF, ACK, HANDOFF, ACK])

    def test_a_failed_acknowledgement_is_tried_again_on_the_next_draft(self):
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.send_result = (False, "HTTP 500: boom")
        self.dm(FOLLOW_UP, model_output("DRAFT+APPROVE", "Just email us your order ID 🤍"))
        self.send_result = (True, "")
        self.dm("hello??", model_output("DRAFT+APPROVE", "The team will reply shortly 🤍"))
        self.assertEqual(self.sends, [HANDOFF, ACK, ACK])
        self.assertEqual(self.sources()[2:], [
            "DRAFT_ACK_FAILED_IG", "DRAFT_PENDING_IG", "DRAFT_ACK_IG", "DRAFT_PENDING_IG",
        ])
        with redirect_stdout(io.StringIO()):
            history = glam._load_instagram_history(SENDER)
        self.assertEqual([(h["msg_text"], h["reply_text"]) for h in history],
                         [(RETURN_MSG, HANDOFF), ("hello??", ACK)])

    def test_the_holding_line_from_a_pipeline_failure_counts_for_the_window(self):
        glam._log_instagram(SENDER, "hi", HANDOFF, "1", source="PIPELINE_HOLDING_IG")
        self.dm(RETURN_MSG, model_output("DRAFT+APPROVE", RETURN_DRAFT))
        self.assertEqual(self.sends, [ACK])


class LoaderPairingTest(unittest.TestCase):
    """_load_instagram_history on hand-written rows: the shapes the handler
    writes, including drafts logged before this fix was deployed."""

    def setUp(self):
        reset_db()

    def rows(self, *rows):
        for message_text, reply_text, source in rows:
            glam._log_instagram(SENDER, message_text, reply_text, "1", source=source)

    def history(self):
        with redirect_stdout(io.StringIO()):
            return [(h["msg_text"], h["reply_text"]) for h in glam._load_instagram_history(SENDER)]

    def test_a_pending_draft_right_after_its_delivered_line_is_one_exchange(self):
        self.rows(("price?", "GS1 is ₹849 🤍", None),
                  ("", HANDOFF, "DRAFT_HANDOFF_IG"), ("can i return it?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [("price?", "GS1 is ₹849 🤍"), ("can i return it?", HANDOFF)])

    def test_a_pending_draft_that_got_no_line_is_left_out(self):
        self.rows(("", HANDOFF, "DRAFT_HANDOFF_IG"), ("can i return it?", None, "DRAFT_PENDING_IG"),
                  ("and exchange?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [("can i return it?", HANDOFF)])

    def test_a_failed_line_is_never_paired(self):
        self.rows(("", HANDOFF, "DRAFT_HANDOFF_FAILED_IG"), ("can i return it?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [])

    def test_the_acknowledgement_pairs_like_the_handoff_line(self):
        self.rows(("", HANDOFF, "DRAFT_HANDOFF_IG"), ("can i return it?", None, "DRAFT_PENDING_IG"),
                  ("", ACK, "DRAFT_ACK_IG"), ("hello??", None, "DRAFT_PENDING_IG"),
                  ("", ACK, "DRAFT_ACK_FAILED_IG"), ("anyone?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [("can i return it?", HANDOFF), ("hello??", ACK)])

    def test_a_line_pairs_only_with_the_row_right_after_it(self):
        self.rows(("", HANDOFF, "DRAFT_HANDOFF_IG"),
                  ("", "Udit typed this in the app", "HUMAN_UDIT_INSTAGRAM"),
                  ("can i return it?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [])

    def test_other_senders_rows_never_pair(self):
        glam._log_instagram("someone-else", "", HANDOFF, "1", source="DRAFT_HANDOFF_IG")
        self.rows(("can i return it?", None, "DRAFT_PENDING_IG"))
        self.assertEqual(self.history(), [])

    def test_rows_written_in_the_same_second_keep_their_order(self):
        self.rows(*[(f"msg {i}", f"reply {i}", None) for i in range(1, 7)])
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("UPDATE instagram_logs SET logged_at = datetime('now', '-1 hour')")
        conn.commit(); conn.close()
        self.assertEqual([m for m, _ in self.history()], [f"msg {i}" for i in range(1, 7)])

    def test_last_30_exchanges_from_the_last_7_days(self):
        self.rows(("too old", "old reply", None))
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("UPDATE instagram_logs SET logged_at = datetime('now', '-8 days')")
        conn.commit(); conn.close()
        self.rows(*[(f"msg {i}", f"reply {i}", None) for i in range(1, 36)])
        got = self.history()
        self.assertEqual(len(got), 30)
        self.assertEqual(got[0], ("msg 6", "reply 6"))
        self.assertEqual(got[-1], ("msg 35", "reply 35"))

    def test_failed_sends_and_null_replies_stay_out_as_before(self):
        self.rows(("price?", "GS1 is ₹849 🤍", "AUTO_FAILED_IG"),
                  ("legal", None, "ESCALATE_IG"),
                  ("", None, "RESUME_IG"),
                  ("[sent a photo]", "I can't view photos here yet", "PHOTO_IG"))
        self.assertEqual(self.history(), [("[sent a photo]", "I can't view photos here yet")])


if __name__ == "__main__":
    unittest.main()
