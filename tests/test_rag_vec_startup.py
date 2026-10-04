"""Startup rebuild of a missing rag_vec table (PR 5a).

Render ran Python 3.14.3, whose sqlite3 can't load extensions, so every
reindex there built rag_chunks without the rag_vec vector table. Once
sqlite-vec loads (the 3.13 pin), retrieval queries rag_vec, finds no table
and falls back to brain-only replies until the hourly reindex. Now:

  - at startup, if sqlite-vec loads and rag_vec is missing, the index is
    rebuilt once, on the startup thread (boot isn't delayed);
  - a table that exists, or an extension that doesn't load, changes
    nothing;
  - a failed or crashing rebuild is logged and the hourly loop carries on
    (today's behaviour: the old index stays, the next reindex retries).

No network: the corpus, the embeddings and the reindex are stubbed, except
the last test, which builds a real vec0 table when this Python can load
sqlite-vec.

Run:  python -m unittest tests.test_rag_vec_startup
"""
import io, os, sqlite3, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ragvec-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import numpy as np
import app as glam

N_CHUNKS = 3


class _StopLoop(BaseException):
    """Ends the hourly loop under test (BaseException, so nothing in the
    loop can swallow it)."""


def _unit_vectors(n):
    vecs = np.zeros((n, glam.RAG_EMBED_DIM), dtype=np.float32)
    for i in range(n):
        vecs[i, i] = 1.0
    return vecs


class Base(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(prefix="glamshelf-ragvec-"), "rag.db")
        p = patch.object(glam, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE rag_chunks (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
            "title TEXT NOT NULL, content TEXT NOT NULL, embedding BLOB NOT NULL)"
        )
        for i, emb in enumerate(_unit_vectors(N_CHUNKS), start=1):
            conn.execute("INSERT INTO rag_chunks VALUES (?, 'product', ?, ?, ?)",
                         (i, f"T{i}", f"chunk {i}", emb.tobytes()))
        conn.execute("CREATE TABLE rag_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO rag_meta VALUES ('embed_model', ?)", (glam.RAG_EMBED_MODEL_NAME,))
        conn.commit()
        conn.close()
        self.reasons = []

    def add_vec_table(self):
        # A plain table stands in for the vec0 one: both are type 'table'
        # in sqlite_master.
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE rag_vec (embedding BLOB)")
        conn.commit()
        conn.close()

    def vec_loads(self, loaded=True):
        return patch.object(glam, "_rag_db", lambda: (sqlite3.connect(self.db), loaded))

    def reindex_returns(self, value):
        def fake(reason="manual"):
            self.reasons.append(reason)
            if isinstance(value, Exception):
                raise value
            return value
        return patch.object(glam, "_rag_reindex", fake)

    def run_startup(self):
        """Run the startup thread's body in this thread, up to the first
        hourly sleep. Returns (log, reached_the_hourly_loop)."""
        captured = {}

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kw):
                captured["target"] = target

            def start(self):
                pass

        me = threading.get_ident()
        real_sleep = time.sleep
        slept = []

        def fake_sleep(seconds):
            if threading.get_ident() != me:     # someone else's thread
                return real_sleep(seconds)
            slept.append(seconds)
            raise _StopLoop

        out = io.StringIO()
        with patch.object(glam.threading, "Thread", FakeThread):
            glam._start_rag_reindex_loop()
        with patch.object(glam.time, "sleep", fake_sleep), redirect_stdout(out):
            try:
                captured["target"]()
            except _StopLoop:
                pass
        return out.getvalue(), slept == [glam.RAG_REINDEX_INTERVAL_SECONDS]


class VecTableMissing(Base):
    def test_true_when_sqlite_vec_loads_and_the_table_is_missing(self):
        with self.vec_loads(True):
            self.assertTrue(glam._rag_vec_table_missing())

    def test_false_when_the_table_exists(self):
        self.add_vec_table()
        with self.vec_loads(True):
            self.assertFalse(glam._rag_vec_table_missing())

    def test_false_when_sqlite_vec_does_not_load(self):
        # Brute-force search over rag_chunks needs no vector table.
        with self.vec_loads(False):
            self.assertFalse(glam._rag_vec_table_missing())

    def test_an_error_means_false_and_never_raises(self):
        def boom():
            raise sqlite3.OperationalError("disk I/O error")
        out = io.StringIO()
        with patch.object(glam, "_rag_db", boom), redirect_stdout(out):
            self.assertFalse(glam._rag_vec_table_missing())
        self.assertIn("Vector-table check failed", out.getvalue())


class StartupRebuild(Base):
    def test_missing_table_is_rebuilt_once_at_startup(self):
        with self.vec_loads(True), self.reindex_returns(N_CHUNKS):
            log, looped = self.run_startup()
        self.assertEqual(self.reasons, ["startup-vec-missing"])
        self.assertIn("has no rag_vec table — rebuilding it once", log)
        self.assertNotIn("Startup rebuild failed", log)
        self.assertNotIn("Existing index found", log)
        self.assertTrue(looped)

    def test_existing_table_means_no_rebuild(self):
        self.add_vec_table()
        with self.vec_loads(True), self.reindex_returns(N_CHUNKS):
            log, looped = self.run_startup()
        self.assertEqual(self.reasons, [])
        self.assertIn("Existing index found (3 chunks", log)
        self.assertTrue(looped)

    def test_extension_not_loading_means_no_rebuild(self):
        with self.vec_loads(False), self.reindex_returns(N_CHUNKS):
            log, looped = self.run_startup()
        self.assertEqual(self.reasons, [])
        self.assertIn("Existing index found", log)
        self.assertTrue(looped)

    def test_failed_rebuild_is_logged_and_the_hourly_loop_carries_on(self):
        with self.vec_loads(True), self.reindex_returns(0):
            log, looped = self.run_startup()
        self.assertEqual(self.reasons, ["startup-vec-missing"])
        self.assertIn("Startup rebuild failed — keeping the existing index", log)
        self.assertTrue(looped)

    def test_crashing_rebuild_is_logged_and_the_hourly_loop_carries_on(self):
        with self.vec_loads(True), self.reindex_returns(RuntimeError("embedder exploded")):
            log, looped = self.run_startup()
        self.assertEqual(self.reasons, ["startup-vec-missing"])
        self.assertIn("Startup rebuild crashed: RuntimeError: embedder exploded", log)
        self.assertIn("Startup rebuild failed", log)
        self.assertTrue(looped)

    def test_the_rebuild_never_delays_boot(self):
        started, release, reached_loop = threading.Event(), threading.Event(), threading.Event()
        rebuild_thread = []
        real_sleep = time.sleep

        def slow_reindex(reason="manual"):
            rebuild_thread.append(threading.get_ident())
            started.set()
            release.wait(10)
            return N_CHUNKS

        def fake_sleep(seconds):
            if not rebuild_thread or threading.get_ident() != rebuild_thread[0]:
                return real_sleep(seconds)
            reached_loop.set()
            raise _StopLoop

        with self.vec_loads(True), patch.object(glam, "_rag_reindex", slow_reindex), \
                patch.object(glam.time, "sleep", fake_sleep), \
                patch.object(threading, "excepthook", lambda args: None), \
                redirect_stdout(io.StringIO()):
            t0 = time.monotonic()
            glam._start_rag_reindex_loop()
            elapsed = time.monotonic() - t0
            self.assertTrue(started.wait(10), "the rebuild never started")
            self.assertLess(elapsed, 1.0)
            self.assertFalse(reached_loop.is_set())     # still rebuilding
            release.set()
            self.assertTrue(reached_loop.wait(10), "the hourly loop was never reached")


class RealExtension(Base):
    """The rebuild with the real sqlite-vec extension: rag_vec exists
    afterwards and holds one vector per chunk."""

    def setUp(self):
        super().setUp()
        conn, loaded = glam._rag_db()
        conn.close()
        if not loaded:
            self.skipTest(f"sqlite-vec doesn't load on this Python: {glam._rag_vec_load_error}")

    def test_rebuild_creates_the_vector_table(self):
        corpus = [("product", f"T{i}", f"chunk {i}") for i in range(1, N_CHUNKS + 1)]
        with patch.object(glam, "_rag_embedder", object()), \
                patch.object(glam, "_rag_build_corpus", lambda: corpus), \
                patch.object(glam, "_rag_embed", lambda texts: _unit_vectors(len(texts))):
            self.assertTrue(glam._rag_vec_table_missing())
            log, looped = self.run_startup()
            self.assertFalse(glam._rag_vec_table_missing())
        self.assertIn("Reindexed 3 chunks (startup-vec-missing; sqlite-vec=yes)", log)
        self.assertTrue(looped)
        conn, loaded = glam._rag_db()
        try:
            self.assertTrue(loaded)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rag_vec").fetchone()[0], N_CHUNKS)
            (rowid, _), = conn.execute(
                "SELECT rowid, distance FROM rag_vec WHERE embedding MATCH ? AND k = 1",
                (_unit_vectors(N_CHUNKS)[1].tobytes(),),
            ).fetchall()
            self.assertEqual(rowid, 2)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
