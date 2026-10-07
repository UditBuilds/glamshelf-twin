"""TWIN_USE_LANGGRAPH: graph.py answering Instagram text DMs behind a flag
that is OFF by default (PR 2 of 4 of the LangGraph plan).

With the flag OFF, app.py never imports graph or langgraph and the legacy
handler answers everything. With it ON, _process_instagram_event keeps its
own gates and media handling and, right before the rate limit, hands a text
DM to graph.handle_instagram_message(..., gates_done=True). graph.py's
generate node is draft_reply_logic itself.

Every test here passes on both CI legs (TWIN_USE_LANGGRAPH=0 and =1). The
in-process tests pick the reply path by setting app._twin_graph, which is
what the real switch (_use_langgraph) reads: the graph module for "graph",
None for "legacy" (what a flag-OFF startup leaves). The subprocess tests
set TWIN_USE_LANGGRAPH themselves.

Run:  python -m unittest tests.test_langgraph_flag
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Same import preamble as the other test modules: point the DB at a temp
# file and disable GitHub backup BEFORE app's startup block runs.
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-langgraph-flag-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-app-password")
os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam
import graph as twin

SENDER = "17841400000000501"
OTHER = "17841400000000502"
TIMESTAMP = 1752800000000
MSG = "Do GS1 lashes suit hooded eyes?"

AUTO_REPLY = "Yes ma'am, GS1 works beautifully for hooded eyes 🤍"
AUTO_JSON = json.dumps({"classification": "AUTO", "reply": AUTO_REPLY})
DRAFT_JSON = json.dumps({"classification": "DRAFT+APPROVE", "reply": "So sorry about the mix-up — here's what we can do..."})
ESCALATE_JSON = json.dumps({"classification": "ESCALATE", "reply": "I'm looping in the founder 🤍"})
LEGAL_THREAT = "This is unacceptable, I will contact my lawyer about this order"

PHOTO = {"type": "image", "payload": {"url": "https://example.invalid/p.jpg"}}
VOICE = {"type": "audio", "payload": {"url": "https://example.invalid/v.mp4"}}
SHARE = {"type": "share", "payload": {"url": "https://instagram.com/p/x"}}

# The calls whose sequence (name, args, kwargs) must match between the two
# paths: the parity suite's list, plus draft_reply_logic (the same call on
# both paths now) and the two media handlers.
EFFECT_FNS = {
    "draft_reply_logic", "ask_claude", "_send_instagram_reply",
    "send_draft_for_approval", "send_telegram_notification", "_pause_number",
    "_log_instagram", "_persist_seen_id", "_alert_send_failure",
    "_ig_rate_limited", "_ig_unanswered", "_ig_username",
    "_handle_instagram_photo", "_handle_instagram_media",
}
GATE_FNS = ("_persist_seen_id", "_is_paused", "_udit_replied_recently_ig", "_llm_admission")


def dm(text=MSG, *, sender=SENDER, mid="", attachments=None):
    message = {"mid": mid}
    if text is not None:
        message["text"] = text
    if attachments:
        message["attachments"] = attachments
    return {
        "sender": {"id": sender}, "recipient": {"id": "17841400000000999"},
        "timestamp": TIMESTAMP, "message": message,
    }


def new_mid():
    return f"langgraph-flag-{uuid.uuid4().hex}"


def named(calls, name):
    return [c for c in calls if c[0] == name]


def effects(calls):
    return [c for c in calls if c[0] in EFFECT_FNS]


def keyed_healthz():
    return glam.app.test_client().get(
        "/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY}
    ).get_json()


class Harness(unittest.TestCase):
    """Runs the Instagram handler (or the webhook route) with every network
    and DB side effect replaced by a recorder, on a chosen reply path.
    draft_reply_logic stays real on both paths; the model call, the prompt
    sources and the sends are stubbed, as in the parity suite."""

    def setUp(self):
        # The prefilters read this per call; a leaked host value would hide
        # the behaviour under test.
        os.environ.pop("ESCALATION_PREFILTER_DISABLED", None)
        self.calls = []
        self.mids = []
        self.addCleanup(lambda: [glam._seen_ids.discard(m) for m in self.mids])

    def mid(self):
        m = new_mid()
        self.mids.append(m)
        return m

    def rec(self, name, ret=None, exc=None, fn=None):
        def f(*a, **k):
            self.calls.append((name, a, k))
            if exc is not None:
                raise exc
            return fn(*a, **k) if fn is not None else ret
        return f

    def stubs(self, *, llm=AUTO_JSON, llm_exc=None, paused=False, human=False,
              limited=None, send_ok=True, buttons_ok=True, order=None, brain_missing=False):
        send = (True, "") if send_ok else (False, "HTTP 400: token expired")
        returns = {
            "_is_paused": paused,
            "_udit_replied_recently_ig": human,
            "_llm_admission": limited,
            "_ig_rate_limited": None,
            "_ig_unanswered": None,
            "_persist_seen_id": None,
            "_load_instagram_history": [],
            "_load_brain_cached": "== BRAIN v-test ==",
            "get_live_inventory": "",
            "get_live_policies": "",
            "_rag_retrieve": "",
            "_send_instagram_reply": send,
            "_ig_username": "",
            "send_draft_for_approval": buttons_ok,
            "send_telegram_notification": None,
            "_pause_number": None,
            "_log_instagram": None,
            "_alert_send_failure": None,
            "_handle_instagram_photo": None,
            "_handle_instagram_media": None,
        }
        patches = [patch.object(glam, n, self.rec(n, r)) for n, r in returns.items()]
        patches += [
            patch.object(glam, "_lookup_recent_order", self.rec("_lookup_recent_order", "", fn=order)),
            patch.object(glam, "ask_claude", self.rec(
                "ask_claude", llm, exc=llm_exc, fn=llm if callable(llm) else None)),
            # Spies: the real functions, recorded.
            patch.object(glam, "draft_reply_logic", self.rec("draft_reply_logic", fn=glam.draft_reply_logic)),
            patch.object(twin, "handle_instagram_message",
                         self.rec("handle_instagram_message", fn=twin.handle_instagram_message)),
        ]
        if brain_missing:
            patches.append(patch.object(
                glam, "BRAIN_FILE", Path(tempfile.gettempdir()) / "glamshelf-no-such-brain.md"))
        return patches

    def drive(self, path, action, **stub_kwargs):
        """Do `action()` on `path` — "graph", "legacy", or None to keep the
        reply path the loader left — and return (calls, stdout lines)."""
        self.calls = []
        out = io.StringIO()
        with ExitStack() as stack:
            for p in self.stubs(**stub_kwargs):
                stack.enter_context(p)
            if path is not None:
                stack.enter_context(patch.object(glam, "_twin_graph", twin if path == "graph" else None))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(io.StringIO()))   # tracebacks
            action()
        return self.calls, out.getvalue().splitlines()

    def process(self, path, events, **stub_kwargs):
        return self.drive(path, lambda: [glam._process_instagram_event(e) for e in events], **stub_kwargs)

    def post(self, *events):
        """The real /instagram-webhook route, so _with_reply_budget runs."""
        with patch.dict(os.environ, {"INSTAGRAM_WEBHOOK_VERIFY_DISABLED": "1"}):
            resp = glam.app.test_client().post(
                "/instagram-webhook", json={"entry": [{"messaging": list(events)}]})
        self.assertEqual(resp.status_code, 200)


# ---- the flag and the startup loader ----

class FlagAndLoader(unittest.TestCase):
    OFF_VALUES = (None, "", "0", "true", "TRUE", "yes", "on", " 1", "1 ", "01", "2")

    def load(self, value, graph_module):
        """Run the startup loader with TWIN_USE_LANGGRAPH=value (None: unset),
        where `import graph` finds `graph_module` (None: the import fails)."""
        out = io.StringIO()
        with patch.dict(os.environ), patch.dict(sys.modules, {"graph": graph_module}), \
                patch.object(glam, "_twin_graph", None), patch.object(glam, "_twin_graph_error", None), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            os.environ.pop("TWIN_USE_LANGGRAPH", None)
            if value is not None:
                os.environ["TWIN_USE_LANGGRAPH"] = value
            glam._load_twin_graph()
            return glam._twin_graph, glam._twin_graph_error, glam._use_langgraph(), out.getvalue()

    def test_only_exactly_1_is_on(self):
        for value in self.OFF_VALUES:
            with self.subTest(value=value), patch.dict(os.environ):
                os.environ.pop("TWIN_USE_LANGGRAPH", None)
                if value is not None:
                    os.environ["TWIN_USE_LANGGRAPH"] = value
                self.assertFalse(glam._langgraph_flag_on())
        with patch.dict(os.environ, {"TWIN_USE_LANGGRAPH": "1"}):
            self.assertTrue(glam._langgraph_flag_on())

    def test_off_never_tries_to_import_graph(self):
        # sys.modules["graph"] = None makes any import attempt fail loudly,
        # so no error and no log line means no attempt was made.
        for value in self.OFF_VALUES:
            with self.subTest(value=value):
                loaded, error, on, out = self.load(value, graph_module=None)
                self.assertIsNone(loaded)
                self.assertIsNone(error)
                self.assertFalse(on)
                self.assertEqual(out, "")

    def test_on_loads_the_graph_and_turns_the_switch_on(self):
        loaded, error, on, out = self.load("1", graph_module=twin)
        self.assertIs(loaded, twin)
        self.assertIsNone(error)
        self.assertTrue(on)
        self.assertIn("[LANGGRAPH] TWIN_USE_LANGGRAPH=1: graph.py answers Instagram text DMs", out)


class ImportFailureKeepsTheLegacyPath(Harness):
    """Flag ON but graph.py can't be imported: startup carries on, the error
    is logged loudly and shown in the keyed /healthz, the old path answers."""

    def _load_failing(self, stack):
        stack.enter_context(patch.dict(os.environ, {"TWIN_USE_LANGGRAPH": "1"}))
        stack.enter_context(patch.object(glam, "_twin_graph", None))
        stack.enter_context(patch.object(glam, "_twin_graph_error", None))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            glam._load_twin_graph()          # must not raise
        return out.getvalue()

    def _assert_legacy_answers(self, error_prefix, out):
        self.assertIsNone(glam._twin_graph)
        self.assertFalse(glam._use_langgraph())
        self.assertTrue(glam._twin_graph_error.startswith(error_prefix), glam._twin_graph_error)
        self.assertIn(f"[LANGGRAPH] TWIN_USE_LANGGRAPH=1 but graph.py FAILED to load: {glam._twin_graph_error}", out)
        self.assertIn("[LANGGRAPH] Instagram replies stay on the legacy path", out)

        calls, _ = self.process(None, [dm(mid=self.mid())])
        self.assertEqual(named(calls, "handle_instagram_message"), [])
        self.assertEqual(len(named(calls, "draft_reply_logic")), 1)
        (send,) = named(calls, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, AUTO_REPLY))

        data = keyed_healthz()
        self.assertEqual(data["reply_path"], "legacy")
        self.assertEqual(data["langgraph_error"], glam._twin_graph_error)

    def test_an_import_error(self):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"graph": None}))
            out = self._load_failing(stack)
            # An ImportError subclass: "import of graph halted; None in sys.modules".
            self._assert_legacy_answers("ModuleNotFoundError: ", out)

    def test_any_other_error_while_importing(self):
        fake_dir = tempfile.mkdtemp(prefix="glamshelf-broken-graph-")
        Path(fake_dir, "graph.py").write_text(
            'raise RuntimeError("graph.py broke while importing")\n', encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules))
            stack.enter_context(patch.object(sys, "path", [fake_dir] + sys.path))
            sys.modules.pop("graph", None)
            out = self._load_failing(stack)
            self._assert_legacy_answers("RuntimeError: graph.py broke while importing", out)


# ---- which path answers what ----

class TextDMsGoThroughTheGraph(Harness):
    def test_flag_on_a_text_dm_is_answered_by_the_graph(self):
        mid = self.mid()
        calls, _ = self.process("graph", [dm(mid=mid)])
        (handed,) = named(calls, "handle_instagram_message")
        self.assertEqual(handed[1], (SENDER, MSG))
        self.assertEqual(handed[2], {"msg_id": mid, "timestamp": str(TIMESTAMP), "gates_done": True})
        # generate is draft_reply_logic, called inside the graph with the
        # handler's arguments.
        (draft,) = named(calls, "draft_reply_logic")
        self.assertEqual(draft[1], (MSG, ""))
        self.assertEqual(draft[2], {"history": [], "source": "Instagram DM"})
        names = [c[0] for c in calls]
        self.assertLess(names.index("handle_instagram_message"), names.index("draft_reply_logic"))
        (send,) = named(calls, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, AUTO_REPLY))

    def test_flag_off_the_graph_is_never_called(self):
        calls, _ = self.process("legacy", [dm(mid=self.mid())])
        self.assertEqual(named(calls, "handle_instagram_message"), [])
        self.assertEqual(len(named(calls, "draft_reply_logic")), 1)
        (send,) = named(calls, "_send_instagram_reply")
        self.assertEqual(send[1], (SENDER, AUTO_REPLY))


class MediaStaysInTheHandler(Harness):
    def test_photo_voice_note_and_shared_post(self):
        cases = (
            (PHOTO, "_handle_instagram_photo", (SENDER, str(TIMESTAMP))),
            (VOICE, "_handle_instagram_media", (SENDER, "voice", str(TIMESTAMP))),
            (SHARE, "_handle_instagram_media", (SENDER, "share", str(TIMESTAMP))),
        )
        for attachment, handler, args in cases:
            with self.subTest(attachment["type"]):
                calls, _ = self.process("graph", [dm(None, mid=self.mid(), attachments=[attachment])])
                (handled,) = named(calls, handler)
                self.assertEqual(handled[1], args)
                self.assertEqual(named(calls, "handle_instagram_message"), [])
                self.assertEqual(named(calls, "draft_reply_logic"), [])


class GatesRunOnce(Harness):
    """The handler runs dedup, the pause gate and the human-handling check;
    graph.py's intake must not repeat them (gates_done)."""

    def test_the_state_schema_declares_gates_done(self):
        # LangGraph drops input keys the schema doesn't declare; without
        # this, intake would re-run dedup and drop every message.
        self.assertIn("gates_done", twin.TwinState.__annotations__)

    def test_an_admitted_dm_runs_each_gate_once(self):
        calls, _ = self.process("graph", [dm(mid=self.mid())])
        for fn in GATE_FNS:
            self.assertEqual(len(named(calls, fn)), 1, fn)
        self.assertEqual(len(named(calls, "handle_instagram_message")), 1)
        self.assertEqual(len(named(calls, "_send_instagram_reply")), 1)

    def test_a_duplicate_mid_is_answered_once(self):
        mid = self.mid()
        calls, out = self.process("graph", [dm(mid=mid), dm(mid=mid)])
        self.assertEqual(len(named(calls, "handle_instagram_message")), 1)
        self.assertEqual(len(named(calls, "_persist_seen_id")), 1)
        self.assertEqual(len(named(calls, "_send_instagram_reply")), 1)
        self.assertEqual(out.count(f"[INSTAGRAM] Skipped: duplicate mid {mid}"), 1)

    def test_a_paused_sender_is_forwarded_once(self):
        calls, _ = self.process("graph", [dm(mid=self.mid())], paused=True)
        (unanswered,) = named(calls, "_ig_unanswered")
        self.assertEqual(unanswered[1], (SENDER, MSG, str(TIMESTAMP), "paused"))
        self.assertEqual(len(named(calls, "_is_paused")), 1)
        self.assertEqual(named(calls, "handle_instagram_message"), [])
        self.assertEqual(named(calls, "ask_claude"), [])

    def test_a_human_handled_sender_is_forwarded_once(self):
        calls, _ = self.process("graph", [dm(mid=self.mid())], human=True)
        (unanswered,) = named(calls, "_ig_unanswered")
        self.assertEqual(unanswered[1], (SENDER, MSG, str(TIMESTAMP), "human_handling"))
        self.assertEqual(len(named(calls, "_udit_replied_recently_ig")), 1)
        self.assertEqual(named(calls, "handle_instagram_message"), [])
        self.assertEqual(named(calls, "ask_claude"), [])

    def test_the_graph_applies_the_rate_limit_once(self):
        calls, _ = self.process("graph", [dm(mid=self.mid())], limited=glam.LIMIT_SENDER_WINDOW)
        self.assertEqual(len(named(calls, "handle_instagram_message")), 1)
        self.assertEqual(len(named(calls, "_llm_admission")), 1)
        (limited,) = named(calls, "_ig_rate_limited")
        self.assertEqual(limited[1], (SENDER, MSG, str(TIMESTAMP), glam.LIMIT_SENDER_WINDOW))
        self.assertEqual(named(calls, "ask_claude"), [])

    def test_gates_done_starts_the_graph_at_the_rate_limit(self):
        # Called the way the handler calls it: the mid is already in
        # _seen_ids. Even a "paused" stub must not be consulted.
        mid = self.mid()
        glam._seen_ids.add(mid)
        states = []
        calls, _ = self.drive("graph", lambda: states.append(twin.handle_instagram_message(
            SENDER, MSG, msg_id=mid, timestamp=str(TIMESTAMP), gates_done=True)), paused=True, human=True)
        self.assertEqual(states[0]["decision"], "AUTO")
        for fn in ("_persist_seen_id", "_is_paused", "_udit_replied_recently_ig"):
            self.assertEqual(named(calls, fn), [], fn)
        self.assertEqual(len(named(calls, "_llm_admission")), 1)
        self.assertEqual(len(named(calls, "_send_instagram_reply")), 1)

    def test_without_gates_done_the_graph_runs_every_gate(self):
        mid = self.mid()
        glam._seen_ids.add(mid)
        states = []
        calls, _ = self.drive("graph", lambda: states.append(twin.handle_instagram_message(
            SENDER, MSG, msg_id=mid, timestamp=str(TIMESTAMP))))
        self.assertEqual(states[0]["drop_reason"], "duplicate_mid")
        self.assertEqual(named(calls, "_send_instagram_reply"), [])


# ---- what must work identically with the flag ON ----

class ReplyBudgetInsideTheGraph(Harness):
    def test_a_node_sees_the_request_s_deadline(self):
        # The model call happens inside the graph's generate node. The
        # budget is thread-local, so the node must run on the request's
        # thread: one deadline for every event of the request.
        seen = []

        def model(*a, **k):
            seen.append((glam._reply_budget.deadline, glam._budget_left(), threading.get_ident()))
            return AUTO_JSON

        with patch.object(glam, "_clock", lambda: 1000.0):
            calls, _ = self.drive("graph", lambda: self.post(
                dm(mid=self.mid()), dm("and GS2?", sender=OTHER, mid=self.mid())), llm=model)
        self.assertEqual(len(named(calls, "handle_instagram_message")), 2)
        self.assertEqual([s[:2] for s in seen], [(1050.0, 50.0), (1050.0, 50.0)])
        self.assertEqual({s[2] for s in seen}, {threading.get_ident()})
        # Cleared once the request is over.
        self.assertIsNone(getattr(glam._reply_budget, "deadline", None))


class WebhookBatches(Harness):
    def test_several_events_in_one_request(self):
        mid = self.mid()
        events = [
            dm(mid=mid),
            dm(None, mid=self.mid(), attachments=[PHOTO]),
            dm(mid=mid),                                   # Meta redelivery
            dm(None, sender=OTHER, mid=self.mid(), attachments=[VOICE]),
            dm("and GS2?", sender=OTHER, mid=self.mid()),
        ]
        calls, out = self.drive("graph", lambda: self.post(*events))
        self.assertEqual([c[1] for c in named(calls, "handle_instagram_message")],
                         [(SENDER, MSG), (OTHER, "and GS2?")])
        self.assertEqual(len(named(calls, "_handle_instagram_photo")), 1)
        self.assertEqual(len(named(calls, "_handle_instagram_media")), 1)
        self.assertEqual([c[1][0] for c in named(calls, "_send_instagram_reply")], [SENDER, OTHER])
        self.assertEqual(out.count(f"[INSTAGRAM] Skipped: duplicate mid {mid}"), 1)

    def test_one_bad_event_does_not_stop_the_next(self):
        def order(sender_id):
            if sender_id == SENDER:
                raise RuntimeError("orders table locked")
            return ""

        # No mids, so the two runs' calls can be compared as they are.
        events = [dm(), dm(sender=OTHER)]
        results = {}
        for path in ("legacy", "graph"):
            calls, out = self.drive(path, lambda: self.post(*events), order=order)
            (alert,) = named(calls, "_alert_send_failure")
            self.assertEqual(alert[1], ("Instagram", "RuntimeError: orders table locked", SENDER), path)
            self.assertEqual(alert[2], {"kind": "pipeline", "holding_sent": None}, path)
            self.assertIn("[INSTAGRAM] Event handler error: RuntimeError: orders table locked", out, path)
            (send,) = named(calls, "_send_instagram_reply")
            self.assertEqual(send[1], (OTHER, AUTO_REPLY), path)
            results[path] = (effects(calls), out)
        self.assertEqual(results["legacy"], results["graph"])


class GraphPathMatchesLegacy(Harness):
    """The same message, both paths: the same calls with the same arguments
    in the same order, and the same log lines (tracebacks, which go to
    stderr, are the only output left out)."""

    SCENARIOS = {
        "auto": dict(llm=AUTO_JSON),
        "auto, send fails": dict(llm=AUTO_JSON, send_ok=False),
        "draft": dict(llm=DRAFT_JSON),
        "draft, buttons fail": dict(llm=DRAFT_JSON, buttons_ok=False),
        "escalate": dict(llm=ESCALATE_JSON),
        "legal threat on an AUTO reply": dict(llm=AUTO_JSON, text=LEGAL_THREAT),
        "bulk commit": dict(llm=AUTO_JSON, text="ok I'll take 50 trays"),
        "DeepSeek down": dict(llm=None, llm_exc=RuntimeError("DeepSeek unavailable")),
        "DeepSeek down, legal threat": dict(
            llm=None, llm_exc=RuntimeError("DeepSeek unavailable"), text="I am going to the consumer court"),
        "brain.md missing": dict(brain_missing=True),
        "unparseable output": dict(llm="sorry, I can't produce JSON today"),
        "empty AUTO reply": dict(llm=json.dumps({"classification": "AUTO", "reply": ""})),
        "unknown classification": dict(llm=json.dumps({"classification": "MAYBE", "reply": "hmm"})),
        "tester": dict(
            llm=json.dumps({"classification": "AUTO", "reply": "Thanks for checking it out! Udit will message you personally 🤍", "tag": "LEAD"}),
            text="Hi! Udit asked me to test your assistant"),
        "order question": dict(
            llm=json.dumps({"classification": "AUTO", "reply": "Sorry for the delay. Could you share your order ID? I've passed this to the team 🤍", "tag": "ORDER"}),
            text="Where is my order? It's been 9 days"),
        "20+ tray question": dict(
            llm=json.dumps({"classification": "DRAFT+APPROVE", "reply": "Let me check GS2 stock for 30 trays 🤍"}),
            text="whats ur rate for 30 trays of GS2?"),
        "rate limited": dict(limited=glam.LIMIT_SENDER_WINDOW),
    }

    def both(self, text=MSG, **stub_kwargs):
        legacy = self.process("legacy", [dm(text)], **stub_kwargs)
        graph = self.process("graph", [dm(text)], **stub_kwargs)
        return legacy, graph

    def test_every_outcome_matches_the_legacy_handler(self):
        for name, kwargs in self.SCENARIOS.items():
            kwargs = dict(kwargs)
            text = kwargs.pop("text", MSG)
            with self.subTest(name):
                (legacy_calls, legacy_out), (graph_calls, graph_out) = self.both(text, **kwargs)
                self.assertEqual(named(legacy_calls, "handle_instagram_message"), [])
                (handed,) = named(graph_calls, "handle_instagram_message")
                self.assertTrue(handed[2]["gates_done"])
                self.assertEqual(effects(legacy_calls), effects(graph_calls))
                self.assertEqual(legacy_out, graph_out)

    def test_the_legacy_shapes_the_comparison_relies_on(self):
        # So the comparison above can't pass with both paths wrong the same way.
        (calls, _), _ = self.both()
        self.assertEqual(named(calls, "_send_instagram_reply")[0][1], (SENDER, AUTO_REPLY))
        (calls, _), _ = self.both(llm=None, llm_exc=RuntimeError("DeepSeek unavailable"))
        self.assertEqual(named(calls, "_send_instagram_reply")[0][1], (SENDER, glam.BRAIN_HOLDING_LINE))
        self.assertEqual(named(calls, "_alert_send_failure")[0][2], {"kind": "pipeline", "holding_sent": True})
        (calls, _), _ = self.both(LEGAL_THREAT, llm=AUTO_JSON)
        self.assertEqual(named(calls, "_send_instagram_reply"), [])
        self.assertEqual(len(named(calls, "_pause_number")), 1)
        (calls, _), _ = self.both(limited=glam.LIMIT_SENDER_WINDOW)
        self.assertEqual(len(named(calls, "_ig_rate_limited")), 1)
        self.assertEqual(named(calls, "ask_claude"), [])

    def test_a_prefilter_hit_is_logged_once_on_a_drafted_reply(self):
        # draft_reply_logic applies and logs the prefilter; triage's re-check
        # adds no line and changes nothing.
        for (calls, out), path in zip(self.both(LEGAL_THREAT, llm=AUTO_JSON), ("legacy", "graph")):
            self.assertEqual(len([line for line in out if line.startswith("[PREFILTER]")]), 1, path)
            (tg,) = named(calls, "send_telegram_notification")
            self.assertEqual(tg[1][0], "ESCALATE", path)

    def test_after_a_failure_the_prefilter_escalates_without_a_log_line(self):
        both = self.both("I am going to the consumer court", llm=None,
                         llm_exc=RuntimeError("DeepSeek unavailable"))
        for (calls, out), path in zip(both, ("legacy", "graph")):
            self.assertEqual([line for line in out if line.startswith("[PREFILTER]")], [], path)
            self.assertIn("[INSTAGRAM] Twin pipeline failed: RuntimeError: DeepSeek unavailable", out, path)
            (tg,) = named(calls, "send_telegram_notification")
            self.assertEqual(tg[1][0], "ESCALATE", path)
            self.assertFalse(tg[2]["holding_reply_sent"], path)   # legal: silent


# ---- keyed /healthz ----

class KeyedHealthz(unittest.TestCase):
    def setUp(self):
        glam._init_db()

    def test_legacy_without_the_graph(self):
        with patch.object(glam, "_twin_graph", None), patch.object(glam, "_twin_graph_error", None):
            data = keyed_healthz()
        self.assertEqual(data["reply_path"], "legacy")
        self.assertNotIn("langgraph_error", data)

    def test_langgraph_when_the_graph_is_loaded(self):
        with patch.object(glam, "_twin_graph", twin), patch.object(glam, "_twin_graph_error", None):
            data = keyed_healthz()
        self.assertEqual(data["reply_path"], "langgraph")
        self.assertNotIn("langgraph_error", data)

    def test_the_import_error_when_the_flag_is_on_but_the_graph_failed(self):
        with patch.object(glam, "_twin_graph", None), \
                patch.object(glam, "_twin_graph_error", "ImportError: No module named 'langgraph'"):
            data = keyed_healthz()
            public = glam.app.test_client().get("/healthz")
        self.assertEqual(data["reply_path"], "legacy")
        self.assertEqual(data["langgraph_error"], "ImportError: No module named 'langgraph'")
        # Never in the public answer.
        self.assertEqual(public.get_json(), {"status": "ok"})


# ---- real startups, in a fresh interpreter ----

def run_python(code, *, flag, extra_path=None):
    """Run `code` in a fresh interpreter from the repo root with
    TWIN_USE_LANGGRAPH=flag (None: unset), offline, on a fresh DB. Returns
    (the JSON after its "RESULT " line, its stdout)."""
    env = dict(os.environ)
    env.update({
        "DB_PATH": os.path.join(tempfile.mkdtemp(prefix="glamshelf-langgraph-sub-"), "test.db"),
        "GITHUB_TOKEN": "", "GITHUB_REPO": "",
        "HF_HUB_OFFLINE": "1",            # no embedding-model download
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("TWIN_USE_LANGGRAPH", None)
    if flag is not None:
        env["TWIN_USE_LANGGRAPH"] = flag
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (extra_path, env.get("PYTHONPATH")) if p)
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    match = re.search(r"^RESULT (.*)$", proc.stdout, re.M)
    if proc.returncode != 0 or not match:
        raise AssertionError(
            f"subprocess failed (exit {proc.returncode})\n--- stdout\n{proc.stdout[-4000:]}"
            f"\n--- stderr\n{proc.stderr[-4000:]}")
    return json.loads(match.group(1)), proc.stdout


SAME_FILE = r'''
def same_file_modules(path):
    """Distinct module objects loaded from `path` (by id: one object can sit
    under several names)."""
    path = os.path.abspath(path)
    found = set()
    for module in list(sys.modules.values()):
        try:
            if module is not None and os.path.abspath(module.__file__ or "") == path:
                found.add(id(module))
        except Exception:
            pass
    return len(found)
'''

FLAG_OFF_CODE = r'''
import json, os, socket, sys, threading
''' + SAME_FILE + r'''
WATCH = ("graph", "langgraph", "langgraph_sdk", "langchain_core", "langsmith")
import app
with_app = sorted(m for m in sys.modules if m.split(".")[0] in WATCH)

# Now import graph the way app.py would, and watch what that import does.
# Only the main thread counts: app's own background threads (the RAG loop
# starts its health-check timer whenever it gets there) may run meanwhile.
main = threading.main_thread()
attempts = []
def blocked(kind):
    def refuse(*args, **kwargs):
        if threading.current_thread() is main:
            attempts.append(kind)
        raise OSError("network blocked by the test")
    return refuse
socket.socket.connect = blocked("connect")
socket.socket.connect_ex = blocked("connect_ex")
socket.getaddrinfo = blocked("getaddrinfo")
started = []
real_start = threading.Thread.start
def start(self):
    if threading.current_thread() is main:
        started.append(self.name)
    return real_start(self)
threading.Thread.start = start
import graph
print("RESULT " + json.dumps({
    "use_langgraph": app._use_langgraph(),
    "imported_with_app": with_app,
    "graph_app_is_app": graph.app is app,
    "app_py_modules": same_file_modules("app.py"),
    "new_threads": started,
    "network_attempts": attempts,
    "eval_modules": sorted(m for m in sys.modules if m == "eval" or m.startswith("eval.")),
}))
'''

FLAG_ON_IMPORT_CODE = r'''
import json, os, sys
''' + SAME_FILE + r'''
import app      # how gunicorn loads it: "gunicorn app:app"
graph = sys.modules.get("graph")
keyed = app.app.test_client().get("/healthz", headers={"X-Dashboard-Key": app.DASHBOARD_KEY}).get_json()
print("RESULT " + json.dumps({
    "use_langgraph": app._use_langgraph(),
    "graph_app_is_app": graph is not None and graph.app is app,
    "app_py_modules": same_file_modules("app.py"),
    "reply_path": keyed.get("reply_path"),
    "langgraph_error": keyed.get("langgraph_error"),
}))
'''

# `python app.py`: app.py runs as __main__. Flask's run() is replaced so
# the check happens where the server would start, with startup complete.
PYTHON_APP_PY_CODE = r'''
import json, os, runpy, sys
''' + SAME_FILE + r'''
import flask
result = {}
def check_instead_of_serving(self, *args, **kwargs):
    main = sys.modules["__main__"]
    graph = sys.modules.get("graph")
    result.update(
        app_py_modules=same_file_modules("app.py"),
        app_is_main=sys.modules.get("app") is main,
        graph_app_is_main=graph is not None and graph.app is main,
        use_langgraph=main._use_langgraph(),
    )
flask.Flask.run = check_instead_of_serving
runpy.run_path("app.py", run_name="__main__")
print("RESULT " + json.dumps(result))
'''

BROKEN_LANGGRAPH_CODE = r'''
import json, sys
from unittest.mock import patch
import app
sent = []
reply = "Yes ma'am, GS1 works beautifully for hooded eyes 🤍"
event = {"sender": {"id": "17841400000000777"}, "recipient": {"id": "17841400000000999"},
         "timestamp": 1, "message": {"mid": "", "text": "Do GS1 lashes suit hooded eyes?"}}
with patch.object(app, "ask_claude", lambda *a, **k: json.dumps({"classification": "AUTO", "reply": reply})), \
        patch.object(app, "get_live_inventory", lambda: ""), \
        patch.object(app, "get_live_policies", lambda: ""), \
        patch.object(app, "_rag_retrieve", lambda text: ""), \
        patch.object(app, "_log_instagram", lambda *a, **k: None), \
        patch.object(app, "_ig_username", lambda sender_id: ""), \
        patch.object(app, "_send_instagram_reply", lambda s, t: (sent.append([s, t]), (True, ""))[1]):
    app._process_instagram_event(event)
keyed = app.app.test_client().get("/healthz", headers={"X-Dashboard-Key": app.DASHBOARD_KEY}).get_json()
print("RESULT " + json.dumps({
    "use_langgraph": app._use_langgraph(),
    "graph_imported": "graph" in sys.modules,
    "error": app._twin_graph_error,
    "sent": sent,
    "reply_path": keyed.get("reply_path"),
    "langgraph_error": keyed.get("langgraph_error"),
}))
'''


class RealStartups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flag_off, _ = run_python(FLAG_OFF_CODE, flag=None)

    def test_flag_off_app_never_imports_graph_or_langgraph(self):
        result = self.flag_off
        self.assertFalse(result["use_langgraph"])
        self.assertEqual(result["imported_with_app"], [])

    def test_importing_graph_only_builds_the_graph(self):
        # In the same flag-OFF interpreter, graph imported after app the way
        # the loader does: no thread, no socket, no eval, and the app it
        # uses is the running one.
        result = self.flag_off
        self.assertTrue(result["graph_app_is_app"])
        self.assertEqual(result["app_py_modules"], 1)
        self.assertEqual(result["new_threads"], [])
        self.assertEqual(result["network_attempts"], [])
        self.assertEqual(result["eval_modules"], [])

    def test_flag_on_under_gunicorn_one_app_and_the_graph_answers(self):
        result, out = run_python(FLAG_ON_IMPORT_CODE, flag="1")
        self.assertTrue(result["use_langgraph"])
        self.assertTrue(result["graph_app_is_app"])
        self.assertEqual(result["app_py_modules"], 1)
        self.assertEqual(result["reply_path"], "langgraph")
        self.assertIsNone(result["langgraph_error"])
        self.assertEqual(out.count("[LANGGRAPH] TWIN_USE_LANGGRAPH=1: graph.py answers Instagram text DMs"), 1)

    def test_flag_on_under_python_app_py_no_second_copy_of_app(self):
        # Without registering __main__ as "app", graph.py's `import app`
        # loaded app.py a second time (second DB init, backup loop and
        # embedding model; its own _seen_ids and reply budget).
        result, out = run_python(PYTHON_APP_PY_CODE, flag="1")
        self.assertEqual(result["app_py_modules"], 1)
        self.assertTrue(result["app_is_main"])
        self.assertTrue(result["graph_app_is_main"])
        self.assertTrue(result["use_langgraph"])
        self.assertEqual(len(re.findall(r"^\[DEDUP\] Loaded ", out, re.M)), 1)

    def test_flag_on_with_langgraph_broken_startup_survives_on_the_old_path(self):
        fake_site = tempfile.mkdtemp(prefix="glamshelf-no-langgraph-")
        os.makedirs(os.path.join(fake_site, "langgraph"))
        Path(fake_site, "langgraph", "__init__.py").write_text(
            'raise ImportError("simulated: langgraph is not installed")\n', encoding="utf-8")
        result, out = run_python(BROKEN_LANGGRAPH_CODE, flag="1", extra_path=fake_site)
        self.assertFalse(result["use_langgraph"])
        self.assertFalse(result["graph_imported"])
        self.assertEqual(result["error"], "ImportError: simulated: langgraph is not installed")
        self.assertIn("[LANGGRAPH] TWIN_USE_LANGGRAPH=1 but graph.py FAILED to load: "
                      "ImportError: simulated: langgraph is not installed", out)
        self.assertEqual(result["sent"], [["17841400000000777", "Yes ma'am, GS1 works beautifully for hooded eyes 🤍"]])
        self.assertEqual(result["reply_path"], "legacy")
        self.assertEqual(result["langgraph_error"], result["error"])


if __name__ == "__main__":
    unittest.main()
