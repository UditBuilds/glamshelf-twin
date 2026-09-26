"""Regression: the live Instagram send path must never be blocked by
eval/harness.py's outbound-raise guard.

Sep 2026 incident: app.py's live reply path imported eval.harness (for a
since-abandoned local-judge guardrail). Importing that module — even when
the import itself then failed with ImportError — ran
eval.harness._install_outbound_guard() as a module-level side effect,
which permanently monkeypatches app._send_instagram_reply /
app.send_whatsapp_reply (and others) to raise, for the rest of the
process's life. Every Instagram AND WhatsApp reply went dark in
production. Fix: the guardrail call was removed from draft_reply_logic
entirely, and _install_outbound_guard() now installs only inside
eval.harness.run_eval() — never merely on import.

This test exercises the exact call chain from the incident traceback
(_process_instagram_event -> _send_instagram_reply) and checks the
actual HTTP call happens, rather than asserting "no exception raised":
_process_instagram_event has a catch-all around its whole body (by
design — one bad webhook event must never crash the handler), so the
original bug's RuntimeError never escaped to a caller either; it was
only visible as a swallowed "[INSTAGRAM] Event handler error: RuntimeError:
EVAL SAFETY: ..." log line. Checking that requests.post was actually
reached is what would have failed against the pre-fix code.

Modeled on tests/test_pause_gate.py — same env kill switches, same
recorder-stub pattern, same redirect_stdout idiom.
"""
import io, os, sys, tempfile, unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-evalguard-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

IG_SENDER_ID = "17800000000000123"
IG_PAGE_ID = "17800000000000999"


class InstagramSendNotBlockedByEvalGuard(unittest.TestCase):
    def setUp(self):
        os.environ["ESCALATION_PREFILTER_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "ESCALATION_PREFILTER_DISABLED", None)
        glam._init_db()

    def test_auto_reply_reaches_the_graph_api_without_eval_safety_raise(self):
        event = {
            "sender": {"id": IG_SENDER_ID},
            "recipient": {"id": IG_PAGE_ID},
            "timestamp": 1234567890,
            "message": {"text": "What's the price of the Clean Girl lashes?"},
        }
        fake_response = MagicMock(ok=True, status_code=200, text="{}")

        patches = [
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", "test-page-token"),
            patch.object(glam, "INSTAGRAM_PAGE_ID", IG_PAGE_ID),
            patch.object(glam, "_is_paused", return_value=False),
            patch.object(glam, "_udit_replied_recently_ig", return_value=False),
            patch.object(glam, "_lookup_recent_order", return_value=""),
            patch.object(glam, "_load_instagram_history", return_value=[]),
            patch.object(glam, "_load_brain_cached", return_value="BRAIN v-test"),
            patch.object(glam, "get_live_inventory", return_value=""),
            # The live prices a real inventory fetch would have cached —
            # without them the output guard holds "₹249" for approval.
            patch.dict(glam._inventory_cache, {"prices": {249.0}}),
            patch.object(glam, "get_live_policies", return_value=""),
            patch.object(glam, "_rag_retrieve", return_value=""),
            patch.object(glam, "ask_claude", return_value=(
                '{"classification": "AUTO", '
                '"reply": "Clean Girl is ₹249, free shipping above ₹799!"}'
            )),
            patch.object(glam, "_log_instagram"),
        ]

        buf = io.StringIO()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            mock_post = stack.enter_context(
                patch.object(glam.requests, "post", return_value=fake_response)
            )
            with redirect_stdout(buf):
                glam._process_instagram_event(event)

        # The real send function must have run far enough to hit the
        # (mocked) network call — this is what the eval-import guard
        # prevented pre-fix, since it replaced _send_instagram_reply
        # wholesale with a function that raises before ever touching
        # requests.post.
        mock_post.assert_called_once()
        self.assertIn("/messages", mock_post.call_args.args[0])

        output = buf.getvalue()
        self.assertNotIn("EVAL SAFETY", output)
        self.assertNotIn("Event handler error", output)
        self.assertIn("[INSTAGRAM-AUTO] Replied to", output)


if __name__ == "__main__":
    unittest.main()
