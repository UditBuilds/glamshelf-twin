"""The FastAPI shell (main.py, PR A of the Flask -> FastAPI move).

main.py mounts the Flask app at "/" through a2wsgi so Render can run
uvicorn. No route moved, so every request must get what Flask gives when
it serves the request directly (Flask's test client here):

  - /healthz and the Flask pages answer the same through FastAPI;
  - the raw body reaches Flask byte for byte, so Meta's HMAC check passes;
  - the Flask session cookie works through the mount;
  - FastAPI's own /docs, /redoc and /openapi.json stay 404;
  - header names with "_" are dropped, as gunicorn did, so
    CF_Connecting_IP can't poison CF-Connecting-IP and dodge the login limit;
  - Flask runs on one thread, as under gunicorn's sync worker;
  - uvicorn's access log is off (it would log the WATI token in
    /webhook/<token>), even when the start command forgets --no-access-log;
  - a request still running after 60s is logged, first path segment only.

A plain GET/HEAD /healthz (no X-Dashboard-Key, no query string) is the one
request FastAPI answers itself, so Render's 5s health check never waits
behind a slow DM on the single Flask thread:

  - same bytes and status as Flask's public answer, DB failure included;
  - it answers in well under 1s while a Flask request is in progress;
  - 503 once one Flask request has held the thread past HUNG_FLASK_SECONDS,
    timed from when Flask picked it up (FlaskClock), and 200 again after;
  - the keyed /healthz, a query string and other methods still go to Flask.

The last class boots the real uvicorn server on a local port.
No live API call.

Run:  python -m unittest tests.test_fastapi_shell
"""
import asyncio, hashlib, hmac, io, json, logging, os, re, signal, subprocess, sys, tempfile, threading, time, unittest
import urllib.error, urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-fastapi-shell-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import main
from a2wsgi import WSGIMiddleware
from fastapi.testclient import TestClient
from starlette.routing import Mount

IG_SECRET = "test-instagram-app-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(IG_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _clear_login_failures():
    with glam._login_failures_lock:
        glam._login_failures.clear()


class ThroughFastAPI(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        _clear_login_failures()
        self.addCleanup(_clear_login_failures)
        self.api = TestClient(main.app, follow_redirects=False)
        self.flask = glam.app.test_client()

    def test_healthz_matches_flask(self):
        via_api, direct = self.api.get("/healthz"), self.flask.get("/healthz")
        self.assertEqual(via_api.status_code, 200)
        self.assertEqual(via_api.json(), {"status": "ok"})
        self.assertEqual((via_api.status_code, via_api.json()), (direct.status_code, direct.get_json()))

    def test_keyed_healthz_matches_flask(self):
        key = {"X-Dashboard-Key": glam.DASHBOARD_KEY}
        via_api, direct = self.api.get("/healthz", headers=key), self.flask.get("/healthz", headers=key)
        self.assertEqual(via_api.status_code, direct.status_code)
        self.assertEqual(sorted(via_api.json()), sorted(direct.get_json()))
        self.assertGreater(len(via_api.json()), 1)     # the full payload, not just status

    def test_flask_redirects_are_unchanged(self):
        with redirect_stdout(io.StringIO()):
            for path in ("/", "/logout"):
                via_api, direct = self.api.get(path), self.flask.get(path)
                self.assertEqual(via_api.status_code, 302, path)
                self.assertEqual(via_api.headers["location"], "/login", path)
                self.assertEqual(direct.headers["Location"], "/login", path)

    def test_login_page_is_byte_identical(self):
        via_api, direct = self.api.get("/login"), self.flask.get("/login")
        self.assertEqual(via_api.status_code, 200)
        self.assertEqual(via_api.content, direct.data)
        self.assertEqual(via_api.headers["content-type"], direct.headers["Content-Type"])

    def test_session_cookie_works_through_the_mount(self):
        with redirect_stdout(io.StringIO()):
            login = self.api.post("/login", data={"password": glam.APP_PASSWORD})
            self.assertEqual(login.status_code, 302)
            self.assertEqual(login.headers["location"], "/")
            self.assertIn("session=", login.headers["set-cookie"])
            self.assertEqual(self.api.get("/").status_code, 200)          # cookie sent back
            fresh = TestClient(main.app, follow_redirects=False)
            self.assertEqual(fresh.get("/").status_code, 302)             # no cookie

    def test_signed_instagram_webhook_reaches_flask_byte_for_byte(self):
        event = {"sender": {"id": "17841400000000001"}, "note": "✨ lash glue — é"}
        body = json.dumps({"object": "instagram", "entry": [{"messaging": [event]}]},
                          ensure_ascii=False).encode("utf-8")
        with patch.object(glam, "INSTAGRAM_APP_SECRET", IG_SECRET), \
                patch.object(glam, "_process_instagram_event") as process, \
                patch.dict(os.environ), redirect_stdout(io.StringIO()):
            os.environ.pop("INSTAGRAM_WEBHOOK_VERIFY_DISABLED", None)
            ok = self.api.post("/instagram-webhook", content=body, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)})
            forged = self.api.post("/instagram-webhook", content=body, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": _sign(body + b" ")})
        self.assertEqual((ok.status_code, ok.json()), (200, {"status": "ok"}))
        self.assertEqual(forged.status_code, 401)
        process.assert_called_once_with(event)

    def test_fastapi_docs_stay_off(self):
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            via_api, direct = self.api.get(path), self.flask.get(path)
            self.assertEqual(via_api.status_code, 404, path)
            self.assertEqual(via_api.content, direct.data, path)

    def test_underscore_header_cannot_poison_cf_connecting_ip(self):
        # a2wsgi would join both into one CF-Connecting-IP value; _client_ip
        # would reject it and fall back to a header the client controls.
        with redirect_stdout(io.StringIO()):
            r = self.api.post("/login", data={"password": "wrong"}, headers={
                "CF-Connecting-IP": "203.0.113.7", "CF_Connecting_IP": "198.51.100.9"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(glam._login_failures), ["203.0.113.7"])


class Shape(unittest.TestCase):
    def test_flask_is_the_only_route_mounted_at_root_on_one_thread(self):
        self.assertEqual(len(main.app.routes), 1)       # no FastAPI route yet (PlainHealthz is middleware)
        mount = main.app.routes[0]
        self.assertIsInstance(mount, Mount)
        self.assertEqual(mount.path, "")                # Mount("/")
        self.assertIsInstance(mount.app, WSGIMiddleware)
        self.assertIs(mount.app.app, main.flask_clock)
        self.assertIs(main.flask_clock.app, glam.app)
        self.assertEqual(main.FLASK_THREADS, 1)
        self.assertEqual(mount.app.executor._max_workers, 1)

    def test_main_uses_the_one_app_module(self):
        self.assertIs(main.twin, sys.modules["app"])

    def test_uvicorn_access_logger_is_off(self):
        self.assertTrue(logging.getLogger("uvicorn.access").disabled)

    def test_middleware_order_and_thresholds(self):
        # Outermost first: headers are cleaned before PlainHealthz looks at them.
        self.assertEqual([m.cls for m in main.app.user_middleware], [main.GunicornParity, main.PlainHealthz])
        parity, healthz = main.app.user_middleware
        self.assertEqual((parity.args, parity.kwargs), ((), {}))
        self.assertEqual(healthz.kwargs, {"flask": main.flask_clock})
        self.assertEqual(main.SLOW_REQUEST_SECONDS, 60)
        self.assertEqual(main.HUNG_FLASK_SECONDS, 55)


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard(message):
    pass


def _scope(path="/", query=b"", headers=()):
    return {"type": "http", "method": "POST", "path": path, "query_string": query,
            "headers": list(headers)}


class GunicornParityMiddleware(unittest.TestCase):
    def test_only_underscore_header_names_are_dropped(self):
        seen = {}

        async def capture(scope, receive, send):
            seen["headers"] = scope["headers"]

        asyncio.run(main.GunicornParity(capture)(_scope(headers=[
            (b"x-hub-signature-256", b"sha256=ab"),
            (b"cf_connecting_ip", b"198.51.100.9"),
            (b"cookie", b"session=x"),
            (b"x_forwarded_for", b"198.51.100.10"),
        ]), _receive, _discard))
        self.assertEqual(seen["headers"], [(b"x-hub-signature-256", b"sha256=ab"), (b"cookie", b"session=x")])

    def _run(self, path, query, delay, slow_after):
        async def slow_app(scope, receive, send):
            await asyncio.sleep(delay)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        out = io.StringIO()
        with redirect_stdout(out):
            asyncio.run(main.GunicornParity(slow_app, slow_after=slow_after)(
                _scope(path, query), _receive, _discard))
        return out.getvalue()

    def test_slow_request_is_logged_by_first_segment_only(self):
        out = self._run("/webhook/s3cr3t-wati-token", b"key=also-s3cr3t", delay=0.3, slow_after=0.05)
        self.assertIn("[SLOW] POST /webhook still running", out)
        self.assertIn("[SLOW] POST /webhook finished after", out)
        self.assertNotIn("s3cr3t", out)

    def test_fast_request_logs_nothing(self):
        self.assertEqual(self._run("/healthz", b"", delay=0, slow_after=5), "")

    def test_first_segment_never_forges_a_log_line(self):
        self.assertEqual(main._first_segment("/insta\ngram-webhook/x"), "/insta?gram-webhook")
        self.assertEqual(main._first_segment("/"), "/")
        self.assertEqual(main._first_segment(""), "/")
        self.assertEqual(main._first_segment("/" + "a" * 100), "/" + "a" * 40)


def _from_flask():
    return "answered by Flask", 200


class PlainHealthzServedByFastAPI(unittest.TestCase):
    def setUp(self):
        glam._init_db()
        self.api = TestClient(main.app, follow_redirects=False)
        self.flask = glam.app.test_client()

    def test_plain_get_is_byte_identical_to_flask(self):
        via_api, direct = self.api.get("/healthz"), self.flask.get("/healthz")
        self.assertEqual(via_api.status_code, 200)
        self.assertEqual(via_api.content, b'{"status":"ok"}\n')
        self.assertEqual((via_api.status_code, via_api.content), (direct.status_code, direct.data))
        self.assertEqual(via_api.headers["content-type"], direct.headers["Content-Type"])
        self.assertEqual(via_api.headers["content-length"], direct.headers["Content-Length"])

    def test_head_matches_flask(self):
        via_api, direct = self.api.head("/healthz"), self.flask.head("/healthz")
        self.assertEqual((via_api.status_code, via_api.content), (200, b""))
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(via_api.headers["content-type"], direct.headers["Content-Type"])
        self.assertEqual(via_api.headers["content-length"], direct.headers["Content-Length"])

    def test_unreadable_db_is_503_like_flask(self):
        with patch.object(glam, "DB_PATH", tempfile.mkdtemp(prefix="glamshelf-not-a-db-")):
            via_api, direct = self.api.get("/healthz"), self.flask.get("/healthz")
        self.assertEqual((via_api.status_code, via_api.content), (503, b'{"status":"error"}\n'))
        self.assertEqual((via_api.status_code, via_api.content), (direct.status_code, direct.data))

    def test_only_a_plain_get_or_head_skips_flask(self):
        with patch.dict(glam.app.view_functions, {"healthz": _from_flask}):
            self.assertEqual(self.api.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(self.api.head("/healthz").status_code, 200)
            for headers, path in (
                ({"X-Dashboard-Key": glam.DASHBOARD_KEY}, "/healthz"),     # the keyed view
                ({"X-Dashboard-Key": "wrong"}, "/healthz"),
                ({}, "/healthz?key=x"),
                ({}, "/healthz?x=1"),
            ):
                r = self.api.get(path, headers=headers)
                self.assertEqual((r.status_code, r.text), (200, "answered by Flask"), (headers, path))
            for method, path in (("POST", "/healthz"), ("GET", "/healthz/")):
                via_api, direct = self.api.request(method, path), self.flask.open(path, method=method)
                self.assertEqual((via_api.status_code, via_api.content), (direct.status_code, direct.data), path)


class HealthzWhileFlaskIsBusy(unittest.TestCase):
    """A real Flask request held open on the single Flask thread (the
    /review-debug view swapped for one that waits), with a short threshold."""

    def test_answers_while_flask_is_busy_then_503_once_hung(self):
        picked_up, release = threading.Event(), threading.Event()
        slow = {}

        def slow_view():
            picked_up.set()
            release.wait(30)
            return "slow done"

        def send_slow_request():
            slow["response"] = TestClient(main.app).get("/review-debug")

        glam._init_db()
        out = io.StringIO()
        with patch.dict(glam.app.view_functions, {"review_debug": slow_view}), \
                patch.object(main, "HUNG_FLASK_SECONDS", 1.0), redirect_stdout(out):
            worker = threading.Thread(target=send_slow_request, daemon=True)
            worker.start()
            try:
                self.assertTrue(picked_up.wait(10), "the slow Flask request never started")
                started = time.monotonic()
                busy = TestClient(main.app).get("/healthz")
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual((busy.status_code, busy.json()), (200, {"status": "ok"}))
                self.assertEqual(TestClient(main.app).head("/healthz").status_code, 200)

                while main.flask_clock.longest_running()[0] <= 1.0:
                    time.sleep(0.05)
                hung = TestClient(main.app).get("/healthz")
                self.assertEqual((hung.status_code, hung.json()), (503, {"status": "error"}))
                hung_head = TestClient(main.app).head("/healthz")
                self.assertEqual((hung_head.status_code, hung_head.content), (503, b""))
            finally:
                release.set()
                worker.join(30)
            self.assertEqual((slow["response"].status_code, slow["response"].text), (200, "slow done"))
            self.assertEqual(main.flask_clock.longest_running(), (0.0, ""))
            self.assertEqual(TestClient(main.app).get("/healthz").status_code, 200)
        self.assertIn("[HEALTHZ] 503: GET /review-debug has held the Flask thread for", out.getvalue())


class FlaskClockTiming(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.seen = []

    def _clock_around(self, wsgi_app):
        return main.FlaskClock(wsgi_app, clock=lambda: self.now[0])

    def test_timed_from_pickup_until_the_response_closes(self):
        def wsgi(environ, start_response):
            self.now[0] += 2                      # the view runs 2s
            start_response("200 OK", [])
            return [b"ok"]

        clock = self._clock_around(wsgi)
        self.assertEqual(clock.longest_running(), (0.0, ""))
        body = clock({"REQUEST_METHOD": "POST", "PATH_INFO": "/webhook/s3cr3t"}, lambda status, headers: None)
        self.now[0] += 5                          # a2wsgi still sending the body
        self.assertEqual(clock.longest_running(), (7.0, "POST /webhook"))
        self.assertEqual(list(body), [b"ok"])
        body.close()
        self.assertEqual(clock.longest_running(), (0.0, ""))

    def test_a_raising_app_is_not_left_running(self):
        def wsgi(environ, start_response):
            raise RuntimeError("boom")

        clock = self._clock_around(wsgi)
        with self.assertRaises(RuntimeError):
            clock({"REQUEST_METHOD": "GET", "PATH_INFO": "/"}, lambda status, headers: None)
        self.assertEqual(clock.longest_running(), (0.0, ""))


class RealUvicornBoot(unittest.TestCase):
    """`uvicorn main:app` in a subprocess, deliberately WITHOUT
    --no-access-log: main.py must keep the access log off on its own."""

    def test_boots_answers_healthz_and_logs_no_paths(self):
        env = dict(os.environ)
        env["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="glamshelf-uvicorn-boot-test-"), "test.db")
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "0",
             "--workers", "1"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        lines = []
        try:
            port = None
            deadline = time.monotonic() + 120
            while port is None and time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                lines.append(line)
                m = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", line)
                if m:
                    port = int(m.group(1))
            self.assertIsNotNone(port, "".join(lines[-40:]))

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=30) as r:
                self.assertEqual((r.status, json.loads(r.read())), (200, {"status": "ok"}))
            head = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="HEAD")
            with opener.open(head, timeout=30) as r:
                self.assertEqual((r.status, r.read(), r.headers["Content-Length"]), (200, b"", "16"))
            keyed = urllib.request.Request(f"http://127.0.0.1:{port}/healthz",
                                           headers={"X-Dashboard-Key": env["DASHBOARD_KEY"]})
            with opener.open(keyed, timeout=30) as r:
                self.assertIn("reply_path", json.loads(r.read()))      # Flask's full payload
            with self.assertRaises(urllib.error.HTTPError) as missing:
                opener.open(f"http://127.0.0.1:{port}/no-such-page/s3cr3t-in-path?key=s3cr3t-in-query", timeout=30)
            self.assertEqual(missing.exception.code, 404)
        finally:
            proc.terminate()
            try:
                rest, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rest, _ = proc.communicate()
            lines.append(rest or "")
        output = "".join(lines)
        self.assertNotIn("s3cr3t", output)
        self.assertNotIn('"GET /healthz', output)        # uvicorn's access-log line format
        # Graceful shutdown on SIGTERM (what Render sends); uvicorn then
        # re-raises the signal, so the exit status is -SIGTERM.
        self.assertIn("Finished server process", output)
        self.assertEqual(proc.returncode, -signal.SIGTERM, output[-2000:])


if __name__ == "__main__":
    unittest.main()
