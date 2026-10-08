"""eval/harness.py: resumable runs (a checkpointer) and founder review
(interrupt()).

Offline. app.draft_reply_logic and the judge are stubs, so no test makes
a model call. The checkpointer is LangGraph's InMemorySaver, which ships
with langgraph, so CI needs no langgraph-checkpoint-sqlite. The real
SqliteSaver is never opened: _sqlite_checkpointer is patched to fail.

run_eval installs the outbound guard on app for the life of the process.
CI runs every test module in one process, and later modules
(test_eval_guard_not_in_live_path, the Instagram tests) need app's real
send functions, so setUp snapshots them and every test puts them back.
"""
import io, json, os, subprocess, sys, tempfile, unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-eval-checkpoint-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from eval import harness

app = harness.app

# What run_eval returned before run_id existed. Without a run_id it must
# still return exactly these keys.
TOP_KEYS = {"judge", "judge_model", "mode", "category", "ideal_source",
            "rows_scored", "elapsed_seconds", "summary", "results"}
ROW_KEYS = {"id", "channel", "category", "question", "ideal_answer",
            "fresh_answer", "classification", "verdict", "reason",
            "human_verdict", "seconds"}


class Crash(BaseException):
    """A killed process or Ctrl+C. The nodes' `except Exception` can't
    catch it, so it stops the run the way a real crash does."""


def make_row(i, human_verdict=None):
    return {
        "id": str(i), "channel": "WhatsApp", "category": "Refund",
        "question": f"q{i}", "ideal_answer": f"ideal answer {i}",
        "actual_answer": f"recorded answer {i}", "human_verdict": human_verdict,
        "ideal_source": "founder-written",
    }


class EvalHarnessTestCase(unittest.TestCase):
    """Stubs the twin and the judge, and undoes the outbound guard."""

    def setUp(self):
        names = harness._OUTBOUND + ["_log_message", "_log_instagram"]
        self.real_app_functions = {n: getattr(app, n) for n in names if hasattr(app, n)}
        self.addCleanup(self.restore_app_functions)

        self.rows = [make_row(i) for i in range(1, 4)]
        self.replies = {}    # question -> reply, or an exception to raise
        self.verdicts = {}   # question -> (verdict, reason), or an exception
        self.calls = {"generate": [], "judge": []}

        runs_dir = Path(tempfile.mkdtemp(prefix="glamshelf-eval-runs-test-"))
        for p in (
            patch.object(app, "draft_reply_logic", self.fake_draft),
            patch.dict(harness.JUDGES, {"groq": self.fake_judge,
                                        "ollama": self.fake_judge}),
            patch.object(harness, "load_rows",
                         lambda *a, **k: [dict(r) for r in self.rows]),
            patch.object(harness, "RUNS_DIR", runs_dir),
            patch.object(harness, "CHECKPOINT_DB", runs_dir / "checkpoints.sqlite"),
            patch.object(harness, "_sqlite_checkpointer", side_effect=AssertionError(
                "a test opened the SQLite checkpointer instead of injecting one")),
        ):
            p.start()
            self.addCleanup(p.stop)

    def restore_app_functions(self):
        for name, fn in self.real_app_functions.items():
            setattr(app, name, fn)

    def fake_draft(self, message, order_context="", history=None, source="WhatsApp"):
        self.calls["generate"].append(message)
        reply = self.replies.get(message, f"twin reply to {message}")
        if isinstance(reply, BaseException):
            raise reply
        return "AUTO", reply, "raw"

    def fake_judge(self, question, candidate, ideal):
        self.calls["judge"].append(question)
        verdict = self.verdicts.get(question, ("Pass", "matches the ideal"))
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    def reset_calls(self):
        self.calls = {"generate": [], "judge": []}

    def by_id(self, report):
        return {r["id"]: r for r in report["results"]}


# ---- a. no run_id: today's behaviour ----

class NoRunIdTests(EvalHarnessTestCase):
    def test_same_result_shape_as_before(self):
        self.verdicts["q2"] = ("Fail", "wrong price")
        report = harness.run_eval()

        self.assertEqual(set(report), TOP_KEYS)
        self.assertEqual(report["rows_scored"], 3)
        for result in report["results"]:
            self.assertEqual(set(result), ROW_KEYS)
        self.assertEqual([r["verdict"] for r in report["results"]],
                         ["Pass", "Fail", "Pass"])
        self.assertEqual(report["summary"]["overall"]["Pass"], 2)

    def test_runs_every_row_every_time_without_a_checkpointer(self):
        self.assertIsNone(harness.GRAPH.checkpointer)
        harness.run_eval()
        harness.run_eval()
        self.assertEqual(self.calls["generate"], ["q1", "q2", "q3"] * 2)

    def test_a_checkpointer_or_review_without_run_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, "needs a run_id"):
            harness.run_eval(checkpointer=InMemorySaver())
        with self.assertRaisesRegex(ValueError, "needs a run_id"):
            harness.run_eval(review=True, ask=lambda payload: "Pass")
        self.assertEqual(self.calls["generate"], [])


class SqliteImportTests(unittest.TestCase):
    def test_importing_the_harness_never_needs_the_sqlite_package(self):
        # A fresh interpreter where the package cannot be imported at all.
        code = ("import sys; sys.modules['langgraph.checkpoint.sqlite'] = None\n"
                "import eval.harness\nprint('IMPORTED')\n")
        env = dict(os.environ)
        env.update({
            "DB_PATH": os.path.join(tempfile.mkdtemp(prefix="glamshelf-eval-sub-"), "test.db"),
            "GITHUB_TOKEN": "", "GITHUB_REPO": "",
            "HF_HUB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8",
        })
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        self.assertIn("IMPORTED", proc.stdout)

    def test_a_resumable_run_without_the_package_names_the_fix(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(harness, "CHECKPOINT_DB", Path(tmp) / "runs" / "c.sqlite"), \
                patch.dict(sys.modules, {"langgraph.checkpoint.sqlite": None}):
            with self.assertRaisesRegex(RuntimeError, "eval/requirements.txt"):
                with harness._sqlite_checkpointer():
                    pass
            self.assertFalse((Path(tmp) / "runs").exists())


# ---- b. resume: one thread per row, "<run_id>:<row id>" ----

class ResumeTests(EvalHarnessTestCase):
    def test_rerun_after_a_crash_reuses_finished_rows_and_reruns_error_rows(self):
        self.rows = [make_row(i) for i in range(1, 6)]
        checkpointer = InMemorySaver()

        # Run 1: row 1 passes; row 2's generate fails (a rate limit); row
        # 3's judge fails; the process dies in row 4's judge; row 5 never
        # starts.
        self.replies["q2"] = RuntimeError("rate limited")
        self.verdicts["q3"] = RuntimeError("judge rate limited")
        self.verdicts["q4"] = Crash()
        with self.assertRaises(Crash):
            harness.run_eval(run_id="oct08", checkpointer=checkpointer)
        self.assertEqual(self.calls["generate"], ["q1", "q2", "q3", "q4"])
        self.assertEqual(self.calls["judge"], ["q1", "q3", "q4"])

        # Run 2, same run_id, everything healthy now.
        self.replies.clear(); self.verdicts.clear(); self.reset_calls()
        report = harness.run_eval(run_id="oct08", checkpointer=checkpointer)

        # Row 1: no call. Rows 2 and 3 (Error): run again from scratch.
        # Row 4: only its judge, since its generate had finished. Row 5: new.
        self.assertEqual(self.calls["generate"], ["q2", "q3", "q5"])
        self.assertEqual(self.calls["judge"], ["q2", "q3", "q4", "q5"])
        results = self.by_id(report)
        # Row 2's old "generate failed" error must not stick to the rerun.
        self.assertEqual({i: r["verdict"] for i, r in results.items()},
                         {"1": "Pass", "2": "Pass", "3": "Pass", "4": "Pass", "5": "Pass"})
        self.assertEqual(results["4"]["fresh_answer"], "twin reply to q4")
        self.assertEqual(report["rows_reused"], 1)

        # Run 3: everything is reused.
        self.reset_calls()
        report = harness.run_eval(run_id="oct08", checkpointer=checkpointer)
        self.assertEqual(self.calls, {"generate": [], "judge": []})
        self.assertEqual(report["rows_reused"], 5)

    def test_an_error_row_that_fails_again_stays_error(self):
        checkpointer = InMemorySaver()
        self.verdicts["q2"] = RuntimeError("judge down")
        harness.run_eval(run_id="r", checkpointer=checkpointer)
        self.reset_calls()
        report = harness.run_eval(run_id="r", checkpointer=checkpointer)
        self.assertEqual(self.calls["judge"], ["q2"])
        self.assertEqual(self.by_id(report)["2"]["verdict"], "Error")

    def test_one_thread_per_row_named_run_id_colon_row_id(self):
        checkpointer = InMemorySaver()
        harness.run_eval(run_id="oct08", checkpointer=checkpointer)
        graph = harness.build_graph(checkpointer)
        for row in self.rows:
            state = graph.get_state({"configurable": {"thread_id": f"oct08:{row['id']}"}})
            self.assertEqual(state.values["question"], row["question"])
            self.assertEqual(state.next, ())

        # Another run_id shares nothing with it.
        self.reset_calls()
        harness.run_eval(run_id="oct09", checkpointer=checkpointer)
        self.assertEqual(self.calls["generate"], ["q1", "q2", "q3"])

    def test_a_different_judge_or_mode_on_the_same_run_id_is_refused(self):
        checkpointer = InMemorySaver()
        harness.run_eval(run_id="r", checkpointer=checkpointer)
        self.reset_calls()
        with self.assertRaisesRegex(ValueError, "judge='groq'"):
            harness.run_eval(run_id="r", checkpointer=checkpointer, judge="ollama")
        with self.assertRaisesRegex(ValueError, "mode='fresh'"):
            harness.run_eval(run_id="r", checkpointer=checkpointer, mode="calibrate")
        self.assertEqual(self.calls, {"generate": [], "judge": []})

    def test_bad_run_ids_are_refused(self):
        for run_id in ("", "a:b", "../x", " r"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    harness.run_eval(run_id=run_id, checkpointer=InMemorySaver())
        self.assertEqual(self.calls["generate"], [])

    def test_results_carry_run_id_and_judge_verdict(self):
        report = harness.run_eval(run_id="r", checkpointer=InMemorySaver())
        self.assertEqual(set(report), TOP_KEYS | {"run_id", "rows_reused"})
        self.assertEqual(report["run_id"], "r")
        for result in report["results"]:
            self.assertEqual(set(result), ROW_KEYS | {"judge_verdict"})
            self.assertEqual(result["judge_verdict"], result["verdict"])


# ---- c. review: interrupt() and Command(resume=...) ----

class ReviewTests(EvalHarnessTestCase):
    def ask_with(self, *answers):
        """An `ask` that records each paused row and answers in turn."""
        self.asked = []
        replies = list(answers)

        def ask(payload):
            self.asked.append(payload)
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return ask

    def test_a_partial_verdict_pauses_and_resume_with_pass_records_founder_verdict(self):
        graph = harness.build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "r:1", "review": True}}
        self.verdicts["q1"] = ("Partial", "omits the ₹799 threshold")

        out = graph.invoke({**make_row(1), "judge": "groq", "mode": "fresh"}, config)

        self.assertEqual(graph.get_state(config).next, ("review",))
        payload = out["__interrupt__"][0].value
        self.assertEqual(payload, {
            "id": "1", "question": "q1", "answer": "twin reply to q1",
            "ideal_answer": "ideal answer 1", "judge_verdict": "Partial",
            "judge_reason": "omits the ₹799 threshold",
        })
        self.assertNotIn("founder_verdict", out)

        out = graph.invoke(Command(resume="Pass"), config)

        self.assertEqual(out["founder_verdict"], "Pass")
        self.assertEqual(out["verdict"], "Partial")     # the judge's, kept
        self.assertEqual(graph.get_state(config).next, ())
        self.assertEqual(self.calls, {"generate": ["q1"], "judge": ["q1"]})

    def test_run_eval_asks_only_about_partial_rows(self):
        self.verdicts["q2"] = ("Partial", "omits the link")
        self.verdicts["q3"] = ("Fail", "wrong price")
        report = harness.run_eval(run_id="r", checkpointer=InMemorySaver(),
                                  review=True, ask=self.ask_with("Pass"))

        self.assertEqual([p["id"] for p in self.asked], ["2"])
        results = self.by_id(report)
        self.assertEqual(results["2"]["verdict"], "Partial")
        self.assertEqual(results["2"]["judge_verdict"], "Partial")
        self.assertEqual(results["2"]["founder_verdict"], "Pass")
        self.assertNotIn("founder_verdict", results["1"])
        self.assertNotIn("founder_verdict", results["3"])
        # The summary still grades the judge.
        self.assertEqual(report["summary"]["overall"]["Partial"], 1)

    def test_calibrate_reviews_disagreements_with_the_founder(self):
        self.rows = [make_row(1, "Fail"), make_row(2, "Pass"),
                     make_row(3, None), make_row(4, "Pass")]
        self.verdicts["q1"] = ("Pass", "looks complete")     # disagrees
        self.verdicts["q3"] = ("Fail", "wrong price")        # no label
        self.verdicts["q4"] = RuntimeError("judge down")     # Error
        report = harness.run_eval(run_id="cal", mode="calibrate",
                                  checkpointer=InMemorySaver(), review=True,
                                  ask=self.ask_with("Fail"))

        self.assertEqual([p["id"] for p in self.asked], ["1"])
        self.assertEqual(self.asked[0]["human_verdict"], "Fail")
        self.assertEqual(self.asked[0]["answer"], "recorded answer 1")
        self.assertEqual(self.calls["generate"], [])    # calibrate: recorded answers
        results = self.by_id(report)
        self.assertEqual(results["1"]["founder_verdict"], "Fail")
        # judge_agreement compares the judge to the founder's label, not the
        # founder's review to itself.
        agreement = report["summary"]["judge_agreement"]
        self.assertEqual(agreement["agreements"], 1)
        self.assertEqual(agreement["disagreements"][0]["judge"], "Pass")

    def test_skip_keeps_the_judge_verdict_and_records_nothing(self):
        checkpointer = InMemorySaver()
        self.verdicts["q1"] = ("Partial", "half right")
        report = harness.run_eval(run_id="r", checkpointer=checkpointer,
                                  review=True, ask=self.ask_with("skip"))
        result = self.by_id(report)["1"]
        self.assertEqual(result["verdict"], "Partial")
        self.assertNotIn("founder_verdict", result)

        # Resolved: the next review run does not ask again.
        report = harness.run_eval(run_id="r", checkpointer=checkpointer,
                                  review=True, ask=self.ask_with())
        self.assertEqual(self.asked, [])

    def test_a_row_left_paused_is_asked_again_with_no_model_call(self):
        checkpointer = InMemorySaver()
        self.verdicts["q2"] = ("Partial", "half right")
        with self.assertRaises(Crash):      # Ctrl+C at the prompt
            harness.run_eval(run_id="r", checkpointer=checkpointer,
                             review=True, ask=self.ask_with(Crash()))

        # A run without review reports the judge's verdict, leaves it paused.
        self.reset_calls()
        report = harness.run_eval(run_id="r", checkpointer=checkpointer)
        self.assertEqual(self.by_id(report)["2"]["verdict"], "Partial")
        self.assertNotIn("founder_verdict", self.by_id(report)["2"])

        report = harness.run_eval(run_id="r", checkpointer=checkpointer,
                                  review=True, ask=self.ask_with("Fail"))
        self.assertEqual([p["id"] for p in self.asked], ["2"])
        self.assertEqual(self.by_id(report)["2"]["founder_verdict"], "Fail")
        # Row 3 never ran before the crash, so only it calls the models.
        self.assertEqual(self.calls, {"generate": ["q3"], "judge": ["q3"]})

    def test_without_review_a_partial_row_finishes(self):
        checkpointer = InMemorySaver()
        self.verdicts["q1"] = ("Partial", "half right")
        report = harness.run_eval(run_id="r", checkpointer=checkpointer)
        self.assertNotIn("founder_verdict", self.by_id(report)["1"])
        graph = harness.build_graph(checkpointer)
        self.assertEqual(graph.get_state({"configurable": {"thread_id": "r:1"}}).next, ())

    def test_console_asks_until_it_gets_a_verdict(self):
        payload = {"id": "7", "question": "q7", "answer": "a", "ideal_answer": "i",
                   "judge_verdict": "Partial", "judge_reason": "r"}
        with patch("builtins.input", side_effect=["maybe", "PARTIAL"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(harness.ask_on_console(payload), "Partial")
        self.assertIn("REVIEW row 7", out.getvalue())
        self.assertIn("Please type Pass, Partial, Fail or skip.", out.getvalue())


# ---- d. the outbound guard ----

class OutboundGuardTests(EvalHarnessTestCase):
    def test_no_send_is_possible_in_any_kind_of_run(self):
        def draft_that_sends(message, **_kw):
            app.send_whatsapp_reply("919999999999", "this must never be sent")
            return "AUTO", "unreachable", "raw"

        runs = {
            "no run_id": {},
            "run_id": {"run_id": "g1", "checkpointer": InMemorySaver()},
            "run_id + review": {"run_id": "g2", "checkpointer": InMemorySaver(),
                                "review": True, "ask": lambda payload: "Pass"},
        }
        for label, kwargs in runs.items():
            with self.subTest(run=label):
                self.restore_app_functions()    # each run must install it itself
                with patch.object(app, "draft_reply_logic", draft_that_sends), \
                        patch.object(app.requests, "post") as post:
                    report = harness.run_eval(**kwargs)
                post.assert_not_called()
                for result in report["results"]:
                    self.assertEqual(result["verdict"], "Error")
                    self.assertIn("EVAL SAFETY", result["reason"])
                for name in harness._OUTBOUND:
                    if hasattr(app, name):
                        with self.assertRaisesRegex(RuntimeError, "EVAL SAFETY"):
                            getattr(app, name)()


# ---- the command line ----

class CommandLineTests(EvalHarnessTestCase):
    def setUp(self):
        super().setUp()
        self.checkpointer = InMemorySaver()

        @contextmanager
        def shared_checkpointer():
            yield self.checkpointer
        p = patch.object(harness, "_sqlite_checkpointer", shared_checkpointer)
        p.start()
        self.addCleanup(p.stop)

    def main(self, *argv, answers=()):
        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=list(answers)), \
                redirect_stdout(out), redirect_stderr(err):
            code = harness.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_review_run_writes_results_and_the_same_command_resumes(self):
        self.verdicts["q2"] = ("Partial", "half right")
        code, out, _ = self.main("--run-id", "cli", "--review", answers=["pass"])
        self.assertEqual(code, 0)
        self.assertIn("Founder verdicts recorded: 1", out)
        saved = json.loads((harness.RUNS_DIR / "cli.json").read_text(encoding="utf-8"))
        self.assertEqual(self.by_id(saved)["2"]["founder_verdict"], "Pass")

        self.reset_calls()
        code, out, _ = self.main("--run-id", "cli", "--review")
        self.assertEqual(code, 0)
        self.assertIn("3 rows, 3 reused", out)
        self.assertEqual(self.calls, {"generate": [], "judge": []})

    def test_ctrl_c_stops_with_a_hint_to_rerun(self):
        self.verdicts["q2"] = KeyboardInterrupt()
        code, _, err = self.main("--run-id", "cli", "--limit", "2")
        self.assertEqual(code, 130)
        self.assertIn("Run the same command again", err)

    def test_bad_settings_exit_2(self):
        self.main("--run-id", "cli")
        code, _, err = self.main("--run-id", "cli", "--mode", "calibrate")
        self.assertEqual(code, 2)
        self.assertIn("error: run 'cli' already has row 1", err)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            harness.main(["--run-id", "cli", "--limit", "0"])


if __name__ == "__main__":
    unittest.main()
