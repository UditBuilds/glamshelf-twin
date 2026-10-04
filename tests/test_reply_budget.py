"""One reply can't outlive gunicorn's 60s worker timeout (audit finding 13).

Webhook events are processed inline, so a worker killed at 60s means no
reply, no alert, and Meta's retry of the message dropped by the mid dedup.
Each webhook request now gets a 50s reply budget, and every outbound call
in the reply path takes its timeout from what's left:

  - Shopify (inventory, policy pages): 5s each, no retry for 60s after a
    failure;
  - RAG retrieval: 3s of wall-clock time, then brain-only;
  - DeepSeek: up to 20s per attempt with 10s kept back for the reply; one
    retry (made by us, not the SDK) only while a 5s attempt still fits;
  - Instagram / Telegram sends: 10s / 5s, never below 5s / 2s.

The worst-case tests drive the real Instagram handler with a fake clock:
every network call "hangs" for its full timeout and then fails, so the
clock reads what the request would have taken.

No live API call anywhere here: every network call is faked.

Run:  python -m unittest tests.test_reply_budget
"""
import io, json, os, sys, tempfile, threading, time, unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-reply-budget-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import httpx
import openai
import requests
import app as glam

SENDER = "17800000000000913"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _timeout_error():
    return openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    )


def _completion(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


class StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Budget(unittest.TestCase):
    def test_no_budget_outside_a_webhook(self):
        self.assertEqual(glam._budget_left(), float("inf"))
        self.assertEqual(glam._budget_timeout(5), 5)

    def test_webhook_views_run_inside_a_budget(self):
        for endpoint in ("instagram_webhook", "webhook"):
            view = glam.app.view_functions[endpoint]
            self.assertTrue(hasattr(view, "__wrapped__"), endpoint)

    def test_budget_is_set_during_the_view_and_cleared_after(self):
        clock = FakeClock()
        seen = []

        def view():
            seen.append(glam._budget_left())
            raise RuntimeError("boom")

        with patch.object(glam, "_clock", clock):
            with self.assertRaises(RuntimeError):
                glam._with_reply_budget(view)()
            self.assertEqual(seen, [50])
            self.assertEqual(glam._budget_left(), float("inf"))

    def test_budget_timeout_caps_and_floors(self):
        clock = FakeClock()
        with patch.object(glam, "_clock", clock):
            glam._reply_budget.deadline = clock.t + 3
            try:
                self.assertEqual(glam._budget_timeout(5), 3)
                self.assertEqual(glam._budget_timeout(2), 2)
                clock.t += 10                           # past the deadline
                self.assertEqual(glam._budget_timeout(5), 1.0)
                self.assertEqual(glam._budget_timeout(10, floor=5), 5)
            finally:
                glam._reply_budget.deadline = None

    def test_the_budget_is_per_thread(self):
        glam._reply_budget.deadline = glam._clock() + 1
        try:
            other = []
            t = threading.Thread(target=lambda: other.append(glam._budget_left()))
            t.start(); t.join()
            self.assertEqual(other, [float("inf")])
        finally:
            glam._reply_budget.deadline = None


class DeepSeekAttempts(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        p = patch.object(glam, "_clock", self.clock)
        p.start(); self.addCleanup(p.stop)

    def fake_client(self, *outcomes):
        outcomes = list(outcomes)

        def create(**kw):
            self.calls.append(kw["timeout"])
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                self.clock.t += kw["timeout"]
                raise out
            return out

        return patch.object(glam, "deepseek_client", SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    def budget(self, seconds):
        glam._reply_budget.deadline = self.clock.t + seconds
        self.addCleanup(setattr, glam._reply_budget, "deadline", None)

    def test_client_does_no_retries_of_its_own(self):
        self.assertEqual(glam.deepseek_client.max_retries, 0)
        self.assertEqual(glam.deepseek_client.timeout, 20)

    def test_without_a_budget_20s_and_one_retry(self):
        ok = _completion("{}")
        with self.fake_client(_timeout_error(), ok), redirect_stdout(io.StringIO()):
            self.assertIs(glam._deepseek_create([]), ok)
        self.assertEqual(self.calls, [20, 20])

    def test_attempts_keep_10s_back_for_the_reply(self):
        self.budget(25)
        with self.fake_client(_completion("{}")):
            glam._deepseek_create([])
        self.assertEqual(self.calls, [15])

    def test_retry_only_when_a_5s_attempt_still_fits(self):
        self.budget(30)          # 20s attempt, then 0s left after the reserve
        with self.fake_client(_timeout_error()), redirect_stdout(io.StringIO()):
            with self.assertRaises(openai.APITimeoutError):
                glam._deepseek_create([])
        self.assertEqual(self.calls, [20])

    def test_retry_gets_what_is_left(self):
        self.budget(40)          # 20s attempt, then 10s for the retry
        ok = _completion("{}")
        with self.fake_client(_timeout_error(), ok), redirect_stdout(io.StringIO()):
            self.assertIs(glam._deepseek_create([]), ok)
        self.assertEqual(self.calls, [20, 10])

    def test_no_attempt_when_the_budget_is_spent(self):
        self.budget(14)          # 14 - 10 reserve = 4s < 5s minimum
        with self.fake_client():
            with self.assertRaises(glam.ReplyBudgetExceeded):
                glam._deepseek_create([])
        self.assertEqual(self.calls, [])

    def test_a_bad_request_is_not_retried(self):
        with self.fake_client(StatusError(400)):
            with self.assertRaises(StatusError):
                glam._deepseek_create([])
        self.assertEqual(self.calls, [20])

    def test_which_failures_are_retried(self):
        for status in (408, 409, 429, 500, 503):
            self.assertTrue(glam._deepseek_retryable(StatusError(status)), status)
        for status in (400, 401, 404, 422):
            self.assertFalse(glam._deepseek_retryable(StatusError(status)), status)
        self.assertTrue(glam._deepseek_retryable(_timeout_error()))
        self.assertFalse(glam._deepseek_retryable(ValueError("x")))


class ShopifyRetryAfterFailure(unittest.TestCase):
    def setUp(self):
        self.gets = []
        for p in (
            patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0, "retry_at": 0.0}),
            patch.dict(glam._policy_cache, {"text": "", "fetched_at": 0.0, "retry_at": 0.0}),
        ):
            p.start(); self.addCleanup(p.stop)

    def failing_get(self, url, params=None, timeout=None, **kw):
        self.gets.append((url, timeout))
        raise requests.ConnectionError("store down")

    def test_inventory_waits_a_minute_after_a_failure(self):
        now = [5000.0]
        with patch.object(glam.requests, "get", self.failing_get), \
                patch.object(glam.time, "time", lambda: now[0]), redirect_stdout(io.StringIO()):
            self.assertEqual(glam.get_live_inventory(), "")
            self.assertEqual(glam.get_live_inventory(), "")      # within the minute: no fetch
            now[0] += 61
            self.assertEqual(glam.get_live_inventory(), "")
        self.assertEqual(len(self.gets), 2)
        self.assertEqual(self.gets[0][1], 5)

    def test_policies_wait_a_minute_after_a_failure(self):
        now = [5000.0]
        with patch.object(glam.requests, "get", self.failing_get), \
                patch.object(glam.time, "time", lambda: now[0]), redirect_stdout(io.StringIO()):
            glam.get_live_policies()
            glam.get_live_policies()
            now[0] += 61
            glam.get_live_policies()
        self.assertEqual(len(self.gets), 4)                       # two pages, twice
        self.assertEqual({t for _, t in self.gets}, {5})

    def test_fetch_never_outlasts_the_budget(self):
        clock = FakeClock()
        with patch.object(glam, "_clock", clock), \
                patch.object(glam.requests, "get", self.failing_get), redirect_stdout(io.StringIO()):
            glam._reply_budget.deadline = clock.t + 2
            try:
                glam.get_live_inventory()
            finally:
                glam._reply_budget.deadline = None
        self.assertEqual(self.gets[0][1], 2)


class RagWallClockCap(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.started = []
        for p in (
            patch.object(glam, "_rag_embedder", object()),
            patch.object(glam, "RAG_RETRIEVAL_TIMEOUT_SECONDS", 0.3),
            patch.object(glam, "_rag_retrieve_now", self.slow_retrieve),
        ):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.release.set)

    def slow_retrieve(self, message):
        self.started.append(message)
        self.release.wait(10)
        return "[RETRIEVED CONTEXT]\nslow"

    def test_slow_retrieval_is_skipped_and_never_stacks(self):
        out = io.StringIO()
        with redirect_stdout(out):
            t0 = time.monotonic()
            self.assertEqual(glam._rag_retrieve("is GS1 reusable?"), "")
            self.assertLess(time.monotonic() - t0, 2)
            # The first one is still running: the next message doesn't start another.
            self.assertEqual(glam._rag_retrieve("and GS2?"), "")
        self.assertEqual(self.started, ["is GS1 reusable?"])
        self.assertIn("Retrieval took longer than 0.5s — skipped", out.getvalue())
        self.assertIn("An earlier slow retrieval is still running", out.getvalue())
        self.release.set()
        for _ in range(100):                       # the abandoned one finishes
            if glam._rag_inflight.acquire(blocking=False):
                glam._rag_inflight.release()
                break
            time.sleep(0.02)
        with patch.object(glam, "_rag_retrieve_now", lambda m: "ctx"):
            self.assertEqual(glam._rag_retrieve("GS1 band?"), "ctx")

    def test_messages_the_gate_misses_start_no_thread(self):
        with patch.object(glam.threading, "Thread", side_effect=AssertionError("no thread")):
            self.assertEqual(glam._rag_retrieve("hi there"), "")


class WorstCase(unittest.TestCase):
    """Every network call hangs for its full timeout. The fake clock reads
    what the webhook request would have taken; gunicorn kills at 60s."""

    def setUp(self):
        self.clock = FakeClock()
        self.log = []          # (what, timeout, clock after)
        self.ig_texts = []
        self.deepseek = []     # outcomes, consumed in order
        self.ig_ok = False     # Instagram sends hang and fail unless set
        for p in (
            patch.object(glam, "_clock", self.clock),
            patch.object(glam.requests, "get", self.hang_get),
            patch.object(glam.requests, "post", self.hang_post),
            patch.object(glam, "deepseek_client", SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=self.hang_deepseek)))),
            patch.object(glam, "_rag_retrieve", self.hang_rag),
            patch.object(glam, "TELEGRAM_BOT_TOKEN", "123:TEST"),
            patch.object(glam, "TELEGRAM_CHAT_ID", "42"),
            patch.object(glam, "INSTAGRAM_PAGE_ACCESS_TOKEN", "TEST-TOKEN-NOT-REAL"),
            patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0, "retry_at": 0.0}),
            patch.dict(glam._policy_cache, {"text": "", "fetched_at": 0.0, "retry_at": 0.0}),
            patch.dict(glam._send_failure_last_alert, {}, clear=True),
            patch.object(glam, "_load_brain_cached", lambda: "== BRAIN =="),
            patch.object(glam, "_record_bot_outbound", lambda *a, **k: None),
        ):
            p.start(); self.addCleanup(p.stop)
        os.environ["INSTAGRAM_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "INSTAGRAM_WEBHOOK_VERIFY_DISABLED", None)

    def advance(self, what, timeout):
        self.clock.t += timeout
        self.log.append((what, timeout, round(self.clock.t - 1000.0, 2)))

    def hang_get(self, url, params=None, timeout=None, **kw):
        self.advance("shopify", timeout)
        raise requests.Timeout("hung")

    def hang_post(self, url, params=None, json=None, timeout=None, **kw):
        if "api.telegram.org" in url:
            self.advance("telegram", timeout)
        else:
            self.ig_texts.append((json or {}).get("message", {}).get("text"))
            self.advance("instagram", timeout)
            if self.ig_ok:          # slow, but delivered
                return SimpleNamespace(ok=True, status_code=200, text="{}",
                                       json=lambda: {"message_id": f"mid.{len(self.ig_texts)}"})
        raise requests.Timeout("hung")

    def hang_deepseek(self, **kw):
        out = self.deepseek.pop(0) if self.deepseek else _timeout_error()
        self.advance("deepseek", kw["timeout"])
        if isinstance(out, Exception):
            raise out
        return out

    def hang_rag(self, message):
        self.advance("rag", glam._budget_timeout(glam.RAG_RETRIEVAL_TIMEOUT_SECONDS, floor=0.5))
        return ""

    def post(self, *texts):
        mid = f"m{time.time_ns()}"
        events = [{
            "sender": {"id": SENDER + str(i)}, "recipient": {"id": "page"}, "timestamp": i,
            "message": {"mid": f"{mid}-{i}", "text": text},
        } for i, text in enumerate(texts)]
        with redirect_stdout(io.StringIO()):
            resp = glam.app.test_client().post(
                "/instagram-webhook", json={"entry": [{"messaging": events}]})
        self.assertEqual(resp.status_code, 200)
        return self.clock.t - 1000.0

    def kinds(self):
        return [w for w, _, _ in self.log]

    def test_deepseek_timing_out_ends_well_inside_60s(self):
        took = self.post("is GS1 reusable?")
        self.assertLessEqual(took, 55, self.log)
        # Shopify x3, RAG, one 20s DeepSeek attempt (no room for a retry),
        # then the holding line and the founder's alert.
        self.assertEqual(self.kinds()[:5], ["shopify", "shopify", "shopify", "rag", "deepseek"])
        self.assertEqual(self.kinds().count("deepseek"), 1)
        self.assertEqual(self.ig_texts, [glam.BRAIN_HOLDING_LINE])
        self.assertIn("telegram", self.kinds())

    def test_unusable_output_then_no_time_for_the_parse_retry(self):
        self.deepseek = [_completion("not json at all")]
        took = self.post("is GS1 reusable?")
        self.assertLessEqual(took, 55, self.log)
        self.assertEqual(self.kinds().count("deepseek"), 1)
        # Today's unusable-output path: escalate with the holding line.
        self.assertEqual(self.ig_texts, [glam.BRAIN_HOLDING_LINE])

    def test_slow_successful_reply_split_in_two(self):
        long_reply = ("GS1 is our everyday tray. " * 50).strip()       # > 900 chars: 2 DMs
        self.deepseek = [_completion(json.dumps({"classification": "AUTO", "reply": long_reply}))]
        self.ig_ok = True
        took = self.post("is GS1 reusable?")
        self.assertLessEqual(took, 58, self.log)
        self.assertEqual(len(self.ig_texts), 2)                         # both parts went out

    def test_a_second_message_in_the_same_request_still_gets_the_holding_line(self):
        took = self.post("is GS1 reusable?", "and GS2?")
        self.assertEqual(self.ig_texts, [glam.BRAIN_HOLDING_LINE, glam.BRAIN_HOLDING_LINE])
        # The second message never reaches DeepSeek: the budget is spent.
        self.assertEqual(self.kinds().count("deepseek"), 1)
        print(f"\n[two-message request, everything hanging] {took:.1f}s: {self.log}")


if __name__ == "__main__":
    unittest.main()
