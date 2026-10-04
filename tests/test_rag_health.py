"""RAG health is visible and alerted (audit finding 16).

Retrieval used to fail silently. Now (founder decision, 4 Oct 2026):

  - Render's Python can't load sqlite-vec; the numpy fallback is the normal
    path, logged once at startup as information (never "FAILED");
  - the keyed /healthz shows the index: healthy = embedding model loaded,
    chunks > 0 and the last retrieval worked, with the numpy fallback
    counting as healthy; the public /healthz never changes over RAG;
  - an unhealthy index (no model, no chunks, a failed or timed-out
    retrieval) sends the founder one Telegram alert a day, from a
    background thread — never from the reply path.

No network: Telegram and the embedding model are stubbed.

Run:  python -m unittest tests.test_rag_health
"""
import io, os, sqlite3, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-rag-health-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import numpy as np
import app as glam


class _NoExtensionConnection:
    """A connection from a CPython built without loadable extensions."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        if name == "enable_load_extension":
            raise AttributeError("'sqlite3.Connection' object has no attribute 'enable_load_extension'")
        return getattr(self._conn, name)


class _StopLoop(BaseException):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="glamshelf-rag-health-"), "t.db")
        self.alerts = []
        for p in (
            patch.object(glam, "DB_PATH", self.db),
            patch.object(glam, "TELEGRAM_CHAT_ID", "42"),
            patch.object(glam, "_telegram_api", lambda method, payload: self.alerts.append(payload["text"])),
            patch.dict(glam._rag_last_retrieval, {"at": None, "error": None}),
        ):
            p.start(); self.addCleanup(p.stop)
        with redirect_stdout(io.StringIO()):
            glam._init_db()

    def add_chunks(self, n):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE IF NOT EXISTS rag_chunks (id INTEGER PRIMARY KEY, source TEXT, "
                     "title TEXT, content TEXT, embedding BLOB)")
        for i in range(n):
            conn.execute("INSERT INTO rag_chunks (source, title, content, embedding) VALUES (?, ?, ?, ?)",
                         ("product:gs1", "GS1", f"chunk {i}", np.ones(4, dtype=np.float32).tobytes()))
        conn.commit()
        conn.close()

    def embedder(self, loaded=True):
        return patch.object(glam, "_rag_embedder", object() if loaded else None)

    def numpy_fallback(self):
        return patch.object(glam, "_rag_db", lambda: (sqlite3.connect(self.db), False))


class StartupLine(Base):
    def test_the_fallback_is_information_logged_once(self):
        real_connect = sqlite3.connect
        out = io.StringIO()
        with patch.object(glam, "_rag_vec_load_error_logged", False), \
                patch.object(glam, "_rag_vec_load_error", None), \
                patch.object(glam.sqlite3, "connect", lambda *a, **k: _NoExtensionConnection(real_connect(*a, **k))), \
                redirect_stdout(out):
            glam._rag_log_vector_search()
            glam._rag_log_vector_search()
            conn, loaded = glam._rag_db()
            conn.close()
        lines = [l for l in out.getvalue().splitlines() if l.startswith("[RAG]")]
        self.assertFalse(loaded)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("[RAG] Vector search: numpy fallback", lines[0])
        self.assertIn("CPython built without loadable-extension support", lines[0])
        self.assertIn("Normal on Render", lines[0])
        self.assertNotIn("FAILED", lines[0])

    def test_startup_thread_logs_it_first(self):
        captured = {}

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kw):
                captured["target"] = target

            def start(self):
                pass

        order = []
        with patch.object(glam.threading, "Thread", FakeThread):
            glam._start_rag_reindex_loop()
        with patch.object(glam, "_rag_log_vector_search", lambda: order.append("log")), \
                patch.object(glam, "_rag_reindex", lambda reason="manual": order.append(reason) or 0), \
                patch.object(glam, "_rag_schedule_startup_check", lambda: order.append("check in 2 min")), \
                patch.object(glam, "_rag_alert_if_unhealthy", lambda when: order.append(f"alert:{when}")), \
                patch.object(glam.time, "sleep", self._sleep_once()), \
                patch.object(glam, "_ig_token_check_if_due", lambda: None), \
                redirect_stdout(io.StringIO()):
            try:
                captured["target"]()
            except _StopLoop:
                pass
        self.assertEqual(order, ["log", "startup", "check in 2 min", "hourly", "alert:hourly"])

    def test_the_startup_check_runs_two_minutes_later_on_a_daemon_timer(self):
        made = []

        class FakeTimer:
            def __init__(self, interval, function, args=None, **kw):
                made.append((interval, function, args))
                self.daemon = False

            def start(self):
                made.append(("started", self.daemon))

        with patch.object(glam.threading, "Timer", FakeTimer):
            glam._rag_schedule_startup_check()
        self.assertEqual(made, [(120, glam._rag_alert_if_unhealthy, ("startup",)), ("started", True)])

    @staticmethod
    def _sleep_once():
        calls = []
        me = threading.get_ident()
        real = time.sleep

        def fake(seconds):
            if threading.get_ident() != me:
                return real(seconds)
            calls.append(seconds)
            if len(calls) > 1:
                raise _StopLoop
        return fake


class Health(Base):
    def test_numpy_fallback_with_chunks_is_healthy(self):
        self.add_chunks(46)
        with self.embedder(), self.numpy_fallback():
            h = glam._rag_health()
        self.assertTrue(h["healthy"], h)
        self.assertEqual((h["chunks"], h["vector_search"], h["problem"]), (46, "numpy fallback", None))

    def test_no_embedding_model(self):
        self.add_chunks(46)
        with self.embedder(False), self.numpy_fallback():
            h = glam._rag_health()
        self.assertFalse(h["healthy"])
        self.assertEqual(h["problem"], "the embedding model isn't loaded, so retrieval is off")

    def test_never_indexed_counts_as_zero_chunks(self):
        with self.embedder(), self.numpy_fallback():
            h = glam._rag_health()
        self.assertEqual((h["healthy"], h["chunks"], h["problem"]), (False, 0, "the index has no chunks"))

    def test_a_failed_retrieval_until_the_next_one_works(self):
        self.add_chunks(3)
        with self.embedder(), self.numpy_fallback(), \
                patch.object(glam, "_rag_alert_if_unhealthy", lambda when: None):
            glam._rag_note_retrieval("RuntimeError: onnx exploded")
            h = glam._rag_health()
            self.assertFalse(h["healthy"])
            self.assertEqual(h["problem"], "the last retrieval failed (RuntimeError: onnx exploded)")
            self.assertEqual(h["last_retrieval"]["ok"], False)
            glam._rag_note_retrieval(None)
            self.assertTrue(glam._rag_health()["healthy"])


class Alert(Base):
    def test_one_alert_a_day_while_unhealthy(self):
        with self.embedder(), self.numpy_fallback(), redirect_stdout(io.StringIO()):
            glam._rag_alert_if_unhealthy("startup")
            glam._rag_alert_if_unhealthy("hourly")
            glam._rag_alert_if_unhealthy("retrieval")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Twin's product search (RAG) has a problem", self.alerts[0])
        self.assertIn("Problem: the index has no chunks.", self.alerts[0])
        self.assertIn("Index: 0 chunks · vector search: numpy fallback", self.alerts[0])

    def test_the_day_is_restart_safe_and_then_it_alerts_again(self):
        now = [10_000.0]
        with self.embedder(False), self.numpy_fallback(), \
                patch.object(glam.time, "time", lambda: now[0]), redirect_stdout(io.StringIO()):
            glam._rag_alert_if_unhealthy("startup")
            now[0] += 23 * 3600                          # a restart: the stamp is in the DB
            glam._rag_alert_if_unhealthy("startup")
            now[0] += 2 * 3600
            glam._rag_alert_if_unhealthy("hourly")
        self.assertEqual(len(self.alerts), 2)

    def test_healthy_fallback_never_alerts(self):
        self.add_chunks(46)
        with self.embedder(), self.numpy_fallback(), redirect_stdout(io.StringIO()):
            glam._rag_alert_if_unhealthy("startup")
            glam._rag_alert_if_unhealthy("hourly")
        self.assertEqual(self.alerts, [])

    def test_a_broken_db_sends_nothing_and_never_raises(self):
        with patch.object(glam, "DB_PATH", tempfile.mkdtemp()), self.embedder(False), \
                redirect_stdout(io.StringIO()):
            glam._rag_alert_if_unhealthy("startup")      # a directory: every query fails
        self.assertEqual(self.alerts, [])


class RetrievalErrorsAlertOffThePath(Base):
    def test_a_failed_retrieval_alerts_from_another_thread(self):
        self.add_chunks(3)
        called, done = [], threading.Event()

        def fake_alert(when):
            called.append((when, threading.get_ident()))
            done.set()

        with self.embedder(), patch.object(glam, "_rag_embed", lambda texts: None), \
                patch.object(glam, "_rag_alert_if_unhealthy", fake_alert), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(glam._rag_retrieve("is GS1 reusable?"), "")
            self.assertTrue(done.wait(5))
        ((when, ident),) = called
        self.assertEqual(when, "retrieval")
        self.assertNotEqual(ident, threading.get_ident())
        self.assertEqual(glam._rag_last_retrieval["error"], "the query embedding failed")

    def test_a_working_retrieval_is_recorded_as_ok(self):
        self.add_chunks(3)
        with self.embedder(), self.numpy_fallback(), \
                patch.object(glam, "_rag_embed", lambda texts: np.ones((1, 4), dtype=np.float32)), \
                redirect_stdout(io.StringIO()):
            glam._rag_retrieve("is GS1 reusable?")
        self.assertIsNotNone(glam._rag_last_retrieval["at"])
        self.assertIsNone(glam._rag_last_retrieval["error"])

    def test_a_timed_out_retrieval_is_a_failure(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with self.embedder(), patch.object(glam, "RAG_RETRIEVAL_TIMEOUT_SECONDS", 0.2), \
                patch.object(glam, "_rag_retrieve_now", lambda m: release.wait(10) and ""), \
                patch.object(glam, "_rag_alert_if_unhealthy", lambda when: None), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(glam._rag_retrieve("is GS1 reusable?"), "")
        self.assertEqual(glam._rag_last_retrieval["error"], "it took longer than 0.5s")
        release.set()
        for _ in range(100):
            if glam._rag_inflight.acquire(blocking=False):
                glam._rag_inflight.release()
                break
            time.sleep(0.02)


class Healthz(Base):
    def test_keyed_view_shows_rag(self):
        self.add_chunks(46)
        with self.embedder(), self.numpy_fallback():
            data = glam.app.test_client().get(
                "/healthz", headers={"X-Dashboard-Key": glam.DASHBOARD_KEY}).get_json()
        self.assertEqual(data["rag"]["healthy"], True)
        self.assertEqual(data["rag"]["chunks"], 46)
        self.assertEqual(data["rag"]["vector_search"], "numpy fallback")
        self.assertIn("embedder_loaded", data["rag"])

    def test_public_view_stays_ok_when_rag_is_unhealthy(self):
        with self.embedder(False), self.numpy_fallback():
            resp = glam.app.test_client().get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
